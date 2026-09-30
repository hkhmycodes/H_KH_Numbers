#!/usr/bin/env python3
"""
Bounded Mininet run for the Khayou SDN experiment.

Per run (all under --run-dir):
  iperf_<src>.json     iperf3 -J --get-server-output (receiver-side UDP loss/jitter)
  ping_<src>.txt       ping -D (epoch timestamps) from each source
  tc_samples.tsv       per-qdisc tc -s counters on selected interfaces
  port_map.json        (dpid, OF port) -> (switch, neighbour); read by the controller
  events.json          wall-clock epochs: run_start, traffic_start/end, failure, per-source plan

Notes:
  * rate 0 really means no traffic (iperf3 -b 0 means UNLIMITED)
  * --seed implemented (per-source start jitter <= 1 s, rate +-2 %); seed 0 = deterministic
  * pollers use popen / subprocess, never Node.cmd from a second thread
  * failure is injected with `ip link set <switch iface> down` (switch-side, no
    Node.cmd) at --inject-failure a:b:T, T = seconds AFTER traffic start
  * all timestamps are wall-clock epochs so controller METRIC_SAMPLE ts= lines align
"""
import argparse
import json
import os
import random
import re
import signal
import subprocess
import threading
import time

from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import CPULimitedHost, OVSKernelSwitch, RemoteController, Switch

from khayou_topo import KhayouSDNTopo

SOURCES = ["u1", "u2", "u3"]
STOP = threading.Event()


def handle_signal(signum, _frame):
    info(f"\n[!] signal {signum}; stopping...\n")
    STOP.set()


def sleep_until(t):
    while time.time() < t and not STOP.is_set():
        time.sleep(0.05)


def terminate(proc, name, timeout=2.0):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        end = time.monotonic() + timeout
        while proc.poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=1.0)
    except Exception as exc:
        info(f"[!] could not stop {name}: {exc}\n")


def is_switch(node):
    # isinstance is safer than hasattr(node, "dpid"): some Mininet releases
    # set dpid = None on every Node, not only on Switch instances.
    return isinstance(node, Switch)


def iface_toward(net, a, b):
    """Interface of node `a` on the link a--b (a's transmit side)."""
    na, nb = net.get(a), net.get(b)
    links = net.linksBetween(na, nb)
    if not links:
        return None
    lk = links[0]
    return lk.intf1 if lk.intf1.node == na else lk.intf2


def build_port_map(net):
    """
    Map (dpid, OF port number) -> (switch name, neighbor name).

    Sources:
      * Peer relationships come from `ip -o link show`, whose output
        annotates each veth as 's1-eth2@s4-eth1'.
      * OpenFlow port numbers come from the numeric suffix of the
        interface name. Mininet always creates switch interfaces as
        '<switch>-eth<N>' where <N> is the OF port, so this is reliable
        across Mininet releases and does not depend on Intf.port,
        Switch.ports, or Link.intfN.node.
    """
    try:
        out = subprocess.run(
            ["ip", "-o", "link", "show"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        out = ""

    # peers: iface name -> peer iface name (from the @ annotation).
    peers = {}
    for line in out.splitlines():
        m = re.match(r"\s*\d+:\s+([^@:]+)(?:@([^:]+))?:", line)
        if m:
            name = m.group(1).strip()
            peer = (m.group(2) or "").strip()
            if peer:
                peers[name] = peer

    rows = []
    for sw in net.switches:
        dpid_raw = sw.dpid
        try:
            dpid_int = int(dpid_raw, 16) if isinstance(dpid_raw, str) else int(dpid_raw)
        except (TypeError, ValueError):
            continue

        # Find every interface named '<sw>-eth<N>' in the root namespace.
        for iface, peer_iface in peers.items():
            m = re.match(rf"^{re.escape(sw.name)}-eth(\d+)$", iface)
            if not m:
                continue
            port_no = int(m.group(1))
            neighbor = peer_iface.rsplit("-eth", 1)[0]
            rows.append(dict(
                sw=sw.name,
                dpid=dpid_int,
                port_no=port_no,
                iface=iface,
                neighbor=neighbor,
            ))
    return rows


# ------------------------------------------------------------------ tc polling
BLOCK_HEAD = re.compile(r"^qdisc\s+(\S+)\s+(\S+):", re.M)
SENT = re.compile(
    r"Sent\s+(\d+)\s+bytes\s+(\d+)\s+pkt\s+"
    r"\(dropped\s+(\d+),\s+overlimits\s+(\d+)\s+requeues\s+(\d+)\)"
)
BACKLOG = re.compile(r"backlog\s+(\d+)b\s+(\d+)p")


def parse_tc(text):
    """One tuple per qdisc: (kind, handle, bytes, pkts, dropped, backlog_pkts)."""
    heads = list(BLOCK_HEAD.finditer(text))
    out = []
    for i, h in enumerate(heads):
        blk = text[h.start(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        s, b = SENT.search(blk), BACKLOG.search(blk)
        if s:
            out.append((
                h.group(1), h.group(2),
                int(s.group(1)), int(s.group(2)), int(s.group(3)),
                int(b.group(2)) if b else 0,
            ))
    return out


def read_tc(node, iface):
    cmd = ["tc", "-s", "-d", "qdisc", "show", "dev", iface]
    try:
        if is_switch(node):                      # root namespace
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            return r.stdout
        p = node.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       universal_newlines=True)
        out, _ = p.communicate(timeout=2)
        return out
    except Exception:
        return ""


def poll_tc(targets, path, interval):
    with open(path, "w") as fh:
        fh.write("ts\tnode\tiface\tqdisc\thandle\ttx_bytes\ttx_pkts\tdropped\tbacklog_pkts\n")
        while not STOP.is_set():
            ts = time.time()
            for name, node, iface in targets:
                for row in parse_tc(read_tc(node, iface)):
                    fh.write(f"{ts:.3f}\t{name}\t{iface}\t" + "\t".join(map(str, row)) + "\n")
            fh.flush()
            STOP.wait(interval)


# --------------------------------------------------------------------- traffic
def start_servers(rho, iperf, port_base):
    procs = []
    for i, _ in enumerate(SOURCES):
        procs.append(rho.popen(
            [iperf, "-s", "-1", "-p", str(port_base + i)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
    return procs


def inject_failure(net, a, b, at_epoch, events):
    def worker():
        sleep_until(at_epoch)
        if STOP.is_set():
            return
        intf = None
        for x, y in ((a, b), (b, a)):
            if is_switch(net.get(x)):
                intf = iface_toward(net, x, y)
                break
        if intf is None:
            events["failure"]["error"] = "no switch-side interface found"
            return
        subprocess.run(["ip", "link", "set", "dev", intf.name, "down"], check=False)
        events["failure"]["iface"] = intf.name
        events["failure"]["injected_at"] = time.time()
        info(f"[!] link {a}--{b} down ({intf.name})\n")
    th = threading.Thread(target=worker, daemon=True)
    th.start()
    return th


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--controller-port", type=int, default=6653)
    ap.add_argument("--duration", type=float, default=30.0,
                    help="per-flow iperf duration (s)")
    ap.add_argument("--rate-mbps", type=float, default=4.0,
                    help="PER SOURCE; 0 = no traffic")
    ap.add_argument("--iperf", default="iperf3")
    ap.add_argument("--port-base", type=int, default=5001)
    ap.add_argument("--run-dir", default="results/runs/single")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--pre-idle", type=float, default=8.0,
                    help="idle seconds before traffic")
    ap.add_argument("--inject-failure", default=None,
                    help="a:b:T  (T seconds after traffic start)")
    ap.add_argument("--tc-links", default="s6:rho,s5:rho,s4:s5,s5:s4",
                    help="comma list a:b -> poll a's interface toward b")
    ap.add_argument("--tc-interval", type=float, default=3.0)
    args = ap.parse_args()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    os.makedirs(args.run_dir, exist_ok=True)
    rd = lambda f: os.path.join(args.run_dir, f)

    events = dict(
        rate_mbps=args.rate_mbps, duration=args.duration, seed=args.seed,
        pre_idle=args.pre_idle, run_start=None, traffic_start=None,
        traffic_end=None, sources={}, failure=None,
    )

    def flush_events():
        with open(rd("events.json"), "w") as fh:
            json.dump(events, fh, indent=2)

    net = None
    servers, clients, files, threads, pings = [], [], [], [], []
    try:
        net = Mininet(
            topo=KhayouSDNTopo(), host=CPULimitedHost, link=TCLink,
            controller=None, switch=OVSKernelSwitch,
            autoSetMacs=False, autoStaticArp=False, waitConnected=10,
        )
        net.addController("c0", controller=RemoteController,
                          ip=args.controller_ip, port=args.controller_port)
        info("[+] starting Mininet\n")
        net.start()
        events["run_start"] = time.time()
        time.sleep(2.0)

        with open(rd("port_map.json"), "w") as fh:
            json.dump(build_port_map(net), fh, indent=1)

        rho = net.get("rho")
        for name in SOURCES:                      # sequential; no threads running yet
            h = net.get(name)
            h.cmd(f"arp -d {rho.IP()} 2>/dev/null || true")
            h.cmd(f"arp -s {rho.IP()} {rho.MAC()}")

        # tc targets: source access interfaces + requested switch links
        targets = [(n, net.get(n), net.get(n).defaultIntf().name) for n in SOURCES]
        for pair in filter(None, args.tc_links.split(",")):
            a, b = pair.split(":")
            try:
                intf = iface_toward(net, a, b)
            except KeyError:
                intf = None
            if intf is None:
                info(f"[!] tc link {pair}: not found, skipped\n")
                continue
            targets.append((a, net.get(a), intf.name))
        th = threading.Thread(
            target=poll_tc,
            args=(targets, rd("tc_samples.tsv"), args.tc_interval),
            daemon=True,
        )
        th.start()
        threads.append(th)

        # per-source plan (seeded)
        rng = random.Random(args.seed)
        plan = []
        for i, name in enumerate(SOURCES):
            jitter = rng.uniform(0.0, 1.0) if args.seed > 0 else 0.0
            factor = 1.0 + rng.uniform(-0.02, 0.02) if args.seed > 0 else 1.0
            plan.append((jitter, name, args.port_base + i, args.rate_mbps * factor))
            events["sources"][name] = dict(
                start_offset=jitter,
                rate_mbps=args.rate_mbps * factor,
                port=args.port_base + i,
            )
        plan.sort()
        max_off = max(p[0] for p in plan)
        total = args.pre_idle + max_off + args.duration + 3.0

        # pings cover idle + traffic + tail.
        # Use shell redirection; Node.popen kwargs vary across Mininet releases.
        n_probes = int((total + 2.0) / 0.2)
        for name in SOURCES:
            host = net.get(name)
            out_path = rd(f"ping_{name}.txt")
            cmd = (
                f"ping -D -i 0.2 -c {n_probes} -W 0.5 {rho.IP()} "
                f"> {out_path} 2>&1"
            )
            p = host.popen(cmd, shell=True)
            pings.append((name, p))

        flush_events()
        sleep_until(events["run_start"] + 2.0 + args.pre_idle)   # idle baseline

        # traffic
        if args.rate_mbps > 0:
            servers = start_servers(rho, args.iperf, args.port_base)
            time.sleep(1.0)
        t0 = time.time()
        events["traffic_start"] = t0
        events["traffic_end"] = t0 + max_off + args.duration
        if args.inject_failure:
            a, b, t_s = args.inject_failure.split(":")
            events["failure"] = dict(
                a=a, b=b, at=t0 + float(t_s), injected_at=None,
            )
            threads.append(
                inject_failure(net, a, b, t0 + float(t_s), events)
            )
        flush_events()

        if args.rate_mbps > 0:
            info(f"[+] 3 UDP flows at ~{args.rate_mbps:.2f} Mbit/s each, "
                 f"seed={args.seed}\n")
            for off, name, port, rate in plan:
                sleep_until(t0 + off)
                fh = open(rd(f"iperf_{name}.json"), "wb")
                p = net.get(name).popen(
                    [args.iperf, "-c", rho.IP(), "-u",
                     "-b", f"{rate:.3f}M",
                     "-t", str(int(round(args.duration))),
                     "-p", str(port),
                     "-J", "--get-server-output"],
                    stdout=fh, stderr=subprocess.DEVNULL,
                )
                clients.append(p)
                files.append(fh)
        else:
            info("[+] rate 0: idle control, no iperf flows\n")

        sleep_until(events["traffic_end"] + 3.0)
        for p in clients:                       # let clients flush their JSON
            try:
                p.wait(timeout=8.0)
            except Exception:
                pass
        flush_events()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        info(f"[-] emulation error: {exc}\n")
        return 1
    finally:
        STOP.set()
        for th in threads:
            th.join(timeout=3.0)
        # Kill the ping shells first, then pkill any orphaned child ping.
        for name, p in pings:
            terminate(p, f"ping shell {name}")
            try:
                net.get(name).cmd(
                    "pkill -f 'ping -D -i 0.2' 2>/dev/null || true"
                )
            except Exception:
                pass
        for fh in files:
            fh.close()
        for i, p in enumerate(clients):
            terminate(p, f"iperf client {i}")
        for i, p in enumerate(servers):
            terminate(p, f"iperf server {i}")
        try:
            flush_events()
        except Exception:
            pass
        if net is not None:
            info("[+] stopping Mininet\n")
            try:
                net.stop()
            except Exception as exc:
                info(f"[!] net.stop: {exc}\n")


if __name__ == "__main__":
    setLogLevel("info")
    raise SystemExit(main())
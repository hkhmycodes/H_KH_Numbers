#!/usr/bin/env python3
"""
Merge controller METRIC_SAMPLE lines with iperf3 JSON and ping logs.
Writes a per-run summary CSV and prints the cross-run Spearman correlation
between settled T_G and mean UDP loss.

Regex matches the exact output of khayou_telemetry.KhayouTelemetryMixin:
  METRIC_SAMPLE ts=... elapsed=... S_G=...
    T_G_b0=... T_G_b05=... T_G_b1=...
    H_G_a0=... H_G_a05=... H_G_a1=... H_G_a2=...
    U_max_net=... U_mean_net=...
    cycle_ms=... rss_mb=... edges_removed=...
"""
import argparse
import csv
import glob
import json
import os
import re

from scipy.stats import spearmanr

METRIC_RE = re.compile(
    r"METRIC_SAMPLE\s+ts=([0-9.]+)\s+"
    r"elapsed=([0-9.]+)\s+"
    r"S_G=(\d+)\s+"
    r"T_G_b0=([0-9.eE+-]+)\s+T_G_b05=([0-9.eE+-]+)\s+T_G_b1=([0-9.eE+-]+)\s+"
    r"H_G_a0=([0-9.eE+-]+)\s+H_G_a05=([0-9.eE+-]+)\s+"
    r"H_G_a1=([0-9.eE+-]+)\s+H_G_a2=([0-9.eE+-]+)\s+"
    r"U_max_net=([0-9.eE+-]+)\s+U_mean_net=([0-9.eE+-]+)\s+"
    r"cycle_ms=([0-9.]+)\s+rss_mb=([0-9.]+)"
)


def parse_metric(log_path):
    rows = []
    if not os.path.exists(log_path):
        return rows
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = METRIC_RE.search(line)
            if not m:
                continue
            rows.append(dict(
                t=float(m.group(2)),                       # elapsed since controller start
                ts=float(m.group(1)),                      # wall-clock epoch
                S_G=int(m.group(3)),
                T_G_b0=float(m.group(4)),
                T_G_b05=float(m.group(5)),
                T_G_b1=float(m.group(6)),
                H_G_a0=float(m.group(7)),
                H_G_a05=float(m.group(8)),
                H_G_a1=float(m.group(9)),
                H_G_a2=float(m.group(10)),
                U_max_net=float(m.group(11)),
                U_mean_net=float(m.group(12)),
                cycle_ms=float(m.group(13)),
                rss_mb=float(m.group(14)),
            ))
    return rows


def _server_json(doc):
    if not isinstance(doc, dict):
        return None
    srv = doc.get("server_output_json")
    if isinstance(srv, dict):
        return srv
    txt = doc.get("server_output_text")
    if isinstance(txt, str):
        try:
            return json.loads(txt)
        except Exception:
            return None
    return None


def _pick_udp_sum(end_block):
    if not isinstance(end_block, dict):
        return None
    for key in ("sum", "sum_received", "sum_sent"):
        s = end_block.get(key)
        if isinstance(s, dict) and ("lost_packets" in s or "lost_percent" in s):
            return s
    return None


def parse_iperf(json_path):
    if not os.path.exists(json_path):
        return None
    try:
        with open(json_path) as fh:
            doc = json.load(fh)
    except Exception:
        return None
    srv = _server_json(doc)
    s = None
    if isinstance(srv, dict):
        s = _pick_udp_sum(srv.get("end"))
    if s is None:
        s = _pick_udp_sum(doc.get("end"))
    if s is None:
        return None
    return {
        "bits_per_second": s.get("bits_per_second", 0.0),
        "lost_packets": s.get("lost_packets", 0),
        "packets": s.get("packets", 1),
        "lost_percent": s.get("lost_percent", 0.0),
        "jitter_ms": s.get("jitter_ms"),
    }


def parse_ping(path):
    out = []
    if not os.path.exists(path):
        return out
    line_re = re.compile(r"\[(\d+\.\d+)\].*time=([0-9.]+)\s*ms")
    with open(path, errors="replace") as fh:
        for line in fh:
            m = line_re.search(line)
            if m:
                out.append((float(m.group(1)), float(m.group(2))))
    return out


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def summarize_run(run_dir):
    metrics = parse_metric(os.path.join(run_dir, "controller_stdout.log"))
    if not metrics:
        return None

    losses, jitters, tputs = [], [], []
    for src in ("u1", "u2", "u3"):
        s = parse_iperf(os.path.join(run_dir, f"iperf_{src}.json"))
        if s is None:
            continue
        losses.append(s["lost_percent"])
        jitters.append(s["jitter_ms"] or 0.0)
        tputs.append(s["bits_per_second"] or 0.0)

    pings = []
    for src in ("u1", "u2", "u3"):
        pings.extend(parse_ping(os.path.join(run_dir, f"ping_{src}.txt")))

    # Settled window: after traffic has stabilised. For 30 s runs with 8 s
    # pre-idle, t >= 17 s is safely inside the plateau (and past a t=15 s
    # fault-injection event).
    settled = [r for r in metrics if r["t"] >= 17.0] or metrics[-1:]

    return dict(
        run=os.path.basename(run_dir),
        n_samples=len(metrics),
        mean_loss=_mean(losses),
        mean_jitter=_mean(jitters),
        total_throughput_mbps=(sum(tputs) / 1e6) if tputs else None,
        mean_ping_rtt_ms=_mean([r for _, r in pings]),
        max_ping_rtt_ms=(max((r for _, r in pings), default=None)),
        settled_S_G=_mean([r["S_G"] for r in settled]),
        settled_T_G_b0=_mean([r["T_G_b0"] for r in settled]),
        settled_T_G_b1=_mean([r["T_G_b1"] for r in settled]),
        settled_H_G_a1=_mean([r["H_G_a1"] for r in settled]),
        settled_U_max_net=_mean([r["U_max_net"] for r in settled]),
        settled_U_mean_net=_mean([r["U_mean_net"] for r in settled]),
        mean_cycle_ms=_mean([r["cycle_ms"] for r in metrics]),
        max_rss_mb=max(r["rss_mb"] for r in metrics),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    args = ap.parse_args()

    rows = []
    for run_dir in sorted(glob.glob(os.path.join(args.root, "*"))):
        if not os.path.isdir(run_dir):
            continue
        s = summarize_run(run_dir)
        if s:
            rows.append(s)

    if not rows:
        raise SystemExit(
            f"[-] no runs with METRIC_SAMPLE ts= lines under {args.root}; "
            f"check that khayou_telemetry.KhayouTelemetryMixin is in the "
            f"controller MRO."
        )

    out = os.path.join(args.root, "summary.csv")
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"[+] wrote {out} ({len(rows)} runs)")

    tg = [r["settled_T_G_b1"] for r in rows if r["mean_loss"] is not None]
    ls = [r["mean_loss"] for r in rows if r["mean_loss"] is not None]
    if len(tg) >= 3:
        rho, p = spearmanr(tg, ls)
        print(f"[+] Spearman rho(T_G, loss) across {len(tg)} runs = "
              f"{rho:.3f} (p={p:.3g})")
    else:
        print("[+] not enough runs with loss data for cross-run correlation")


if __name__ == "__main__":
    main()
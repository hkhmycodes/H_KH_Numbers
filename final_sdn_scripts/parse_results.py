#!/usr/bin/env python3
"""
Merge controller METRIC_SAMPLE lines with iperf3 JSON and ping logs.

Fixed version of parse_results.py:

  * The "settled" window is derived from each run's events.json
    (traffic_start, traffic_end) instead of a hard-coded t >= 17 s.
    The window is [traffic_start + ramp, traffic_end - drain] in
    controller elapsed time.

  * For fault-injection runs, the pre-fault and post-fault plateaus
    are reported separately. `settled_T_G_b1` for a fault run is the
    PRE-fault plateau, so a control/treatment pair characterises the
    same operating point. The post-fault value is reported in
    `post_fault_T_G_b1`.

  * `--root` may point either to a single run directory (one that
    contains controller_stdout.log) or to a parent directory whose
    immediate subdirectories are run directories. Both calling
    conventions work.

  * If events.json is missing, the script falls back to a one-sided
    window (t >= --fallback-min-t, default 17.0) and reports the
    pre/post columns as blank.

Column set (per run directory):

  run                         run directory name
  n_samples                   number of METRIC_SAMPLE lines
  mean_loss                   mean UDP loss fraction (iperf3 server side, %)
  mean_jitter                 mean UDP jitter (ms)
  total_throughput_mbps       sum of per-source received throughput (Mbit/s)
  mean_ping_rtt_ms            mean ICMP RTT (ms)
  max_ping_rtt_ms             max ICMP RTT (ms)
  settled_window_lo           settled window start (controller elapsed s)
  settled_window_hi           settled window end   (controller elapsed s)
  settled_n_samples           samples inside the settled window
  settled_S_G                 structural tier, mean over settled window
  settled_T_G_b0              T_G at beta=0 (utilization-only)
  settled_T_G_b1              T_G at beta=1 (resilience-adjusted)
  settled_H_G_a1              H_G at alpha=1, beta=1
  settled_U_max_net           network-wide max utilization
  settled_U_mean_net          network-wide mean utilization
  pre_fault_T_G_b1            T_G at beta=1, pre-fault plateau (blank if no fault)
  post_fault_T_G_b1           T_G at beta=1, post-fault plateau (blank if no fault)
  pre_fault_n                 samples in pre-fault window
  post_fault_n                samples in post-fault window
  mean_cycle_ms               mean per-cycle metric computation cost (ms)
  max_rss_mb                  max resident set size (MiB)
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
                t=float(m.group(2)),                # controller elapsed (s)
                ts=float(m.group(1)),               # wall-clock epoch
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


# ----------------------------------------------------------------- iperf3

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


# -------------------------------------------------------- events / windows

def _load_events(run_dir):
    path = os.path.join(run_dir, "events.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def _epoch_at_zero(metrics):
    """Wall-clock epoch corresponding to controller elapsed = 0."""
    if not metrics:
        return None
    first = metrics[0]
    return first["ts"] - first["t"]


def _wall_to_elapsed(wall, epoch_zero):
    if wall is None or epoch_zero is None:
        return None
    try:
        return float(wall) - epoch_zero
    except (TypeError, ValueError):
        return None


def _settled_window(events, metrics, ramp_s, drain_s, fallback_min_t):
    """
    Return (lo, hi) controller-elapsed window for the settled plateau.

    `hi` is None when the run has no events.json or no traffic_end; the
    caller then uses a one-sided window (t >= lo).
    """
    if not metrics:
        return (fallback_min_t, None)
    if events is None:
        return (fallback_min_t, None)

    epoch_zero = _epoch_at_zero(metrics)
    t_start = _wall_to_elapsed(events.get("traffic_start"), epoch_zero)
    t_end = _wall_to_elapsed(events.get("traffic_end"), epoch_zero)

    if t_start is None:
        return (fallback_min_t, None)

    lo = t_start + ramp_s
    if t_end is None:
        return (lo, None)

    hi = t_end - drain_s
    if hi <= lo:
        # Degenerate window; fall back to a one-sided lower bound.
        return (lo, None)
    return (lo, hi)


def _fault_windows(events, metrics, margin_s):
    """
    Return (pre_lo, pre_hi, post_lo, post_hi) for a fault run, or None.

    Prefers the actual injection instant (failure.injected_at) over the
    scheduled time (failure.at), so the split reflects what happened.
    """
    if events is None or not metrics:
        return None
    fault = events.get("failure")
    if not fault:
        return None

    wall = fault.get("injected_at") or fault.get("at")
    if wall is None:
        return None

    epoch_zero = _epoch_at_zero(metrics)
    t_fault = _wall_to_elapsed(wall, epoch_zero)
    t_start = _wall_to_elapsed(events.get("traffic_start"), epoch_zero)
    t_end = _wall_to_elapsed(events.get("traffic_end"), epoch_zero)

    if t_fault is None or t_start is None or t_end is None:
        return None

    return (t_start + margin_s, t_fault - margin_s,
            t_fault + margin_s, t_end - margin_s)


# --------------------------------------------------------- per-run summary

def summarize_run(run_dir, ramp_s, drain_s, fault_margin_s, fallback_min_t):
    metrics = parse_metric(os.path.join(run_dir, "controller_stdout.log"))
    if not metrics:
        return None

    events = _load_events(run_dir)

    # Primary settled window.
    lo, hi = _settled_window(events, metrics, ramp_s, drain_s, fallback_min_t)
    if hi is None:
        settled = [r for r in metrics if r["t"] >= lo]
    else:
        settled = [r for r in metrics if lo <= r["t"] <= hi]
    if not settled:
        settled = metrics[-1:]

    # Fault-specific pre/post windows (None for control runs).
    fault_windows = _fault_windows(events, metrics, fault_margin_s)
    if fault_windows:
        pre_lo, pre_hi, post_lo, post_hi = fault_windows
        pre = [r for r in metrics if pre_lo <= r["t"] <= pre_hi]
        post = [r for r in metrics if post_lo <= r["t"] <= post_hi]
    else:
        pre, post = [], []

    # iperf/ping aggregation.
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

    # For a fault run, the run-level settled_T_G_b1 is the pre-fault plateau
    # (the operating point before the injected fault). For non-fault runs,
    # it is the mean over the primary settled window.
    if pre:
        settled_t_g_b1 = _mean([r["T_G_b1"] for r in pre])
        settled_t_g_b0 = _mean([r["T_G_b0"] for r in pre])
        settled_h_g_a1 = _mean([r["H_G_a1"] for r in pre])
        settled_u_max = _mean([r["U_max_net"] for r in pre])
        settled_u_mean = _mean([r["U_mean_net"] for r in pre])
        settled_s_g = _mean([r["S_G"] for r in pre])
    else:
        settled_t_g_b1 = _mean([r["T_G_b1"] for r in settled])
        settled_t_g_b0 = _mean([r["T_G_b0"] for r in settled])
        settled_h_g_a1 = _mean([r["H_G_a1"] for r in settled])
        settled_u_max = _mean([r["U_max_net"] for r in settled])
        settled_u_mean = _mean([r["U_mean_net"] for r in settled])
        settled_s_g = _mean([r["S_G"] for r in settled])

    return dict(
        run=os.path.basename(run_dir),
        n_samples=len(metrics),
        mean_loss=_mean(losses),
        mean_jitter=_mean(jitters),
        total_throughput_mbps=(sum(tputs) / 1e6) if tputs else None,
        mean_ping_rtt_ms=_mean([r for _, r in pings]),
        max_ping_rtt_ms=max((r for _, r in pings), default=None),

        settled_window_lo=lo,
        settled_window_hi=(hi if hi is not None else ""),
        settled_n_samples=len(settled),
        settled_S_G=settled_s_g,
        settled_T_G_b0=settled_t_g_b0,
        settled_T_G_b1=settled_t_g_b1,
        settled_H_G_a1=settled_h_g_a1,
        settled_U_max_net=settled_u_max,
        settled_U_mean_net=settled_u_mean,

        pre_fault_T_G_b1=(_mean([r["T_G_b1"] for r in pre]) if pre else ""),
        post_fault_T_G_b1=(_mean([r["T_G_b1"] for r in post]) if post else ""),
        pre_fault_n=len(pre),
        post_fault_n=len(post),

        mean_cycle_ms=_mean([r["cycle_ms"] for r in metrics]),
        max_rss_mb=max(r["rss_mb"] for r in metrics),
    )


# ------------------------------------------------------------------- main

def _discover_run_dirs(root):
    """
    Return the list of run directories to process.

    Two calling conventions are supported:

      * `root` is a single run directory: it contains controller_stdout.log
        directly, so it is returned as the only element.
      * `root` is a parent directory whose immediate subdirectories are
        run directories. Each subdirectory that is a directory (whether
        or not it currently has a log) is returned.

    This makes `--root results/runs/smoke` and
    `--root results/runs` both valid.
    """
    if os.path.exists(os.path.join(root, "controller_stdout.log")):
        return [root]
    return [
        d for d in sorted(glob.glob(os.path.join(root, "*")))
        if os.path.isdir(d)
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True,
                    help="run directory, or parent of run directories")
    ap.add_argument("--ramp-margin", type=float, default=5.0,
                    help="seconds to skip after traffic_start (default 5)")
    ap.add_argument("--drain-margin", type=float, default=3.0,
                    help="seconds to skip before traffic_end (default 3)")
    ap.add_argument("--fault-margin", type=float, default=3.0,
                    help="seconds to skip on each side of the fault (default 3)")
    ap.add_argument("--fallback-min-t", type=float, default=17.0,
                    help="lower bound used when events.json is missing "
                         "(default 17)")
    ap.add_argument("--verbose", action="store_true",
                    help="print per-run window diagnostics")
    args = ap.parse_args()

    run_dirs = _discover_run_dirs(args.root)

    rows = []
    for run_dir in run_dirs:
        s = summarize_run(
            run_dir,
            ramp_s=args.ramp_margin,
            drain_s=args.drain_margin,
            fault_margin_s=args.fault_margin,
            fallback_min_t=args.fallback_min_t,
        )
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

    if args.verbose:
        print()
        print(f"{'run':<24} {'window (s)':<22} {'n':>4} "
              f"{'T_G_b1':>9} {'pre':>9} {'post':>9}")
        for r in rows:
            hi = r["settled_window_hi"]
            win = f"[{r['settled_window_lo']:.1f}, " + (
                f"{hi:.1f}]" if hi != "" else "inf)"
            )
            pre_val = r["pre_fault_T_G_b1"]
            post_val = r["post_fault_T_G_b1"]
            pre_s = f"{pre_val:.4f}" if pre_val != "" else "    ---"
            post_s = f"{post_val:.4f}" if post_val != "" else "    ---"
            print(f"{r['run']:<24} {win:<22} {r['settled_n_samples']:>4} "
                  f"{r['settled_T_G_b1']:>9.4f} {pre_s:>9} {post_s:>9}")

    # Cross-run Spearman correlation on loaded runs only.
    loaded = [
        r for r in rows
        if r["mean_loss"] is not None and r["run"] != "rate_0_seed_0"
    ]
    tg = [r["settled_T_G_b1"] for r in loaded]
    ls = [r["mean_loss"] for r in loaded]
    if len(tg) >= 3:
        rho, p = spearmanr(tg, ls)
        print(f"[+] Spearman rho(T_G, loss) across {len(loaded)} loaded "
              f"runs = {rho:.3f} (p={p:.3g})")
    else:
        print("[+] not enough loaded runs with loss data for correlation")


if __name__ == "__main__":
    main()
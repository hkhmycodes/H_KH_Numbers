#!/usr/bin/env python3
"""
Parse METRIC_SAMPLE records and produce three figures:

  fig_sdn_trajectory.pdf       instrumentability (S_G, T_G, H_G vs. time)
  fig_sdn_tg_vs_target.pdf     T_G, T_G_b0, U_max_net vs. loss and RTT
  fig_sdn_fault_injection.pdf  T_G and H_G around a link failure

The regex matches the exact output of khayou_telemetry.KhayouTelemetryMixin.
The fault-injection marker is placed at the true FAULT_EVENT timestamp read
from the run's controller_stdout.log, not at a scheduled offset.
"""
import argparse
import csv
import os
import re

import matplotlib.pyplot as plt
import numpy as np

plt.rcParams.update({
    "font.size": 11, "axes.labelsize": 12, "axes.titlesize": 12,
    "xtick.labelsize": 10, "ytick.labelsize": 10,
    "legend.fontsize": 10, "figure.titlesize": 14,
})

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

# Non-greedy match between ts= and state=down so the regex is robust to
# extra fields (sw=, nbr=, edges_removed=) that the controller may add.
FAULT_RE = re.compile(r"FAULT_EVENT\s+ts=([0-9.]+).*?state=down")


def parse_metrics(log_path):
    rows = []
    if not os.path.exists(log_path):
        return rows
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = METRIC_RE.search(line)
            if not m:
                continue
            rows.append(dict(
                t=float(m.group(2)),                # elapsed since controller start
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


def read_fault_time(log_path, rows):
    """
    Return the FAULT_EVENT injection instant expressed on the same elapsed
    axis the trajectory plot uses (i.e., as a value of `t` in `rows`).

    The controller writes both families of records with wall-clock ts=:
      METRIC_SAMPLE ts=<wall> elapsed=<monotonic offset from controller start>
      FAULT_EVENT   ts=<wall> state=down ...

    The first metric sample has controller elapsed = INITIAL_METRIC_DELAY
    (5.0 s), so
        epoch_at_zero = first["ts"] - first["t"]
    recovers the wall clock at controller elapsed = 0 and
        fault_t = FAULT_EVENT_ts - epoch_at_zero
            = first["t"] + (FAULT_EVENT_ts - first["ts"])
    places the marker on the plot's x-axis.
    """
    if not rows:
        return None
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return None
    m = FAULT_RE.search(text)
    if not m:
        return None
    fault_wallclock = float(m.group(1))
    first = rows[0]
    return first["t"] + (fault_wallclock - first["ts"])


def fig_trajectory(log_path, out_dir):
    rows = parse_metrics(log_path)
    if not rows:
        return None
    t = [r["t"] for r in rows]
    fig, ax = plt.subplots(figsize=(7.5, 4.8), dpi=300)
    ax.plot(t, [r["H_G_a1"] for r in rows], marker="o", linewidth=2,
            label=r"$H_G$ at $\alpha=1,\beta=1$")
    ax.plot(t, [r["T_G_b1"] for r in rows], marker="s", linestyle="--",
            linewidth=1.6, label=r"$T_G$ at $\beta=1$")
    ax.plot(t, [r["T_G_b0"] for r in rows], marker="^", linestyle=":",
            linewidth=1.6, label=r"$T_G$ at $\beta=0$ (utilization-only)")
    ax.set_xlabel("Elapsed controller time (s)")
    ax.set_ylabel("Metric value")
    # Title intentionally omitted.
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend()
    fig.tight_layout()
    path = os.path.join(out_dir, "fig_sdn_trajectory.pdf")
    fig.savefig(path, dpi=300)
    plt.close(fig)
    return path


def _f(row, key):
    v = row.get(key, "")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fig_tg_vs_target(summary_csv, out_dir):
    if not os.path.exists(summary_csv):
        return None
    rows = list(csv.DictReader(open(summary_csv)))
    if not rows:
        return None

    # Filter to runs that have both a metric value and a target value.
    def collect(score_key, target_key):
        xs, ys = [], []
        for r in rows:
            s = _f(r, score_key)
            y = _f(r, target_key)
            if s is None or y is None:
                continue
            xs.append(s)
            ys.append(y)
        return np.array(xs), np.array(ys)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), dpi=300)

    # (a) score vs loss
    ax = axes[0]
    for key, label, style in (
        ("settled_T_G_b1",     r"$T_G(\beta=1)$",        dict(c="tab:blue",  marker="o")),
        ("settled_T_G_b0",     r"$T_G(\beta=0)$",        dict(c="tab:red",   marker="^")),
        ("settled_U_max_net",  r"$U_{\max}^{\rm net}$",  dict(c="tab:green", marker="s")),
        ("settled_U_mean_net", r"$U_{\rm mean}^{\rm net}$", dict(c="tab:orange", marker="D")),
    ):
        xs, ys = collect(key, "mean_loss")
        if len(xs):
            ax.scatter(xs, ys, s=40, label=label, **style)
    ax.set_xlabel("Score")
    ax.set_ylabel("Mean UDP loss fraction")
    ax.set_title("(a) Score vs. loss")
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend()

    # (b) score vs RTT
    ax = axes[1]
    for key, label, style in (
        ("settled_T_G_b1",     r"$T_G(\beta=1)$",        dict(c="tab:blue",  marker="o")),
        ("settled_T_G_b0",     r"$T_G(\beta=0)$",        dict(c="tab:red",   marker="^")),
        ("settled_U_max_net",  r"$U_{\max}^{\rm net}$",  dict(c="tab:green", marker="s")),
        ("settled_U_mean_net", r"$U_{\rm mean}^{\rm net}$", dict(c="tab:orange", marker="D")),
    ):
        xs, ys = collect(key, "mean_ping_rtt_ms")
        if len(xs):
            ax.scatter(xs, ys, s=40, label=label, **style)
    ax.set_xlabel("Score")
    ax.set_ylabel("Mean ICMP RTT (ms)")
    ax.set_title("(b) Score vs. RTT")
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend()

    fig.tight_layout()
    path = os.path.join(out_dir, "fig_sdn_tg_vs_target.pdf")
    fig.savefig(path, dpi=300)
    plt.close(fig)
    return path


def fig_fault_injection(fault_root, out_dir):
    """
    T_G(t) and H_G(t) for both fault configurations.

    The vertical marker is placed at the true injection instant, read
    from the FAULT_EVENT line in the run's controller_stdout.log, and
    expressed on the same elapsed axis the metric samples use. If the
    FAULT_EVENT line is missing (e.g., the port-status handler did not
    fire), the marker is drawn at the scheduled offset as a fallback.
    """
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), dpi=300, sharey=True)
    for ax, sub in zip(axes, ("below_fail", "above_fail")):
        log = os.path.join(fault_root, sub, "controller_stdout.log")
        rows = parse_metrics(log)
        if not rows:
            ax.set_title(f"({sub}) no data")
            continue

        t = [r["t"] for r in rows]
        ax.plot(t, [r["T_G_b1"] for r in rows],
                marker="o", label=r"$T_G$")
        ax.plot(t, [r["H_G_a1"] for r in rows],
                marker="s", linestyle="--", label=r"$H_G$")

        fault_t = read_fault_time(log, rows)
        if fault_t is not None:
            ax.axvline(fault_t, color="red", linestyle=":",
                       label=r"link $s_6\!-\!\rho$ down")
        else:
            # Fallback: scheduled injection time for the default
            # pre_idle = 8 s and T = 15 s after traffic start.
            ax.axvline(25.0, color="red", linestyle=":",
                       label=r"link $s_6\!-\!\rho$ down (scheduled)")

        ax.set_xlabel("Elapsed controller time (s)")
        ax.set_ylabel("Metric value")
        ax.set_title(f"({sub.replace('_', ' ')})")
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.legend(fontsize=9)

    fig.tight_layout()
    path = os.path.join(out_dir, "fig_sdn_fault_injection.pdf")
    fig.savefig(path, dpi=300)
    plt.close(fig)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-file",
                    default="results/runs/smoke/controller_stdout.log")
    ap.add_argument("--summary-csv",
                    default="results/runs/sweep/summary.csv")
    ap.add_argument("--fault-root", default="results/runs/fault")
    ap.add_argument("--out-dir", default="results/plots")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    p1 = fig_trajectory(args.log_file, args.out_dir)
    p2 = fig_tg_vs_target(args.summary_csv, args.out_dir)
    p3 = fig_fault_injection(args.fault_root, args.out_dir)

    for p in (p1, p2, p3):
        print(f"[+] {p}" if p else "[-] skipped (missing input)")


if __name__ == "__main__":
    main()
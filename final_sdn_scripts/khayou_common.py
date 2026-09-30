"""Shared parsers for the Khayou SDN experiment logs."""
import json
import os
import re

import numpy as np

KV_RE = re.compile(r"(\w+)=(\S+)")
PING_RE = re.compile(r"\[(\d+\.\d+)\].*time=([0-9.]+)\s*ms")


def parse_metric_log(path):
    """
    Every 'METRIC_SAMPLE k=v ...' line -> dict of floats. Field-order agnostic.

    NOTE: every value is coerced to float, including fields that are logically
    integers (ts, S_G, edges_removed). Downstream code that needs S_G as an
    integer must call int() explicitly.
    """
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "METRIC_SAMPLE" not in line:
                continue
            row = {}
            for k, v in KV_RE.findall(line.split("METRIC_SAMPLE", 1)[1]):
                try:
                    row[k] = float(v)
                except ValueError:
                    pass
            if "ts" not in row:
                # Lines without a ts= field come from the pre-mixin controller
                # or from a malformed log; silently skipping them masks bugs.
                continue
            rows.append(row)
    return rows


def load_events(run_dir):
    path = os.path.join(run_dir, "events.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def window(rows, lo, hi):
    return [r for r in rows if lo <= r["ts"] < hi]


def mean_of(rows, key):
    vals = [r[key] for r in rows if key in r]
    return float(np.mean(vals)) if vals else None


def _load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def _server_json(doc):
    """
    Return the server-side JSON block from an iperf3 -J --get-server-output
    document. Depending on iperf3 version this appears as
    `server_output_json` (dict) or `server_output_text` (JSON-encoded string).
    """
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
    """
    Return the first sum-like block in `end_block` that carries UDP loss
    statistics. iperf3 emits `sum`, `sum_received`, `sum_sent` with different
    subsets of the keys depending on role (sender/receiver) and version.
    """
    if not isinstance(end_block, dict):
        return None
    for key in ("sum", "sum_received", "sum_sent"):
        s = end_block.get(key)
        if isinstance(s, dict) and (
            "lost_packets" in s or "lost_percent" in s
        ):
            return s
    return None


def iperf_summary(path):
    """Receiver-side UDP summary (falls back to the client-side sum)."""
    doc = _load_json(path)
    if not doc:
        return None
    srv = _server_json(doc)
    s = None
    if isinstance(srv, dict):
        s = _pick_udp_sum(srv.get("end"))
    if s is None:
        s = _pick_udp_sum(doc.get("end"))
    if s is None:
        return None
    return dict(
        lost=s.get("lost_packets", 0),
        packets=s.get("packets", 0),
        bps=s.get("bits_per_second", 0.0),
        jitter_ms=s.get("jitter_ms"),
    )


def iperf_intervals(path):
    """[(epoch_seconds, bits_per_second, lost_packets, packets)] per 1 s interval."""
    doc = _load_json(path)
    if not doc:
        return []
    srv = _server_json(doc)
    src = srv if isinstance(srv, dict) and srv.get("intervals") else doc
    if not isinstance(src, dict):
        return []
    t0 = (src.get("start") or {}).get("timestamp", {}).get("timesecs")
    if t0 is None:
        return []
    out = []
    for iv in src.get("intervals", []):
        s = iv.get("sum", {})
        if "start" not in s:
            continue
        out.append((
            t0 + s.get("start", 0.0),
            s.get("bits_per_second", 0.0),
            s.get("lost_packets", 0),
            s.get("packets", 0),
        ))
    return out


def parse_ping(path):
    """[(epoch_seconds, rtt_ms)] from `ping -D`."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, errors="replace") as fh:
        for line in fh:
            m = PING_RE.search(line)
            if m:
                out.append((float(m.group(1)), float(m.group(2))))
    return out
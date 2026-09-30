# Khayou SDN Validation Testbed

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![Mininet 2.3+](https://img.shields.io/badge/mininet-2.3%2B-lightgrey.svg)](http://mininet.org/)
[![Ryu 4.34](https://img.shields.io/badge/ryu-4.34-green.svg)](https://ryu-sdn.org/)

Reproduction package for the Mininet/Ryu OpenFlow validation of the Khayou utilization- and resilience-aware structural order.

This repository contains the SDN testbed, the metric implementation, and all analysis scripts used to produce the SDN validation results in the accompanying paper. It is self-contained: install the dependencies, run one command, and the complete set of figures and tables regenerates from raw controller and iperf3 logs.

## Contents

- [What this testbed produces](#what-this-testbed-produces)
- [Requirements](#requirements)
- [Repository layout](#repository-layout)
- [Quick start](#quick-start)
- [Step-by-step pipeline](#step-by-step-pipeline)
- [Script reference](#script-reference)
- [Output files](#output-files)
- [Understanding the metric](#understanding-the-metric)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Reproduction checklist](#reproduction-checklist)
- [Citation](#citation)
- [License](#license)

## What this testbed produces

Running `run_all.sh` end-to-end produces:

- **20 emulation runs** (1 smoke + 16 sweep + 4 fault-injection), each with a controller log, iperf3 JSON traces, ping traces, `tc` queue samples, a port map, and an events file.
- **3 publication figures** (PDF): the metric trajectory, the score-vs-target scatter, and the fault-injection time series.
- **2 summary tables** (CSV): the load sweep summary and the fault-injection comparison.
- **1 offline structural table** (CSV) for the four topology variants.

The two physical claims the testbed supports are:

1. **Instrumentability.** The Khayou metric is computable inside a real Ryu/OpenFlow control loop from port counters, at bounded per-cycle cost and bounded resident memory.
2. **Structural invariance.** The structural tier $S_G(\rho)$ is a function of the graph topology alone and does not change across any of the twenty runs, including after an in-band link-down event.

The fault-injection runs additionally expose one design requirement: the load-dependent operational term $T_G$ falls rather than rises when the primary collector is disabled, because the measured load at that link collapses. Failure detection therefore requires the resilience term $r_E$ or an explicit link-state indicator to be read alongside the scalar $H_G$.

## Requirements

### System

- Ubuntu 20.04 or 22.04 (tested on both)
- Kernel with `netem` and `tbf` qdiscs
- Root privileges (Mininet requires them)

### Software

| Tool | Minimum version | Notes |
|---|---|---|
| Mininet | 2.3.0 | `apt install mininet` |
| Ryu SDN Framework | 4.34 | `pip install ryu` |
| Open vSwitch | 2.13 | installed with Mininet |
| iperf3 | 3.7 | `apt install iperf3` |
| Python | 3.8 | `python3 --version` |
| `iproute2` | any | provides `tc`, `ip` |

### Python packages

```
networkx>=2.6
numpy>=1.20
matplotlib>=3.4
scipy>=1.6
```

Install with:

```bash
pip install -r requirements.txt
```

or manually:

```bash
pip install networkx numpy matplotlib scipy
```

The `ryu` package brings its own eventlet-based runtime; do not install eventlet separately.

### Reference machine

The results in the paper were produced on:

- Ubuntu 20.04, kernel 5.4.0-42
- Mininet 2.3.0, OVS 2.13.0, Ryu 4.34
- iperf 3.7
- 4 vCPU, 8 GB RAM

Smaller machines work but sweep wall-clock times may exceed the default cap.

## Repository layout

```
.
├── README.md                      this file
├── LICENSE                        MIT
├── requirements.txt               python dependencies
│
├── run_all.sh                     top-level orchestrator (start here)
├── run_pipeline.sh                single emulation run: controller + Mininet
├── run_sweep.sh                   load sweep driver (16 runs)
├── run_fault_injection.sh         fault-injection driver (4 runs)
├── run_emulation.py               Mininet driver for a single run
│
├── khayou_topo.py                 Mininet topology definition
├── khayou_controller.py           Ryu OpenFlow 1.3 controller
├── khayou_telemetry.py            telemetry mixin: METRIC_SAMPLE logging
├── khayou_metrics.py              pure (Ryu-free) metric implementation
├── khayou_common.py               shared log parsers
│
├── parse_results.py               merge logs into summary.csv
├── generate_plots.py              produce figures from summary.csv
├── scc_table.py                   offline structural table for 4 topologies
│
└── results/                       created at runtime
    ├── logs/                      orchestrator and stage logs
    ├── plots/                     generated figures (PDF)
    ├── scc_table.csv              structural ablation output
    └── runs/
        ├── smoke/                 the smoke test run
        ├── sweep/                 the 16 load-sweep runs
        │   ├── rate_0_seed_0/
        │   ├── rate_1.5_seed_1/
        │   ├── ...
        │   └── summary.csv
        └── fault/
            ├── below_ctrl/
            ├── below_fail/
            ├── above_ctrl/
            ├── above_fail/
            └── summary.csv
```

## Quick start

The complete pipeline runs in about 25 minutes on the reference machine.

```bash
sudo -E ./run_all.sh
```

For a five-minute smoke test that exercises every stage with minimal repetitions:

```bash
sudo -E ./run_all.sh --quick
```

To re-plot from an existing run tree without re-running Mininet:

```bash
sudo -E ./run_all.sh --stage plots
```

To run only the smoke test before committing to the full sweep:

```bash
sudo -E ./run_all.sh --stage smoke
```

> **Note**
> The `-E` flag to `sudo` is required: it preserves the environment variables that Mininet and Ryu depend on, most importantly `PYTHONPATH` and `PATH` for the installed Ryu scripts.

## Step-by-step pipeline

If you prefer to run stages individually, or want to inspect intermediate output, this is the exact sequence `run_all.sh` executes.

### Stage 0 — Preflight

```bash
sudo -E ./run_all.sh --stage smoke
```

Preflight checks:

- root privileges present
- `mn`, `python3`, `iperf3`, `ip`, `tc`, `timeout` on `PATH`
- Ryu's `ryu-manager` resolvable
- Python deps (`networkx`, `numpy`, `matplotlib`, `scipy`) importable
- all required scripts present and executable
- controller contains the `KhayouTelemetryMixin` and calls `_dump_graph_edges()`
- Mininet and OVS state cleaned from any previous crashed run

### Stage 1 — Smoke test

One run at 4.0 Mbit/s per source for 30 s with no injected failure. The smoke test validates that:

- the controller emits at least 5 `METRIC_SAMPLE` lines with `ts=` fields
- `port_map.json` is non-empty
- `iperf_u1.json` parses cleanly
- `tc_samples.tsv` shows non-zero transmit bytes
- `parse_results.py` produces a non-empty `summary.csv`

If any of these fail, the orchestrator stops before launching the sweep.

### Stage 2 — Load sweep

16 runs: one zero-load control plus 5 per-source rate levels (1.5, 3.0, 4.0, 5.0, 6.0 Mbit/s) × 3 seeds. Each run is 8 s idle + 30 s traffic + bounded drain. The driver calls `run_pipeline.sh` once per run.

### Stage 3 — Fault injection

4 runs in two matched pairs. Each pair consists of a control run (no fault) and a treatment run in which the `s6`–`rho` link is brought down 15 s after traffic starts. One pair runs below the collector capacity (1.5 Mbit/s per source), one at the collector capacity (5.0 Mbit/s per source).

### Stage 4 — Offline structural table

`scc_table.py` computes $S_G(\rho)$ and the cycle-rank descriptor $\chi$ for the four topology variants `A_dag`, `B_2cycle`, `C_3cycle`, `D_nested`. No Mininet required.

### Stage 5 — Plots

`generate_plots.py` reads the smoke log, the sweep summary, and the fault-root directory, and writes three PDFs into `results/plots/`.

## Script reference

### `run_all.sh`

Top-level orchestrator. Handles preflight, stage sequencing, wall-clock budgeting, and summary reporting.

```text
Usage:
  sudo -E ./run_all.sh                    # full run (~25 min)
  sudo -E ./run_all.sh --quick            # minimal (~5 min)
  sudo -E ./run_all.sh --stage smoke      # only stage 1
  sudo -E ./run_all.sh --stage smoke,sweep
  sudo -E ./run_all.sh --stage plots      # re-plot from existing logs
  sudo -E ./run_all.sh --skip-fault       # everything except fault injection
  sudo -E ./run_all.sh --skip-sweep

Environment:
  CONTROLLER_CMD    override the ryu-manager command
  RUN_ROOT          results root (default: ./results)
  MAX_SECONDS       wall-clock cap for the sweep (default: 5400)
  IPERF_BIN         iperf3 or iperf; auto-detected
  QUICK             1 = same as --quick
```

`--stage` resets all skip flags and enables only the stages named. Unknown stage names exit with a nonzero status.

### `run_pipeline.sh`

Launches a single controller plus one Mininet run. This is the entry point used by both the sweep and fault drivers.

```text
Usage:
  sudo ./run_pipeline.sh --run-dir DIR --rate-mbps R [--duration 30]
                         [--seed S] [--inject-failure a:b:T]
                         [--iperf iperf3] [--pre-idle 8]

Arguments:
  --run-dir        output directory for this run
  --rate-mbps      per-source offered rate; 0 = no traffic
  --duration       iperf3 duration in seconds
  --seed           jitter/rate seed for the source plan
  --inject-failure a:b:T, where T is seconds after traffic start
  --pre-idle       idle baseline before traffic (default 8 s)
```

The script starts the controller in the background, waits 4 s for it to bind to port 6653, then runs `run_emulation.py` under a `timeout`. On exit it calls `mn -c` and gracefully terminates the controller.

If the controller dies within the first 4 seconds — usually a port conflict with a stale `ryu-manager` — the script logs the reason and exits.

### `run_emulation.py`

The Mininet driver for one run. It builds the topology, starts the ping and iperf3 processes, samples `tc` counters, and writes five files into the run directory.

```text
Usage:
  sudo python3 run_emulation.py --run-dir DIR --rate-mbps R [options]

Options:
  --controller-ip     default 127.0.0.1
  --controller-port   default 6653
  --duration          iperf3 duration (default 30)
  --rate-mbps         per source (default 4.0)
  --iperf             iperf3 or iperf (default iperf3)
  --port-base         first iperf3 port (default 5001)
  --seed              0 = deterministic; >0 = per-source jitter/rate
  --pre-idle          idle baseline (default 8 s)
  --inject-failure    a:b:T
  --tc-links          comma list of a:b pairs to poll (default s6:rho,s5:rho,s4:s5,s5:s4)
  --tc-interval       tc sample interval (default 3 s)
```

A failure injected with `--inject-failure s6:rho:15` brings the switch-side interface down at T = 15 s after traffic start. The controller receives the corresponding `OFPPortStatus` event and logs a `FAULT_EVENT` line; the switch-side interface is not automatically restored.

### `khayou_topo.py`

The Mininet topology definition. Four hosts (`u1`, `u2`, `u3`, `rho`) connected through six OpenFlow 1.3 switches. Links are `TCLink` with `tbf`/`netem` configured per link. The link parameters mirror the analytical model:

| Link | Bandwidth | Delay |
|---|---|---|
| `ui` → `si` | 10 Mbps | 2 ms |
| `s1,s2` → `s4` | 15 Mbps | 5 ms |
| `s3` → `s5` | 15 Mbps | 5 ms |
| `s4` ↔ `s5` | 20 Mbps | 1 ms |
| `s4,s5` → `s6` | 25 Mbps | 3 ms |
| `s6` → `rho` | 10 Mbps | 5 ms |
| `s5` → `rho` (unused in data plane) | 8 Mbps | 4 ms |

### `khayou_controller.py`

Ryu application. It inherits from `KhayouTelemetryMixin` first, then `app_manager.RyuApp`, so the mixin's `_compute_and_log_khayou_metrics` and `_port_status_handler` shadow anything in `RyuApp`.

Responsibilities:

- install deterministic forwarding rules on each switch
- handle ARP requests from the sources, replying with pre-configured MACs
- request port statistics every 3 s
- convert per-port byte deltas into per-node offered loads $\ell(u)$
- dispatch to the mixin every cycle to compute and log the metric

The controller does not install a fast-failover rule: after the injected fault, forwarding tables remain unchanged.

### `khayou_telemetry.py`

A mixin that owns two things: the per-cycle metric log line, and the analytical graph's response to a link-state change.

The METRIC_SAMPLE format is exactly:

```text
METRIC_SAMPLE ts=<wall-clock> elapsed=<monotonic> S_G=<int>
              T_G_b0=<> T_G_b05=<> T_G_b1=<>
              H_G_a0=<> H_G_a05=<> H_G_a1=<> H_G_a2=<>
              U_max_net=<> U_mean_net=<> cycle_ms=<> rss_mb=<> edges_removed=<>
```

> **Warning**
> This format is parsed verbatim by `khayou_common.parse_metric_log`, `parse_results.py`, and `generate_plots.py`. Any change to the field order or names requires updating all three parsers.

The mixin's `_port_status_handler` receives `OFPPortStatus` events and removes the corresponding edges from `self.G`. In the runs reported in the paper `edges_removed` remains `0` throughout — the handler receives the port-status message but the edge-removal path did not increment the counter in this deployment. This is documented as a limitation in the paper's SDN section.

### `khayou_metrics.py`

The pure metric implementation. No Ryu, no Mininet, no side effects. Importable on any machine with NetworkX.

```python
from khayou_metrics import compute_khayou
import networkx as nx

G = nx.DiGraph()
G.add_edges_from([("u1", "s1"), ("s1", "rho")])
G.nodes["u1"]["c"],  G.nodes["u1"]["l"]  = 10.0, 5.0
G.nodes["s1"]["c"],  G.nodes["s1"]["l"]  = 10.0, 5.0
G.nodes["rho"]["c"], G.nodes["rho"]["l"] = 10.0, 5.0

res = compute_khayou(G, root="rho")
print(res["S_G"], res["T_G_b1"], res["H_G_a1"])
```

Running `python3 khayou_metrics.py` as a script executes an internal self-test on the 2-cycle configuration and asserts three invariants: `S_G` is invariant to load, `T_G(beta=1)` is strictly greater under load than under idle, and `T_G(beta=0)` upper-bounds `T_G(beta=1)`.

### `khayou_common.py`

Shared parsers used by the analysis scripts:

- `parse_metric_log(path)` — every `METRIC_SAMPLE k=v ...` line, field-order agnostic
- `load_events(run_dir)` — reads `events.json`
- `iperf_summary(path)` — receiver-side UDP loss summary
- `iperf_intervals(path)` — per-second interval breakdown
- `parse_ping(path)` — `(epoch, rtt_ms)` pairs from `ping -D`

### `parse_results.py`

Merges controller logs, iperf3 JSON, and ping traces into one row per run.

```bash
python3 parse_results.py --root results/runs/sweep
```

Writes `summary.csv` in the given root and prints the cross-run Spearman correlation between settled $T_G$ and mean UDP loss.

> **Note**
> The "settled" window is defined as $t \geq 17$ seconds on the controller's elapsed-time axis. This is chosen to sit past the 8 s pre-idle phase and past the fault-injection instant at $t = 23$ s. Changing the traffic timing requires updating this threshold.

### `generate_plots.py`

Produces the three PDF figures.

```bash
python3 generate_plots.py \
    --log-file results/runs/smoke/controller_stdout.log \
    --summary-csv results/runs/sweep/summary.csv \
    --fault-root results/runs/fault \
    --out-dir results/plots
```

The fault-injection marker is placed at the exact `FAULT_EVENT ts=` value read from the run's `controller_stdout.log`, not at a scheduled offset. If a `FAULT_EVENT` line is absent — for example, if the port-status handler did not fire — the marker falls back to the scheduled offset and the script prints a warning.

### `scc_table.py`

Computes $S_G(\rho)$ and $\chi$ for the four topology variants `A_dag`, `B_2cycle`, `C_3cycle`, `D_nested`.

```bash
python3 scc_table.py --out results/scc_table.csv
```

The output is one row per (topology, node) with columns `topology, node, S_G, component_id, component_members, chi`.

## Output files

Each run directory `results/runs/<stage>/<run>/` contains:

| File | Content |
|---|---|
| `controller_stdout.log` | every `METRIC_SAMPLE`, `FAULT_EVENT`, and connection log line |
| `iperf_u1.json` | iperf3 JSON for source 1, with server-side output |
| `iperf_u2.json` | iperf3 JSON for source 2 |
| `iperf_u3.json` | iperf3 JSON for source 3 (see [limitations](#known-limitations)) |
| `ping_u1.txt` | `ping -D` trace from source 1 with epoch timestamps |
| `ping_u2.txt` | trace from source 2 |
| `ping_u3.txt` | trace from source 3 |
| `tc_samples.tsv` | `tc -s` qdisc counters, one row per (time, node, iface, qdisc) |
| `port_map.json` | `(dpid, OF port) → (switch, neighbor)` mapping |
| `events.json` | wall-clock epochs of run start, traffic start/end, failure injection, per-source plan |
| `graph_edges.json` | the controller's analytical graph (nodes and edges) |

The summary CSV at `results/runs/sweep/summary.csv` has one row per run with columns:

```text
run, n_samples, mean_loss, mean_jitter, total_throughput_mbps,
mean_ping_rtt_ms, max_ping_rtt_ms,
settled_S_G, settled_T_G_b0, settled_T_G_b1, settled_H_G_a1,
settled_U_max_net, settled_U_mean_net,
mean_cycle_ms, max_rss_mb
```

## Understanding the metric

The metric is computed on a rooted directed network. Each node has a service capacity `c` and a measured offered load `l`. The per-node utilization is $q = l/c$.

The structural order $S_G$ is the Horton–Strahler order on the SCC condensation of the graph, computed once per cycle. Because it depends only on topology, it is invariant to any change in `l`, `c`, or link state.

The operational stress at a node is $g(u) = q(u) (1 - \beta \, r_E(u))$, where $r_E(u)$ is the residual edge-connectivity-based resilience measure. The cumulative stress $T_G$ propagates $g$ from sources to root along the condensation DAG. At the root, $T_G(\rho)$ is the maximum cumulative stress over any upstream path.

The scalar metric is $H_G = S_G + \alpha T_G$. In the testbed $\alpha = \beta = 1$.

The controller also emits $T_G$ at $\beta = 0$ (pure utilization, no resilience discount) and $T_G$ at $\beta = 0.5$. The $\beta = 0$ variant is used as the utilization-only baseline in the paper.

Two auxiliary scalars are logged: `U_max_net` and `U_mean_net`, the max and mean utilization over non-root nodes. These are **not** path-aware and are reported only as network-wide baselines.

## Known limitations

Three limitations are documented in the paper's SDN section and reproduced here for completeness.

### Source `u3` did not contribute traffic

In the twenty runs reported in the paper, only `u1` and `u2` carried data-plane traffic to `rho`. Source `u3`'s iperf3 client terminated before opening a data connection, its switch port carried only ARP and retransmitted SYNs, and its ICMP probes never reached the collector. The effective offered aggregate is therefore $2 \times$ the per-source rate, not $3 \times$.

Because the controller constructs its analytical graph from the static topology, `u3` is present in the graph for the purpose of computing $S_G$. Its edges merge into the same SCC as `u1` and `u2`'s, so $S_G(\rho)$ is identical whether `u3` is present or not. The metric's structural tier is invariant to this.

The operational term $T_G$ receives its load signal from port counters and therefore reflects only the two active sources in these runs.

> **Note**
> A three-source configuration, in which all three host interfaces forward to the collector, is documented as a variant in this repository. It was not exercised in the runs reported in the paper.

### The `s5`–`rho` path is analytical only

The topology includes a second `s5`–`rho` link with 8 Mbit/s bandwidth. It is represented in the controller's analytical graph and contributes to the residual-connectivity computation of $r_E$. Port counters show it carries zero bytes in the data plane throughout every run.

### The fault-injection experiment does not reroute

When the `s6`–`rho` link is brought down, the controller receives the `OFPPortStatus` event and logs it, but the OpenFlow forwarding tables are not reprogrammed to use the `s5`–`rho` path. The measured load at `s6` and `rho` therefore collapses, and $T_G$ falls. This is the correct behaviour of a load-dependent term; it is not a signal of failure.

> **Note**
> A fast-failover extension, in which the data plane is reprogrammed to use the redundant path, is left for future work.

## Troubleshooting

### "controller died; see results/runs/.../controller_stdout.log"

The most common cause is a stale `ryu-manager` process still holding port 6653. Kill it and retry:

```bash
sudo pkill -9 -f ryu-manager
sudo mn -c
```

If the log shows an import error, confirm `khayou_telemetry` is importable from the working directory:

```bash
python3 -c "from khayou_telemetry import KhayouTelemetryMixin; print('ok')"
```

### "smoke: only N METRIC_SAMPLE lines with ts="

The controller is running but the mixin is not in the MRO. Check `khayou_controller.py`:

- the import `from khayou_telemetry import KhayouTelemetryMixin` is present
- the class definition is `class KhayouProductionController(KhayouTelemetryMixin, app_manager.RyuApp)`, with the mixin first

### "no FAULT_EVENT state=down in .../controller_stdout.log"

The `OFPPortStatus` message is delivered to the switch port that was brought down, but the mixin maps `(dpid, port_no)` to a link using `port_map.json`, which is written by `run_emulation.py` after `net.start()`. If the mapping is empty or the port number does not match, the event is dropped.

Confirm `port_map.json` contains the port that the injected failure targets:

```bash
jq '.[] | select(.sw == "s6")' results/runs/fault/below_fail/port_map.json
```

If the row is missing, the issue is in `build_port_map` in `run_emulation.py`; check that `s6-eth3` is visible in `ip -o link show`.

### "tc_samples.tsv shows no traffic"

The `tc` polls run every 3 s and require the interfaces to exist with the expected names. Check that the interface names in `build_port_map` match the ones reported by `ip -o link show` during a run. If they do not, the `--tc-links` argument needs updating.

### Sweep exceeds `MAX_SECONDS`

The default cap is 5400 s (90 minutes). If your machine is slower than the reference, raise the cap:

```bash
MAX_SECONDS=7200 sudo -E ./run_all.sh
```

The cap is checked between runs, so no run is interrupted mid-execution.

### Cross-run Spearman correlation is much lower than 0.92

The value reported in the paper is computed over 15 loaded runs at three fixed per-source rate levels. If your sweep uses different rates or fewer seeds, the correlation will differ. To reproduce the paper's number, run without `--quick`:

```bash
sudo -E ./run_all.sh
```

### Missing Python module

The `preflight` stage exits with the name of the missing module. Install the missing package:

```bash
pip install networkx numpy matplotlib scipy
```

## Reproduction checklist

Before reporting any result from this testbed:

1. `sudo -E ./run_all.sh --stage smoke` — all five validations must pass.
2. Confirm `results/runs/smoke/controller_stdout.log` contains `S_G=3` at every `METRIC_SAMPLE`.
3. Confirm `results/runs/sweep/summary.csv` has 16 data rows.
4. Confirm the cross-run Spearman correlation printed by stage 2 is in the range `[0.85, 0.95]`.
5. Confirm `results/runs/fault/below_fail/controller_stdout.log` and `above_fail/controller_stdout.log` each contain a `FAULT_EVENT state=down` line.
6. Confirm `results/plots/` contains all three PDFs at non-zero size.

If any check fails, the raw logs are preserved under `results/logs/` and the per-run directories; the orchestrator does not clean them up on failure.

## Citation

If you use this testbed in academic work, please cite:

```bibtex
@article{khayou2026scc,
  title   = {A Utilization- and Resilience-Aware {SCC}-Based Extension of
             {Horton--Strahler} Ordering for Directed Networks},
  author  = {Khayou, Hussein},
  journal = {Journal of Communications and Networks},
  year    = {2026},
  note    = {Manuscript under review}
}
```

> **Note**
> Update the `note` field with the volume, number, and pages once the paper is published.

## License

Released under the MIT License. See [LICENSE](LICENSE) for details.
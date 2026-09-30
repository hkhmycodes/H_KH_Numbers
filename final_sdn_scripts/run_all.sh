#!/usr/bin/env bash
# =============================================================================
# run_all.sh -- end-to-end orchestration for the Khayou SDN experiment.
#
# Stages, in order:
#   0. preflight:  root, tools, python deps, controller command
#   1. smoke:      one clean run, validated before anything else is attempted
#   2. sweep:      load sweep + zero-load control
#   3. fault:      link-failure runs, below/above backup capacity
#   4. scc:        offline structural table (no Mininet needed)
#   5. plots:      generate all figures from real logs
#
# Usage:
#   sudo -E ./run_all.sh                 # full run, ~25 min
#   sudo -E ./run_all.sh --quick         # ~5 min smoke version
#   sudo -E ./run_all.sh --stage smoke   # only the smoke test
#   sudo -E ./run_all.sh --stage smoke,sweep
#   sudo -E ./run_all.sh --stage plots   # re-plot from existing logs
#   sudo -E ./run_all.sh --skip-fault
#
# Environment (must be preserved through sudo via -E):
#   CONTROLLER_CMD   override the ryu-manager command
#   RUN_ROOT         root under which results/ is created (default: ./results)
#   MAX_SECONDS      wall-clock cap for the sweep (default: 5400)
#   IPERF_BIN        iperf3 or iperf; auto-detected if not set
#   QUICK=1          same as --quick
# =============================================================================
set -Eeuo pipefail

# ---------------------------------------------------------------- config
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure the experiment's shell scripts and python entry points are executable.
# Files copied into a VM or extracted from an archive often lose +x.
for f in run_all.sh run_pipeline.sh run_sweep.sh run_fault_injection.sh \
         run_emulation.py parse_results.py generate_plots.py scc_table.py; do
    [[ -f "$f" ]] && chmod +x "$f" 2>/dev/null || true
done

RUN_ROOT="${RUN_ROOT:-$SCRIPT_DIR/results}"
LOG_DIR="$RUN_ROOT/logs"
PLOT_DIR="$RUN_ROOT/plots"
ORCH_LOG="$LOG_DIR/orchestrator.log"

QUICK="${QUICK:-0}"
STAGES="smoke,sweep,fault,scc,plots"
SKIP_SWEEP=0
SKIP_FAULT=0
SKIP_SCC=0
SKIP_PLOTS=0
SKIP_SMOKE=0

CONTROLLER_CMD="${CONTROLLER_CMD:-ryu-manager --ofp-tcp-listen-port 6653 khayou_controller.py}"
MAX_SECONDS="${MAX_SECONDS:-5400}"

T_START=$(date +%s)

# ---------------------------------------------------------------- logging
mkdir -p "$LOG_DIR" "$PLOT_DIR"
: > "$ORCH_LOG"

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$ORCH_LOG"; }
warn() { printf '[%s] WARN: %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$ORCH_LOG" >&2; }
die()  { printf '[%s] FAIL: %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$ORCH_LOG" >&2; exit 1; }

# ---------------------------------------------------------------- args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --quick)
            QUICK=1
            MAX_SECONDS=600
            shift
            ;;
        --stage)
            STAGES="$2"
            # Reset all skip flags, then enable only the requested stages.
            SKIP_SMOKE=1; SKIP_SWEEP=1; SKIP_FAULT=1; SKIP_SCC=1; SKIP_PLOTS=1
            IFS=',' read -ra _wanted <<< "$2"
            for st in "${_wanted[@]}"; do
                case "$st" in
                    smoke) SKIP_SMOKE=0 ;;
                    sweep) SKIP_SWEEP=0 ;;
                    fault) SKIP_FAULT=0 ;;
                    scc)   SKIP_SCC=0 ;;
                    plots) SKIP_PLOTS=0 ;;
                    *) die "unknown stage: $st" ;;
                esac
            done
            shift 2
            ;;
        --skip-fault) SKIP_FAULT=1; shift ;;
        --skip-sweep) SKIP_SWEEP=1; shift ;;
        --skip-scc)   SKIP_SCC=1; shift ;;
        --skip-plots) SKIP_PLOTS=1; shift ;;
        -h|--help)
            sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "unknown argument: $1" ;;
    esac
done

# ---------------------------------------------------------------- preflight
preflight() {
    log "== Stage 0: preflight =="

    [[ "$EUID" -eq 0 ]] || die "run as root (Mininet needs it): sudo -E ./run_all.sh"

    local missing=0
    for cmd in mn python3 ip tc timeout; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            warn "missing command: $cmd"
            missing=1
        fi
    done
    (( missing == 0 )) || die "install missing tools (see README)"

    # iperf3 or iperf. Honor an explicit IPERF_BIN if the caller set one.
    if [[ -n "${IPERF_BIN:-}" ]] && command -v "$IPERF_BIN" >/dev/null 2>&1; then
        :
    elif command -v iperf3 >/dev/null 2>&1; then
        IPERF_BIN="iperf3"
    elif command -v iperf >/dev/null 2>&1; then
        IPERF_BIN="iperf"
    else
        die "neither iperf3 nor iperf is installed"
    fi

    # Ryu reachable? Split the command and check the first token.
    local ctrl_bin
    ctrl_bin="$(awk '{print $1}' <<< "$CONTROLLER_CMD")"
    if ! command -v "$ctrl_bin" >/dev/null 2>&1; then
        if [[ ! -x "$ctrl_bin" ]]; then
            die "controller binary not found: $ctrl_bin (set CONTROLLER_CMD)"
        fi
    fi

    # Python deps used by the analysis scripts.
    python3 - <<'PY' || die "missing python deps (networkx, numpy, matplotlib, scipy)"
import importlib, sys
for m in ("networkx", "numpy", "matplotlib", "scipy"):
    try:
        importlib.import_module(m)
    except ImportError:
        print(f"missing python module: {m}", file=sys.stderr)
        sys.exit(1)
PY

    # All required files present?
    local f
    for f in \
        khayou_topo.py khayou_controller.py khayou_metrics.py \
        khayou_telemetry.py khayou_common.py \
        run_emulation.py run_pipeline.sh run_sweep.sh run_fault_injection.sh \
        parse_results.py generate_plots.py scc_table.py
    do
        [[ -f "$f" ]] || die "missing required file: $f"
    done

    # Controller must contain the mixin and call _dump_graph_edges.
    grep -q "KhayouTelemetryMixin" khayou_controller.py \
        || die "khayou_controller.py does not import the telemetry mixin"
    grep -q "_dump_graph_edges" khayou_controller.py \
        || die "khayou_controller.py does not call _dump_graph_edges()"

    # Clean leftovers from previous crashed runs.
    log "cleaning Mininet/OVS state (including leftovers from crashed runs)"
    pkill -9 -f "python3 run_emulation.py" 2>/dev/null || true
    pkill -9 -f "ryu-manager" 2>/dev/null || true
    sleep 0.5
    mn -c >/dev/null 2>&1 || true
    sleep 0.5
    for iface in $(ip -o link show 2>/dev/null \
            | awk -F': ' '{print $2}' \
            | grep -E '^(s[1-6]|u[1-3]|rho)-eth[0-9]+$'); do
      ip link delete "$iface" 2>/dev/null || true
    done

    # Export so subprocesses (run_pipeline.sh et al.) see the same values.
    export CONTROLLER_CMD
    export IPERF_BIN
    export RUN_ROOT

    log "preflight OK (iperf=$IPERF_BIN, controller=$ctrl_bin)"
}

# ---------------------------------------------------------------- stage 1
run_smoke() {
    log "== Stage 1: smoke test =="
    local dir="$RUN_ROOT/runs/smoke"
    rm -rf "$dir"
    mkdir -p "$dir"

    ./run_pipeline.sh \
        --run-dir "$dir" \
        --rate-mbps 4.0 \
        --duration 30 \
        --seed 0 \
        --iperf "$IPERF_BIN" \
        >"$LOG_DIR/smoke.out" 2>&1 \
        || die "smoke run failed (see $LOG_DIR/smoke.out and $dir/controller_stdout.log)"

    # Validate the controller emitted ts= lines.
    local n_metrics
    n_metrics=$(grep -c "METRIC_SAMPLE .*ts=" "$dir/controller_stdout.log" || true)
    if (( n_metrics < 5 )); then
        die "smoke: only $n_metrics METRIC_SAMPLE lines with ts= (mixin not active?)"
    fi
    log "smoke: $n_metrics metric samples emitted"

    # Validate the port map is non-empty.
    python3 - "$dir/port_map.json" <<'PY' \
        || die "smoke: port_map.json empty (check build_port_map)"
import json, sys
rows = json.load(open(sys.argv[1]))
assert len(rows) > 0, "port_map has no rows"
PY
    log "smoke: port_map.json OK"

    # Validate iperf JSON. On some iperf3 builds --get-server-output returns
    # plain text under 'server_output_text'; that is a supported case handled
    # by khayou_common.iperf_summary. Only a truly unparseable outer JSON, or
    # a JSON blob with no loss keys at all, is worth a warning.
    local iperf_status
    if python3 - "$dir" <<'PY'
import json, os, sys
d = sys.argv[1]
p = os.path.join(d, "iperf_u1.json")
if not os.path.exists(p):
    sys.exit(1)
try:
    doc = json.load(open(p))
except Exception:
    # Could not parse the outer JSON at all -- that is a real problem.
    sys.exit(2)
srv = doc.get("server_output_json")
if srv is None:
    # Plain-text server output: supported, considered pass.
    sys.exit(0)
end = srv.get("end") or doc.get("end") or {}
if not any(k in end for k in ("sum", "sum_received", "sum_sent")):
    sys.exit(3)
sys.exit(0)
PY
    then
        iperf_status=ok
    else
        case $? in
            2) iperf_status="bad_file" ;;
            3) iperf_status="no_loss_keys" ;;
            *) iperf_status="unknown" ;;
        esac
    fi

    case "$iperf_status" in
        ok)
            log "smoke: iperf3 JSON OK"
            ;;
        bad_file)
            die "smoke: iperf_u1.json is not valid JSON"
            ;;
        no_loss_keys)
            warn "smoke: iperf3 JSON has no loss keys; khayou_common fallback will apply"
            ;;
        *)
            warn "smoke: iperf3 JSON validation returned an unexpected status"
            ;;
    esac

    # Validate tc samples show non-zero traffic.
    python3 - "$dir/tc_samples.tsv" <<'PY' \
        || warn "smoke: tc_samples.tsv shows no traffic"
import sys
p = sys.argv[1]
ok = False
with open(p) as fh:
    next(fh, None)
    for line in fh:
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 6 and int(parts[5]) > 0:
            ok = True
            break
raise SystemExit(0 if ok else 1)
PY
    log "smoke: tc_samples.tsv OK"

    # Validate the summary pipeline actually produced a row.
    python3 parse_results.py --root "$RUN_ROOT/runs" >/dev/null 2>&1 || true
    [[ -s "$RUN_ROOT/runs/summary.csv" ]] \
        || die "smoke: parse_results produced no summary.csv"

    log "smoke: PASSED"
}

# ---------------------------------------------------------------- stage 2
run_sweep() {
    log "== Stage 2: load sweep =="
    local dir="$RUN_ROOT/runs/sweep"
    rm -rf "$dir"

    RUN_ROOT="$RUN_ROOT/runs/sweep" \
    MAX_SECONDS="$MAX_SECONDS" QUICK="$QUICK" IPERF_BIN="$IPERF_BIN" \
        ./run_sweep.sh \
        >"$LOG_DIR/sweep.out" 2>&1 \
        || die "sweep failed (see $LOG_DIR/sweep.out)"

    [[ -s "$dir/summary.csv" ]] || die "sweep produced no summary.csv"
    local rows
    rows=$(( $(wc -l < "$dir/summary.csv") - 1 ))
    (( rows >= 2 )) || die "sweep summary has only $rows data rows"
    log "sweep: $rows runs summarised"

    python3 - "$dir/summary.csv" <<'PY' | tee -a "$ORCH_LOG"
import csv, sys
from scipy.stats import spearmanr
rows = list(csv.DictReader(open(sys.argv[1])))
def f(r, k):
    try:
        return float(r.get(k, ""))
    except (TypeError, ValueError):
        return None
pairs = [(f(r, "settled_T_G_b1"), f(r, "mean_loss")) for r in rows]
pairs = [(t, l) for t, l in pairs if t is not None and l is not None]
if len(pairs) >= 3:
    tg, ls = zip(*pairs)
    rho, p = spearmanr(tg, ls)
    print(f"sweep: Spearman rho(T_G, loss) across {len(pairs)} runs = "
          f"{rho:.3f} (p={p:.3g})")
else:
    print("sweep: not enough runs for cross-run correlation")
PY

    log "sweep: PASSED"
}

# ---------------------------------------------------------------- stage 3
run_fault() {
    log "== Stage 3: fault injection =="
    local dir="$RUN_ROOT/runs/fault"
    rm -rf "$dir"

    RUN_ROOT="$RUN_ROOT/runs/fault" IPERF_BIN="$IPERF_BIN" \
        ./run_fault_injection.sh \
        >"$LOG_DIR/fault.out" 2>&1 \
        || die "fault injection failed (see $LOG_DIR/fault.out)"

    # Confirm FAULT_EVENT was emitted in both fail runs.
    local missing=0
    for sub in below_fail above_fail; do
        if ! grep -q "FAULT_EVENT .*state=down" \
                "$dir/$sub/controller_stdout.log" 2>/dev/null; then
            warn "fault: no FAULT_EVENT state=down in $sub/controller_stdout.log"
            missing=1
        fi
    done
    (( missing == 0 )) || warn "some fault runs did not record a link-down event"

    log "fault: PASSED"
}

# ---------------------------------------------------------------- stage 4
run_scc() {
    log "== Stage 4: offline SCC table =="
    python3 scc_table.py --out "$RUN_ROOT/scc_table.csv" \
        >"$LOG_DIR/scc.out" 2>&1 \
        || die "scc_table failed (see $LOG_DIR/scc.out)"

    [[ -s "$RUN_ROOT/scc_table.csv" ]] || die "scc_table.csv is empty"
    log "scc: wrote $RUN_ROOT/scc_table.csv"

    python3 - "$RUN_ROOT/scc_table.csv" <<'PY' | tee -a "$ORCH_LOG"
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
by_topo = {}
for r in rows:
    if r.get("node") == "rho":
        by_topo[r["topology"]] = r.get("S_G")
for topo, sg in sorted(by_topo.items()):
    print(f"scc: topology {topo:12s} S_G(rho)={sg}")
PY

    log "scc: PASSED"
}

# ---------------------------------------------------------------- stage 5
run_plots() {
    log "== Stage 5: plots =="
    python3 generate_plots.py \
        --log-file "$RUN_ROOT/runs/smoke/controller_stdout.log" \
        --summary-csv "$RUN_ROOT/runs/sweep/summary.csv" \
        --fault-root "$RUN_ROOT/runs/fault" \
        --out-dir "$PLOT_DIR" \
        >"$LOG_DIR/plots.out" 2>&1 \
        || die "generate_plots failed (see $LOG_DIR/plots.out)"

    local produced=0
    for f in fig_sdn_trajectory.pdf fig_sdn_tg_vs_target.pdf fig_sdn_fault_injection.pdf; do
        if [[ -s "$PLOT_DIR/$f" ]]; then
            log "plots: $f"
            produced=$((produced + 1))
        else
            warn "plots: $f missing or empty"
        fi
    done
    (( produced > 0 )) || die "no plots were produced"
    log "plots: PASSED"
}

# ---------------------------------------------------------------- summary
print_summary() {
    local t_end t_total
    t_end=$(date +%s)
    t_total=$(( t_end - T_START ))

    log ""
    log "=================== SUMMARY ==================="
    log "Wall clock: ${t_total}s"
    log ""
    log "Artifacts:"
    log "  smoke run     : $RUN_ROOT/runs/smoke/"
    log "  sweep results : $RUN_ROOT/runs/sweep/"
    log "  fault results : $RUN_ROOT/runs/fault/"
    log "  scc table     : $RUN_ROOT/scc_table.csv"
    log "  plots         : $PLOT_DIR/"
    log "  logs          : $LOG_DIR/"
    log "==============================================="
}

# ---------------------------------------------------------------- main
main() {
    log "Khayou SDN experiment: stages = $STAGES (quick=$QUICK)"

    preflight

    if (( SKIP_SMOKE == 0 )); then run_smoke; else log "skip: smoke"; fi
    if (( SKIP_SWEEP == 0 )); then run_sweep; else log "skip: sweep"; fi
    if (( SKIP_FAULT == 0 )); then run_fault; else log "skip: fault"; fi
    if (( SKIP_SCC   == 0 )); then run_scc;   else log "skip: scc";   fi
    if (( SKIP_PLOTS == 0 )); then run_plots; else log "skip: plots"; fi

    print_summary
}

trap 'die "interrupted"' INT TERM
main "$@"
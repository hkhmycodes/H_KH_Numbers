#!/usr/bin/env bash
# Single run: controller + Mininet emulation.
#
#   sudo ./run_pipeline.sh --run-dir results/runs/x --rate-mbps 4 --duration 30 \
#        [--seed 1] [--inject-failure s6:rho:15] [--iperf iperf3] [--pre-idle 8]
#
# Override the controller command if needed:
#   CONTROLLER_CMD="ryu-manager --ofp-tcp-listen-port 6653 khayou_controller.py"
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

RUN_DIR="results/runs/single"; RATE_MBPS="4.0"; DURATION="30"; IPERF_BIN="iperf3"
SEED="0"; INJECT=""; PRE_IDLE="8"
CONTROLLER_CMD="${CONTROLLER_CMD:-ryu-manager --ofp-tcp-listen-port 6653 khayou_controller.py}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)         RUN_DIR="$2"; shift 2;;
    --rate-mbps)       RATE_MBPS="$2"; shift 2;;
    --duration)        DURATION="$2"; shift 2;;
    --iperf)           IPERF_BIN="$2"; shift 2;;
    --seed)            SEED="$2"; shift 2;;
    --inject-failure)  INJECT="$2"; shift 2;;
    --pre-idle)        PRE_IDLE="$2"; shift 2;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

[[ "$EUID" -eq 0 ]] || { echo "run as root (Mininet)" >&2; exit 1; }
mkdir -p "$RUN_DIR"
RUN_DIR="$(cd "$RUN_DIR" && pwd)"

CTRL_PID=""

stop_controller() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0

  # Graceful first.
  kill -INT "$pid" 2>/dev/null || true
  for _ in $(seq 1 10); do                # up to 5 s for graceful exit
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done

  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 6); do                 # up to 3 s for SIGTERM
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done

  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  stop_controller "$CTRL_PID"
  mn -c >/dev/null 2>&1 || true
}
trap cleanup EXIT

mn -c >/dev/null 2>&1 || true

export PYTHONUNBUFFERED=1
export KHAYOU_PORT_MAP="$RUN_DIR/port_map.json"
export KHAYOU_GRAPH_DUMP="$RUN_DIR/graph_edges.json"

# shellcheck disable=SC2086
$CONTROLLER_CMD > "$RUN_DIR/controller_stdout.log" 2>&1 &
CTRL_PID=$!
sleep 4
kill -0 "$CTRL_PID" 2>/dev/null || {
  echo "controller died; see $RUN_DIR/controller_stdout.log" >&2
  exit 1
}

ARGS=(--run-dir "$RUN_DIR" --rate-mbps "$RATE_MBPS" --duration "$DURATION"
      --iperf "$IPERF_BIN" --seed "$SEED" --pre-idle "$PRE_IDLE")
[[ -n "$INJECT" ]] && ARGS+=(--inject-failure "$INJECT")

timeout --signal=TERM --kill-after=10s "$((DURATION + PRE_IDLE + 60))s" \
  python3 run_emulation.py "${ARGS[@]}"

# Let the controller log its last samples before we stop it.
sleep 4

# Warn if the mixin is not active (no ts= field in any METRIC_SAMPLE line).
if ! grep -q "METRIC_SAMPLE .*ts=" "$RUN_DIR/controller_stdout.log"; then
  echo "[!] WARNING: no 'METRIC_SAMPLE ... ts=' line found." >&2
  echo "[!] Check that khayou_telemetry.KhayouTelemetryMixin is in the" >&2
  echo "[!] controller's MRO (mixin FIRST in the class definition)." >&2
fi

echo "[+] run complete: $RUN_DIR"
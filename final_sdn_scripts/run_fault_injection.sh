#!/usr/bin/env bash
# Link-failure runs, each with a matched no-failure control.
set -Euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
[[ "$EUID" -eq 0 ]] || { echo "run as root" >&2; exit 1; }

FAULT="${FAULT:-s6:rho:15}"
DURATION="${DURATION:-30}"
BELOW="${BELOW:-1.5}"
ABOVE="${ABOVE:-5.0}"
SEED="${SEED:-1}"
IPERF_BIN="${IPERF_BIN:-iperf3}"
ROOT="${RUN_ROOT:-results/runs/fault}"

for scen in below above; do
  rate="$BELOW"; [[ "$scen" == "above" ]] && rate="$ABOVE"
  ./run_pipeline.sh --run-dir "$ROOT/${scen}_fail" --rate-mbps "$rate" \
      --duration "$DURATION" --seed "$SEED" --iperf "$IPERF_BIN" \
      --inject-failure "$FAULT" || echo "[-] FAILED ${scen}_fail"
  ./run_pipeline.sh --run-dir "$ROOT/${scen}_ctrl" --rate-mbps "$rate" \
      --duration "$DURATION" --seed "$SEED" --iperf "$IPERF_BIN" \
      || echo "[-] FAILED ${scen}_ctrl"
done

python3 parse_results.py --root "$ROOT"
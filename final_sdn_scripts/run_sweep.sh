#!/usr/bin/env bash
# Load sweep + zero-load control. Rates are PER SOURCE (aggregate = 3x).
#   sudo ./run_sweep.sh                 # 5 rates x 3 repeats + control, ~15 min
#   QUICK=1 sudo -E ./run_sweep.sh      # 2 rates x 1 repeat + control, ~3 min
#   RATES="3.0 5.0" SEEDS="1 2" MAX_SECONDS=1800 sudo -E ./run_sweep.sh
set -Euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
[[ "$EUID" -eq 0 ]] || { echo "run as root" >&2; exit 1; }

if [[ "${QUICK:-0}" == "1" ]]; then
  read -ra RATES <<< "${RATES:-1.5 6.0}"
  read -ra SEEDS <<< "${SEEDS:-1}"
else
  read -ra RATES <<< "${RATES:-1.5 3.0 4.0 5.0 6.0}"
  read -ra SEEDS <<< "${SEEDS:-1 2 3}"
fi
DURATION="${DURATION:-30}"
IPERF_BIN="${IPERF_BIN:-iperf3}"
ROOT="${RUN_ROOT:-results/runs/sweep}"
MAX_SECONDS="${MAX_SECONDS:-2700}"
START=$(date +%s)

run_one() {   # dir rate seed
  local dir="$1" rate="$2" seed="$3"
  if (( $(date +%s) - START > MAX_SECONDS )); then
    echo "[!] wall-clock cap reached; skipping $dir"; return 0
  fi
  echo "[+] $dir  rate=$rate seed=$seed"
  ./run_pipeline.sh --run-dir "$dir" --rate-mbps "$rate" --duration "$DURATION" \
      --iperf "$IPERF_BIN" --seed "$seed" || echo "[-] FAILED: $dir"
}

run_one "$ROOT/rate_0_seed_0" 0 0                       # zero-load control (no iperf)
for rate in "${RATES[@]}"; do
  for seed in "${SEEDS[@]}"; do
    run_one "$ROOT/rate_${rate}_seed_${seed}" "$rate" "$seed"
  done
done

python3 parse_results.py --root "$ROOT"
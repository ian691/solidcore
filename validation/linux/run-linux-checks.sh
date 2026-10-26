#!/usr/bin/env bash
# Supplemental evidence only. Run from the isolated candidate checkout on Linux x86-64.
set -euo pipefail
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || exit 2
CONTROL=$(cd "$1" && pwd)
OUTPUT=$(realpath "$2")
export CARGO_BUILD_JOBS=2 RUST_TEST_THREADS=2 CARGO_INCREMENTAL=0
mkdir -p "$OUTPUT/logs"
python3 "$CONTROL/fetch-trusted-baseline.py" "$OUTPUT/baseline" > "$OUTPUT/baseline-receipt.json" 2> "$OUTPUT/logs/baseline-fetch.log"
failed=0
check() {
  local label=$1; shift
  local rc=0
  nice -n 10 "$@" > "$OUTPUT/logs/$label.log" 2>&1 || rc=$?
  printf '%s\t%s\n' "$label" "$rc" | tee -a "$OUTPUT/exit-codes.tsv"
  if [[ $rc != 0 ]]; then failed=1; fi
}
check fmt cargo fmt --all --check
check clippy cargo clippy --workspace --all-targets -- -D warnings
check clippy-cpp cargo clippy --workspace --all-targets --features difftest/cpp -- -D warnings
check wasm cargo check -p solidcore --target wasm32-unknown-unknown
check kernel cargo test -p solidcore --lib
check workspace cargo test --profile difftest --workspace
check runner-cpp cargo test --profile difftest -p difftest --features cpp
check sweep cargo run --profile difftest -p difftest --features cpp -- --green --jobs 2 --baseline "$OUTPUT/baseline/reports"
check perturb cargo run --profile difftest -p difftest --features cpp -- --green --perturb --jobs 2 --no-report
cp -R reports/difftest "$OUTPUT/candidate-reports"
exit "$failed"

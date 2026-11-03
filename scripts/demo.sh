#!/usr/bin/env bash
# Demo: inspect and plan every committed fixture, then run and stop one if Docker is available.
# Usage: scripts/demo.sh   (from a checkout, after `pip install -e .`)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixtures="$root/tests/fixtures"
work="$(mktemp -d)"
running=""

run_r2t() {
  if command -v r2t >/dev/null 2>&1; then r2t "$@"; else python3 -m r2t "$@"; fi
}

cleanup() {
  if [ -n "$running" ]; then run_r2t stop "$running" || true; fi
  rm -rf "$work"
}
trap cleanup EXIT

for fx in vite-app next-app fastapi-app ambiguous-app; do
  echo
  echo "=== r2t inspect $fx"
  run_r2t inspect "$fixtures/$fx"
done

for fx in vite-app next-app fastapi-app ambiguous-app; do
  echo
  echo "=== r2t plan $fx"
  rc=0
  run_r2t plan "$fixtures/$fx" --output "$work/$fx" || rc=$?
  # ambiguous-app is expected to need input (exit 3); anything else non-zero is a failure.
  if [ "$fx" = "ambiguous-app" ]; then
    [ "$rc" -eq 3 ] || { echo "expected exit 3 for ambiguous-app, got $rc" >&2; exit 1; }
  else
    [ "$rc" -eq 0 ] || { echo "plan failed for $fx (exit $rc)" >&2; exit 1; }
  fi
done

echo
if docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "=== r2t run vite-app (builds an image and starts a container; needs network)"
  running="$work/vite-app"
  run_r2t run "$running" --yes --host-port 15173
  echo
  echo "=== curl http://127.0.0.1:15173/"
  curl -fsS -m 10 http://127.0.0.1:15173/ | head -n 5
  echo
  echo "=== r2t stop"
  run_r2t stop "$running"
  running=""
else
  echo "Docker (daemon and Compose v2) is not available here: skipping the run step."
fi

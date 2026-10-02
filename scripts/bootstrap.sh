#!/usr/bin/env bash
set -euo pipefail
: "${CI_BENCH_CACHE_ROOT:?CI_BENCH_CACHE_ROOT must be an absolute external cache path}"
: "${CI_BENCH_WORKLOAD:?CI_BENCH_WORKLOAD must be vue or hugo}"
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo "Only Linux x64 is supported" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  if [[ "$(id -u)" != 0 ]]; then
    echo "Missing python3 prerequisite (no sudo attempted)" >&2
    exit 1
  fi
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential git curl python3 ca-certificates
fi
exec python3 "$(dirname "$(realpath "$0")")/workload.py" bootstrap \
  --cache-root "$CI_BENCH_CACHE_ROOT" --workload "$CI_BENCH_WORKLOAD"

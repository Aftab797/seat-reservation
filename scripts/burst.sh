#!/bin/bash
set -e

BASE_URL=${1:-https://seat-reservation-production-ee5f.up.railway.app}
TOTAL_REQUESTS=${2:-20000}
CONCURRENCY=${3:-200}

python3 scripts/burst.py "$BASE_URL" "$TOTAL_REQUESTS" "$CONCURRENCY"


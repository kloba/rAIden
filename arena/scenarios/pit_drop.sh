#!/bin/bash
# ROUND 3 - THE PIT: a clean-up script runs against the wrong database and drops the ledger table.
set -euo pipefail
runuser -u arena -- psql -X -d ledger -v ON_ERROR_STOP=1 -c "DROP TABLE public.entries"
date -u +%FT%T.%3NZ > /var/lib/arena-private/pit_drop.at

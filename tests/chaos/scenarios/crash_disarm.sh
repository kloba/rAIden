#!/bin/bash
# Arena housekeeping (not a rAIden tool): remove the experiment file and restart. Used between dry runs only.
set -euo pipefail
rm -f /etc/postgresql/18/main/conf.d/50-experiment.conf /etc/postgresql/18/main/conf.d/99-raiden-*.conf
systemctl restart postgresql@18-main

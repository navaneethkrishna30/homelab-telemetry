#!/usr/bin/env bash
# Expose the stack to the tailnet only (never --funnel). Run on the machine hosting the stack.
# Leaves the existing :443 (funnel) and :8443 serves untouched.
set -euo pipefail
tailscale serve --bg --https=4318 http://127.0.0.1:4318   # OTLP ingest  (Alloy)
tailscale serve --bg --https=3443 http://127.0.0.1:3000   # Grafana
tailscale serve status

#!/bin/sh
# Seed the read-only config into a writable runtime dir, then run kea-dhcp4
# (which creates the control socket) and the Control Agent (the REST API) in the
# foreground. Loading from the writable copy lets a `config-write` cutover persist
# until the next restart, while the git-tracked seed under /etc/kea-seed stays
# pristine (a restart re-seeds from it -- a clean slate).
set -e

SEED_DIR="/etc/kea-seed"
RUNTIME="/run/kea-conf"
SOCKET="/run/kea/kea4-ctrl-socket"

mkdir -p "$RUNTIME" /run/kea /var/lib/kea
cp "$SEED_DIR"/*.conf "$RUNTIME"/

kea-dhcp4 -c "$RUNTIME/kea-dhcp4.conf" &

# Wait up to 10s for dhcp4 to create its command socket before the CA connects.
i=0
while [ ! -S "$SOCKET" ] && [ "$i" -lt 20 ]; do
  i=$((i + 1))
  sleep 0.5
done

exec kea-ctrl-agent -c "$RUNTIME/kea-ctrl-agent.conf"

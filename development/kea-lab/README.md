# 2-node Kea lab

A pair of ISC Kea DHCPv4 servers, each running `kea-dhcp4` + `kea-ctrl-agent`
(the REST management API), with **High Availability** configured between them.
A test bed for the Control Agent integration and the MS&rarr;Kea migration cutover.

```
node1  CA REST API -> http://127.0.0.1:8001   (HA primary, 172.30.0.11)
node2  CA REST API -> http://127.0.0.1:8002   (HA standby, 172.30.0.12)
```

## Use

```bash
make up          # build + start both nodes
make ps          # status
make config-get  NODE=8001   # full running Dhcp4 config via the CA
make status      NODE=8001   # status-get
make leases      NODE=8001   # lease4-get-all (lease_cmds hook)
make ha-status   NODE=8001   # HA heartbeat / state
make down        # stop + remove
```

A raw command against a Control Agent is just an HTTP POST:

```bash
curl -s -X POST http://127.0.0.1:8001/ -H "Content-Type: application/json" \
  -d '{"command":"config-get","service":["dhcp4"]}'
```

Useful commands: `config-get`, `config-set`, `config-write` (push + persist a new
config &mdash; the migration cutover), `lease4-get-all`, `status-get`,
`ha-heartbeat`, `list-commands`.

## Notes

- Built from Debian's Kea 2.2 packages; the open-source `lease_cmds` and `ha`
  hook libraries ship in the image.
- Kea 2.2's HA hook requires peer URLs to be **IP addresses**, not hostnames, so
  the nodes get static IPs (`172.30.0.11`/`.12`) on the `kea-lab-net` network.
- Each node seeds its config (read-only `node1/`, `node2/`) into a tmpfs runtime
  dir on start, so a `config-write` cutover persists until the next restart while
  the committed seed stays pristine. `make reseed` restarts to a clean slate.
- The nodes don't bind a DHCP interface (`interfaces: []`) &mdash; this lab is for
  exercising the management API and config, not serving real DHCP clients.

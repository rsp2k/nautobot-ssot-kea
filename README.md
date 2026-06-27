# Nautobot SSoT — ISC Kea DHCP

A [Nautobot](https://nautobot.com/) [SSoT](https://docs.nautobot.com/projects/ssot/en/latest/)
data source that syncs an **ISC Kea** DHCPv4 config into
[`nautobot-dhcp-models`](https://github.com/rsp2k/nautobot-app-dhcp-models). One-way:
the Kea config is the source being read *from*; dhcp-models is the SSoT target.

It's the second per-vendor adapter for the vendor-neutral dhcp-models store. It reuses the
**shared bitemporal target** (the Nautobot adapter and DiffSync base models from
`nautobot_dhcp_models.ssot`) exactly as the Microsoft DHCP adapter does, so a Kea config and
a Microsoft export can be diffed against the same store to drive an MS→Kea migration.

## How it works

1. An operator grabs the running `kea-dhcp4.conf` from the Kea server.
2. They run the **Kea → Nautobot** SSoT job in Nautobot, upload the config, and give the Kea
   instance a logical **server name** (the config carries no hostname, so it's supplied here).
3. DiffSync compares the config against what Nautobot currently believes for that server and
   creates/updates **subnets (scopes), pools, reservations, and options**.

The job is **additive-only by default** — it never deletes Nautobot records unless you opt in.

## Scope (v1: config-only)

- **Subnets** (`subnet4`) → scopes, keyed by their CIDR `subnet` (no mask conversion needed).
- **Pools** → both `"start - end"` ranges and CIDR pools are expanded to a start/end pair.
- **Reservations** → `hw-address` becomes the MAC; if absent, `client-id` / `duid` /
  `circuit-id` / `flex-id` is used as the identifier.
- **Options** at the global (server), subnet (scope), and reservation levels. Kea's
  comma+space `data` strings are normalized to comma-no-space so an MS-vs-Kea diff is clean.

**Leases are out of scope in v1.** Kea leases live in the lease database (memfile / `lease_cmds`
/ SQL backend), not in `kea-dhcp4.conf`, so they can't be read from the config. Syncing them
would need a separate memfile / `lease4-get-all` dump and is future work. Kea config also has
no explicit exclusion concept, so no exclusions are emitted.

## Honoring the bitemporal contracts

The target writes through the dhcp-models contracts (the same shared adapter the MS source
uses): new objects are created as the first belief and drift is applied via `.amend()`, so the
belief log rotates instead of overwriting and every re-sync stays queryable as history. IPAM is
materialized first: subnet prefixes and reservation IPs are `get_or_create`-d before the DHCP
record links to them. Requires PostgreSQL for the bitemporal belief log (see dhcp-models).

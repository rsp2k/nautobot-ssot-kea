"""Kea-specific value helpers for the Kea DHCP source adapter.

Vendor-neutral helpers (canonical_dt, normalize_mac) are re-exported from the
shared ``nautobot_dhcp_models.ssot.helpers`` so the source adapter has one
import surface.
"""

from __future__ import annotations

import csv
import datetime
import io
import ipaddress

from nautobot_dhcp_models.ssot.helpers import (  # noqa: F401 -- re-exported for the adapter
    canonical_dt,
    normalize_mac,
)

# Kea lease state code -> DHCPLeaseStateChoices value.
# 0 = default (active), 1 = declined, 2 = expired-reclaimed.
KEA_LEASE_STATE_MAP = {0: "active", 1: "declined", 2: "expired"}


def parse_kea_pool(pool_str: str) -> tuple[str, str]:
    """Parse a Kea pool definition into (start_address, end_address).

    Kea expresses a pool either as an explicit range or as a CIDR prefix:

    >>> parse_kea_pool("10.0.10.10 - 10.0.10.250")
    ('10.0.10.10', '10.0.10.250')
    >>> parse_kea_pool("10.0.30.0/24")
    ('10.0.30.0', '10.0.30.255')

    For the CIDR form the first and last addresses of the network are used
    (network and broadcast for IPv4), matching Kea's own pool expansion.
    """
    text = (pool_str or "").strip()
    if "/" in text:
        net = ipaddress.ip_network(text, strict=False)
        return str(net[0]), str(net[-1])
    if "-" in text:
        start, end = text.split("-", 1)
        return start.strip(), end.strip()
    raise ValueError(f"Unrecognized Kea pool definition: {pool_str!r}")


def normalize_option_data(data) -> str:
    """Normalize a Kea option ``data`` string to comma-separated, no spaces.

    Kea writes multi-value option data as a comma+space separated string
    (``"10.0.0.10, 10.0.0.11"``). Joining without spaces matches how the MS
    side joins list values, so a future MS-vs-Kea diff stays clean.

    >>> normalize_option_data("10.0.0.10, 10.0.0.11")
    '10.0.0.10,10.0.0.11'
    """
    if data is None:
        return ""
    if isinstance(data, (list, tuple)):
        parts = [str(v).strip() for v in data]
    else:
        parts = [p.strip() for p in str(data).split(",")]
    return ",".join(p for p in parts if p)


def kea_lease_state(code) -> str:
    """Map a Kea numeric lease state to a DHCPLeaseStateChoices value."""
    try:
        return KEA_LEASE_STATE_MAP.get(int(code), "active")
    except (TypeError, ValueError):
        return "active"


def kea_expire_to_iso(expire) -> str:
    """Convert a Kea memfile ``expire`` (absolute UNIX timestamp) to canonical ISO.

    >>> kea_expire_to_iso(1782000000)
    '2026-06-21T00:00:00+00:00'
    """
    try:
        ts = int(expire)
    except (TypeError, ValueError):
        return ""
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return canonical_dt(dt)


def parse_kea_leases_csv(text: str) -> list[dict]:
    """Parse a Kea memfile lease CSV (``kea-leases4.csv`` / ``kea-admin lease-dump``).

    Columns: ``address,hwaddr,client_id,valid_lifetime,expire,subnet_id,fqdn_fwd,
    fqdn_rev,hostname,state,user_context,pool_id``.

    Memfile is append-only with periodic cleanup, so an address can appear
    multiple times; the LAST row wins (Kea's own load semantics). Rows with
    ``valid_lifetime == 0`` are delete markers (released/expired-away) and are
    dropped. Returns one dict per *current* lease, keyed by the raw column names
    plus an int ``subnet_id``.
    """
    current: dict[str, dict] = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        address = (row.get("address") or "").strip()
        if not address:
            continue
        try:
            valid_lifetime = int(row.get("valid_lifetime") or 0)
        except ValueError:
            valid_lifetime = 0
        if valid_lifetime == 0:
            current.pop(address, None)  # delete marker -- forget any prior row
            continue
        try:
            subnet_id = int(row.get("subnet_id") or 0)
        except ValueError:
            subnet_id = 0
        current[address] = {
            "address": address,
            "hwaddr": (row.get("hwaddr") or "").strip(),
            "client_id": (row.get("client_id") or "").strip(),
            "expire": (row.get("expire") or "").strip(),
            "subnet_id": subnet_id,
            "hostname": (row.get("hostname") or "").strip(),
            "state": (row.get("state") or "0").strip(),
        }
    return list(current.values())

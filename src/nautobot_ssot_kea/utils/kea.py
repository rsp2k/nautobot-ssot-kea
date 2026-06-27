"""Kea-specific value helpers for the Kea DHCP source adapter.

Vendor-neutral helpers (canonical_dt, normalize_mac) are re-exported from the
shared ``nautobot_dhcp_models.ssot.helpers`` so the source adapter has one
import surface.
"""

from __future__ import annotations

import ipaddress

from nautobot_dhcp_models.ssot.helpers import (  # noqa: F401 -- re-exported for the adapter
    canonical_dt,
    normalize_mac,
)


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

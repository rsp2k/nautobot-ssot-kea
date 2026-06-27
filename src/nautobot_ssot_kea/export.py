"""Generate a kea-dhcp4.conf from a DHCPServer's config in nautobot-dhcp-models.

The reverse of the Kea source adapter. Because the dhcp-models store is
vendor-neutral, exporting a server that was synced *from Microsoft* yields the
equivalent **Kea** config -- the store is the migration pivot.

The only non-trivial transform is exclusions: Kea has no exclusion primitive, so
a migrated MS scope (one range + carved-out exclusions) becomes Kea **pool gaps**
via ``pools_minus_exclusions``. dhcp-models stores no Kea numeric subnet id, so
ids are synthesized deterministically (1..N, ordered by network).
"""

from __future__ import annotations

import ipaddress


def _ip_int(addr: str) -> int:
    return int(ipaddress.ip_address(addr))


def _int_ip(value: int) -> str:
    return str(ipaddress.ip_address(value))


def pools_minus_exclusions(pools: list[tuple[str, str]], exclusions: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Subtract exclusion ranges from pool ranges, returning the remaining sub-ranges.

    Both inputs are lists of ``(start, end)`` inclusive IP-string ranges. Kea has
    no exclusion concept, so the result expresses the holes as gaps between pools.

    >>> pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.50", "10.0.0.60")])
    [('10.0.0.10', '10.0.0.49'), ('10.0.0.61', '10.0.0.250')]
    """
    excl = sorted((_ip_int(s), _ip_int(e)) for s, e in exclusions)
    out: list[tuple[str, str]] = []
    for ps, pe in pools:
        cur, end = _ip_int(ps), _ip_int(pe)
        for es, ee in excl:
            if ee < cur or es > end:  # no overlap with the remaining pool
                continue
            if es > cur:  # gap before this exclusion
                out.append((_int_ip(cur), _int_ip(min(es - 1, end))))
            cur = max(cur, ee + 1)
            if cur > end:
                break
        if cur <= end:
            out.append((_int_ip(cur), _int_ip(end)))
    return out


# dhcp-models identifier_type -> Kea reservation identifier key.
_IDENTIFIER_KEY = {
    "hw-address": "hw-address",
    "client-id": "client-id",
    "circuit-id": "circuit-id",
    "flex-id": "flex-id",
    "duid": "duid",
}


def _options_for(option_qs) -> list[dict]:
    """Render a DHCPOption queryset as Kea ``option-data`` entries."""
    data = []
    for opt in option_qs.select_related("option_definition").order_by("option_definition__code"):
        entry = {"code": opt.option_definition.code, "data": opt.value}
        if opt.option_definition.name:
            entry["name"] = opt.option_definition.name
        data.append(entry)
    return data


def build_kea_config(server) -> dict:
    """Build a ``{"Dhcp4": {...}}`` config dict from one DHCPServer's stored config."""
    from nautobot_dhcp_models.models import (
        DHCPExclusion,
        DHCPOption,
        DHCPPool,
        DHCPReservation,
        DHCPScope,
    )

    scopes = list(
        DHCPScope.objects.filter(server=server)
        .select_related("prefix")
        .order_by("prefix__network", "prefix__prefix_length")
    )
    subnet_id = {scope.pk: idx for idx, scope in enumerate(scopes, start=1)}

    subnet4 = []
    for scope in scopes:
        pools = [(str(p.start_address), str(p.end_address)) for p in DHCPPool.objects.filter(scope=scope)]
        exclusions = [(str(e.start_address), str(e.end_address)) for e in DHCPExclusion.objects.filter(scope=scope)]
        gapped = pools_minus_exclusions(pools, exclusions) if exclusions else pools

        subnet: dict = {
            "id": subnet_id[scope.pk],
            "subnet": str(scope.prefix.prefix),
            "pools": [{"pool": f"{s} - {e}"} for s, e in gapped],
        }
        if scope.default_lease_time:
            subnet["valid-lifetime"] = scope.default_lease_time
        if scope.renew_timer:
            subnet["renew-timer"] = scope.renew_timer
        if scope.rebind_timer:
            subnet["rebind-timer"] = scope.rebind_timer
        comment = scope.name or scope.description
        if comment:
            subnet["comment"] = comment

        scope_opts = _options_for(DHCPOption.objects.filter(scope=scope))
        if scope_opts:
            subnet["option-data"] = scope_opts

        reservations = []
        for res in DHCPReservation.objects.filter(scope=scope).select_related("ip_address"):
            entry: dict = {"ip-address": str(res.ip_address.host)}
            identifier = res.mac_address or res.client_id
            if identifier:
                entry[_IDENTIFIER_KEY.get(res.identifier_type, "hw-address")] = identifier
            if res.hostname:
                entry["hostname"] = res.hostname
            res_opts = _options_for(DHCPOption.objects.filter(reservation=res))
            if res_opts:
                entry["option-data"] = res_opts
            reservations.append(entry)
        if reservations:
            subnet["reservations"] = reservations

        subnet4.append(subnet)

    dhcp4: dict = {"subnet4": subnet4}
    server_opts = _options_for(DHCPOption.objects.filter(server=server))
    if server_opts:
        dhcp4["option-data"] = server_opts

    return {"Dhcp4": dhcp4}

"""Generate a kea-dhcp4.conf / kea-dhcp6.conf from a DHCPServer's config in dhcp-models.

The reverse of the Kea source adapter. Because the dhcp-models store is
vendor-neutral, exporting a server that was synced *from Microsoft* yields the
equivalent **Kea** config -- the store is the migration pivot.

Family is auto-detected from the server's scopes (a Kea daemon is single-family
by convention; see the DHCPServer note in the dhcp-models roadmap). The v4 path's
only non-trivial transform is exclusions: Kea has no exclusion primitive, so a
migrated MS scope (one range + carved-out exclusions) becomes Kea **pool gaps**
via ``pools_minus_exclusions``. The v6 path's counterpart is reservations: the
store keeps delegated prefixes and addresses as flat one-row-per-binding records,
which are regrouped by DUID at emit time into Kea's per-client ``reservations[]``
entries (``ip-addresses[]`` + ``prefixes[]``). dhcp-models stores no Kea numeric
subnet id, so ids are synthesized deterministically (1..N, ordered by network).
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


def _apply_context(element: dict, obj) -> None:
    """Re-emit an object's user_context + extra passthrough onto a Kea element.

    ``user_context`` becomes ``user-context``; ``extra`` keys fill gaps only and
    never override a field already set from a modeled column. This is what makes a
    Kea config round-trip lossless -- the global daemon config, unmodeled subnet
    keys, etc. that the source stashed in ``extra`` come back out here.
    """
    if getattr(obj, "user_context", None):
        element["user-context"] = obj.user_context
    for key, value in (getattr(obj, "extra", None) or {}).items():
        element.setdefault(key, value)


def _options_for(option_qs) -> list[dict]:
    """Render a DHCPOption queryset as Kea ``option-data`` entries."""
    data = []
    for opt in option_qs.select_related("option_definition").order_by("option_definition__code"):
        entry = {"code": opt.option_definition.code, "data": opt.value}
        if opt.option_definition.name:
            entry["name"] = opt.option_definition.name
        _apply_context(entry, opt)
        data.append(entry)
    return data


def _scope_timers(scope, subnet: dict) -> None:
    """Apply the lease/renew/rebind timers + comment common to v4 and v6 subnets."""
    if scope.min_lease_time is not None:
        subnet["min-valid-lifetime"] = scope.min_lease_time
    if scope.default_lease_time:
        subnet["valid-lifetime"] = scope.default_lease_time
    if scope.max_lease_time is not None:
        subnet["max-valid-lifetime"] = scope.max_lease_time
    if scope.renew_timer:
        subnet["renew-timer"] = scope.renew_timer
    if scope.rebind_timer:
        subnet["rebind-timer"] = scope.rebind_timer
    comment = scope.name or scope.description
    if comment:
        subnet["comment"] = comment


def build_kea_config(server, family: int | None = None) -> dict:
    """Build a ``{"Dhcp4": {...}}`` or ``{"Dhcp6": {...}}`` config from one DHCPServer.

    ``family`` (4 or 6) forces the output family; when omitted it is detected from
    the server's scopes. A server holding both families requires an explicit
    ``family`` (a single Kea daemon serves one family).
    """
    from nautobot_dhcp_models.models import DHCPScope

    scopes = list(
        DHCPScope.objects.filter(server=server)
        .select_related("prefix")
        .order_by("prefix__network", "prefix__prefix_length")
    )
    if family is None:
        families = {s.family for s in scopes}
        if len(families) > 1:
            raise ValueError(
                f"DHCPServer {server.name!r} has scopes in multiple families {sorted(families)}; "
                "pass family=4 or family=6 to export a single Kea daemon config."
            )
        family = families.pop() if families else 4

    scopes = [s for s in scopes if s.family == family]
    subnet_id = {scope.pk: idx for idx, scope in enumerate(scopes, start=1)}
    if family == 6:
        return _build_dhcp6(server, scopes, subnet_id)
    return _build_dhcp4(server, scopes, subnet_id)


def _build_dhcp4(server, scopes, subnet_id) -> dict:
    from nautobot_dhcp_models.models import DHCPExclusion, DHCPOption, DHCPPool, DHCPReservation

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
        _scope_timers(scope, subnet)

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
            _apply_context(entry, res)
            reservations.append(entry)
        if reservations:
            subnet["reservations"] = reservations

        _apply_context(subnet, scope)
        subnet4.append(subnet)

    dhcp4: dict = {"subnet4": subnet4}
    server_opts = _options_for(DHCPOption.objects.filter(server=server))
    if server_opts:
        dhcp4["option-data"] = server_opts

    _apply_context(dhcp4, server)
    return {"Dhcp4": dhcp4}


def _build_dhcp6(server, scopes, subnet_id) -> dict:
    from nautobot_dhcp_models.models import (
        DHCPDelegatedPrefixReservation,
        DHCPOption,
        DHCPPool,
        DHCPPrefixDelegationPool,
        DHCPReservation,
    )

    subnet6 = []
    for scope in scopes:
        subnet: dict = {
            "id": subnet_id[scope.pk],
            "subnet": str(scope.prefix.prefix),
            "pools": [
                {"pool": f"{p.start_address} - {p.end_address}"} for p in DHCPPool.objects.filter(scope=scope)
            ],
        }
        _scope_timers(scope, subnet)
        if scope.min_preferred_lifetime is not None:
            subnet["min-preferred-lifetime"] = scope.min_preferred_lifetime
        if scope.preferred_lifetime:
            subnet["preferred-lifetime"] = scope.preferred_lifetime
        if scope.max_preferred_lifetime is not None:
            subnet["max-preferred-lifetime"] = scope.max_preferred_lifetime
        if scope.rapid_commit is not None:
            subnet["rapid-commit"] = scope.rapid_commit
        if scope.allocator:
            subnet["allocator"] = scope.allocator
        if scope.pd_allocator:
            subnet["pd-allocator"] = scope.pd_allocator
        if scope.relay_addresses:
            subnet["relay"] = {"ip-addresses": list(scope.relay_addresses)}
        if scope.interface:
            subnet["interface"] = scope.interface
        if scope.interface_id:
            subnet["interface-id"] = scope.interface_id
        if scope.reservations_in_subnet is not None:
            subnet["reservations-in-subnet"] = scope.reservations_in_subnet
        if scope.reservations_out_of_pool is not None:
            subnet["reservations-out-of-pool"] = scope.reservations_out_of_pool

        pd_pools = []
        for pdp in DHCPPrefixDelegationPool.objects.filter(scope=scope):
            entry: dict = {
                "prefix": str(pdp.prefix),
                "prefix-len": pdp.prefix_length,
                "delegated-len": pdp.delegated_length,
            }
            if pdp.excluded_prefix:
                entry["excluded-prefix"] = str(pdp.excluded_prefix)
                if pdp.excluded_prefix_length is not None:
                    entry["excluded-prefix-len"] = pdp.excluded_prefix_length
            pd_opts = _options_for(DHCPOption.objects.filter(pd_pool=pdp))
            if pd_opts:
                entry["option-data"] = pd_opts
            _apply_context(entry, pdp)
            pd_pools.append(entry)
        if pd_pools:
            subnet["pd-pools"] = pd_pools

        scope_opts = _options_for(DHCPOption.objects.filter(scope=scope))
        if scope_opts:
            subnet["option-data"] = scope_opts

        reservations = _v6_reservations(scope, DHCPReservation, DHCPDelegatedPrefixReservation, DHCPOption)
        if reservations:
            subnet["reservations"] = reservations

        _apply_context(subnet, scope)
        subnet6.append(subnet)

    dhcp6: dict = {"subnet6": subnet6}
    server_opts = _options_for(DHCPOption.objects.filter(server=server))
    if server_opts:
        dhcp6["option-data"] = server_opts

    _apply_context(dhcp6, server)
    return {"Dhcp6": dhcp6}


def _v6_reservations(scope, DHCPReservation, DHCPDelegatedPrefixReservation, DHCPOption) -> list[dict]:
    """Regroup flat address + delegated-prefix reservations by client identity.

    The store keeps one row per address and one per delegated prefix (each
    carrying the client's DUID); Kea wants one ``reservations[]`` entry per
    client with ``ip-addresses[]`` and ``prefixes[]``. Group by (identifier_type,
    duid-or-mac), preserving a stable order so the export round-trips cleanly.
    """
    groups: dict[tuple[str, str], dict] = {}

    def _group(row):
        key = (row.identifier_type, row.duid or row.mac_address)
        g = groups.get(key)
        if g is None:
            g = {"identifier_type": row.identifier_type, "duid": row.duid, "mac": row.mac_address,
                 "ip-addresses": [], "prefixes": [], "hostname": "", "option-data": [],
                 "user_context": {}, "extra": {}}
            groups[key] = g
        if row.hostname:
            g["hostname"] = row.hostname
        # All rows of one Kea reservation share its context; take the first seen.
        if row.user_context and not g["user_context"]:
            g["user_context"] = row.user_context
        if row.extra and not g["extra"]:
            g["extra"] = row.extra
        return g

    for res in DHCPReservation.objects.filter(scope=scope).select_related("ip_address").order_by("ip_address__host"):
        g = _group(res)
        g["ip-addresses"].append(str(res.ip_address.host))
        g["option-data"].extend(_options_for(DHCPOption.objects.filter(reservation=res)))

    for dpr in DHCPDelegatedPrefixReservation.objects.filter(scope=scope).order_by("delegated_prefix"):
        g = _group(dpr)
        g["prefixes"].append(f"{dpr.delegated_prefix}/{dpr.delegated_prefix_length}")

    entries = []
    for g in groups.values():
        entry: dict = {}
        identifier = g["duid"] or g["mac"]
        if identifier:
            entry[_IDENTIFIER_KEY.get(g["identifier_type"], "duid")] = identifier
        if g["ip-addresses"]:
            entry["ip-addresses"] = g["ip-addresses"]
        if g["prefixes"]:
            entry["prefixes"] = g["prefixes"]
        if g["hostname"]:
            entry["hostname"] = g["hostname"]
        if g["option-data"]:
            entry["option-data"] = g["option-data"]
        if g["user_context"]:
            entry["user-context"] = g["user_context"]
        for key, value in g["extra"].items():
            entry.setdefault(key, value)
        entries.append(entry)
    return entries

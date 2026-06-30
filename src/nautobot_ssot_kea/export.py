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

import copy
import ipaddress

# Default Kea hooks dir on Debian/Ubuntu amd64 (the lab image). Overridable per cutover.
DEFAULT_HOOKS_DIR = "/usr/lib/x86_64-linux-gnu/kea/hooks"
# Daemon plumbing the store doesn't model, preserved from a node's live config on cutover.
_PRESERVED_KEYS = ("control-socket", "interfaces-config", "lease-database", "multi-threading")


def build_cutover_config(generated: dict, current: dict, *, this_server_name, peers, mode, hooks_dir=DEFAULT_HOOKS_DIR):
    """Build the config to push to one Kea node during a migration cutover.

    Takes the store-generated config (``build_kea_config`` output) and merges it onto
    the node's *live* config so node plumbing the store doesn't model -- the control
    socket (which keeps the Control Agent reachable!), interfaces, lease database --
    is preserved. Then it (re)installs the lease_cmds + ha hooks so the pair is
    redundant, with this node's ``this-server-name``.

    ``generated``/``current`` are wrapped configs (``{"Dhcp4": {...}}``); ``peers`` is
    the shared HA peer list (``[{"name","url","role"}, ...]``).
    """
    key = "Dhcp6" if "Dhcp6" in generated else "Dhcp4"
    out = copy.deepcopy(generated)
    root = out[key]
    live = current.get(key, {})

    # Preserve the node's plumbing; ensure interfaces-config exists either way.
    for preserved in _PRESERVED_KEYS:
        if preserved in live:
            root[preserved] = live[preserved]
    root.setdefault("interfaces-config", {"interfaces": []})

    # Reinstall hooks: keep any non-HA/non-lease_cmds hooks the node already had,
    # then add lease_cmds (HA depends on it) and the HA hook for this server.
    kept = [
        h
        for h in root.get("hooks-libraries", [])
        if "libdhcp_ha" not in h.get("library", "") and "libdhcp_lease_cmds" not in h.get("library", "")
    ]
    kept.append({"library": f"{hooks_dir}/libdhcp_lease_cmds.so"})
    kept.append(
        {
            "library": f"{hooks_dir}/libdhcp_ha.so",
            "parameters": {"high-availability": [{"this-server-name": this_server_name, "mode": mode, "peers": peers}]},
        }
    )
    root["hooks-libraries"] = kept
    return out


def strip_option_code(config: dict, code: int) -> dict:
    """Return a copy of a wrapped Kea config with every option-data entry of ``code`` removed.

    Migrated source data can carry an option Kea rejects (e.g. a client-only option,
    or one whose value doesn't fit Kea's definition). Since config-set is atomic, the
    cutover drops the offending code everywhere it appears -- global, subnets, pools,
    pd-pools, reservations, shared-networks -- and retries. A generic recursive walk
    handles every nesting level without enumerating them.
    """
    out = copy.deepcopy(config)

    def walk(node):
        if isinstance(node, dict):
            od = node.get("option-data")
            if isinstance(od, list):
                node["option-data"] = [o for o in od if o.get("code") != code]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(out)
    return out


def find_option_codes_by_data(config: dict, data_value: str) -> set:
    """Return every option-data ``code`` anywhere in the config whose ``data`` equals ``data_value``.

    Kea's "not a valid string of hexadecimal digits: <data>" error names the offending
    value but not its code; this locates which option(s) carry it so the cutover can
    repair them.
    """
    codes = set()

    def walk(node):
        if isinstance(node, dict):
            for o in node.get("option-data", []) or []:
                if o.get("data") == data_value and o.get("code") is not None:
                    codes.add(o["code"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(config)
    return codes


def _infer_option_type(data) -> tuple[str, bool]:
    """Infer a Kea option ``(type, array)`` from its comma-joined data value.

    A comma-list of IPv4 addresses -> ``("ipv4-address", array)``; a single IPv4 ->
    ``("ipv4-address", False)``; anything else -> ``("string", False)``. Used to define
    non-standard option codes (e.g. Cisco's 150) whose vendor metadata mislabels them as
    ``string`` even though the live data is an address list.
    """
    parts = [p.strip() for p in str(data).split(",") if p.strip()]
    if parts:
        try:
            for p in parts:
                ipaddress.IPv4Address(p)
            return "ipv4-address", len(parts) > 1
        except ipaddress.AddressValueError:
            pass
    return "string", False


def add_option_def(config: dict, code: int, data_sample, *, space: str = "dhcp4") -> bool:
    """Inject an ``option-def`` for ``code`` (type inferred from ``data_sample``) into the config.

    Kea only parses an option's data when it has a definition for that code. Standard
    codes are built in; non-standard ones (Cisco 150, site-locals) are not, so Kea falls
    back to expecting hex and rejects an IP-list value. Defining the option teaches Kea
    the real shape so the migrated value applies verbatim. Returns False if a definition
    for ``(code, space)`` already exists.
    """
    dhcp = config["Dhcp4"] if "Dhcp4" in config else config.get("Dhcp6", config)
    defs = dhcp.setdefault("option-def", [])
    if any(d.get("code") == code and d.get("space", space) == space for d in defs):
        return False
    dtype, array = _infer_option_type(data_sample)
    defs.append({"name": f"option-{code}", "code": code, "space": space, "type": dtype, "array": array})
    return True


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

_HEX_DIGITS = set("0123456789abcdefABCDEF")

# Kea's hex host-identifier parser accepts colon-separated, contiguous, or 0x-prefixed
# hex -- but NOT dash separators. Microsoft writes every MAC/client-id dash-separated
# ("00-01-00-01-..."), so those keys need their separators normalized to colons on emit.
_HEX_IDENTIFIER_KEYS = {"hw-address", "client-id", "circuit-id", "duid"}


def _normalize_kea_hex(value: str) -> str:
    """Rewrite a dash-separated hex identifier to colon-separated (Kea's accepted form).

    A Microsoft ``00-01-00-01-...`` value makes Kea reject the whole config ("invalid
    host identifier value"); ``00:01:00:01:...`` is accepted. Colon-separated, contiguous,
    and 0x-prefixed values have no dashes and pass through unchanged.
    """
    return value.replace("-", ":") if value else value


def _octet_count(value: str) -> int:
    """Count hex octets in a MAC/identifier value, ignoring ``:`` ``-`` ``.`` separators."""
    return len([c for c in (value or "") if c in _HEX_DIGITS]) // 2


def _kea_reservation_identifier(res):
    """Pick the (Kea host-identifier key, value) pair for a reservation, or None.

    A Kea ``hw-address`` must be a 6-octet MAC. Microsoft reservations imported
    with identifier_type ``hw-address`` sometimes carry an RFC 4361 / DUID-style
    client id (e.g. ``00-01-00-01-...`` -- 18 octets) in ``client_id``; emitting
    that as ``hw-address`` makes Kea reject the entire config ("invalid host
    identifier value"). When the value isn't a 6-octet MAC we fall back to
    ``client-id``, Kea's generic variable-length host identifier.
    """
    value = res.mac_address or res.client_id
    if not value:
        return None
    key = _IDENTIFIER_KEY.get(res.identifier_type, "hw-address")
    if key == "hw-address" and _octet_count(value) != 6:
        key = "client-id"
    if key in _HEX_IDENTIFIER_KEYS:
        value = _normalize_kea_hex(value)
    return key, value


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


def _emit_require_classes(element: dict, obj) -> None:
    """Emit ``require-client-classes`` from a scope/pool/pd-pool's stored list.

    We write the classic ``require-client-classes`` key (supported across all Kea
    versions we target); the source reads either that or the newer
    ``evaluate-additional-classes`` alias, so the value round-trips regardless of
    which spelling the original config used.
    """
    classes = list(getattr(obj, "require_client_classes", None) or [])
    if classes:
        element["require-client-classes"] = classes


def _emit_ddns(scope, subnet: dict) -> None:
    """Emit a scope's DNS dynamic-update settings onto its Kea subnet element.

    Each key is omitted when the field is at its neutral default (None for the
    bools/float, "" for the strings) so the output stays minimal and a scope with
    no DDNS config produces a bare subnet.
    """
    if scope.ddns_send_updates is not None:
        subnet["ddns-send-updates"] = scope.ddns_send_updates
    if scope.ddns_override_client_update is not None:
        subnet["ddns-override-client-update"] = scope.ddns_override_client_update
    if scope.ddns_override_no_update is not None:
        subnet["ddns-override-no-update"] = scope.ddns_override_no_update
    if scope.ddns_qualifying_suffix:
        subnet["ddns-qualifying-suffix"] = scope.ddns_qualifying_suffix
    if scope.ddns_generated_prefix:
        subnet["ddns-generated-prefix"] = scope.ddns_generated_prefix
    if scope.ddns_replace_client_name:
        subnet["ddns-replace-client-name"] = scope.ddns_replace_client_name
    if scope.ddns_conflict_resolution_mode:
        subnet["ddns-conflict-resolution-mode"] = scope.ddns_conflict_resolution_mode
    if scope.ddns_update_on_renew is not None:
        subnet["ddns-update-on-renew"] = scope.ddns_update_on_renew
    if scope.ddns_ttl_percent is not None:
        subnet["ddns-ttl-percent"] = scope.ddns_ttl_percent
    if scope.hostname_char_set:
        subnet["hostname-char-set"] = scope.hostname_char_set
    if scope.hostname_char_replacement:
        subnet["hostname-char-replacement"] = scope.hostname_char_replacement


def _emit_server_daemon(server, root: dict) -> None:
    """Emit the promoted daemon-level settings onto the Dhcp4/Dhcp6 root.

    Rebuilds the nested ``dhcp-ddns`` / ``interfaces-config`` objects by merging the
    first-class fields over the un-promoted remainder kept in ``server.extra`` (see
    the source's ``_keep_remainder``). Must run before ``_apply_context`` so the
    merged blocks win over the extra remainder's setdefault.
    """
    extra = getattr(server, "extra", None) or {}

    d2 = dict(extra.get("dhcp-ddns") or {})
    if server.ddns_enabled is not None:
        d2["enable-updates"] = server.ddns_enabled
    if server.ddns_server_ip:
        d2["server-ip"] = server.ddns_server_ip
    if server.ddns_server_port is not None:
        d2["server-port"] = server.ddns_server_port
    if d2:
        root["dhcp-ddns"] = d2

    ifc = dict(extra.get("interfaces-config") or {})
    if server.listen_interfaces:
        ifc["interfaces"] = list(server.listen_interfaces)
    if ifc:
        root["interfaces-config"] = ifc

    if server.host_identifier_priority:
        root["host-reservation-identifiers"] = list(server.host_identifier_priority)


def _emit_subnet_selection(scope, subnet: dict) -> None:
    """Emit the selection/allocation/reservation-mode keys common to v4 and v6 subnets.

    relay/interface/allocator and the reservation-mode flags are valid for both
    families; emitting them only for v6 would silently drop a v4 subnet's relay
    config on export (the inverse of the source-side load fix).
    """
    if scope.allocator:
        subnet["allocator"] = scope.allocator
    if scope.relay_addresses:
        subnet["relay"] = {"ip-addresses": list(scope.relay_addresses)}
    if scope.interface:
        subnet["interface"] = scope.interface
    if scope.reservations_in_subnet is not None:
        subnet["reservations-in-subnet"] = scope.reservations_in_subnet
    if scope.reservations_out_of_pool is not None:
        subnet["reservations-out-of-pool"] = scope.reservations_out_of_pool


def _options_for(option_qs) -> list[dict]:
    """Render a DHCPOption queryset as Kea ``option-data`` entries.

    Emit by ``code`` (Kea resolves standard options from it) and only include
    ``name`` when it's a valid Kea identifier. A name with whitespace is a vendor
    *friendly* name (e.g. Microsoft's "DNS Servers") that Kea's config parser
    rejects -- "invalid option name ..., space character is not allowed" -- so it is
    dropped in favor of the code.
    """
    data = []
    for opt in option_qs.select_related("option_definition").order_by("option_definition__code"):
        code = opt.option_definition.code
        entry = {"code": code, "data": opt.value}
        name = opt.option_definition.name
        # Emit a name only when it's a Kea-native identifier (lowercase, hyphenated:
        # "routers", "domain-name-servers"). A vendor *friendly* name -- Microsoft's
        # "Router"/"DNS Servers" -- or our synthetic "option-<code>" placeholder won't
        # match Kea's definition for that code and makes config-set reject it; for
        # those we emit code only and let Kea resolve the standard option.
        if name and name != f"option-{code}" and name.islower() and not any(c.isspace() for c in name):
            entry["name"] = name
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


def _partition_by_shared_network(scopes):
    """Split scopes into standalone + per-shared-network groups, preserving order.

    Returns ``(standalone, groups)`` where standalone is the scopes with no
    shared-network and groups is a list of ``(shared_network_obj, [member_scopes])``
    in first-seen order. Grouping by the FK object (not name) keeps it correct even
    if two servers reused a name.
    """
    standalone = []
    groups: dict = {}  # sn.pk -> (sn, [scopes]); dict preserves insertion order
    for scope in scopes:
        sn = scope.shared_network
        if sn is None:
            standalone.append(scope)
        else:
            groups.setdefault(sn.pk, (sn, []))[1].append(scope)
    return standalone, list(groups.values())


def _emit_sharednet_fields(sn, element: dict) -> None:
    """Emit a shared-network's operational keys onto its ``shared-networks[]`` entry."""
    if sn.min_lease_time is not None:
        element["min-valid-lifetime"] = sn.min_lease_time
    if sn.default_lease_time is not None:
        element["valid-lifetime"] = sn.default_lease_time
    if sn.max_lease_time is not None:
        element["max-valid-lifetime"] = sn.max_lease_time
    if sn.renew_timer is not None:
        element["renew-timer"] = sn.renew_timer
    if sn.rebind_timer is not None:
        element["rebind-timer"] = sn.rebind_timer
    if sn.min_preferred_lifetime is not None:
        element["min-preferred-lifetime"] = sn.min_preferred_lifetime
    if sn.preferred_lifetime is not None:
        element["preferred-lifetime"] = sn.preferred_lifetime
    if sn.max_preferred_lifetime is not None:
        element["max-preferred-lifetime"] = sn.max_preferred_lifetime
    if sn.match_client_id is not None:
        element["match-client-id"] = sn.match_client_id
    if sn.authoritative is not None:
        element["authoritative"] = sn.authoritative
    if sn.rapid_commit is not None:
        element["rapid-commit"] = sn.rapid_commit
    if sn.relay_addresses:
        element["relay"] = {"ip-addresses": list(sn.relay_addresses)}
    if sn.interface:
        element["interface"] = sn.interface
    if sn.interface_id:
        element["interface-id"] = sn.interface_id
    if sn.allocator:
        element["allocator"] = sn.allocator
    if sn.pd_allocator:
        element["pd-allocator"] = sn.pd_allocator
    if sn.reservations_in_subnet is not None:
        element["reservations-in-subnet"] = sn.reservations_in_subnet
    if sn.reservations_out_of_pool is not None:
        element["reservations-out-of-pool"] = sn.reservations_out_of_pool
    _emit_require_classes(element, sn)
    if sn.description:
        element["comment"] = sn.description
    _apply_context(element, sn)  # user-context + extra (carries shared-network option-data)


def _subnet4_dict(scope, sid) -> dict:
    from nautobot_dhcp_models.models import DHCPExclusion, DHCPOption, DHCPPool, DHCPReservation

    pool_objs = list(DHCPPool.objects.filter(scope=scope))
    exclusions = [(str(e.start_address), str(e.end_address)) for e in DHCPExclusion.objects.filter(scope=scope)]
    if exclusions:
        # Kea has no exclusion primitive, so express the holes as pool gaps. This
        # synthesizes (start, end) ranges and loses the source pool object -- so
        # per-pool attributes (require-client-classes) cannot be carried in this
        # case. Only scopes that actually carry exclusions pay this cost.
        ranges = [(str(p.start_address), str(p.end_address)) for p in pool_objs]
        pool_dicts = [{"pool": f"{s} - {e}"} for s, e in pools_minus_exclusions(ranges, exclusions)]
    else:
        # No exclusions (the common case): emit each stored pool directly so its
        # per-pool require-client-classes survives the round-trip.
        pool_dicts = []
        for p in pool_objs:
            entry: dict = {"pool": f"{p.start_address} - {p.end_address}"}
            _emit_require_classes(entry, p)
            pool_dicts.append(entry)

    subnet: dict = {
        "id": sid,
        "subnet": str(scope.prefix.prefix),
        "pools": pool_dicts,
    }
    _scope_timers(scope, subnet)
    _emit_require_classes(subnet, scope)
    _emit_ddns(scope, subnet)
    _emit_subnet_selection(scope, subnet)

    scope_opts = _options_for(DHCPOption.objects.filter(scope=scope))
    if scope_opts:
        subnet["option-data"] = scope_opts

    reservations = []
    for res in DHCPReservation.objects.filter(scope=scope).select_related("ip_address"):
        entry: dict = {"ip-address": str(res.ip_address.host)}
        ident = _kea_reservation_identifier(res)
        if ident:
            entry[ident[0]] = ident[1]
        if res.hostname:
            entry["hostname"] = res.hostname
        if res.client_classes:
            entry["client-classes"] = list(res.client_classes)
        res_opts = _options_for(DHCPOption.objects.filter(reservation=res))
        if res_opts:
            entry["option-data"] = res_opts
        _apply_context(entry, res)
        reservations.append(entry)
    if reservations:
        subnet["reservations"] = reservations

    _apply_context(subnet, scope)
    return subnet


def _build_dhcp4(server, scopes, subnet_id) -> dict:
    from nautobot_dhcp_models.models import DHCPOption

    standalone, groups = _partition_by_shared_network(scopes)
    dhcp4: dict = {"subnet4": [_subnet4_dict(s, subnet_id[s.pk]) for s in standalone]}

    shared_networks = []
    for sn, members in groups:
        entry: dict = {"name": sn.name, "subnet4": [_subnet4_dict(s, subnet_id[s.pk]) for s in members]}
        _emit_sharednet_fields(sn, entry)
        shared_networks.append(entry)
    if shared_networks:
        dhcp4["shared-networks"] = shared_networks

    server_opts = _options_for(DHCPOption.objects.filter(server=server))
    if server_opts:
        dhcp4["option-data"] = server_opts

    _emit_server_daemon(server, dhcp4)
    _apply_context(dhcp4, server)
    return {"Dhcp4": dhcp4}


def _subnet6_dict(scope, sid) -> dict:
    from nautobot_dhcp_models.models import (
        DHCPDelegatedPrefixReservation,
        DHCPOption,
        DHCPPool,
        DHCPPrefixDelegationPool,
        DHCPReservation,
    )

    pools = []
    for p in DHCPPool.objects.filter(scope=scope):
        pool_entry: dict = {"pool": f"{p.start_address} - {p.end_address}"}
        _emit_require_classes(pool_entry, p)
        pools.append(pool_entry)
    subnet: dict = {
        "id": sid,
        "subnet": str(scope.prefix.prefix),
        "pools": pools,
    }
    _scope_timers(scope, subnet)
    _emit_require_classes(subnet, scope)
    _emit_ddns(scope, subnet)
    _emit_subnet_selection(scope, subnet)
    # DHCPv6-only selection/allocation keys.
    if scope.min_preferred_lifetime is not None:
        subnet["min-preferred-lifetime"] = scope.min_preferred_lifetime
    if scope.preferred_lifetime:
        subnet["preferred-lifetime"] = scope.preferred_lifetime
    if scope.max_preferred_lifetime is not None:
        subnet["max-preferred-lifetime"] = scope.max_preferred_lifetime
    if scope.rapid_commit is not None:
        subnet["rapid-commit"] = scope.rapid_commit
    if scope.pd_allocator:
        subnet["pd-allocator"] = scope.pd_allocator
    if scope.interface_id:
        subnet["interface-id"] = scope.interface_id

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
        _emit_require_classes(entry, pdp)
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
    return subnet


def _build_dhcp6(server, scopes, subnet_id) -> dict:
    from nautobot_dhcp_models.models import DHCPOption

    standalone, groups = _partition_by_shared_network(scopes)
    dhcp6: dict = {"subnet6": [_subnet6_dict(s, subnet_id[s.pk]) for s in standalone]}

    shared_networks = []
    for sn, members in groups:
        entry: dict = {"name": sn.name, "subnet6": [_subnet6_dict(s, subnet_id[s.pk]) for s in members]}
        _emit_sharednet_fields(sn, entry)
        shared_networks.append(entry)
    if shared_networks:
        dhcp6["shared-networks"] = shared_networks

    server_opts = _options_for(DHCPOption.objects.filter(server=server))
    if server_opts:
        dhcp6["option-data"] = server_opts

    _emit_server_daemon(server, dhcp6)
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
            g = {
                "identifier_type": row.identifier_type,
                "duid": row.duid,
                "mac": row.mac_address,
                "ip-addresses": [],
                "prefixes": [],
                "hostname": "",
                "option-data": [],
                "client_classes": [],
                "user_context": {},
                "extra": {},
            }
            groups[key] = g
        if row.hostname:
            g["hostname"] = row.hostname
        # All rows of one Kea reservation share its client-classes; take the first seen.
        if row.client_classes and not g["client_classes"]:
            g["client_classes"] = list(row.client_classes)
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
            key = _IDENTIFIER_KEY.get(g["identifier_type"], "duid")
            entry[key] = _normalize_kea_hex(identifier) if key in _HEX_IDENTIFIER_KEYS else identifier
        if g["ip-addresses"]:
            entry["ip-addresses"] = g["ip-addresses"]
        if g["prefixes"]:
            entry["prefixes"] = g["prefixes"]
        if g["hostname"]:
            entry["hostname"] = g["hostname"]
        if g["client_classes"]:
            entry["client-classes"] = g["client_classes"]
        if g["option-data"]:
            entry["option-data"] = g["option-data"]
        if g["user_context"]:
            entry["user-context"] = g["user_context"]
        for key, value in g["extra"].items():
            entry.setdefault(key, value)
        entries.append(entry)
    return entries

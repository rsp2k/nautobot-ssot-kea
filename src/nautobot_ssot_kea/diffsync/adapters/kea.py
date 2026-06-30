"""Source adapter: load an ISC Kea DHCPv4 or DHCPv6 config into DiffSync.

This is the read side (PULL). It parses a Kea ``Dhcp4`` or ``Dhcp6`` object and
normalizes every value to its dhcp-models-native form (CIDR prefix, pool range,
MAC/DUID, option data) so the diff against the Nautobot side is apples-to-apples.

Family is detected from the config: a ``subnet6`` key (or an explicit
``family=6``) selects the DHCPv6 path -- subnet6 + pd-pools + prefix-delegation
reservations + the ``dhcp6`` option space. Otherwise the DHCPv4 path runs.

Leases are NOT in the config -- they live in the lease database. To sync them,
pass the parsed memfile lease rows (``kea-leases4.csv`` / ``kea-leases6.csv``)
as ``leases=``; the adapter maps each lease's numeric ``subnet_id`` back to a
CIDR prefix using the config's ``subnet{4,6}[].id``. Kea config has no explicit
exclusion concept, so no exclusions are emitted.
"""

from __future__ import annotations

import re

from diffsync import Adapter
from nautobot_dhcp_models.ssot.base import (
    DhcpDelegatedPrefixReservation,
    DhcpExclusion,
    DhcpLease,
    DhcpOption,
    DhcpPool,
    DhcpPrefixDelegationPool,
    DhcpRedundancyGroup,
    DhcpRedundancyGroupMember,
    DhcpReservation,
    DhcpScope,
    DhcpServer,
    DhcpSharedNetwork,
)

from nautobot_ssot_kea.utils.kea import (
    canonical_cidr,
    canonical_ip,
    kea_expire_to_iso,
    kea_lease6_type,
    kea_lease_state,
    normalize_mac,
    normalize_option_data,
    parse_kea_pd_pool,
    parse_kea_pool,
    split_kea_prefix,
)

# Kea reservation identifier keys, in preference order. hw-address maps cleanly
# to a MAC; the others are opaque client identifiers passed through normalize_mac
# (which leaves non-MAC values lowercased but otherwise intact).
_RESERVATION_ID_KEYS = ("hw-address", "client-id", "duid", "circuit-id", "flex-id")

# Per-element keys this adapter maps to first-class columns or sub-collections.
# Everything else (plus the explicit ``user-context``) is preserved verbatim in the
# element's ``extra`` passthrough so a Kea config round-trips without loss -- the
# global daemon config (interfaces-config, lease-database, hooks-libraries, ...),
# unmodeled subnet keys (min/max-valid-lifetime, ddns-*, require-client-classes),
# pool/reservation extras, and so on.
_GLOBAL_CONSUMED = {
    "subnet4", "subnet6", "option-data", "valid-lifetime", "shared-networks",
    # host-reservation-identifiers is a top-level list, fully promoted to a field.
    "host-reservation-identifiers",
}
# Keys consumed off a shared-network element -- the operational fields we promote
# to first-class columns plus the member subnet lists. option-data is intentionally
# NOT consumed: shared-network options ride the extra escape hatch (lossless) rather
# than becoming first-class DhcpOption rows.
_SHAREDNET_CONSUMED = {
    "name",
    "subnet4",
    "subnet6",
    "comment",
    "user-context-description",
    "valid-lifetime",
    "min-valid-lifetime",
    "max-valid-lifetime",
    "renew-timer",
    "rebind-timer",
    "preferred-lifetime",
    "min-preferred-lifetime",
    "max-preferred-lifetime",
    "match-client-id",
    "authoritative",
    "rapid-commit",
    "relay",
    "interface",
    "interface-id",
    "allocator",
    "pd-allocator",
    "reservations-in-subnet",
    "reservations-out-of-pool",
    "require-client-classes",
    "evaluate-additional-classes",
}
_SUBNET_CONSUMED = {
    "id",
    "subnet",
    "valid-lifetime",
    "min-valid-lifetime",
    "max-valid-lifetime",
    "renew-timer",
    "rebind-timer",
    "comment",
    "user-context-description",
    "option-data",
    "pools",
    "pd-pools",
    "reservations",
    "preferred-lifetime",
    "min-preferred-lifetime",
    "max-preferred-lifetime",
    "rapid-commit",
    "allocator",
    "pd-allocator",
    "relay",
    "interface",
    "interface-id",
    "reservations-in-subnet",
    "reservations-out-of-pool",
    "require-client-classes",
    "evaluate-additional-classes",
    "ddns-send-updates",
    "ddns-override-client-update",
    "ddns-override-no-update",
    "ddns-qualifying-suffix",
    "ddns-generated-prefix",
    "ddns-replace-client-name",
    "ddns-conflict-resolution-mode",
    "ddns-update-on-renew",
    "ddns-ttl-percent",
    "hostname-char-set",
    "hostname-char-replacement",
}
_POOL_CONSUMED = {"pool", "require-client-classes", "evaluate-additional-classes"}
_PDPOOL_CONSUMED = {
    "prefix",
    "prefix-len",
    "delegated-len",
    "excluded-prefix",
    "excluded-prefix-len",
    "option-data",
    "require-client-classes",
    "evaluate-additional-classes",
}
_RES_CONSUMED = {
    "ip-address",
    "hw-address",
    "client-id",
    "duid",
    "circuit-id",
    "flex-id",
    "hostname",
    "comment",
    "option-data",
    "client-classes",
}
_V6RES_CONSUMED = {
    "duid",
    "hw-address",
    "flex-id",
    "ip-addresses",
    "prefixes",
    "hostname",
    "comment",
    "option-data",
    "client-classes",
}
_OPTION_CONSUMED = {"code", "name", "space", "data", "csv-format", "always-send", "never-send"}


def _split_context(element: dict, consumed: set) -> tuple[dict, dict]:
    """Return ``(user_context, extra)`` for a Kea config element.

    ``user_context`` is the element's explicit ``user-context``; ``extra`` is every
    key this adapter did not consume, preserved verbatim for a lossless round-trip.
    """
    user_context = element.get("user-context") or {}
    extra = {k: v for k, v in element.items() if k != "user-context" and k not in consumed}
    return user_context, extra


def _keep_remainder(extra: dict, key: str, obj: dict, promoted: set) -> None:
    """Trim a nested config object in ``extra`` down to just its un-promoted sub-keys.

    For nested daemon blocks (``dhcp-ddns``, ``interfaces-config``) we promote a few
    sub-keys to first-class columns but must not lose the siblings. The whole block
    is in ``extra`` initially (it was not in the consumed set); replace it with only
    the remainder so the round-trip stays lossless, or drop the key if nothing's left.
    """
    remainder = {k: v for k, v in obj.items() if k not in promoted}
    if remainder:
        extra[key] = remainder
    else:
        extra.pop(key, None)


# A normalized MAC is exactly six colon-separated hex octets.
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def _split_lease_identifier(hwaddr: str, client_id: str) -> tuple[str, str]:
    """Split a v4 lease's identifiers into ``(mac_address, duid)``.

    A Kea v4 lease may be keyed by a client-id (RFC 4361 / DUID-style) with no
    hwaddr. Such an id is far longer than a MAC, so stuffing it into mac_address(17)
    overflows the column and crashes the sync. A real MAC goes to mac_address;
    anything else routes to duid (the lease model's wide opaque-identifier slot),
    matching where the MS adapter puts an extended lease identifier so the two
    vendors' leases diff cleanly.
    """
    mac = normalize_mac(hwaddr or "")
    if _MAC_RE.match(mac):
        return mac, ""
    other = normalize_mac(client_id or hwaddr or "")
    return "", other


def _require_classes(element: dict) -> list:
    """Read the 'additionally evaluate these classes' list off a Kea element.

    Kea renamed ``require-client-classes`` to ``evaluate-additional-classes`` in
    2.5.x; both carry the same semantics (a subnet/pool/pd-pool draws from a list
    of class names to evaluate beyond the ones the client already matched). Prefer
    the newer key when both are present, and normalize to a plain list of strings.
    """
    value = element.get("evaluate-additional-classes")
    if value is None:
        value = element.get("require-client-classes")
    return list(value or [])


def _ddns_kwargs(element: dict) -> dict:
    """Map a subnet's DNS dynamic-update keys to the scope's ddns_* / hostname_char_* fields.

    These apply to both DHCPv4 and DHCPv6 subnets; a key the config omits maps to
    the field's neutral default (None for the bools/float, "" for the strings) so it
    does not churn against an MS scope that never sets DDNS.
    """
    return dict(
        ddns_send_updates=element.get("ddns-send-updates"),
        ddns_override_client_update=element.get("ddns-override-client-update"),
        ddns_override_no_update=element.get("ddns-override-no-update"),
        ddns_qualifying_suffix=element.get("ddns-qualifying-suffix", ""),
        ddns_generated_prefix=element.get("ddns-generated-prefix", ""),
        ddns_replace_client_name=element.get("ddns-replace-client-name", ""),
        ddns_conflict_resolution_mode=element.get("ddns-conflict-resolution-mode", ""),
        ddns_update_on_renew=element.get("ddns-update-on-renew"),
        ddns_ttl_percent=element.get("ddns-ttl-percent"),
        hostname_char_set=element.get("hostname-char-set", ""),
        hostname_char_replacement=element.get("hostname-char-replacement", ""),
    )


# Kea HA mode -> vendor-neutral DHCPRedundancyMode. Kea spells load balancing
# "load-balancing"; our neutral choice (shared with MS "load-balance") drops the -ing.
_KEA_HA_MODE = {
    "load-balancing": "load-balance",
    "hot-standby": "hot-standby",
    "passive-backup": "passive-backup",
}


def _ha_relationships(config: dict):
    """Yield each high-availability relationship dict from the HA hook, if present.

    The HA config lives in hooks-libraries -> libdhcp_ha -> parameters
    -> high-availability[]. The whole hooks-libraries list also stays in the
    server's ``extra`` (untouched) so it round-trips on export verbatim; this is a
    read-only projection that makes the relationship queryable + cross-vendor diffable.
    """
    for hook in config.get("hooks-libraries") or []:
        if "libdhcp_ha" in (hook.get("library") or ""):
            params = hook.get("parameters") or {}
            yield from params.get("high-availability") or []


def _pd_pool_key(fields: dict) -> str:
    """Build the DiffSync pd_pool identity string from parse_kea_pd_pool() output."""
    return f"{fields['pd_prefix']}/{fields['prefix_length']}-{fields['delegated_length']}"


class KeaAdapter(Adapter):
    """Load a parsed Kea ``Dhcp4``/``Dhcp6`` config dict into the DiffSync store."""

    dhcpserver = DhcpServer
    dhcpredundancygroup = DhcpRedundancyGroup
    dhcpredundancygroupmember = DhcpRedundancyGroupMember
    dhcpsharednetwork = DhcpSharedNetwork
    dhcpscope = DhcpScope
    dhcppool = DhcpPool
    dhcpexclusion = DhcpExclusion
    dhcpreservation = DhcpReservation
    dhcpprefixdelegationpool = DhcpPrefixDelegationPool
    dhcpdelegatedprefixreservation = DhcpDelegatedPrefixReservation
    dhcpoption = DhcpOption
    dhcplease = DhcpLease

    top_level = (
        "dhcpserver",
        "dhcpredundancygroup",
        "dhcpredundancygroupmember",
        "dhcpsharednetwork",
        "dhcpscope",
        "dhcppool",
        "dhcpexclusion",
        "dhcpreservation",
        "dhcpprefixdelegationpool",
        "dhcpdelegatedprefixreservation",
        "dhcpoption",
        "dhcplease",
    )

    def __init__(
        self,
        *args,
        config: dict,
        server_name: str,
        leases=None,
        family: int | None = None,
        job=None,
        sync=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.config = config
        self.server_name = server_name
        self.leases = leases or []
        # Detect family: explicit arg wins, else a subnet6 key means DHCPv6.
        self.family = family or (6 if "subnet6" in config else 4)
        self.job = job
        self.sync = sync
        # subnet{4,6}[].id -> CIDR prefix, built while loading subnets; lets the
        # lease loader resolve a memfile lease's numeric subnet_id to a prefix.
        self._subnet_id_to_prefix: dict[int, str] = {}
        # Name of the HA relationship protecting this daemon's subnets, set while
        # loading the HA hook. Kea HA is server-wide, so every scope inherits it.
        self._ha_group_name: str = ""

    @property
    def _option_space(self) -> str:
        return "dhcp6" if self.family == 6 else "dhcp4"

    def load(self) -> None:
        """Walk the config: server, global options, then each subnet, then leases."""
        server_name = self.server_name
        if not server_name:
            raise ValueError("KeaAdapter requires a server_name (Kea config has no server identity)")

        # Kea config carries no AD-authorization concept; leave it unknown. The
        # whole global daemon config we do not model (interfaces, databases, hooks,
        # shared-networks, client-classes, ...) is preserved in the server's extra.
        server_uc, server_extra = _split_context(self.config, _GLOBAL_CONSUMED)
        # Promote a few daemon-level settings; preserve the un-promoted siblings of
        # the nested blocks in extra (under their original key) for lossless round-trip.
        # A malformed non-dict block is left untouched in extra rather than crashing.
        d2 = self.config.get("dhcp-ddns")
        if isinstance(d2, dict):
            _keep_remainder(server_extra, "dhcp-ddns", d2, {"enable-updates", "server-ip", "server-port"})
        else:
            d2 = {}
        ifc = self.config.get("interfaces-config")
        if isinstance(ifc, dict):
            _keep_remainder(server_extra, "interfaces-config", ifc, {"interfaces"})
        else:
            ifc = {}
        self.add(
            self.dhcpserver(
                name=server_name,
                vendor="kea",
                ad_authorized=None,
                ddns_enabled=d2.get("enable-updates"),
                ddns_server_ip=d2.get("server-ip", ""),
                ddns_server_port=d2.get("server-port"),
                listen_interfaces=list(ifc.get("interfaces") or []),
                host_identifier_priority=list(self.config.get("host-reservation-identifiers") or []),
                user_context=server_uc,
                extra=server_extra,
            )
        )

        # Redundancy: project the HA hook into a group + THIS server's own membership.
        for rel in _ha_relationships(self.config):
            self._load_ha_relationship(server_name, rel)

        for opt in self.config.get("option-data", []):
            self._add_option(server_name, "", "", "", opt)

        global_lifetime = self.config.get("valid-lifetime")
        subnet_key = "subnet6" if self.family == 6 else "subnet4"

        # Shared-networks first: each emits a shared-network record, then its member
        # subnets load tagged with the shared-network name (membership link).
        for shared_net in self.config.get("shared-networks", []):
            self._load_shared_network(server_name, shared_net, subnet_key, global_lifetime)

        # Standalone subnets (no shared-network) carry an empty membership.
        for subnet in self.config.get(subnet_key, []):
            self._load_subnet(server_name, subnet, global_lifetime)

        for lease in self.leases:
            self._load_lease(server_name, lease)

    def _load_ha_relationship(self, server_name: str, rel: dict) -> None:
        """Project one Kea HA relationship into a redundancy group + this server's member.

        Emits ONLY this server's own membership (role/url from the peer whose name
        matches this-server-name); the peer servers contribute their own member rows
        from their own syncs. The relationship name is explicit (Kea 2.4+) or, for
        older single-relationship configs, synthesized deterministically from the
        sorted peer names so every daemon in the relationship agrees on it.
        """
        peers = rel.get("peers") or []
        name = rel.get("name") or ("ha:" + ",".join(sorted(p.get("name", "") for p in peers)))
        # Kea HA is daemon-wide: every subnet on this server is protected by this
        # relationship, so remember it to tag each scope. (Kea 2.4+ multi-relationship
        # configs would map subnets to relationships individually; the first wins here.)
        if not self._ha_group_name:
            self._ha_group_name = name
        self.add(
            self.dhcpredundancygroup(
                name=name,
                mode=_KEA_HA_MODE.get(rel.get("mode", ""), "hot-standby"),
                # Kea HA tuning; mclt / load_balance_percent / state_switch_interval
                # are Microsoft/ISC-dhcpd concepts and stay unset for Kea.
                max_response_delay=rel.get("max-response-delay"),
                max_unacked_clients=rel.get("max-unacked-clients"),
                heartbeat_delay=rel.get("heartbeat-delay"),
                enabled=True,
            )
        )
        this_peer = next((p for p in peers if p.get("name") == rel.get("this-server-name")), None)
        if this_peer is not None:
            self.add(
                self.dhcpredundancygroupmember(
                    group_name=name,
                    server_name=server_name,
                    role=this_peer.get("role", "primary"),
                    url=this_peer.get("url", ""),
                )
            )

    def _load_shared_network(self, server_name: str, shared_net: dict, subnet_key: str, global_lifetime) -> None:
        name = shared_net["name"]
        sn_uc, sn_extra = _split_context(shared_net, _SHAREDNET_CONSUMED)
        self.add(
            self.dhcpsharednetwork(
                server_name=server_name,
                name=name,
                min_lease_time=shared_net.get("min-valid-lifetime"),
                default_lease_time=shared_net.get("valid-lifetime"),
                max_lease_time=shared_net.get("max-valid-lifetime"),
                renew_timer=shared_net.get("renew-timer"),
                rebind_timer=shared_net.get("rebind-timer"),
                min_preferred_lifetime=shared_net.get("min-preferred-lifetime"),
                preferred_lifetime=shared_net.get("preferred-lifetime"),
                max_preferred_lifetime=shared_net.get("max-preferred-lifetime"),
                match_client_id=shared_net.get("match-client-id"),
                authoritative=shared_net.get("authoritative"),
                rapid_commit=shared_net.get("rapid-commit"),
                relay_addresses=list((shared_net.get("relay") or {}).get("ip-addresses", [])),
                interface=shared_net.get("interface", ""),
                interface_id=shared_net.get("interface-id", ""),
                allocator=shared_net.get("allocator", ""),
                pd_allocator=shared_net.get("pd-allocator", ""),
                reservations_in_subnet=shared_net.get("reservations-in-subnet"),
                reservations_out_of_pool=shared_net.get("reservations-out-of-pool"),
                require_client_classes=_require_classes(shared_net),
                description=shared_net.get("comment") or shared_net.get("user-context-description") or "",
                user_context=sn_uc,
                extra=sn_extra,
            )
        )
        # A member subnet that omits valid-lifetime inherits the shared-network's,
        # falling back to the global default.
        member_lifetime = shared_net.get("valid-lifetime") or global_lifetime
        for subnet in shared_net.get(subnet_key, []):
            self._load_subnet(server_name, subnet, member_lifetime, shared_network=name)

    def _load_subnet(self, server_name: str, subnet: dict, global_lifetime, shared_network: str = "") -> None:
        # Canonicalize the CIDR so it matches the form the store round-trips to
        # (else a non-canonical literal re-creates the scope on every sync).
        prefix = canonical_cidr(subnet["subnet"])
        if subnet.get("id") is not None:
            self._subnet_id_to_prefix[int(subnet["id"])] = prefix

        scope_uc, scope_extra = _split_context(subnet, _SUBNET_CONSUMED)
        scope_kwargs = dict(
            server_name=server_name,
            prefix=prefix,
            shared_network=shared_network,
            redundancy_group=self._ha_group_name,
            name="",  # Kea subnets have no name attribute.
            state="enabled",
            min_lease_time=subnet.get("min-valid-lifetime"),
            default_lease_time=subnet.get("valid-lifetime") or global_lifetime or 86400,
            max_lease_time=subnet.get("max-valid-lifetime"),
            description=subnet.get("comment") or subnet.get("user-context-description") or "",
            require_client_classes=_require_classes(subnet),
            # Subnet selection / allocation / reservation-mode -- valid for BOTH
            # families (v4 relay agents are near-universal), so load unconditionally;
            # all are in _SUBNET_CONSUMED, so skipping them for v4 would drop them.
            allocator=subnet.get("allocator", ""),
            relay_addresses=list((subnet.get("relay") or {}).get("ip-addresses", [])),
            interface=subnet.get("interface", ""),
            reservations_in_subnet=subnet.get("reservations-in-subnet"),
            reservations_out_of_pool=subnet.get("reservations-out-of-pool"),
            **_ddns_kwargs(subnet),
            user_context=scope_uc,
            extra=scope_extra,
        )
        if self.family == 6:
            # Genuinely DHCPv6-only: preferred lifetimes, rapid-commit, the PD
            # allocator, and the interface-id (option 18) selector.
            scope_kwargs.update(
                min_preferred_lifetime=subnet.get("min-preferred-lifetime"),
                preferred_lifetime=subnet.get("preferred-lifetime"),
                max_preferred_lifetime=subnet.get("max-preferred-lifetime"),
                rapid_commit=subnet.get("rapid-commit"),
                pd_allocator=subnet.get("pd-allocator", ""),
                interface_id=subnet.get("interface-id", ""),
            )
        self.add(self.dhcpscope(**scope_kwargs))

        for pool in subnet.get("pools", []):
            start, end = parse_kea_pool(pool.get("pool", ""))
            pool_uc, pool_extra = _split_context(pool, _POOL_CONSUMED)
            self.add(
                self.dhcppool(
                    server_name=server_name,
                    prefix=prefix,
                    start_address=canonical_ip(start),
                    end_address=canonical_ip(end),
                    require_client_classes=_require_classes(pool),
                    user_context=pool_uc,
                    extra=pool_extra,
                )
            )

        for pd_pool in subnet.get("pd-pools", []):
            self._load_pd_pool(server_name, prefix, pd_pool)

        for opt in subnet.get("option-data", []):
            self._add_option(server_name, prefix, "", "", opt)

        for res in subnet.get("reservations", []):
            self._load_reservation(server_name, prefix, res)

    def _load_pd_pool(self, server_name: str, prefix: str, pd_pool: dict) -> None:
        fields = parse_kea_pd_pool(pd_pool)
        # Canonicalize the IPv6 prefix bases so identity matches the store's form.
        fields["pd_prefix"] = canonical_ip(fields["pd_prefix"])
        fields["excluded_prefix"] = canonical_ip(fields["excluded_prefix"])
        pd_uc, pd_extra = _split_context(pd_pool, _PDPOOL_CONSUMED)
        self.add(
            self.dhcpprefixdelegationpool(
                server_name=server_name,
                prefix=prefix,
                pd_prefix=fields["pd_prefix"],
                prefix_length=fields["prefix_length"],
                delegated_length=fields["delegated_length"],
                excluded_prefix=fields["excluded_prefix"],
                excluded_prefix_length=fields["excluded_prefix_length"],
                description="",
                require_client_classes=_require_classes(pd_pool),
                user_context=pd_uc,
                extra=pd_extra,
            )
        )
        for opt in pd_pool.get("option-data", []):
            self._add_option(server_name, prefix, _pd_pool_key(fields), "", opt)

    def _load_reservation(self, server_name: str, prefix: str, res: dict) -> None:
        """Load a reservation; v6 fans ip-addresses[] + prefixes[] out into flat rows."""
        if self.family == 6:
            self._load_v6_reservation(server_name, prefix, res)
            return
        ip = canonical_ip(res["ip-address"])
        # Route the identifier to the field that matches its type. Stuffing a
        # client-id/DUID into mac_address (max_length=17) overflows the column and
        # crashes the sync; only a real hw-address belongs there.
        identifier_type, mac_address, client_id, duid = "hw-address", "", "", ""
        for key in _RESERVATION_ID_KEYS:
            if res.get(key):
                identifier_type, value = key, res[key]
                if key == "hw-address":
                    mac_address = normalize_mac(value)
                elif key == "duid":
                    duid = value
                else:  # client-id / circuit-id / flex-id
                    client_id = value
                break
        res_uc, res_extra = _split_context(res, _RES_CONSUMED)
        self.add(
            self.dhcpreservation(
                server_name=server_name,
                prefix=prefix,
                ip_address=ip,
                identifier_type=identifier_type,
                mac_address=mac_address,
                client_id=client_id,
                duid=duid,
                hostname=res.get("hostname", ""),
                reservation_type="dhcp",
                description=res.get("comment", ""),
                client_classes=list(res.get("client-classes") or []),
                user_context=res_uc,
                extra=res_extra,
            )
        )
        for opt in res.get("option-data", []):
            self._add_option(server_name, prefix, "", ip, opt)

    def _load_v6_reservation(self, server_name: str, prefix: str, res: dict) -> None:
        """Fan a v6 reservation out: one address row per ip-addresses[], one PD row per prefixes[]."""
        duid = res.get("duid", "")
        hw = res.get("hw-address", "")
        flex = res.get("flex-id", "")
        identifier_type = "duid" if duid else ("hw-address" if hw else "flex-id")
        mac = normalize_mac(hw) if hw else ""
        client_id = "" if (duid or hw) else flex
        hostname = res.get("hostname", "")
        # One Kea v6 reservation fans into N rows; they share its context. The
        # exporter regroups by DUID and takes the context from any row in the group.
        res_uc, res_extra = _split_context(res, _V6RES_CONSUMED)
        cc = list(res.get("client-classes") or [])
        for ip in res.get("ip-addresses", []):
            self.add(
                self.dhcpreservation(
                    server_name=server_name,
                    prefix=prefix,
                    ip_address=canonical_ip(ip),
                    identifier_type=identifier_type,
                    mac_address=mac,
                    client_id=client_id,
                    duid=duid,
                    hostname=hostname,
                    reservation_type="dhcp",
                    description=res.get("comment", ""),
                    client_classes=cc,
                    user_context=res_uc,
                    extra=res_extra,
                )
            )
        for pd_cidr in res.get("prefixes", []):
            base, length = split_kea_prefix(pd_cidr)
            self.add(
                self.dhcpdelegatedprefixreservation(
                    server_name=server_name,
                    prefix=prefix,
                    delegated_prefix=canonical_ip(base),
                    delegated_prefix_length=length,
                    identifier_type=identifier_type,
                    duid=duid,
                    mac_address=mac,
                    hostname=hostname,
                    description=res.get("comment", ""),
                    client_classes=cc,
                    user_context=res_uc,
                    extra=res_extra,
                )
            )

    def _load_lease(self, server_name: str, lease: dict) -> None:
        prefix = self._subnet_id_to_prefix.get(lease["subnet_id"])
        if prefix is None:
            if self.job:
                self.job.logger.warning(
                    f"Skipping lease {lease['address']}: subnet_id {lease['subnet_id']} not in config"
                )
            return
        if self.family == 6:
            self.add(
                self.dhcplease(
                    server_name=server_name,
                    prefix=prefix,
                    ip_address=canonical_ip(lease["address"]),
                    mac_address=normalize_mac(lease.get("hwaddr", "")) if lease.get("hwaddr") else "",
                    duid=lease.get("duid", ""),
                    hostname=lease.get("hostname", ""),
                    lease_state=kea_lease_state(lease.get("state")),
                    lease_type=kea_lease6_type(lease.get("lease_type")),
                    prefix_length=lease.get("prefix_len"),
                    expires=kea_expire_to_iso(lease.get("expire")),
                )
            )
            return
        mac, duid = _split_lease_identifier(lease.get("hwaddr", ""), lease.get("client_id", ""))
        self.add(
            self.dhcplease(
                server_name=server_name,
                prefix=prefix,
                ip_address=canonical_ip(lease["address"]),
                mac_address=mac,
                duid=duid,
                hostname=lease.get("hostname", ""),
                lease_state=kea_lease_state(lease.get("state")),
                expires=kea_expire_to_iso(lease.get("expire")),
            )
        )

    def _add_option(
        self, server_name: str, scope_prefix: str, pd_pool_key: str, reservation_ip: str, opt: dict
    ) -> None:
        code = int(opt["code"])
        opt_uc, opt_extra = _split_context(opt, _OPTION_CONSUMED)
        # Match the option-definition name the target will store: _option_definition
        # seeds an unnamed optdef as "option-<code>", so emitting the same here keeps
        # a code-only Kea option from diffing forever against that synthesized name.
        self.add(
            self.dhcpoption(
                server_name=server_name,
                scope_prefix=scope_prefix,
                pd_pool_key=pd_pool_key,
                reservation_ip=reservation_ip,
                code=code,
                value=normalize_option_data(opt.get("data")),
                option_name=opt.get("name") or f"option-{code}",
                user_context=opt_uc,
                extra=opt_extra,
                # Kea config doesn't carry the option data type; the shared
                # optdef get_or_create resolves it on the target side.
                data_type="string",
                space=self._option_space,
            )
        )

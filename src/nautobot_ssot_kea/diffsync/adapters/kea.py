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

from diffsync import Adapter
from nautobot_dhcp_models.ssot.base import (
    DhcpDelegatedPrefixReservation,
    DhcpExclusion,
    DhcpLease,
    DhcpOption,
    DhcpPool,
    DhcpPrefixDelegationPool,
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
_GLOBAL_CONSUMED = {"subnet4", "subnet6", "option-data", "valid-lifetime", "shared-networks"}
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


def _pd_pool_key(fields: dict) -> str:
    """Build the DiffSync pd_pool identity string from parse_kea_pd_pool() output."""
    return f"{fields['pd_prefix']}/{fields['prefix_length']}-{fields['delegated_length']}"


class KeaAdapter(Adapter):
    """Load a parsed Kea ``Dhcp4``/``Dhcp6`` config dict into the DiffSync store."""

    dhcpserver = DhcpServer
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
        self.add(
            self.dhcpserver(
                name=server_name, vendor="kea", ad_authorized=None, user_context=server_uc, extra=server_extra
            )
        )

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
            name="",  # Kea subnets have no name attribute.
            state="enabled",
            min_lease_time=subnet.get("min-valid-lifetime"),
            default_lease_time=subnet.get("valid-lifetime") or global_lifetime or 86400,
            max_lease_time=subnet.get("max-valid-lifetime"),
            description=subnet.get("comment") or subnet.get("user-context-description") or "",
            require_client_classes=_require_classes(subnet),
            user_context=scope_uc,
            extra=scope_extra,
        )
        if self.family == 6:
            scope_kwargs.update(
                min_preferred_lifetime=subnet.get("min-preferred-lifetime"),
                preferred_lifetime=subnet.get("preferred-lifetime"),
                max_preferred_lifetime=subnet.get("max-preferred-lifetime"),
                rapid_commit=subnet.get("rapid-commit"),
                allocator=subnet.get("allocator", ""),
                pd_allocator=subnet.get("pd-allocator", ""),
                relay_addresses=list((subnet.get("relay") or {}).get("ip-addresses", [])),
                interface=subnet.get("interface", ""),
                interface_id=subnet.get("interface-id", ""),
                reservations_in_subnet=subnet.get("reservations-in-subnet"),
                reservations_out_of_pool=subnet.get("reservations-out-of-pool"),
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
        identifier = lease.get("hwaddr") or lease.get("client_id") or ""
        self.add(
            self.dhcplease(
                server_name=server_name,
                prefix=prefix,
                ip_address=canonical_ip(lease["address"]),
                mac_address=normalize_mac(identifier),
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

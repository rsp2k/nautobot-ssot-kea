"""Unit tests for the Kea config export's pure logic (pool/exclusion subtraction)."""

from types import SimpleNamespace

from nautobot_ssot_kea.diffsync.adapters.kea import _require_classes
from nautobot_ssot_kea.export import _emit_require_classes, build_cutover_config, pools_minus_exclusions


def test_no_exclusions_passes_through():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], []) == [("10.0.0.10", "10.0.0.250")]


def test_single_exclusion_splits_pool():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.50", "10.0.0.60")]) == [
        ("10.0.0.10", "10.0.0.49"),
        ("10.0.0.61", "10.0.0.250"),
    ]


def test_exclusion_at_start():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.10", "10.0.0.19")]) == [
        ("10.0.0.20", "10.0.0.250"),
    ]


def test_exclusion_at_end():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.200", "10.0.0.250")]) == [
        ("10.0.0.10", "10.0.0.199"),
    ]


def test_exclusion_covers_whole_pool():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.1", "10.0.0.255")]) == []


def test_multiple_exclusions():
    assert pools_minus_exclusions(
        [("10.0.0.10", "10.0.0.250")],
        [("10.0.0.50", "10.0.0.60"), ("10.0.0.100", "10.0.0.110")],
    ) == [
        ("10.0.0.10", "10.0.0.49"),
        ("10.0.0.61", "10.0.0.99"),
        ("10.0.0.111", "10.0.0.250"),
    ]


def test_exclusion_outside_pool_is_ignored():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.20")], [("10.0.9.0", "10.0.9.9")]) == [
        ("10.0.0.10", "10.0.0.20"),
    ]


# --- client-class associations: key-aliasing on read, single spelling on write ---


def test_require_classes_reads_legacy_key():
    assert _require_classes({"require-client-classes": ["a", "b"]}) == ["a", "b"]


def test_require_classes_reads_new_key():
    assert _require_classes({"evaluate-additional-classes": ["a"]}) == ["a"]


def test_require_classes_prefers_new_key_when_both_present():
    element = {"require-client-classes": ["old"], "evaluate-additional-classes": ["new"]}
    assert _require_classes(element) == ["new"]


def test_require_classes_absent_is_empty_list():
    assert _require_classes({}) == []


def test_emit_require_classes_writes_legacy_key():
    element = {}
    _emit_require_classes(element, SimpleNamespace(require_client_classes=["corp"]))
    assert element == {"require-client-classes": ["corp"]}


def test_emit_require_classes_omits_when_empty():
    element = {}
    _emit_require_classes(element, SimpleNamespace(require_client_classes=[]))
    assert element == {}  # no empty list emitted -> no diff churn against a bare config


def test_emit_then_read_round_trips():
    element = {}
    _emit_require_classes(element, SimpleNamespace(require_client_classes=["x", "y"]))
    assert _require_classes(element) == ["x", "y"]


# --- migration cutover config builder ---


def _peers():
    return [
        {"name": "node1", "url": "http://172.30.0.11:8000/", "role": "primary"},
        {"name": "node2", "url": "http://172.30.0.12:8000/", "role": "standby"},
    ]


def test_cutover_preserves_node_plumbing_and_injects_ha():
    generated = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.10.0/24"}]}}
    current = {
        "Dhcp4": {
            "control-socket": {"socket-type": "unix", "socket-name": "/run/kea/kea4-ctrl-socket"},
            "interfaces-config": {"interfaces": ["eth0"]},
            "lease-database": {"type": "memfile"},
            "subnet4": [{"id": 9, "subnet": "192.0.2.0/24"}],
        }
    }
    out = build_cutover_config(generated, current, this_server_name="node1", peers=_peers(), mode="hot-standby")[
        "Dhcp4"
    ]
    # Generated DHCP content wins...
    assert [s["subnet"] for s in out["subnet4"]] == ["10.0.10.0/24"]
    # ...but the node's plumbing (incl. the control socket the CA needs) is preserved.
    assert out["control-socket"]["socket-name"] == "/run/kea/kea4-ctrl-socket"
    assert out["interfaces-config"] == {"interfaces": ["eth0"]}
    assert out["lease-database"] == {"type": "memfile"}
    # HA + lease_cmds hooks installed with this server's name.
    libs = [h["library"] for h in out["hooks-libraries"]]
    assert any("libdhcp_lease_cmds.so" in lib for lib in libs)
    ha = [h for h in out["hooks-libraries"] if "libdhcp_ha.so" in h["library"]][0]
    rel = ha["parameters"]["high-availability"][0]
    assert rel["this-server-name"] == "node1"
    assert rel["mode"] == "hot-standby"
    assert [p["name"] for p in rel["peers"]] == ["node1", "node2"]


def test_cutover_replaces_existing_ha_not_duplicates():
    # A node that already had an HA hook shouldn't end up with two.
    generated = {"Dhcp4": {"subnet4": []}}
    current = {
        "Dhcp4": {
            "control-socket": {"socket-name": "/run/kea/kea4-ctrl-socket"},
            "hooks-libraries": [
                {"library": "/x/libdhcp_ha.so", "parameters": {"high-availability": [{"this-server-name": "old"}]}},
                {"library": "/x/libdhcp_lease_cmds.so"},
            ],
        }
    }
    out = build_cutover_config(generated, current, this_server_name="node2", peers=_peers(), mode="hot-standby")[
        "Dhcp4"
    ]
    ha_hooks = [h for h in out["hooks-libraries"] if "libdhcp_ha.so" in h["library"]]
    lease_hooks = [h for h in out["hooks-libraries"] if "libdhcp_lease_cmds.so" in h["library"]]
    assert len(ha_hooks) == 1 and len(lease_hooks) == 1
    assert ha_hooks[0]["parameters"]["high-availability"][0]["this-server-name"] == "node2"


def test_strip_option_code_removes_everywhere():
    from nautobot_ssot_kea.export import strip_option_code

    cfg = {
        "Dhcp4": {
            "option-data": [{"code": 6, "data": "x"}, {"code": 81, "data": "55"}],
            "subnet4": [
                {
                    "subnet": "10.0.0.0/24",
                    "option-data": [{"code": 81, "data": "55"}],
                    "reservations": [{"option-data": [{"code": 81, "data": "55"}, {"code": 3, "data": "1"}]}],
                }
            ],
            "shared-networks": [{"name": "sn", "subnet4": [{"option-data": [{"code": 81, "data": "z"}]}]}],
        }
    }
    out = strip_option_code(cfg, 81)["Dhcp4"]
    assert [o["code"] for o in out["option-data"]] == [6]
    assert out["subnet4"][0]["option-data"] == []
    assert [o["code"] for o in out["subnet4"][0]["reservations"][0]["option-data"]] == [3]
    assert out["shared-networks"][0]["subnet4"][0]["option-data"] == []


# --- reservation host-identifier hardening (real MS export quirks) ---


def test_kea_reservation_identifier_normalizes_dashes_to_colons():
    # Kea rejects dash-separated hex ("invalid host identifier value"); MS writes dashes.
    from nautobot_ssot_kea.export import _kea_reservation_identifier

    res = SimpleNamespace(identifier_type="hw-address", mac_address="aa-bb-cc-dd-ee-ff", client_id="")
    assert _kea_reservation_identifier(res) == ("hw-address", "aa:bb:cc:dd:ee:ff")


def test_kea_reservation_identifier_downgrades_non_mac_to_client_id():
    # An 18-octet RFC 4361 client id stored under a hw-address type can't be a MAC;
    # emitting it as hw-address makes Kea reject the whole config -> fall back to client-id.
    from nautobot_ssot_kea.export import _kea_reservation_identifier

    val = "00-01-00-01-de-ad-be-ef-ca-fe-00-00-00-00-00-00-00-01"
    res = SimpleNamespace(identifier_type="client-id", mac_address="", client_id=val)
    key, out = _kea_reservation_identifier(res)
    assert key == "client-id"
    assert out == val.replace("-", ":")


def test_kea_reservation_identifier_hw_address_non_mac_length_downgrades():
    from nautobot_ssot_kea.export import _kea_reservation_identifier

    # type says hw-address but value is 8 octets -> not a MAC -> client-id.
    res = SimpleNamespace(identifier_type="hw-address", mac_address="01-02-03-04-05-06-07-08", client_id="")
    key, _ = _kea_reservation_identifier(res)
    assert key == "client-id"


def test_kea_reservation_identifier_none_when_empty():
    from nautobot_ssot_kea.export import _kea_reservation_identifier

    empty = SimpleNamespace(identifier_type="hw-address", mac_address="", client_id="")
    assert _kea_reservation_identifier(empty) is None


# --- non-standard option-def synthesis (preserve, don't drop) ---


def test_infer_option_type_ip_list_is_array():
    from nautobot_ssot_kea.export import _infer_option_type

    assert _infer_option_type("172.20.6.100,172.20.6.101") == ("ipv4-address", True)
    assert _infer_option_type("10.0.128.1") == ("ipv4-address", False)
    assert _infer_option_type("some-host.example") == ("string", False)


def test_find_option_codes_by_data_walks_all_levels():
    from nautobot_ssot_kea.export import find_option_codes_by_data

    cfg = {
        "Dhcp4": {
            "subnet4": [{"option-data": [{"code": 150, "data": "172.20.6.100,172.20.6.101"}]}],
            "shared-networks": [{"subnet4": [{"option-data": [{"code": 150, "data": "172.20.6.100,172.20.6.101"}]}]}],
        }
    }
    assert find_option_codes_by_data(cfg, "172.20.6.100,172.20.6.101") == {150}


def test_add_option_def_infers_type_and_is_idempotent():
    from nautobot_ssot_kea.export import add_option_def

    cfg = {"Dhcp4": {}}
    assert add_option_def(cfg, 150, "172.20.6.100,172.20.6.101") is True
    d = cfg["Dhcp4"]["option-def"][0]
    assert d == {"name": "option-150", "code": 150, "space": "dhcp4", "type": "ipv4-address", "array": True}
    # second call for the same (code, space) is a no-op
    assert add_option_def(cfg, 150, "172.20.6.100,172.20.6.101") is False
    assert len(cfg["Dhcp4"]["option-def"]) == 1

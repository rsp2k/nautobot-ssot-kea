"""Unit tests for the Kea Control Agent client (fake transport, no network)."""

import pytest

from nautobot_ssot_kea.utils.ctrl_agent import KeaCommandError, KeaControlAgent


def _agent(responder):
    """Build a client whose HTTP POST is replaced by `responder(payload) -> json`."""
    return KeaControlAgent("http://kea:8000", post=responder)


def test_command_builds_payload_and_returns_result():
    captured = {}

    def responder(payload):
        captured.update(payload)
        return [{"result": 0, "arguments": {"ok": True}}]

    a = _agent(responder)
    result = a.command("config-get", service=["dhcp4"])
    assert captured == {"command": "config-get", "service": ["dhcp4"]}
    assert result["arguments"] == {"ok": True}


def test_arguments_included_only_when_given():
    captured = {}

    def responder(payload):
        captured.clear()
        captured.update(payload)
        return [{"result": 0}]

    a = _agent(responder)
    a.command("config-set", service=["dhcp4"], arguments={"Dhcp4": {}})
    assert captured["arguments"] == {"Dhcp4": {}}


def test_error_result_raises():
    a = _agent(lambda p: [{"result": 1, "text": "boom"}])
    with pytest.raises(KeaCommandError, match="error: boom"):
        a.command("config-get")


def test_unsupported_result_raises():
    # result 2 = command not supported (e.g. lease_cmds hook not loaded).
    a = _agent(lambda p: [{"result": 2, "text": "not supported"}])
    with pytest.raises(KeaCommandError, match="unsupported"):
        a.leases_get_all()


def test_empty_result_is_not_an_error():
    # result 3 = success but no data (e.g. zero leases). Must not raise.
    a = _agent(lambda p: [{"result": 3, "text": "0 IPv4 lease(s) found.", "arguments": {}}])
    assert a.leases_get_all() == []


def test_config_get_unwraps_arguments():
    a = _agent(lambda p: [{"result": 0, "arguments": {"Dhcp4": {"subnet4": [{"subnet": "10.0.0.0/24"}]}}}])
    cfg = a.config_get()
    assert cfg["Dhcp4"]["subnet4"][0]["subnet"] == "10.0.0.0/24"


def test_leases_get_all_returns_lease_list():
    a = _agent(lambda p: [{"result": 0, "arguments": {"leases": [{"ip-address": "10.0.0.5"}]}}])
    leases = a.leases_get_all(family=4)
    assert leases == [{"ip-address": "10.0.0.5"}]


def test_leases_get_all_v6_uses_v6_command():
    captured = {}
    a = _agent(lambda p: captured.update(p) or [{"result": 0, "arguments": {"leases": []}}])
    a.leases_get_all(family=6)
    assert captured["command"] == "lease6-get-all"
    assert captured["service"] == ["dhcp6"]


def test_config_write_passes_filename():
    captured = {}
    a = _agent(lambda p: captured.update(p) or [{"result": 0}])
    a.config_write(filename="/etc/kea/kea-dhcp4.conf")
    assert captured["command"] == "config-write"
    assert captured["arguments"] == {"filename": "/etc/kea/kea-dhcp4.conf"}


def test_config_write_without_filename_sends_no_arguments():
    captured = {}
    a = _agent(lambda p: captured.update(p) or [{"result": 0}])
    a.config_write()
    assert "arguments" not in captured

"""Configuration parsing (known issues 1, 3, 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dnp3_mcp.config import ConfigError, ServerConfig, parse_bool


def test_defaults_are_read_only_tcp() -> None:
    config = ServerConfig.from_env({})
    dnp3 = config.dnp3
    assert config.writes_enabled is False
    assert dnp3.writes_enabled is False
    assert (dnp3.connection_type, dnp3.host, dnp3.port) == ("tcp", "127.0.0.1", 20000)
    assert (dnp3.master_address, dnp3.timeout_ms, dnp3.max_retries) == (1, 5000, 3)
    assert dnp3.control_mode == "select_before_operate"
    assert config.point_map_path is None


@pytest.mark.parametrize("raw", ["false", "False", " FALSE ", "0", "no", "off", "n"])
def test_false_strings_never_enable_writes(raw: str) -> None:
    assert ServerConfig.from_env({"DNP3_WRITES_ENABLED": raw}).writes_enabled is False


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on", "y"])
def test_true_strings_enable_writes(raw: str) -> None:
    assert ServerConfig.from_env({"DNP3_WRITES_ENABLED": raw}).writes_enabled is True


@pytest.mark.parametrize("raw", ["banana", "enabled", "2", "truthy"])
def test_unrecognised_boolean_is_an_error(raw: str) -> None:
    with pytest.raises(ConfigError, match="DNP3_WRITES_ENABLED"):
        ServerConfig.from_env({"DNP3_WRITES_ENABLED": raw})


def test_empty_boolean_uses_default() -> None:
    assert parse_bool("X", "", False) is False
    assert parse_bool("X", "   ", True) is True
    assert parse_bool("X", None, False) is False


def test_connection_settings_are_used() -> None:
    config = ServerConfig.from_env(
        {
            "DNP3_HOST": "10.1.2.3",
            "DNP3_PORT": "20001",
            "DNP3_MASTER_ADDRESS": "3",
            "DNP3_TIMEOUT": "1500",
            "DNP3_MAX_RETRIES": "0",
            "DNP3_CONTROL_MODE": "direct_operate",
        }
    )
    dnp3 = config.dnp3
    assert (dnp3.host, dnp3.port, dnp3.master_address) == ("10.1.2.3", 20001, 3)
    assert (dnp3.timeout_ms, dnp3.timeout_s, dnp3.max_retries) == (1500, 1.5, 0)
    assert dnp3.control_mode == "direct_operate"


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("DNP3_PORT", "abc"),
        ("DNP3_PORT", "0"),
        ("DNP3_PORT", "70000"),
        ("DNP3_MASTER_ADDRESS", "65520"),
        ("DNP3_MASTER_ADDRESS", "-1"),
        ("DNP3_TIMEOUT", "0"),
        ("DNP3_TIMEOUT", "5s"),
        ("DNP3_MAX_RETRIES", "-1"),
        ("DNP3_MAX_RETRIES", "11"),
        ("DNP3_CONTROL_MODE", "yolo"),
        ("DNP3_CONNECTION_TYPE", "udp"),
    ],
)
def test_invalid_settings_are_errors(name: str, raw: str) -> None:
    with pytest.raises(ConfigError, match=name):
        ServerConfig.from_env({name: raw})


def test_serial_is_reported_as_not_implemented() -> None:
    with pytest.raises(ConfigError, match="serial is not implemented"):
        ServerConfig.from_env({"DNP3_CONNECTION_TYPE": "serial", "DNP3_SERIAL_PORT": "/dev/ttyUSB0"})


def test_local_address_is_an_alias_for_master_address() -> None:
    assert ServerConfig.from_env({"DNP3_LOCAL_ADDRESS": "7"}).dnp3.master_address == 7
    same = {"DNP3_LOCAL_ADDRESS": "7", "DNP3_MASTER_ADDRESS": "7"}
    assert ServerConfig.from_env(same).dnp3.master_address == 7


def test_conflicting_local_and_master_address_is_an_error() -> None:
    with pytest.raises(ConfigError, match="conflicts"):
        ServerConfig.from_env({"DNP3_LOCAL_ADDRESS": "7", "DNP3_MASTER_ADDRESS": "3"})


def test_point_map_path_is_expanded() -> None:
    config = ServerConfig.from_env({"POINT_MAP_FILE": "~/points.json"})
    assert config.point_map_path == Path("~/points.json").expanduser()

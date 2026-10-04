"""Server configuration, read from the environment when the server is built.

Nothing here runs at import time: `ServerConfig.from_env()` is called by
`DNP3MCPServer` after `cli.main()` has loaded `.env`, so values from that file
apply to every setting.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# IEEE 1815-2012 link addresses: 0xFFF0-0xFFFF are reserved (broadcast and
# self-address), so neither a master nor an outstation may use them.
MAX_LINK_ADDRESS = 0xFFEF

CONTROL_MODES = ("select_before_operate", "direct_operate")

_TRUE = frozenset({"1", "true", "yes", "y", "on"})
_FALSE = frozenset({"0", "false", "no", "n", "off"})


class ConfigError(ValueError):
    """Raised when an environment setting is invalid or unsupported."""


def parse_bool(name: str, raw: str | None, default: bool) -> bool:
    """Parse a boolean setting strictly.

    Only the usual spellings of true and false are accepted. Anything else is
    an error rather than being treated as true, so a typo can never switch
    writes on.
    """
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name}={raw!r} is not a boolean; use true or false")


def parse_int(name: str, raw: str | None, default: int, *, minimum: int, maximum: int) -> int:
    """Parse an integer setting and check its range."""
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip(), 10)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not an integer") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name}={value} is out of range ({minimum}-{maximum})")
    return value


@dataclass(frozen=True, slots=True)
class DNP3Config:
    """How the master reaches outstations."""

    host: str = "127.0.0.1"
    port: int = 20000
    master_address: int = 1
    timeout_ms: int = 5000
    max_retries: int = 3
    control_mode: str = "select_before_operate"
    writes_enabled: bool = False

    @property
    def connection_type(self) -> str:
        """Only TCP is implemented."""
        return "tcp"

    @property
    def timeout_s(self) -> float:
        return self.timeout_ms / 1000.0


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Everything the server reads from the environment, read once."""

    dnp3: DNP3Config
    point_map_path: Path | None = None

    @property
    def writes_enabled(self) -> bool:
        return self.dnp3.writes_enabled

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ServerConfig:
        """Build the configuration from `environ` (default: `os.environ`).

        Raises:
            ConfigError: A setting is malformed, out of range or names a
                feature this server does not implement.
        """
        env = os.environ if environ is None else environ

        connection_type = (env.get("DNP3_CONNECTION_TYPE") or "tcp").strip().lower()
        if connection_type == "serial":
            raise ConfigError(
                "DNP3_CONNECTION_TYPE=serial is not implemented; only tcp is supported. "
                "Use a serial-to-TCP gateway or terminal server and point DNP3_HOST/DNP3_PORT at it."
            )
        if connection_type != "tcp":
            raise ConfigError(f"DNP3_CONNECTION_TYPE={connection_type!r} is not supported; use tcp")

        host = (env.get("DNP3_HOST") or "127.0.0.1").strip()
        port = parse_int("DNP3_PORT", env.get("DNP3_PORT"), 20000, minimum=1, maximum=65535)

        master_address = parse_int(
            "DNP3_MASTER_ADDRESS", env.get("DNP3_MASTER_ADDRESS"), 1, minimum=0, maximum=MAX_LINK_ADDRESS
        )
        # DNP3_LOCAL_ADDRESS is the old name for the same setting: the master's
        # own link address. It is still honoured on its own, but two different
        # values cannot both be right.
        local_raw = env.get("DNP3_LOCAL_ADDRESS")
        if local_raw is not None and local_raw.strip():
            local_address = parse_int(
                "DNP3_LOCAL_ADDRESS", local_raw, master_address, minimum=0, maximum=MAX_LINK_ADDRESS
            )
            master_raw = env.get("DNP3_MASTER_ADDRESS")
            if master_raw is None or not master_raw.strip():
                master_address = local_address
            elif local_address != master_address:
                raise ConfigError(
                    f"DNP3_LOCAL_ADDRESS={local_address} conflicts with DNP3_MASTER_ADDRESS={master_address}; "
                    "both name the master's own link address, so set only DNP3_MASTER_ADDRESS"
                )

        timeout_ms = parse_int("DNP3_TIMEOUT", env.get("DNP3_TIMEOUT"), 5000, minimum=1, maximum=600_000)
        max_retries = parse_int("DNP3_MAX_RETRIES", env.get("DNP3_MAX_RETRIES"), 3, minimum=0, maximum=10)

        control_mode = (env.get("DNP3_CONTROL_MODE") or "select_before_operate").strip().lower()
        if control_mode not in CONTROL_MODES:
            raise ConfigError(f"DNP3_CONTROL_MODE={control_mode!r} is not one of {', '.join(CONTROL_MODES)}")

        writes_enabled = parse_bool("DNP3_WRITES_ENABLED", env.get("DNP3_WRITES_ENABLED"), False)

        map_raw = env.get("POINT_MAP_FILE")
        point_map_path = Path(map_raw.strip()).expanduser() if map_raw and map_raw.strip() else None

        return cls(
            dnp3=DNP3Config(
                host=host,
                port=port,
                master_address=master_address,
                timeout_ms=timeout_ms,
                max_retries=max_retries,
                control_mode=control_mode,
                writes_enabled=writes_enabled,
            ),
            point_map_path=point_map_path,
        )

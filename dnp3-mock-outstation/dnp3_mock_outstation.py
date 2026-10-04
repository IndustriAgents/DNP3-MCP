"""Mock DNP3 outstation for developing and testing the DNP3 MCP server.

This is a real DNP3 (IEEE 1815-2012) outstation over TCP, built on the
pure-Python dnp3py stack, so the MCP server's master talks to it exactly as it
would to a device. Points:

- 16 binary inputs (index 0-15): value is True for odd indexes. Class 1.
- 8 analog inputs (index 0-7): start at their index and take a random step of
  -1, 0 or +1 every update interval. Class 2 events (deadband 1). Reported as
  32-bit integers (g30v1 / g32v1), which is how dnp3py encodes analogs.
- 8 binary outputs (index 0-7): start OFF. LATCH_ON and LATCH_OFF controls
  (select-before-operate or direct operate) set them. Any other control code
  or index is answered NOT_SUPPORTED. Output status is read with Class 0.

Unsolicited responses are disabled and the outstation does not ask for time
synchronisation. Logs go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import random
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from dnp3.core.enums import ControlCode
from dnp3.core.flags import AnalogQuality, BinaryQuality
from dnp3.database import (
    AnalogInputConfig,
    BinaryInputConfig,
    BinaryOutputConfig,
    Database,
    DatabaseConfig,
    EventBufferConfig,
    EventClass,
)
from dnp3.outstation import (
    CommandResult,
    DefaultCommandHandler,
    Outstation,
    OutstationConfig,
    OutstationTcpRunner,
    UnsolicitedConfig,
)

logger = logging.getLogger("dnp3_mock_outstation")

BINARY_INPUTS = 16
ANALOG_INPUTS = 8
BINARY_OUTPUTS = 8
MAX_LINK_ADDRESS = 0xFFEF
EVENT_BUFFER_SIZE = 1000


@dataclass(frozen=True)
class MockConfig:
    host: str = "127.0.0.1"
    port: int = 20000
    outstation_address: int = 10
    update_interval: float = 1.0
    verbose: bool = False


def build_database() -> Database:
    """The mock's points, all ONLINE, with no events queued at start."""
    buffers = EventBufferConfig(
        max_binary_events=EVENT_BUFFER_SIZE,
        max_analog_events=EVENT_BUFFER_SIZE,
        max_counter_events=EVENT_BUFFER_SIZE,
    )
    db = Database(config=DatabaseConfig(event_buffer_config=buffers))
    for i in range(BINARY_INPUTS):
        db.add_binary_input(
            i, BinaryInputConfig(event_class=EventClass.CLASS_1), value=bool(i % 2), quality=BinaryQuality.ONLINE
        )
    for i in range(ANALOG_INPUTS):
        point = db.add_analog_input(
            i,
            AnalogInputConfig(event_class=EventClass.CLASS_2, deadband=1.0),
            value=float(i),
            quality=AnalogQuality.ONLINE,
        )
        point.last_event_value = point.value
    for i in range(BINARY_OUTPUTS):
        # No output events: dnp3py would report them as binary input events.
        db.add_binary_output(
            i, BinaryOutputConfig(event_class=EventClass.NONE), value=False, quality=BinaryQuality.ONLINE
        )
    return db


class MockCommandHandler(DefaultCommandHandler):
    """Accepts LATCH_ON / LATCH_OFF on existing binary outputs; refuses everything else."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def _check(self, index: int, code: ControlCode) -> CommandResult:
        if index not in self.database.binary_outputs:
            return CommandResult.not_supported(f"no binary output {index}")
        if code not in (ControlCode.LATCH_ON, ControlCode.LATCH_OFF):
            return CommandResult.not_supported(f"control code 0x{int(code):02X} not supported")
        return CommandResult.success()

    def _apply(self, index: int, code: ControlCode) -> CommandResult:
        result = self._check(index, code)
        if result.is_success:
            state = code == ControlCode.LATCH_ON
            self.database.update_binary_output(index, state)
            logger.info("Binary output %d latched %s", index, "ON" if state else "OFF")
        else:
            logger.info("Refused control on binary output %d: %s", index, result.message)
        return result

    def select_binary_output(
        self, index: int, code: ControlCode, count: int, on_time: int, off_time: int
    ) -> CommandResult:
        return self._check(index, code)

    def operate_binary_output(
        self, index: int, code: ControlCode, count: int, on_time: int, off_time: int, select_sequence: int
    ) -> CommandResult:
        return self._apply(index, code)

    def direct_operate_binary_output(
        self, index: int, code: ControlCode, count: int, on_time: int, off_time: int
    ) -> CommandResult:
        return self._apply(index, code)


class DNP3MockOutstation:
    def __init__(self, config: MockConfig, *, rng: random.Random | None = None) -> None:
        self.config = config
        self.database = build_database()
        self.handler = MockCommandHandler(self.database)
        self.outstation = Outstation(
            database=self.database,
            config=OutstationConfig(
                address=config.outstation_address,
                master_address=0,  # accept any master
                unsolicited=UnsolicitedConfig(enabled=False),
                time_sync_required=False,
            ),
            handler=self.handler,
        )
        self.runner = OutstationTcpRunner(outstation=self.outstation, host=config.host, port=config.port)
        self._rng = rng or random.Random()
        self._run_task: asyncio.Task[None] | None = None
        self._update_task: asyncio.Task[None] | None = None

    @property
    def local_address(self) -> tuple[str, int] | None:
        return self.runner.local_address

    async def start(self) -> None:
        """Start listening; returns once the socket is bound."""
        if self._run_task is not None:
            return
        self._run_task = asyncio.create_task(self.runner.run(), name="dnp3-mock-runner")
        while not self.runner.is_running:
            if self._run_task.done():
                self._run_task.result()  # raises the bind error
                raise RuntimeError("outstation runner exited before listening")
            await asyncio.sleep(0.01)
        if self.config.update_interval > 0:
            # Keep a reference so the task is not garbage collected mid-run.
            self._update_task = asyncio.create_task(self._update_loop(), name="dnp3-mock-updates")
        host, port = self.local_address or (self.config.host, self.config.port)
        logger.info(
            "DNP3 mock outstation listening on %s:%d (outstation address %d)",
            host,
            port,
            self.config.outstation_address,
        )

    async def stop(self) -> None:
        if self._update_task is not None:
            self._update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_task
            self._update_task = None
        if self._run_task is not None:
            await self.runner.stop()
            try:
                await asyncio.wait_for(self._run_task, timeout=5)
            except TimeoutError:
                self._run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._run_task
            self._run_task = None

    def step(self) -> None:
        """One random-walk step for every analog input."""
        for index, point in self.database.analog_inputs.items():
            self.database.update_analog_input(index, point.value + self._rng.choice((-1.0, 0.0, 1.0)))
        logger.debug("Analog inputs now %s", [int(p.value) for _, p in sorted(self.database.analog_inputs.items())])

    async def _update_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.update_interval)
            self.step()


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"dnp3-mock-outstation: {key}={raw!r} is not a number") from None


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw, 10)
    except ValueError:
        raise SystemExit(f"dnp3-mock-outstation: {key}={raw!r} is not an integer") from None


def _env_bool(env: Mapping[str, str], key: str) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if raw in {"", "0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    raise SystemExit(f"dnp3-mock-outstation: {key}={env.get(key)!r} is not a boolean")


def parse_args(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> MockConfig:
    """Command line options, with defaults from the environment read at call time."""
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description="Mock DNP3 outstation (DNP3 over TCP).")
    parser.add_argument("--host", default=env.get("MOCK_DNP3_HOST") or "127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=_env_int(env, "MOCK_DNP3_PORT", 20000), help="TCP port")
    parser.add_argument(
        "--outstation-address",
        type=int,
        default=_env_int(env, "MOCK_DNP3_OUTSTATION_ADDRESS", 10),
        help="this outstation's DNP3 link address",
    )
    parser.add_argument(
        "--update-interval",
        type=float,
        default=_env_float(env, "MOCK_DNP3_UPDATE_INTERVAL", 1.0),
        help="seconds between analog random-walk steps (0 disables them)",
    )
    parser.add_argument(
        "--verbose", action="store_true", default=_env_bool(env, "MOCK_DNP3_VERBOSE"), help="log every update"
    )
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error(f"--port must be 0-65535, got {args.port}")
    if not 0 <= args.outstation_address <= MAX_LINK_ADDRESS:
        parser.error(f"--outstation-address must be 0-{MAX_LINK_ADDRESS}, got {args.outstation_address}")
    if args.update_interval < 0:
        parser.error("--update-interval must be >= 0")
    return MockConfig(
        host=args.host,
        port=args.port,
        outstation_address=args.outstation_address,
        update_interval=args.update_interval,
        verbose=args.verbose,
    )


async def run_server(config: MockConfig) -> None:
    mock = DNP3MockOutstation(config)
    await mock.start()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        logger.info("Stopping DNP3 mock outstation")
        await mock.stop()


def main(argv: Sequence[str] | None = None) -> None:
    config = parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if config.verbose:
        logger.setLevel(logging.DEBUG)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_server(config))


if __name__ == "__main__":
    main()

"""A fake ASF02 feeder: only the radio I/O is simulated.

The fake speaks the real wire protocol — compact JSON-RPC frames written to
the write characteristic, responses delivered asynchronously on the notify
characteristic — and reproduces the device behaviors that matter:

- the feeder is locked across every (re)connect; only ``info`` works while
  locked
- a wrong unlock code (or any locked command) yields an error frame with
  code -31400
- the ``feeder.plan`` READ frame is not valid JSON (``{"m":...,"i":N,"p":}``)
  — the device parses it leniently
- ``feeder.log`` may stay completely silent (no response frame)
- a feed sets ``s=1`` until stopped or the (test-configured) duration elapses

Everything above the radio — the asf02 library's framing, error wrapping,
request/response matching, and the integration's coordinator/flows — runs
unmodified.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from asf02.protocol import (
    ERR_DEVICE_LOCKED,
    NOTIFY_CHAR_UUID,
    WRITE_CHAR_UUID,
    unlock_code_for,
)

# Matches the request header in frames that are not valid JSON as a whole
# (the feeder.plan read quirk).
_REQUEST_RE = re.compile(r'"m":"([^"]+)","i":(\d+)')


class _DeviceLocked(Exception):
    """The device answered with the locked error (-31400)."""

    code = ERR_DEVICE_LOCKED
    message = "device is locked"


class _Silent:
    """Marker: the device sends no response frame at all."""


@dataclass
class FakeFeederState:
    """Feeder state the fake device reports and mutates."""

    token: str = "testtoken1"
    firmware: str = "1.19.12"
    hw_version: str = "1.0"
    model: str = "ASF02"
    serial: str = "SNFAKE0001"
    feed_count: int = 0
    battery_raw: int = 522
    uptime_raw: int = 1000
    tz_offset_seconds: int = -25200
    device_epoch: int = field(default_factory=lambda: int(time.time()))
    feeding: bool = False
    log_silent: bool = True
    # When > 0, a feed auto-completes after this many seconds (lets tests
    # exercise wait=True without real motor time).
    feed_duration: float = 0.0
    schedule: list[str] = field(default_factory=lambda: [""] * 10)
    locked: bool = True
    connect_count: int = 0


class FakeFeeder:
    """BleakClient-surface fake of one ASF02 feeder (module docstring)."""

    def __init__(self, address: str, state: FakeFeederState) -> None:
        self.address = address
        self.state = state
        self.is_connected = False
        self._notify_cb: Any | None = None
        self._feed_timer: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------
    # bleak-client surface
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        # The feeder re-locks on every (re)connect (live-verified).
        self.state.locked = True
        self.state.connect_count += 1
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False
        self._notify_cb = None
        self._cancel_feed_timer()
        self.state.locked = True

    async def start_notify(self, char_uuid: str, cb: Any) -> None:
        if char_uuid != NOTIFY_CHAR_UUID:
            raise AssertionError(f"unexpected notify characteristic {char_uuid}")
        self._notify_cb = cb

    async def write_gatt_char(self, char_uuid: str, data: bytes, response: bool) -> None:
        if char_uuid != WRITE_CHAR_UUID:
            raise AssertionError(f"unexpected write characteristic {char_uuid}")
        if not self.is_connected:
            raise ConnectionError("not connected")
        method, req_id, params = self._parse_request(data)
        try:
            outcome = self._dispatch(method, params)
        except _DeviceLocked as err:
            # The device answers locked commands with an error frame.
            outcome = err
        # Deliver on the next loop iteration, like a real radio.
        asyncio.get_running_loop().call_soon(self._deliver, req_id, outcome)

    # ------------------------------------------------------------------
    # device model
    # ------------------------------------------------------------------
    def _parse_request(self, data: bytes) -> tuple[str, int, Any]:
        raw = data.decode("utf-8")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            match = _REQUEST_RE.search(raw)
            if match is None:
                raise ValueError(f"unparseable request: {raw!r}") from None
            return match.group(1), int(match.group(2)), None
        return msg["m"], msg["i"], msg.get("p")

    def _dispatch(self, method: str, params: Any) -> Any:
        s = self.state

        if method == "info":
            return {
                "f": s.firmware,
                "h": s.hw_version,
                "m": s.model,
                "s": s.serial,
                "z": s.tz_offset_seconds,
            }
        if method == "unlock":
            if not isinstance(params, dict) or params.get("s") != unlock_code_for(s.token):
                raise _DeviceLocked()
            s.locked = False
            return {"t": s.device_epoch, "z": s.tz_offset_seconds, "l": 0, "k": 0}
        if method == "bind":
            # bind works while locked (live-verified); overwrites the token.
            s.token = str(params["t"])
            s.tz_offset_seconds = int(params.get("z", 0))
            return {}
        if s.locked:
            raise _DeviceLocked()
        if method == "time.calibration":
            s.device_epoch = int(params["t"])
            s.tz_offset_seconds = int(params["z"])
            return {}
        if method == "feeder.status":
            return {
                "s": 1 if s.feeding else 0,
                "n": s.feed_count,
                "t": s.feed_count,
                "b": s.battery_raw,
                "u": s.uptime_raw,
                "d": 0,
            }
        if method == "feeder.feed":
            if s.feeding:
                raise ValueError("already feeding")
            portions = int(params["n"])
            s.feed_count += portions
            s.feeding = True
            if s.feed_duration > 0:
                self._feed_timer = asyncio.get_running_loop().call_later(
                    s.feed_duration, self._finish_feed
                )
            return True
        if method == "feeder.stop":
            self._cancel_feed_timer()
            s.feeding = False
            return True
        if method == "feeder.plan":
            if params is None:
                return {"p": list(s.schedule)}
            if not isinstance(params, list) or len(params) != 10:
                raise ValueError(f"plan write must be a 10-slot array, got {params!r}")
            s.schedule = [str(slot) for slot in params]
            return {"p": list(s.schedule)}
        if method == "feeder.log":
            if s.log_silent:
                return _Silent()
            return {"d": []}
        raise ValueError(f"unknown method {method!r}")

    def _finish_feed(self) -> None:
        self._feed_timer = None
        self.state.feeding = False

    def _cancel_feed_timer(self) -> None:
        if self._feed_timer is not None:
            self._feed_timer.cancel()
            self._feed_timer = None

    # ------------------------------------------------------------------
    # response delivery
    # ------------------------------------------------------------------
    def _deliver(self, req_id: int, outcome: Any) -> None:
        if isinstance(outcome, _Silent) or self._notify_cb is None:
            return
        if isinstance(outcome, _DeviceLocked):
            frame = json.dumps(
                {"i": req_id, "e": {"c": _DeviceLocked.code, "m": _DeviceLocked.message}}
            )
        elif isinstance(outcome, BaseException):
            # A non-device exception is a bug in the fake; fail loudly.
            raise outcome
        else:
            frame = json.dumps({"i": req_id, "r": outcome})
        self._notify_cb(None, bytearray(frame.encode("utf-8")))

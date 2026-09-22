"""Read H9/H7 state from Sony Hub or the dongle's COM interface."""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import psutil
import serial
from serial.tools import list_ports


@dataclass(frozen=True)
class HeadsetState:
    connected: bool | None = None
    battery: int | None = None
    charging: bool | None = None
    source: str = ""
    error: str | None = None
    port: str | None = None
    updated: float | None = None


def hub_session() -> tuple[int, float] | None:
    """Use process creation time to reject logs from an earlier Hub session."""
    for proc in psutil.process_iter(["name", "create_time"]):
        try:
            if (proc.info["name"] or "").lower() == "inzonehub.exe":
                return proc.pid, proc.info["create_time"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def find_inzone_port() -> str | None:
    matches = [p for p in list_ports.comports()
               if p.vid == 0x054C and p.pid == 0x0E53]
    matches.sort(key=lambda p: ("MI_06" not in p.hwid.upper(), p.device))
    return matches[0].device if matches else None


def read_hub_snapshot(session: tuple[int, float], port: str) -> HeadsetState | None:
    """Read only Hub's known status fields in an isolated, bounded helper."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    helper = root / "hub-snapshot" / "HubSnapshot.exe"
    if not helper.is_file():
        helper = root / "build" / "hub-snapshot" / "HubSnapshot.exe"
    if not helper.is_file():
        return None
    sampled = time.time()
    try:
        result = subprocess.run([str(helper), str(session[0])], capture_output=True,
                                timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
                                check=True)
        value = json.loads(result.stdout)
        if (value.get("pid") != session[0]
                or abs(float(value["started"]) - session[1]) > 0.01
                or value.get("port", "").upper() != port.upper()
                or type(value.get("connected")) is not bool):
            return None
        battery, charging = value.get("battery"), value.get("charging")
        if battery is not None and (type(battery) is not int or not 0 <= battery <= 100):
            return None
        if charging is not None and type(charging) is not bool:
            return None
        if not value["connected"]:
            battery = charging = None
        return HeadsetState(connected=value["connected"], battery=battery, charging=charging,
                            source="Hub", port=port, updated=sampled)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return None


def decode_frames(data: bytes):
    """Validate complete Sony vendor events, tolerating noise/partial packets."""
    for i in range(max(0, len(data) - 2)):
        if data[i:i + 2] != b"\x04\xff":
            continue
        size = 3 + data[i + 2]
        frame = data[i:i + size]
        if size < 13 or len(frame) != size or frame[3:6] != b"\x00\x96\xc3":
            continue
        if sum(frame[3:-1]) & 0xFF == frame[-1]:
            yield frame


def make_query(address: int, event: int, transaction: int) -> bytes:
    body = bytes((0x96, 0xC3, address, event, 1,
                  transaction & 255, transaction >> 8))
    return b"\x01\x00\xfc\x08" + body + bytes((sum(body) & 255,))


def request(port, address: int, event: int, transaction: int,
            timeout: float = 0.7) -> bytes | None:
    port.write(make_query(address, event, transaction))
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        data.extend(port.read(min(port.in_waiting or 1, 4096)))
        for frame in decode_frames(data):
            if (frame[6] == ((address & 15) << 4 | address >> 4)
                    and frame[7] == event and frame[8] == 0x10
                    and int.from_bytes(frame[9:11], "little") == transaction):
                return frame[11:-1]
        if len(data) > 8192:
            del data[:-512]
    return None


class HubLogReader:
    def __init__(self, path: Path):
        self.path = path
        self.session = None
        self.identity = None
        self.offset = 0
        self.pending = b""
        self.state = HeadsetState(source="Hub", error="Waiting for Hub status")
        self.event_time = 0.0
        self.not_before = 0.0

    def reset(self, session):
        self.session = session
        self.identity = None
        self.offset = 0
        self.pending = b""
        self.event_time = 0.0
        self.state = HeadsetState(source="Hub", error="Waiting for Hub status")

    def invalidate(self, since):
        self.not_before = max(self.not_before, since)
        self.reset(None)

    def consume(self, line: bytes, since: float):
        try:
            event = json.loads(line)
            stamp = float(event["timeStamp"]) / 1000
            if stamp < since or stamp < self.event_time or stamp > time.time() + 5:
                return
            model = event.get("serviceInfo", {}).get("targetDeviceModelName", "")
            if not any(name in model for name in ("WH-G900N", "WH-G700")):
                return
            kind = event.get("actionTypeId")
            action = event.get("action", {})
            if kind == 23011:
                self.state = HeadsetState(connected=True, source="Hub", updated=stamp)
            elif kind == 23012:
                self.state = HeadsetState(connected=False, source="Hub", updated=stamp)
            elif kind == 23014 and action.get("item") == "BatteryStatus":
                value = str(action.get("value", ""))
                if value == "Disconnect":
                    self.state = HeadsetState(connected=False, source="Hub", updated=stamp)
                elif value == "Charge":
                    self.state = replace(self.state, connected=True, charging=True,
                                         error=None, updated=stamp)
                elif value.isdigit() and 0 <= int(value) <= 100:
                    self.state = replace(self.state, connected=True, battery=int(value),
                                         charging=False, error=None, updated=stamp)
                else:
                    return
            else:
                return
            self.event_time = stamp
        except (ValueError, TypeError, KeyError, AttributeError):
            return

    def poll(self, session: tuple[int, float]) -> HeadsetState:
        if session != self.session:
            self.reset(session)
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self.identity is not None and (identity != self.identity or stat.st_size < self.offset):
                self.reset(session)
            self.identity = identity
            with self.path.open("rb") as stream:
                # Bound first-read work if Hub has kept a very large history.
                if self.offset == 0 and stat.st_size > 8 * 1024 * 1024:
                    stream.seek(stat.st_size - 8 * 1024 * 1024)
                    stream.readline()
                else:
                    stream.seek(self.offset)
                data = self.pending + stream.read(8 * 1024 * 1024)
                self.offset = stream.tell()
            lines = data.split(b"\n")
            self.pending = lines.pop()
            if len(self.pending) > 65536:
                self.pending = b""
            for line in lines:
                self.consume(line, max(session[1], self.not_before))
            return self.state
        except OSError as exc:
            return HeadsetState(source="Hub", error=f"Hub log unavailable: {exc}")


class HeadsetMonitor:
    def __init__(self):
        self.hub = HubLogReader(Path(os.environ.get("APPDATA", ".")) /
                                "Sony" / "INZONE Hub" / "ActionLog.log")
        self.transaction = 0
        self.battery_at = 0.0
        self.cached = HeadsetState()
        self.snapshot_session = None
        self.snapshot_at = 0.0
        self.snapshot = None

    def poll_hub(self, session, port_name):
        previous = self.cached
        state = self.hub.poll(session)
        if session != self.snapshot_session:
            self.snapshot_session = session
            self.snapshot_at = 0
            self.snapshot = None
        if time.monotonic() >= self.snapshot_at:
            snapshot = read_hub_snapshot(session, port_name)
            self.snapshot_at = time.monotonic() + 3
            if snapshot is not None:
                self.snapshot = snapshot
                # Apply events that arrived during the snapshot read as well.
                state = self.hub.poll(session)
        if (self.snapshot is not None and self.snapshot.updated is not None
                and self.snapshot.port == port_name
                and (state.updated is None or self.snapshot.updated >= state.updated)):
            if 0 <= time.time() - self.snapshot.updated <= 6:
                state = self.snapshot
            else:
                # Do not resurrect an older log state after a newer snapshot
                # expires (e.g. old ON event after a confirmed OFF snapshot).
                state = HeadsetState(source="Hub", error="Waiting for current Hub status")
        if (state.connected is None and previous.source == "COM"
                and previous.port == port_name and previous.updated is not None
                and 0 <= time.time() - previous.updated <= 5):
            # Preserve the last confirmed reading briefly during Hub startup.
            # Do not seed it as an indefinitely valid Hub observation.
            return previous
        self.cached = replace(state, port=port_name)
        return self.cached

    def next_transaction(self):
        self.transaction = self.transaction % 65535 + 1
        return self.transaction

    def poll(self) -> HeadsetState:
        port_name = find_inzone_port()
        if port_name is None:
            self.hub.invalidate(time.time())
            self.cached = HeadsetState(connected=False, source="USB", error="Dongle not found")
            self.battery_at = 0
            self.snapshot_session = None
            self.snapshot_at = 0
            return self.cached
        session = hub_session()
        if session:
            self.battery_at = 0
            return self.poll_hub(session, port_name)
        self.hub.reset(None)
        self.snapshot_session = None
        try:
            with serial.Serial(port_name, 460800, timeout=0.05, write_timeout=0.3,
                               rtscts=False, dsrdtr=False) as port:
                port.dtr = True
                port.rts = True
                status = request(port, 0x21, 1, self.next_transaction())
                if status not in (b"\x00", b"\x01"):
                    return HeadsetState(source="COM", port=port_name,
                                        error="No valid connection response")
                if status == b"\x00":
                    self.battery_at = 0
                    self.cached = HeadsetState(connected=False, source="COM", port=port_name,
                                               updated=time.time())
                    return self.cached
                state = HeadsetState(connected=True, source="COM", port=port_name,
                                     battery=self.cached.battery, charging=self.cached.charging,
                                     updated=time.time())
                if time.monotonic() - self.battery_at >= 30 or state.battery is None:
                    payload = request(port, 0x41, 4, self.next_transaction())
                    if payload is not None and len(payload) == 2 and payload[0] in (0, 1) and payload[1] <= 100:
                        state = replace(state, charging=bool(payload[0]), battery=payload[1])
                        self.battery_at = time.monotonic()
                self.cached = state
                return state
        except (OSError, serial.SerialException) as exc:
            # Hub may start between process detection and opening the port.
            session = hub_session()
            if session:
                return self.poll_hub(session, port_name)
            return HeadsetState(source="COM", port=port_name, error=f"COM unavailable: {exc}")

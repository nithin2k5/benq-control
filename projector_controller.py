"""RS232 communication with a BenQ projector.

This module has no Tkinter dependency. It provides:

* the BenQ ASCII power commands and their exact wire encoding,
* a byte buffer that reassembles fragmented replies into messages,
* classification of replies (echo, power state, projector errors),
* a pySerial transport and a simulated transport with the same interface,
* ``ProjectorController``, which owns a single worker thread that performs
  all serial I/O in order and reports results through a thread-safe queue.

The commands are the standard BenQ RS232 commands. Whether a particular
projector (for example the MX528) accepts them must be verified on the
hardware itself; nothing here assumes a model-specific reply format beyond
the standard ``*POW=ON#`` / ``*POW=OFF#`` status replies.
"""

from __future__ import annotations

import enum
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

BAUD_RATES = (9600, 14400, 19200, 38400, 57600, 115200)
DEFAULT_BAUD_RATE = 115200

SERIAL_FORMAT_DESCRIPTION = "8 data bits, no parity, 1 stop bit, no flow control"

TROUBLESHOOTING_HINT = (
    "Troubleshooting: (1) confirm the baud rate matches the projector's menu "
    "setting; (2) use a true RS232 adapter (not USB-to-TTL); (3) check the "
    "cable pinout - a null-modem/crossover cable or adapter may be required; "
    "(4) make sure the projector has mains power - some models only answer "
    "RS232 in standby if a standby setting in their menu allows it; (5) try "
    "the status query again."
)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """A BenQ ASCII command such as ``*pow=?#``."""

    name: str
    text: str
    is_query: bool

    @property
    def payload(self) -> bytes:
        """Bytes sent on the wire: CR, the command, CR."""
        return b"\r" + self.text.encode("ascii") + b"\r"


POWER_ON = Command("Power ON", "*pow=on#", is_query=False)
POWER_OFF = Command("Power OFF", "*pow=off#", is_query=False)
POWER_STATUS = Command("Power status query", "*pow=?#", is_query=True)


def format_bytes(data: bytes) -> str:
    """Render raw bytes for the log with CR/LF and non-printables escaped."""
    out = []
    for byte in data:
        if byte == 0x0D:
            out.append("\\r")
        elif byte == 0x0A:
            out.append("\\n")
        elif byte == 0x5C:
            out.append("\\\\")
        elif 0x20 <= byte < 0x7F:
            out.append(chr(byte))
        else:
            out.append(f"\\x{byte:02x}")
    return "".join(out)


# ---------------------------------------------------------------------------
# Reply buffering and classification
# ---------------------------------------------------------------------------


class MessageBuffer:
    """Reassembles a byte stream into messages.

    A message ends at CR, LF or ``#`` (the ``#`` is kept as part of the
    message). Empty segments produced by consecutive delimiters are dropped,
    and an unterminated tail is kept until more bytes arrive or ``flush`` is
    called.
    """

    def __init__(self) -> None:
        self._pending = bytearray()

    def feed(self, data: bytes) -> list[str]:
        self._pending.extend(data)
        messages = []
        start = 0
        for index, byte in enumerate(self._pending):
            if byte in (0x0D, 0x0A):
                segment = self._pending[start:index]
            elif byte == 0x23:  # '#'
                segment = self._pending[start : index + 1]
            else:
                continue
            start = index + 1
            text = _decode(segment).strip()
            if text:
                messages.append(text)
        del self._pending[:start]
        return messages

    def flush(self) -> Optional[str]:
        """Return and clear any unterminated tail, or None if empty."""
        text = _decode(self._pending).strip()
        self._pending.clear()
        return text or None

    @property
    def pending(self) -> bytes:
        return bytes(self._pending)


def _decode(data: bytes | bytearray) -> str:
    return bytes(data).decode("ascii", errors="backslashreplace")


class ReplyKind(enum.Enum):
    ECHO = "command echo or acknowledgement (not proof of projector state)"
    POWER_ON = "power status: ON"
    POWER_OFF = "power status: OFF"
    ILLEGAL_FORMAT = "projector error: Illegal format (command not understood)"
    UNSUPPORTED_ITEM = "projector error: Unsupported item (command not supported)"
    BLOCK_ITEM = (
        "projector error: Block item (command refused right now, e.g. while "
        "warming up or cooling down)"
    )
    PROMPT = "prompt character"
    UNRECOGNIZED = "unrecognized reply (meaning unknown)"


ERROR_KINDS = frozenset(
    {ReplyKind.ILLEGAL_FORMAT, ReplyKind.UNSUPPORTED_ITEM, ReplyKind.BLOCK_ITEM}
)

_POWER_REPLY = re.compile(r"^\*?\s*pow\s*=\s*(on|off)\s*#?$", re.IGNORECASE)
_ERROR_PATTERNS = (
    ("illegal format", ReplyKind.ILLEGAL_FORMAT),
    ("unsupported item", ReplyKind.UNSUPPORTED_ITEM),
    ("block item", ReplyKind.BLOCK_ITEM),
)


@dataclass(frozen=True)
class Reply:
    raw: str
    kind: ReplyKind

    @property
    def is_error(self) -> bool:
        return self.kind in ERROR_KINDS


def classify_reply(message: str, command: Command) -> Reply:
    """Classify one received message in the context of the command sent.

    A power state is only reported for replies to the status query. Any
    ``POW=...`` text received after a power ON/OFF command is treated as an
    echo or acknowledgement because it cannot be told apart from the echoed
    command and does not prove the projector's actual state.
    """
    normalized = message.strip().lstrip(">").strip()
    lowered = normalized.lower()

    if not normalized:
        return Reply(message, ReplyKind.PROMPT)
    if _squash(lowered) == _squash(command.text.lower()):
        return Reply(message, ReplyKind.ECHO)
    for needle, kind in _ERROR_PATTERNS:
        if needle in lowered:
            return Reply(message, kind)

    match = _POWER_REPLY.match(normalized)
    if match:
        if not command.is_query:
            return Reply(message, ReplyKind.ECHO)
        state = match.group(1).lower()
        return Reply(message, ReplyKind.POWER_ON if state == "on" else ReplyKind.POWER_OFF)
    return Reply(message, ReplyKind.UNRECOGNIZED)


def _squash(text: str) -> str:
    return "".join(text.split())


class PowerState(enum.Enum):
    ON = "On"
    OFF = "Off"
    UNKNOWN = "Unknown"


@dataclass
class Transaction:
    """Everything received in response to one command."""

    command: Command
    raw: bytes = b""
    fragments: int = 0
    replies: list[Reply] = field(default_factory=list)
    incomplete_tail: bool = False

    @property
    def meaningful_replies(self) -> list[Reply]:
        return [r for r in self.replies if r.kind is not ReplyKind.PROMPT]

    @property
    def error_reply(self) -> Optional[Reply]:
        return next((r for r in self.replies if r.is_error), None)

    @property
    def power_state(self) -> Optional[PowerState]:
        """Confirmed power state, or None unless exactly one state was reported."""
        if not self.command.is_query or self.error_reply is not None:
            return None
        states = {
            r.kind
            for r in self.replies
            if r.kind in (ReplyKind.POWER_ON, ReplyKind.POWER_OFF)
        }
        if states == {ReplyKind.POWER_ON}:
            return PowerState.ON
        if states == {ReplyKind.POWER_OFF}:
            return PowerState.OFF
        return None

    @property
    def echo_only(self) -> bool:
        replies = self.meaningful_replies
        return bool(replies) and all(r.kind is ReplyKind.ECHO for r in replies)

    @property
    def has_unrecognized(self) -> bool:
        return any(r.kind is ReplyKind.UNRECOGNIZED for r in self.replies)

    def is_conclusive(self) -> bool:
        """True once a reply arrived that ends the wait early."""
        if self.error_reply is not None:
            return True
        if self.command.is_query:
            return any(
                r.kind in (ReplyKind.POWER_ON, ReplyKind.POWER_OFF) for r in self.replies
            )
        return False


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class TransportError(Exception):
    """The port could not be opened or the connection was lost."""


class TransportTimeout(Exception):
    """A write did not complete within the write timeout."""


class Transport:
    """Byte-level link to a projector. Used only from the worker thread."""

    simulated = False
    description = "transport"

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self) -> bytes:
        """Return available bytes, waiting at most the read timeout.

        Returns ``b""`` when nothing arrived.
        """
        raise NotImplementedError


def _import_serial():
    try:
        import serial  # noqa: PLC0415 - imported lazily so tests need only stdlib
    except ImportError as exc:
        raise RuntimeError(
            "pySerial is not installed. Install it with: "
            "python -m pip install -r requirements.txt"
        ) from exc
    return serial


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str

    @property
    def label(self) -> str:
        desc = self.description.strip()
        if not desc or desc.lower() == "n/a" or desc == self.device:
            return self.device
        return f"{self.device} — {desc}"


def list_serial_ports() -> list[PortInfo]:
    """List serial ports known to the OS (COM ports, /dev/tty.*, /dev/ttyUSB*, ...)."""
    _import_serial()
    from serial.tools import list_ports  # noqa: PLC0415

    ports = [PortInfo(p.device, p.description or "") for p in list_ports.comports()]
    return sorted(ports, key=lambda p: p.device)


def describe_open_error(port: str, exc: BaseException) -> str:
    """Turn a pySerial/OS error from opening a port into actionable text."""
    text = str(exc)
    lowered = text.lower()
    if "busy" in lowered or "errno 16" in lowered or "exclusively lock" in lowered:
        return (
            f"Port {port} is already in use by another program. Close other "
            f"serial terminals or apps using it and try again. ({text})"
        )
    if isinstance(exc, PermissionError) or "permission" in lowered or "access is denied" in lowered:
        return (
            f"Permission denied opening {port}. It may be in use by another "
            "program (on Windows this is the usual cause), or your user may lack "
            "access (on Linux add yourself to the 'dialout' group, then log out "
            f"and back in). ({text})"
        )
    if (
        isinstance(exc, FileNotFoundError)
        or "no such file" in lowered
        or "filenotfounderror" in lowered
        or "cannot find the file" in lowered
        or "does not exist" in lowered
    ):
        return (
            f"Port {port} was not found. The adapter may be unplugged or the "
            f"port name may have changed - click Refresh. ({text})"
        )
    return f"Could not open {port}: {text}"


class SerialTransport(Transport):
    """pySerial-backed transport using 8N1 with no flow control."""

    def __init__(
        self,
        port: str,
        baudrate: int,
        read_timeout: float = 0.1,
        write_timeout: float = 1.0,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self.description = f"{port} @ {baudrate} baud"
        self._serial = None
        self._serial_module = None

    def open(self) -> None:
        try:
            serial = _import_serial()
        except RuntimeError as exc:
            raise TransportError(str(exc)) from exc
        self._serial_module = serial
        kwargs = dict(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.read_timeout,
            write_timeout=self.write_timeout,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        if os.name == "posix":
            # Fail instead of silently sharing a port another program holds.
            kwargs["exclusive"] = True
        try:
            self._serial = serial.Serial(**kwargs)
        except (serial.SerialException, OSError, ValueError) as exc:
            raise TransportError(describe_open_error(self.port, exc)) from exc

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def write(self, data: bytes) -> None:
        serial = self._serial_module
        if self._serial is None:
            raise TransportError("Serial port is not open.")
        try:
            self._serial.write(data)
        except serial.SerialTimeoutException as exc:
            raise TransportTimeout(
                f"Write to {self.port} timed out after {self.write_timeout:g} s."
            ) from exc
        except (serial.SerialException, OSError) as exc:
            raise TransportError(
                f"Serial error while writing to {self.port} (adapter unplugged?): {exc}"
            ) from exc

    def read(self) -> bytes:
        serial = self._serial_module
        if self._serial is None:
            raise TransportError("Serial port is not open.")
        try:
            waiting = self._serial.in_waiting
            return self._serial.read(waiting or 1)
        except (serial.SerialException, OSError) as exc:
            raise TransportError(
                f"Serial error while reading from {self.port} (adapter unplugged?): {exc}"
            ) from exc


class SimulationScenario(enum.Enum):
    NORMAL = "Normal replies"
    DELAYED_FRAGMENTED = "Delayed, fragmented replies"
    NO_RESPONSE = "No response"
    UNSUPPORTED = "Unsupported commands"
    CONNECTION_FAILURE = "Connection failure"


class SimulatedTransport(Transport):
    """A fake projector for use without hardware. Its replies are not real.

    The simulated projector echoes each command, then replies. After a power
    OFF it "cools down" for a while, during which power ON is refused with
    ``Block item``.
    """

    simulated = True

    def __init__(
        self,
        scenario: SimulationScenario = SimulationScenario.NORMAL,
        *,
        initial_power_on: bool = False,
        read_timeout: float = 0.1,
        time_scale: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.scenario = scenario
        self.description = f"Simulated projector ({scenario.value})"
        self.read_timeout = read_timeout
        self.time_scale = time_scale
        self._clock = clock
        self._power_on = initial_power_on
        self._cooling_until = 0.0
        self._incoming = MessageBuffer()
        self._pending: list[tuple[float, bytes]] = []
        self._lock = threading.Lock()
        self._open = False

    def open(self) -> None:
        if self.scenario is SimulationScenario.CONNECTION_FAILURE:
            raise TransportError(
                "[SIMULATED] Connection failure: the simulated port could not be opened."
            )
        self._open = True

    def close(self) -> None:
        with self._lock:
            self._open = False
            self._pending.clear()

    def write(self, data: bytes) -> None:
        with self._lock:
            if not self._open:
                raise TransportError("[SIMULATED] Port is not open.")
            now = self._clock()
            for message in self._incoming.feed(data):
                self._schedule(now, self._respond(message, now))

    def read(self) -> bytes:
        deadline = self._clock() + self.read_timeout
        while True:
            with self._lock:
                if not self._open:
                    raise TransportError("[SIMULATED] Port is not open.")
                now = self._clock()
                ready = [chunk for due, chunk in self._pending if due <= now]
                self._pending = [(due, chunk) for due, chunk in self._pending if due > now]
            if ready:
                return b"".join(ready)
            if now >= deadline:
                return b""
            time.sleep(0.005)

    def _respond(self, message: str, now: float) -> bytes:
        if self.scenario is SimulationScenario.NO_RESPONSE:
            return b""
        command = _squash(message.lower())
        if self.scenario is SimulationScenario.UNSUPPORTED:
            reply = "*Unsupported item#"
        elif command == "*pow=?#":
            reply = "*POW=ON#" if self._power_on else "*POW=OFF#"
        elif command == "*pow=on#":
            if now < self._cooling_until:
                reply = "*Block item#"
            else:
                self._power_on = True
                reply = "*POW=ON#"
        elif command == "*pow=off#":
            if self._power_on:
                self._power_on = False
                self._cooling_until = now + 10.0 * self.time_scale
            reply = "*POW=OFF#"
        else:
            reply = "*Illegal format#"
        return b">" + message.encode("ascii", "replace") + b"\r\n" + reply.encode("ascii") + b"\r\n"

    def _schedule(self, now: float, data: bytes) -> None:
        if not data:
            return
        if self.scenario is SimulationScenario.DELAYED_FRAGMENTED:
            data = b"\r\n\r\n" + data  # leading empty delimiters
            due = now + 1.2 * self.time_scale
            for start in range(0, len(data), 3):
                self._pending.append((due, data[start : start + 3]))
                due += 0.08 * self.time_scale
        else:
            self._pending.append((now + 0.05 * self.time_scale, data))


# ---------------------------------------------------------------------------
# Controller and events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogEvent:
    timestamp: datetime
    category: str  # "TX", "RX", "INFO", "WARN" or "ERROR"
    text: str
    simulated: bool


class ConnectionChange(enum.Enum):
    CONNECTED = "connected"
    CONNECT_FAILED = "connect failed"
    DISCONNECTED = "disconnected"
    LOST = "connection lost"


@dataclass(frozen=True)
class ConnectionEvent:
    change: ConnectionChange
    description: str
    simulated: bool
    error: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self.change is ConnectionChange.CONNECTED


@dataclass(frozen=True)
class PowerStatusEvent:
    state: PowerState
    detail: str
    simulated: bool
    confirmed: bool = False


@dataclass(frozen=True)
class BusyEvent:
    busy: bool
    activity: str = ""


@dataclass
class ControllerTiming:
    response_timeout: float = 2.0  # max wait for a reply to one command
    # After a power command, stop waiting once replies have been quiet this long.
    reply_quiet_period: float = 0.8
    post_power_on_delay: float = 5.0
    post_power_off_delay: float = 3.0
    status_attempts: int = 3  # status queries after a power command
    manual_status_attempts: int = 2  # status queries for "Check Power Status"
    status_retry_interval: float = 2.0


class ProjectorController:
    """Runs all projector I/O on one worker thread.

    Public methods may be called from any thread; they only enqueue jobs.
    Results are delivered as event objects on ``self.events`` (a
    ``queue.Queue``), which the GUI drains on its own thread.
    """

    def __init__(self, timing: Optional[ControllerTiming] = None) -> None:
        self.timing = timing or ControllerTiming()
        self.events: "queue.Queue[object]" = queue.Queue()
        self._jobs: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._cancel = threading.Event()
        self._busy_lock = threading.Lock()
        self._busy = False
        self._transport: Optional[Transport] = None
        self._simulated = False
        self._thread = threading.Thread(target=self._run, name="serial-worker", daemon=True)
        self._thread.start()

    # -- public API -------------------------------------------------------

    @property
    def busy(self) -> bool:
        with self._busy_lock:
            return self._busy

    def connect(self, transport: Transport) -> bool:
        return self._submit("connect", transport, "Connecting")

    def query_power(self) -> bool:
        return self._submit("query", None, "Checking power status")

    def power_on(self) -> bool:
        return self._submit("power", POWER_ON, "Turning on")

    def power_off(self) -> bool:
        return self._submit("power", POWER_OFF, "Turning off")

    def disconnect(self) -> None:
        """Cancel any pending command and close the port."""
        self._cancel.set()
        self._jobs.put(("disconnect", None))

    def shutdown(self, timeout: float = 3.0) -> None:
        """Stop the worker and close the port. Never sends power commands."""
        self._cancel.set()
        self._jobs.put(("stop", None))
        self._thread.join(timeout)

    # -- worker -----------------------------------------------------------

    def _submit(self, kind: str, arg: object, activity: str) -> bool:
        with self._busy_lock:
            if self._busy:
                return False
            self._busy = True
        self._emit(BusyEvent(True, activity))
        self._jobs.put((kind, arg))
        return True

    def _run(self) -> None:
        while True:
            kind, arg = self._jobs.get()
            try:
                if kind == "stop":
                    self._close_transport(ConnectionChange.DISCONNECTED, "Port closed (application exiting).")
                    return
                if kind == "disconnect":
                    self._close_transport(ConnectionChange.DISCONNECTED, "Disconnected.")
                    self._cancel.clear()
                elif kind == "connect":
                    self._do_connect(arg)
                elif kind == "query":
                    self._do_status_check(self.timing.manual_status_attempts)
                elif kind == "power":
                    self._do_power(arg)
            except Exception as exc:  # keep the worker alive on unexpected bugs
                self._log("ERROR", f"Unexpected internal error: {exc!r}")
            finally:
                if kind in ("connect", "query", "power"):
                    with self._busy_lock:
                        self._busy = False
                    self._emit(BusyEvent(False))

    def _do_connect(self, transport: Transport) -> None:
        if self._transport is not None:
            self._close_transport(ConnectionChange.DISCONNECTED, "Closed previous connection.")
        self._cancel.clear()
        self._simulated = transport.simulated
        if transport.simulated:
            self._log("INFO", "SIMULATION MODE: no hardware is used; replies are simulated.")
        self._log("INFO", f"Opening {transport.description} ({SERIAL_FORMAT_DESCRIPTION})...")
        try:
            transport.open()
        except TransportError as exc:
            self._fail_connect(transport, str(exc))
            return
        except Exception as exc:
            self._fail_connect(transport, f"Could not open {transport.description}: {exc}")
            return
        self._transport = transport
        label = "Simulated port connected." if transport.simulated else "Serial port connected."
        self._log(
            "INFO",
            f"{label} This does not mean the projector has responded; "
            "use Check Power Status to test communication.",
        )
        self._emit(ConnectionEvent(ConnectionChange.CONNECTED, transport.description, transport.simulated))
        self._emit(PowerStatusEvent(PowerState.UNKNOWN, "Not queried yet.", transport.simulated))

    def _fail_connect(self, transport: Transport, message: str) -> None:
        self._log("ERROR", message)
        self._emit(
            ConnectionEvent(
                ConnectionChange.CONNECT_FAILED, transport.description, transport.simulated, message
            )
        )

    def _close_transport(self, change: ConnectionChange, message: str, error: Optional[str] = None) -> None:
        transport = self._transport
        if transport is None:
            return
        self._transport = None
        try:
            transport.close()
        except Exception as exc:
            self._log("WARN", f"Error while closing port: {exc}")
        self._log("ERROR" if error else "INFO", message)
        self._emit(ConnectionEvent(change, transport.description, transport.simulated, error))
        self._emit(PowerStatusEvent(PowerState.UNKNOWN, "Not connected.", transport.simulated))

    def _do_power(self, command: Command) -> None:
        tx = self._transact(command)
        if tx is None:
            return
        error = tx.error_reply
        if error is not None:
            self._log("WARN", f"Projector rejected {command.name}: '{error.raw}' - {error.kind.value}.")
        elif not tx.meaningful_replies:
            self._log(
                "INFO",
                f"No reply to {command.name}. A missing reply does not prove the "
                "command failed; the power status will be queried.",
            )
        else:
            self._log(
                "INFO",
                "Reply received, but an echo/acknowledgement does not confirm the "
                "power state; the power status will be queried.",
            )
        if command is POWER_OFF:
            self._log(
                "INFO",
                "Shutdown puts the projector into standby. Its cooling fan may keep "
                "running for a while; do not disconnect mains power during cooling.",
            )
        self._emit(
            PowerStatusEvent(
                PowerState.UNKNOWN,
                f"{command.name} sent - waiting to confirm with a status query.",
                self._simulated,
            )
        )
        delay = (
            self.timing.post_power_on_delay
            if command is POWER_ON
            else self.timing.post_power_off_delay
        )
        self._log("INFO", f"Waiting {delay:g} s before querying power status.")
        if not self._wait(delay):
            return
        self._do_status_check(self.timing.status_attempts)

    def _do_status_check(self, attempts: int) -> None:
        detail = "No response / status unknown."
        saw_reply = False
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._log("INFO", f"Retrying power status query (attempt {attempt} of {attempts}).")
                if not self._wait(self.timing.status_retry_interval):
                    return
            tx = self._transact(POWER_STATUS)
            if tx is None:
                return
            state = tx.power_state
            if state is not None:
                raw = next(r.raw for r in tx.replies if r.kind in (ReplyKind.POWER_ON, ReplyKind.POWER_OFF))
                self._log("INFO", f"Power status confirmed: {state.value.upper()} (reply '{raw}').")
                self._emit(
                    PowerStatusEvent(state, f"Confirmed by projector reply '{raw}'.", self._simulated, True)
                )
                return
            error = tx.error_reply
            if error is not None:
                saw_reply = True
                detail = f"Projector replied '{error.raw}' - {error.kind.value}. Status unknown."
                if error.kind is not ReplyKind.BLOCK_ITEM:
                    break  # a format/support error will not change on retry
            elif tx.has_unrecognized:
                saw_reply = True
                raw = ", ".join(f"'{r.raw}'" for r in tx.replies if r.kind is ReplyKind.UNRECOGNIZED)
                detail = f"Unrecognized reply {raw}. Status unknown."
            elif tx.echo_only:
                detail = (
                    "Only the command echo was received - status unknown. An echo "
                    "alone can come from a loopback or the adapter, not the projector."
                )
        self._log("WARN", detail)
        if not saw_reply:
            self._log("WARN", TROUBLESHOOTING_HINT)
        self._emit(PowerStatusEvent(PowerState.UNKNOWN, detail, self._simulated))

    def _transact(self, command: Command) -> Optional[Transaction]:
        """Send one command and collect its replies.

        Returns None when the command was cancelled or could not be sent.
        """
        transport = self._transport
        if transport is None:
            self._log("ERROR", f"Cannot send {command.name}: not connected.")
            return None
        tx = Transaction(command)
        buffer = MessageBuffer()
        try:
            stale = self._drain(transport)
            if stale:
                self._log("RX", f"Unsolicited data before command (raw): {format_bytes(stale)}")
            self._log("TX", f"{command.name}: {format_bytes(command.payload)}")
            transport.write(command.payload)

            deadline = time.monotonic() + self.timing.response_timeout
            last_reply_at: Optional[float] = None
            while True:
                now = time.monotonic()
                if now >= deadline:
                    break
                if (
                    not command.is_query
                    and last_reply_at is not None
                    and now - last_reply_at >= self.timing.reply_quiet_period
                ):
                    break
                if self._cancel.is_set():
                    self._log("INFO", f"{command.name} cancelled.")
                    return None
                chunk = transport.read()
                if not chunk:
                    continue
                tx.raw += chunk
                tx.fragments += 1
                for message in buffer.feed(chunk):
                    tx.replies.append(classify_reply(message, command))
                    last_reply_at = time.monotonic()
                if tx.is_conclusive():
                    break
        except TransportTimeout as exc:
            self._log("ERROR", f"{exc} The adapter may be faulty or blocked.")
            self._emit(PowerStatusEvent(PowerState.UNKNOWN, "Write timed out - status unknown.", self._simulated))
            return None
        except TransportError as exc:
            self._close_transport(ConnectionChange.LOST, f"Connection lost: {exc}", str(exc))
            return None

        tail = buffer.flush()
        if tail is not None:
            tx.replies.append(classify_reply(tail, command))
            tx.incomplete_tail = True
        self._log_transaction(tx)
        return tx

    def _drain(self, transport: Transport) -> bytes:
        data = b""
        for _ in range(10):
            chunk = transport.read()
            if not chunk:
                break
            data += chunk
        return data

    def _log_transaction(self, tx: Transaction) -> None:
        if not tx.raw:
            self._log("RX", f"No reply within {self.timing.response_timeout:g} s.")
            return
        plural = "s" if tx.fragments != 1 else ""
        self._log("RX", f"Raw ({tx.fragments} fragment{plural}): {format_bytes(tx.raw)}")
        for reply in tx.replies:
            if reply.kind is ReplyKind.PROMPT:
                continue
            self._log("RX", f"'{reply.raw}' -> {reply.kind.value}")
        if tx.incomplete_tail:
            self._log("WARN", "The last reply had no terminator; it may be incomplete.")

    def _wait(self, seconds: float) -> bool:
        """Sleep unless cancelled. Returns False if cancelled."""
        if self._cancel.wait(seconds):
            self._log("INFO", "Pending operation cancelled.")
            return False
        return True

    def _log(self, category: str, text: str) -> None:
        self._emit(LogEvent(datetime.now(), category, text, self._simulated))

    def _emit(self, event: object) -> None:
        self.events.put(event)

"""Tests for the worker thread, command serialization, retries and timeouts.

All tests use simulated transports; no serial hardware is required.
"""

import queue
import threading
import time
import unittest

from projector_controller import (
    POWER_OFF,
    POWER_ON,
    POWER_STATUS,
    BusyEvent,
    ConnectionChange,
    ConnectionEvent,
    ControllerTiming,
    LogEvent,
    PowerState,
    PowerStatusEvent,
    ProjectorController,
    SimulatedTransport,
    SimulationScenario,
    Transport,
    TransportError,
    TransportTimeout,
)

FAST = ControllerTiming(
    response_timeout=0.5,
    reply_quiet_period=0.1,
    post_power_on_delay=0.05,
    post_power_off_delay=0.05,
    status_attempts=3,
    manual_status_attempts=2,
    status_retry_interval=0.05,
)


class ScriptedTransport(Transport):
    """Replies to each written payload with a scripted list of (delay, bytes)."""

    def __init__(self, script=None, *, open_error=None, read_error_after=None, write_error=None):
        self.script = script or {}
        self.open_error = open_error
        self.read_error_after = read_error_after
        self.write_error = write_error
        self.description = "scripted"
        self.writes = []
        self.is_open = False
        self.closed = False
        self._pending = []
        self._reads = 0
        self._lock = threading.Lock()

    def open(self):
        if self.open_error:
            raise self.open_error
        self.is_open = True

    def close(self):
        self.is_open = False
        self.closed = True

    def write(self, data):
        if self.write_error:
            raise self.write_error
        now = time.monotonic()
        with self._lock:
            self.writes.append(data)
            responses = self.script.get(data, [])
            if callable(responses):
                responses = responses(len(self.writes))
            for delay, chunk in responses:
                self._pending.append((now + delay, chunk))

    def read(self):
        self._reads += 1
        if self.read_error_after is not None and self._reads > self.read_error_after:
            raise TransportError("device disconnected")
        deadline = time.monotonic() + 0.02
        while time.monotonic() < deadline:
            with self._lock:
                now = time.monotonic()
                ready = [c for due, c in self._pending if due <= now]
                self._pending = [(due, c) for due, c in self._pending if due > now]
            if ready:
                return b"".join(ready)
            time.sleep(0.002)
        return b""


class ControllerTestCase(unittest.TestCase):
    def setUp(self):
        self.controller = ProjectorController(FAST)

    def tearDown(self):
        self.controller.shutdown()

    def wait_idle(self, timeout=5.0):
        """Collect events until the current job reports it is no longer busy."""
        events = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.controller.events.get(timeout=0.05)
            except queue.Empty:
                continue
            events.append(event)
            if isinstance(event, BusyEvent) and not event.busy:
                return events
        self.fail("controller did not become idle")

    def connect(self, transport):
        self.assertTrue(self.controller.connect(transport))
        return self.wait_idle()

    @staticmethod
    def of_type(events, cls):
        return [e for e in events if isinstance(e, cls)]

    def last_power(self, events):
        return self.of_type(events, PowerStatusEvent)[-1]


class ConnectionTests(ControllerTestCase):
    def test_connect_sends_nothing(self):
        transport = ScriptedTransport()
        events = self.connect(transport)
        self.assertEqual(transport.writes, [])
        self.assertTrue(self.of_type(events, ConnectionEvent)[-1].connected)
        self.assertIs(self.last_power(events).state, PowerState.UNKNOWN)
        texts = " ".join(e.text for e in self.of_type(events, LogEvent))
        self.assertIn("Serial port connected.", texts)

    def test_connect_failure_is_reported(self):
        events = self.connect(ScriptedTransport(open_error=TransportError("Port COM9 was not found.")))
        change = self.of_type(events, ConnectionEvent)[-1]
        self.assertIs(change.change, ConnectionChange.CONNECT_FAILED)
        self.assertIn("COM9", change.error)

    def test_connection_lost_during_read(self):
        transport = ScriptedTransport(read_error_after=2)
        self.connect(transport)
        self.controller.query_power()
        events = self.wait_idle()
        self.assertIs(self.of_type(events, ConnectionEvent)[-1].change, ConnectionChange.LOST)
        self.assertTrue(transport.closed)

    def test_write_timeout_keeps_connection(self):
        transport = ScriptedTransport(write_error=TransportTimeout("Write timed out."))
        self.connect(transport)
        self.controller.query_power()
        events = self.wait_idle()
        self.assertEqual(self.of_type(events, ConnectionEvent), [])
        self.assertIs(self.last_power(events).state, PowerState.UNKNOWN)
        self.assertFalse(transport.closed)

    def test_shutdown_closes_port_without_power_commands(self):
        transport = ScriptedTransport()
        self.connect(transport)
        self.controller.shutdown()
        self.assertTrue(transport.closed)
        self.assertEqual(transport.writes, [])


class StatusQueryTests(ControllerTestCase):
    def test_fragmented_reply_confirms_state(self):
        fragments = [(0.05, b"\r\n>*po"), (0.08, b"w=?#\r"), (0.12, b"\n*POW"), (0.15, b"=ON#\r\n")]
        self.connect(ScriptedTransport({POWER_STATUS.payload: fragments}))
        self.controller.query_power()
        power = self.last_power(self.wait_idle())
        self.assertIs(power.state, PowerState.ON)
        self.assertTrue(power.confirmed)

    def test_no_response_retries_query_then_reports_unknown(self):
        transport = ScriptedTransport()
        self.connect(transport)
        self.controller.query_power()
        events = self.wait_idle()
        self.assertEqual(transport.writes, [POWER_STATUS.payload] * FAST.manual_status_attempts)
        power = self.last_power(events)
        self.assertIs(power.state, PowerState.UNKNOWN)
        self.assertFalse(power.confirmed)
        self.assertIn("No response / status unknown", power.detail)

    def test_reply_timeout_is_bounded(self):
        self.connect(ScriptedTransport())
        started = time.monotonic()
        self.controller.query_power()
        self.wait_idle()
        elapsed = time.monotonic() - started
        budget = FAST.manual_status_attempts * (FAST.response_timeout + FAST.status_retry_interval)
        self.assertLess(elapsed, budget + 1.0)

    def test_echo_only_is_not_a_state(self):
        self.connect(ScriptedTransport({POWER_STATUS.payload: [(0.01, b"*pow=?#\r\n")]}))
        self.controller.query_power()
        power = self.last_power(self.wait_idle())
        self.assertIs(power.state, PowerState.UNKNOWN)
        self.assertIn("echo", power.detail)

    def test_unsupported_item_is_not_retried(self):
        transport = ScriptedTransport({POWER_STATUS.payload: [(0.01, b"*Unsupported item#\r\n")]})
        self.connect(transport)
        self.controller.query_power()
        power = self.last_power(self.wait_idle())
        self.assertEqual(len(transport.writes), 1)
        self.assertIn("Unsupported item", power.detail)

    def test_unrecognized_reply_is_kept_visible(self):
        self.connect(ScriptedTransport({POWER_STATUS.payload: [(0.01, b"*POW=WEIRD#\r\n")]}))
        self.controller.query_power()
        events = self.wait_idle()
        self.assertIs(self.last_power(events).state, PowerState.UNKNOWN)
        log = " ".join(e.text for e in self.of_type(events, LogEvent))
        self.assertIn("*POW=WEIRD#", log)

    def test_retry_succeeds_after_block_item(self):
        def status(write_number):
            if write_number == 1:
                return [(0.01, b"*Block item#\r\n")]
            return [(0.01, b"*POW=OFF#\r\n")]

        transport = ScriptedTransport({POWER_STATUS.payload: status})
        self.connect(transport)
        self.controller.query_power()
        self.assertIs(self.last_power(self.wait_idle()).state, PowerState.OFF)
        self.assertEqual(len(transport.writes), 2)


class PowerCommandTests(ControllerTestCase):
    def test_power_off_is_sent_once_then_status_queried(self):
        transport = ScriptedTransport({POWER_STATUS.payload: [(0.01, b"*POW=OFF#\r\n")]})
        self.connect(transport)
        self.controller.power_off()
        power = self.last_power(self.wait_idle())
        self.assertEqual(transport.writes, [POWER_OFF.payload, POWER_STATUS.payload])
        self.assertIs(power.state, PowerState.OFF)

    def test_power_command_never_resent_without_reply(self):
        transport = ScriptedTransport()
        self.connect(transport)
        self.controller.power_on()
        self.wait_idle()
        self.assertEqual(transport.writes.count(POWER_ON.payload), 1)
        self.assertEqual(transport.writes[1:], [POWER_STATUS.payload] * FAST.status_attempts)

    def test_acknowledgement_does_not_set_state(self):
        transport = ScriptedTransport(
            {
                POWER_ON.payload: [(0.01, b"*POW=ON#\r\n")],
                POWER_STATUS.payload: [(0.01, b"*POW=OFF#\r\n")],
            }
        )
        self.connect(transport)
        self.controller.power_on()
        events = self.wait_idle()
        confirmed = [e for e in self.of_type(events, PowerStatusEvent) if e.confirmed]
        self.assertEqual([e.state for e in confirmed], [PowerState.OFF])

    def test_commands_are_serialized_while_busy(self):
        transport = ScriptedTransport({POWER_STATUS.payload: [(0.2, b"*POW=ON#\r\n")]})
        self.connect(transport)
        self.assertTrue(self.controller.query_power())
        self.assertFalse(self.controller.power_off())
        self.assertFalse(self.controller.query_power())
        self.wait_idle()
        self.assertEqual(transport.writes, [POWER_STATUS.payload])

    def test_disconnect_cancels_pending_status_checks(self):
        transport = ScriptedTransport()
        self.connect(transport)
        self.controller.power_off()
        time.sleep(0.02)
        self.controller.disconnect()
        self.wait_idle()
        time.sleep(0.1)  # let the queued disconnect job run
        self.assertTrue(transport.closed)
        self.assertLess(len(transport.writes), 1 + FAST.status_attempts)
        self.assertEqual(transport.writes.count(POWER_OFF.payload), 1)


class SimulationTests(ControllerTestCase):
    def sim(self, scenario, **kwargs):
        return SimulatedTransport(scenario, time_scale=0.1, read_timeout=0.02, **kwargs)

    def test_normal_power_cycle(self):
        self.connect(self.sim(SimulationScenario.NORMAL))
        self.controller.power_on()
        self.assertIs(self.last_power(self.wait_idle()).state, PowerState.ON)
        self.controller.power_off()
        events = self.wait_idle()
        power = self.last_power(events)
        self.assertIs(power.state, PowerState.OFF)
        self.assertTrue(power.simulated)
        self.assertTrue(all(e.simulated for e in self.of_type(events, LogEvent)))

    def test_power_on_blocked_while_cooling(self):
        self.connect(self.sim(SimulationScenario.NORMAL, initial_power_on=True))
        self.controller.power_off()
        self.wait_idle()
        self.controller.power_on()
        events = self.wait_idle()
        log = " ".join(e.text for e in self.of_type(events, LogEvent))
        self.assertIn("Block item", log)
        self.assertIs(self.last_power(events).state, PowerState.OFF)

    def test_delayed_fragmented_replies(self):
        self.connect(self.sim(SimulationScenario.DELAYED_FRAGMENTED, initial_power_on=True))
        self.controller.query_power()
        self.assertIs(self.last_power(self.wait_idle()).state, PowerState.ON)

    def test_no_response(self):
        self.connect(self.sim(SimulationScenario.NO_RESPONSE))
        self.controller.query_power()
        self.assertIn("No response", self.last_power(self.wait_idle()).detail)

    def test_unsupported(self):
        self.connect(self.sim(SimulationScenario.UNSUPPORTED))
        self.controller.query_power()
        self.assertIn("Unsupported item", self.last_power(self.wait_idle()).detail)

    def test_connection_failure(self):
        events = self.connect(self.sim(SimulationScenario.CONNECTION_FAILURE))
        change = self.of_type(events, ConnectionEvent)[-1]
        self.assertIs(change.change, ConnectionChange.CONNECT_FAILED)
        self.assertTrue(change.simulated)
        self.assertIn("SIMULATED", change.error)


if __name__ == "__main__":
    unittest.main()

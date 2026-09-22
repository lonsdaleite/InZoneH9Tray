import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from inzone_monitor import (HeadsetMonitor, HeadsetState, HubLogReader, decode_frames,
                            make_query, read_hub_snapshot, request)


def event(kind, stamp, action=None, model="INZONE H9 WH-G900N"):
    return json.dumps({"timeStamp": stamp * 1000, "actionTypeId": kind,
                       "serviceInfo": {"targetDeviceModelName": model},
                       "action": action or {}}).encode() + b"\n"


class ProtocolTests(unittest.TestCase):
    def test_query_and_hardware_captures(self):
        self.assertEqual(make_query(0x21, 1, 1).hex(), "0100fc0896c321010101007d")
        on = bytes.fromhex("04ff0a0096c31201100100017e")
        off = bytes.fromhex("04ff0a0096c31201100100007d")
        self.assertEqual([f[11] for f in decode_frames(b"noise" + on + off)], [1, 0])
        self.assertEqual(list(decode_frames(on[:-1])), [])
        self.assertEqual(list(decode_frames(on[:-1] + b"\x00")), [])

    def test_fragmented_response_and_transaction_matching(self):
        notification = bytes.fromhex("04ff0a0096c31201200100018e")
        response = bytes.fromhex("04ff0a0096c31201100100017e")
        class Port:
            in_waiting = 0
            def __init__(self):
                self.parts = iter([notification, response[:5], response[5:]])
            def write(self, data):
                pass
            def read(self, count):
                return next(self.parts, b"")
        self.assertEqual(request(Port(), 0x21, 1, 1), b"\x01")
        self.assertIsNone(request(Port(), 0x21, 1, 2, timeout=0.01))


class HubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ActionLog.log"
        self.now = time.time() - 10
        self.session = (100, self.now)
        self.reader = HubLogReader(self.path)

    def test_reject_old_session_and_other_models(self):
        self.path.write_bytes(event(23011, self.now - 1) +
                              event(23011, self.now + 1, model="INZONE H9 II WH-G910N"))
        self.assertIsNone(self.reader.poll(self.session).connected)

    def test_connect_battery_bt_off_and_disconnect(self):
        self.path.write_bytes(event(23011, self.now + 1) +
                              event(23014, self.now + 2, {"item": "BatteryStatus", "value": "100"}) +
                              event(23014, self.now + 3, {"item": "BtStatus", "value": "Off"}))
        state = self.reader.poll(self.session)
        self.assertTrue(state.connected)
        self.assertEqual(state.battery, 100)
        with self.path.open("ab") as stream:
            stream.write(event(23012, self.now + 4))
        state = self.reader.poll(self.session)
        self.assertFalse(state.connected)
        self.assertIsNone(state.battery)

    def test_partial_line_is_not_lost(self):
        line = event(23011, self.now + 1)
        self.path.write_bytes(line[:20])
        self.assertIsNone(self.reader.poll(self.session).connected)
        with self.path.open("ab") as stream:
            stream.write(line[20:])
        self.assertTrue(self.reader.poll(self.session).connected)

    def test_truncate_and_new_process_invalidate_previous_state(self):
        self.path.write_bytes(event(23011, self.now + 1))
        self.assertTrue(self.reader.poll(self.session).connected)
        self.path.write_bytes(b"")
        self.assertIsNone(self.reader.poll(self.session).connected)
        self.path.write_bytes(event(23011, self.now + 1))
        self.assertIsNone(self.reader.poll((101, self.now + 2)).connected)

    def test_corrupt_line_does_not_hide_valid_event(self):
        self.path.write_bytes(b"{broken\n" + event(23012, self.now + 1))
        self.assertFalse(self.reader.poll(self.session).connected)


    def test_usb_loss_requires_a_new_hub_event(self):
        self.path.write_bytes(event(23011, self.now + 1))
        self.assertTrue(self.reader.poll(self.session).connected)
        self.reader.invalidate(self.now + 2)
        self.assertIsNone(self.reader.poll(self.session).connected)
        with self.path.open("ab") as stream:
            stream.write(event(23012, self.now + 3))
        self.assertFalse(self.reader.poll(self.session).connected)


class HubSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ActionLog.log"
        self.path.write_bytes(b"")
        self.now = time.time()
        self.session = (100, self.now - 20)
        self.monitor = HeadsetMonitor()
        self.monitor.hub = HubLogReader(self.path)

    def snapshot(self, connected=True, stamp=None):
        return HeadsetState(connected=connected, battery=75 if connected else None,
                            source="Hub", port="COM3", updated=stamp or self.now)

    def test_cold_start_with_empty_or_missing_log(self):
        with patch("inzone_monitor.read_hub_snapshot", return_value=self.snapshot()):
            state = self.monitor.poll_hub(self.session, "COM3")
            self.assertTrue(state.connected)
            self.assertEqual(state.battery, 75)
            self.path.unlink()
            self.assertTrue(self.monitor.poll_hub(self.session, "COM3").connected)

    def test_com_to_hub_handoff_retains_confirmed_state_during_initialization(self):
        self.monitor.cached = HeadsetState(connected=True, battery=70, source="COM",
                                            port="COM3", updated=self.now)
        with patch("inzone_monitor.read_hub_snapshot", return_value=None):
            self.assertEqual(self.monitor.poll_hub(self.session, "COM3").battery, 70)
            with patch("inzone_monitor.time.time", return_value=self.now + 7):
                self.assertIsNone(self.monitor.poll_hub(self.session, "COM3").connected)
        self.monitor.snapshot_at = 0
        with patch("inzone_monitor.read_hub_snapshot", return_value=self.snapshot(False)):
            self.assertFalse(self.monitor.poll_hub(self.session, "COM3").connected)

    def test_later_event_overrides_snapshot(self):
        with patch("inzone_monitor.read_hub_snapshot", return_value=self.snapshot()):
            self.assertTrue(self.monitor.poll_hub(self.session, "COM3").connected)
            self.path.write_bytes(event(23012, self.now + 1))
            self.assertFalse(self.monitor.poll_hub(self.session, "COM3").connected)

    def test_stale_log_cannot_resurrect_expired_snapshot(self):
        self.path.write_bytes(event(23011, self.now - 2))
        with patch("inzone_monitor.read_hub_snapshot", return_value=self.snapshot(False)):
            self.assertFalse(self.monitor.poll_hub(self.session, "COM3").connected)
        self.monitor.snapshot_at = 0
        with patch("inzone_monitor.read_hub_snapshot", return_value=None), \
                patch("inzone_monitor.time.time", return_value=self.now + 7):
            self.assertIsNone(self.monitor.poll_hub(self.session, "COM3").connected)

    def test_new_hub_session_discards_old_snapshot(self):
        with patch("inzone_monitor.read_hub_snapshot", return_value=self.snapshot()):
            self.monitor.poll_hub(self.session, "COM3")
        with patch("inzone_monitor.read_hub_snapshot", return_value=None):
            self.assertIsNone(self.monitor.poll_hub((101, self.now + 1), "COM3").connected)

    def test_log_fallback_when_helper_unavailable(self):
        self.path.write_bytes(event(23012, self.now - 1))
        with patch("inzone_monitor.read_hub_snapshot", return_value=None):
            self.assertFalse(self.monitor.poll_hub(self.session, "COM3").connected)

    def test_snapshot_rejects_wrong_process_or_device_and_times_out(self):
        import subprocess
        from types import SimpleNamespace
        value = dict(pid=100, started=self.session[1], port="COM3", connected=True,
                     battery=75, charging=False)
        with patch("inzone_monitor.Path.is_file", return_value=True), \
                patch("inzone_monitor.subprocess.run") as run:
            run.return_value = SimpleNamespace(stdout=json.dumps(value).encode())
            self.assertTrue(read_hub_snapshot(self.session, "COM3").connected)
            self.assertIsNone(read_hub_snapshot(self.session, "COM4"))
            self.assertIsNone(read_hub_snapshot((101, self.session[1]), "COM3"))
            self.assertIsNone(read_hub_snapshot((100, self.session[1] + 10), "COM3"))
            run.side_effect = subprocess.TimeoutExpired("helper", 5)
            self.assertIsNone(read_hub_snapshot(self.session, "COM3"))
            self.assertEqual(run.call_args.kwargs["timeout"], 5)

if __name__ == "__main__":
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from _ctypes import COMError
from unittest.mock import Mock

from audio_switch import AutoSwitcher, Output, Settings, WindowsAudio, save_json


class FakeAudio:
    def __init__(self):
        self.devices = [Output("game", "INZONE Game", True, True),
                        Output("chat", "INZONE Chat", True),
                        Output("screen", "Screen"), Output("speakers", "Speakers")]
        self.current = "screen"
        self.changes = []
        self.fallbacks = 0

    def outputs(self):
        return self.devices

    def default(self):
        return self.current

    def set_default(self, target):
        self.changes.append(target)
        self.current = target

    def windows_fallback(self, outputs):
        self.fallbacks += 1
        return "speakers"


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "settings.json"
        self.settings = Settings(self.path)
        self.audio = FakeAudio()
        self.switch = AutoSwitcher(self.audio, self.settings)

    def enable(self):
        self.settings.set("auto_switch", True)

    def test_disabled_by_default_and_remembers_manual_output(self):
        self.switch.update(True)
        self.assertEqual(self.audio.changes, [])
        self.assertEqual(self.settings.get("last_output"), "screen")

    def test_connect_and_restore_without_changing_calls(self):
        self.enable()
        self.switch.update(True)
        self.assertEqual(self.audio.changes, ["game"])
        self.switch.update(False)
        self.assertEqual(self.audio.changes, ["game", "screen"])
        self.assertEqual(self.audio.fallbacks, 0)

    def test_unknown_never_means_disconnected(self):
        self.enable()
        self.switch.update(True)
        self.switch.update(None)
        self.assertEqual(self.audio.changes, ["game"])

    def test_manual_selection_is_not_fought_while_connected(self):
        self.enable()
        self.switch.update(True)
        self.audio.current = "speakers"
        self.switch.update(True)
        self.assertEqual(self.audio.current, "speakers")
        self.switch.update(False)
        self.switch.update(True)
        self.switch.update(False)
        self.assertEqual(self.audio.current, "speakers")

    def test_no_history_uses_windows_fallback(self):
        self.enable()
        self.audio.current = "game"
        self.switch.update(False)
        self.assertEqual(self.audio.fallbacks, 1)
        self.assertEqual(self.audio.current, "speakers")

    def test_unavailable_history_and_chat_are_not_restore_targets(self):
        self.enable()
        self.settings.set("last_output", "chat")
        self.audio.current = "game"
        self.switch.update(False)
        self.assertEqual(self.audio.current, "speakers")

    def test_disabling_stops_actions_and_enabling_applies_current_state(self):
        self.enable()
        self.switch.update(True)
        self.settings.set("auto_switch", False)
        self.switch.update(False)
        self.assertEqual(self.audio.current, "game")
        self.enable()
        self.switch.update(False)
        self.assertEqual(self.audio.current, "screen")

    def test_settings_survive_restart(self):
        self.enable()
        self.switch.update(True)
        loaded = Settings(self.path)
        self.assertTrue(loaded.get("auto_switch"))
        self.assertEqual(loaded.get("last_output"), "screen")


class RecoveryTests(unittest.TestCase):
    def test_failed_fallback_restores_all_endpoints(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = object.__new__(WindowsAudio)
            audio.recovery_path = Path(folder) / "recovery.json"
            calls = []
            def visibility(device, enabled):
                calls.append((device, enabled))
                if device == "chat" and not enabled:
                    raise OSError("simulated disable failure")
            audio.policy = Mock()
            audio.policy.SetEndpointVisibility.side_effect = visibility
            audio.default = lambda role=1: "game"
            audio.outputs = FakeAudio().outputs
            with self.assertRaises(OSError):
                audio.windows_fallback(FakeAudio().devices)
            self.assertIn(("game", True), calls)
            self.assertIn(("chat", True), calls)
            self.assertFalse(audio.recovery_path.exists())
            self.assertEqual(audio.policy.SetDefaultEndpoint.call_count, 3)

    def test_failed_restore_keeps_journal_for_next_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = object.__new__(WindowsAudio)
            audio.recovery_path = Path(folder) / "recovery.json"
            save_json(audio.recovery_path, {"enabled": ["game", "chat"], "defaults": {}})
            audio.policy = Mock()
            audio.outputs = FakeAudio().outputs
            audio.policy.SetEndpointVisibility.side_effect = [COMError(-2147467259, "failure", None), None]
            with self.assertRaises(OSError):
                audio.recover()
            self.assertTrue(audio.recovery_path.exists())
            self.assertEqual(audio.policy.SetEndpointVisibility.call_count, 2)

    def test_missing_previous_default_does_not_block_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = object.__new__(WindowsAudio)
            audio.recovery_path = Path(folder) / "recovery.json"
            save_json(audio.recovery_path, {"enabled": ["game"], "defaults": {"1": "unplugged-monitor"}})
            audio.policy = Mock()
            audio.outputs = FakeAudio().outputs
            audio.recover()
            self.assertFalse(audio.recovery_path.exists())
            audio.policy.SetDefaultEndpoint.assert_not_called()


if __name__ == "__main__":
    unittest.main()

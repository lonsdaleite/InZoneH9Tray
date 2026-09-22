from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import windows_startup as startup


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.registry = patch.object(startup, "winreg").start()
        self.addCleanup(patch.stopall)
        self.registry.REG_SZ = 1
        self.key = self.registry.OpenKey.return_value.__enter__.return_value
        with patch.object(startup, "startup_command", return_value='"C:\\My apps\\Tray.exe" --autostart'):
            self.setting = startup.WindowsStartup()

    def test_read_does_not_register_startup(self):
        self.registry.OpenKey.side_effect = FileNotFoundError
        self.assertFalse(self.setting.enabled())
        self.registry.CreateKeyEx.assert_not_called()
        self.registry.SetValueEx.assert_not_called()

    def test_enable_registers_current_user_and_disable_deletes_only_our_value(self):
        self.setting.set_enabled(True)
        self.assertEqual(self.registry.CreateKeyEx.call_args.args[:2],
                         (self.registry.HKEY_CURRENT_USER, startup.RUN_KEY))
        self.registry.SetValueEx.assert_called_once_with(
            self.registry.CreateKeyEx.return_value.__enter__.return_value,
            "InZoneH9Tray", 0, 1, self.setting.command)
        self.setting.set_enabled(False)
        self.registry.DeleteValue.assert_called_once_with(self.key, "InZoneH9Tray")
        self.registry.DeleteKey.assert_not_called()

    def test_checkbox_reflects_this_location_and_missing_delete_is_safe(self):
        self.registry.QueryValueEx.return_value = (self.setting.command, 1)
        self.assertTrue(self.setting.enabled())
        self.registry.QueryValueEx.return_value = ('"C:\\Old\\Tray.exe" --autostart', 1)
        self.assertFalse(self.setting.enabled())
        self.registry.DeleteValue.side_effect = FileNotFoundError
        self.setting.set_enabled(False)

    def test_write_errors_are_not_reported_as_success(self):
        self.registry.SetValueEx.side_effect = PermissionError
        with self.assertRaises(PermissionError):
            self.setting.set_enabled(True)
        self.setting.command = 'x' * 261
        with self.assertRaises(ValueError):
            self.setting.set_enabled(True)

    def test_frozen_command_quotes_path_and_does_not_reuse_cli_arguments(self):
        executable = str(Path(r"C:\My apps\Наушники\Tray.exe").resolve())
        with patch.object(startup.sys, "frozen", True, create=True), \
                patch.object(startup.sys, "executable", executable), \
                patch.object(startup.sys, "argv", [executable, "--diagnose"]):
            self.assertEqual(startup.startup_command(),
                             subprocess.list2cmdline([executable, "--autostart"]))

    def test_source_uses_windowless_python_and_absolute_script(self):
        executable = Path(r"C:\Python env\python.exe").resolve()
        with patch.object(startup.sys, "frozen", False, create=True), \
                patch.object(startup.sys, "executable", str(executable)), \
                patch.object(startup.Path, "is_file", return_value=True):
            self.assertEqual(startup.startup_command(), subprocess.list2cmdline([
                str(executable.with_name("pythonw.exe")),
                str(Path(startup.__file__).resolve().with_name("inzone_tray.py")),
                "--autostart"]))


if __name__ == "__main__":
    unittest.main()

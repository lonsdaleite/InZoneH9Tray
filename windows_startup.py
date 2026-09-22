"""Opt-in startup for the current user; registry writes only on menu clicks."""

from pathlib import Path
import subprocess
import sys
import winreg


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "InZoneH9Tray"


def startup_command():
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        arguments = [str(executable)]
    else:
        windowless = executable.with_name("pythonw.exe")
        arguments = [str(windowless if windowless.is_file() else executable),
                     str(Path(__file__).resolve().with_name("inzone_tray.py"))]
    return subprocess.list2cmdline(arguments + ["--autostart"])


class WindowsStartup:
    def __init__(self):
        self.command = startup_command()

    def enabled(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                value, kind = winreg.QueryValueEx(key, VALUE_NAME)
                return kind == winreg.REG_SZ and value == self.command
        except FileNotFoundError:
            return False

    def set_enabled(self, enabled):
        if enabled:
            if len(self.command) > 260:
                raise ValueError("The app path is too long for Windows startup. Move it to a shorter path.")
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, self.command)
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass

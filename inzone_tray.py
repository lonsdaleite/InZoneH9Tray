"""INZONE H9/H7 battery tray with optional automatic output switching."""

import argparse
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
import time
from dataclasses import asdict

# Set this before pystray can create a window, including when run from source.
# The packaged executable also declares PerMonitorV2 in its manifest.
if os.name == "nt":
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

import comtypes
import pystray
from pystray import MenuItem as item
from PIL import Image, ImageDraw, ImageFont

from audio_switch import AutoSwitcher, Settings, WindowsAudio
from inzone_monitor import HeadsetMonitor, HeadsetState
from windows_startup import WindowsStartup


DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "InZoneH9Tray"
state = HeadsetState()
audio_message = "Auto-switch off"
stop_event = threading.Event()
refresh_event = threading.Event()
tray_icon = None
settings = None
startup = None


def describe(value):
    if value.connected is False:
        text = "Headset disconnected"
    elif value.connected is True:
        text = f"Battery {value.battery}%" if value.battery is not None else "Headset connected"
        if value.charging:
            text += " / charging"
    else:
        text = value.error or "Waiting for headset status"
    return text + (f" [{value.source}]" if value.source else "")


def make_icon(value):
    picture = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(picture)
    if value.connected is True:
        color = (60, 110, 60) if value.charging else (90, 35, 150)
        if value.battery is not None and value.battery <= 20:
            color = (150, 40, 40)
        text = str(value.battery) if value.battery is not None else "ON"
    else:
        color = (80, 80, 80)
        text = "OFF" if value.connected is False else "?"
    draw.rounded_rectangle((2, 2, 62, 62), radius=12, fill=color)
    size = 25 if len(text) >= 3 else 31
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf", size)
    except OSError:
        font = ImageFont.load_default()
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((64 - (right - left)) / 2 - left,
               (64 - (bottom - top)) / 2 - top), text, font=font, fill="white")
    return picture


def save_status(value):
    (DATA_DIR / "inzone_battery_status.txt").write_text(describe(value), encoding="utf-8")
    battery_file = DATA_DIR / "inzone_battery.txt"
    if value.connected is True and value.battery is not None:
        battery_file.write_text(str(value.battery), encoding="utf-8")
    else:
        battery_file.unlink(missing_ok=True)


def worker_loop():
    global state, audio_message
    comtypes.CoInitialize()
    try:
        monitor = HeadsetMonitor()
        switcher = None
        audio_retry_at = 0.0
        last_display = None
        while not stop_event.is_set():
            try:
                state = monitor.poll()
                if switcher is None and time.monotonic() >= audio_retry_at:
                    try:
                        audio = WindowsAudio(DATA_DIR / "audio-recovery.json")
                        audio.recover()
                        switcher = AutoSwitcher(audio, settings)
                    except Exception:
                        logging.exception("Audio initialization/recovery failed")
                        audio_message = "Audio unavailable; retrying (see diagnostic log)"
                        audio_retry_at = time.monotonic() + 5
                if switcher is not None:
                    switcher.update(state.connected)
                    audio_message = switcher.message
                display = (describe(state), audio_message)
                if display != last_display:
                    logging.info("%s; %s", *display)
                    save_status(state)
                    tray_icon.icon = make_icon(state)
                    tray_icon.title = ("INZONE H9/H7: " + display[0])[:127]
                    tray_icon.update_menu()
                    last_display = display
            except Exception:
                logging.exception("Refresh failed")
                state = HeadsetState(error="Refresh failed; see diagnostic log")
                tray_icon.title = "INZONE: refresh failed; see diagnostic log"
                tray_icon.icon = make_icon(state)
            refresh_event.wait(1.0)
            refresh_event.clear()
    except Exception:
        logging.exception("Audio initialization/recovery failed")
        audio_message = "Audio unavailable; see diagnostic log and restart"
        tray_icon.title = "INZONE: initialization failed; see diagnostic log"
        tray_icon.update_menu()
    finally:
        comtypes.CoUninitialize()


def on_toggle(icon, menu_item):
    try:
        settings.set("auto_switch", not settings.get("auto_switch"))
        refresh_event.set()
        icon.update_menu()
    except OSError:
        logging.exception("Cannot save auto-switch setting")
        icon.notify("Cannot save the auto-switch setting. See the diagnostic log.")


def startup_checked(menu_item):
    try:
        return startup.enabled()
    except OSError:
        logging.exception("Cannot read Windows startup setting")
        return False


def on_startup_toggle(icon, menu_item):
    try:
        startup.set_enabled(not startup.enabled())
    except (OSError, ValueError) as exc:
        logging.exception("Cannot change Windows startup setting")
        icon.notify(f"Cannot change Windows startup: {exc}")
    finally:
        icon.update_menu()


def on_refresh(icon, menu_item):
    refresh_event.set()


def on_exit(icon, menu_item):
    stop_event.set()
    refresh_event.set()
    icon.stop()


def acquire_instance():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, "Local\\InZoneH9Tray.Monitor")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)
        return None
    return handle


def main():
    global tray_icon, settings, startup
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="Read one status sample without changing audio")
    parser.add_argument("--diagnose", action="store_true", help="Read status and available audio outputs as JSON")
    parser.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    handle = acquire_instance()
    if handle is None:
        if args.status or args.diagnose:
            print(json.dumps({"error": "Another InZoneH9Tray instance is running"}))
        elif not args.autostart:
            ctypes.windll.user32.MessageBoxW(None, "InZoneH9Tray is already running.", "INZONE", 0)
        return
    try:
        if args.status or args.diagnose:
            result = {"headset": asdict(HeadsetMonitor().poll())}
            if args.diagnose:
                audio = WindowsAudio(DATA_DIR / "audio-recovery.json")
                result.update(outputs=[asdict(d) for d in audio.outputs()], default=audio.default())
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(DATA_DIR / "inzone_tray_error.log", maxBytes=512_000,
                                      backupCount=2, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, handlers=[handler],
                            format="%(asctime)s %(levelname)s %(message)s")
        settings = Settings(DATA_DIR / "settings.json")
        startup = WindowsStartup()
        tray_icon = pystray.Icon(
            "InZoneH9Tray", make_icon(state), "INZONE: starting...",
            menu=pystray.Menu(
                item(lambda _: describe(state), None, enabled=False),
                item("Auto-switch audio output", on_toggle,
                     checked=lambda _: settings.get("auto_switch")),
                item(lambda _: audio_message, None, enabled=False),
                item("Start with Windows", on_startup_toggle, checked=startup_checked),
                item("Refresh now", on_refresh),
                item("Open data folder", lambda *_: os.startfile(DATA_DIR)),
                item("Exit", on_exit)))
        thread = threading.Thread(target=worker_loop, name="INZONE monitor", daemon=False)
        thread.start()
        try:
            tray_icon.run()
        finally:
            stop_event.set()
            refresh_event.set()
            thread.join()
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()

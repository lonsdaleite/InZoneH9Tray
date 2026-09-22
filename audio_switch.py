"""Optional output switching. Microphones and communication defaults are preserved."""

import json
import os
import threading
import time
from _ctypes import COMError
from dataclasses import dataclass
from pathlib import Path


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"auto_switch": False, "last_output": None}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value.get("auto_switch"), bool):
                self.data["auto_switch"] = value["auto_switch"]
            if isinstance(value.get("last_output"), str):
                self.data["last_output"] = value["last_output"]
        except (OSError, ValueError, AttributeError):
            pass

    def get(self, key):
        with self.lock:
            return self.data[key]

    def set(self, key, value):
        with self.lock:
            if self.data.get(key) != value:
                updated = {**self.data, key: value}
                save_json(self.path, updated)
                self.data = updated


@dataclass(frozen=True)
class Output:
    id: str
    name: str
    inzone: bool = False
    game: bool = False


class WindowsAudio:
    def __init__(self, recovery_path: Path):
        # Construct and use the backend on the same COM-initialized thread.
        import comtypes
        from pycaw.api.policyconfig import IPolicyConfig
        from pycaw.constants import CLSID_CPolicyConfigClient
        from pycaw.utils import AudioUtilities
        self.utilities = AudioUtilities
        self.enumerator = AudioUtilities.GetDeviceEnumerator()
        self.policy = comtypes.CoCreateInstance(CLSID_CPolicyConfigClient,
                                                IPolicyConfig, comtypes.CLSCTX_ALL)
        self.recovery_path = recovery_path

    def outputs(self):
        devices = self.utilities.GetAllDevices(data_flow=0, device_state=1)
        result = []
        for device in devices:
            # Interface friendly name still identifies INZONE if the user renames its endpoint.
            interface = device.properties.get("{B3F8FA53-0004-438E-9003-51A46E139BFC} 6", "")
            identity = f"{device.FriendlyName} {interface}".casefold()
            is_inzone = (("inzone h9" in identity or "inzone h7" in identity)
                         and "h9 ii" not in identity)
            is_game = is_inzone and "game" in identity
            result.append(Output(device.id, device.FriendlyName or str(interface), is_inzone, is_game))
        return result

    def default(self, role=1):
        try:
            return self.enumerator.GetDefaultAudioEndpoint(0, role).GetId()
        except (OSError, COMError):
            return None

    def set_default(self, device_id):
        # Console + multimedia only: do not reroute calls or change microphone selection.
        for role in (0, 1):
            self.policy.SetDefaultEndpoint(device_id, role)

    def recover(self):
        if not self.recovery_path.exists():
            return
        saved = json.loads(self.recovery_path.read_text(encoding="utf-8"))
        errors = []
        for device_id in saved["enabled"]:
            try:
                self.policy.SetEndpointVisibility(device_id, True)
            except Exception as exc:
                errors.append(str(exc))
        active = {d.id for d in self.outputs()}
        for role, device_id in saved["defaults"].items():
            # A disconnected monitor must not block recovery forever.
            if device_id in active:
                try:
                    self.policy.SetDefaultEndpoint(device_id, int(role))
                except Exception as exc:
                    errors.append(str(exc))
        if errors:
            raise OSError("Audio recovery incomplete: " + "; ".join(errors))
        self.recovery_path.unlink()

    def windows_fallback(self, outputs):
        """Let Windows select among the remaining active render endpoints.

        GetDefaultAudioEndpoint has no exclusion-list API. Temporarily disabling
        INZONE render endpoints invokes Windows' own preference/ranking policy.
        A durable journal and finally block restore them and all previous roles.
        The caller then applies the selected external endpoint for media only.
        """
        external = {d.id for d in outputs if not d.inzone}
        if not external:
            raise OSError("No other active output is available")
        current = self.default()
        if current in external:
            return current
        if len(external) == 1:
            return next(iter(external))
        self.recover()
        saved = {"enabled": [d.id for d in outputs if d.inzone],
                 "defaults": {str(role): self.default(role) for role in (0, 1, 2)}}
        save_json(self.recovery_path, saved)
        try:
            for device_id in saved["enabled"]:
                self.policy.SetEndpointVisibility(device_id, False)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                selected = self.default()
                if selected in external:
                    return selected
                time.sleep(0.05)
            raise OSError("Windows did not select another output")
        finally:
            self.recover()


class AutoSwitcher:
    def __init__(self, audio, settings):
        self.audio = audio
        self.settings = settings
        self.applied = None
        self.message = ""
        self.retry_at = 0.0

    def update(self, connected: bool | None):
        enabled = self.settings.get("auto_switch")
        try:
            outputs = self.audio.outputs()
            external = {d.id: d for d in outputs if not d.inzone}
            current = self.audio.default()
            # Observe manual selection even when automatic switching is disabled.
            if current in external:
                self.settings.set("last_output", current)
            if not enabled:
                self.applied = None
                self.message = "Auto-switch off"
                return
            if connected is None:
                self.message = "Auto-switch: waiting for headset status"
                return
            if connected == self.applied or time.monotonic() < self.retry_at:
                return
            if connected:
                candidates = [d for d in outputs if d.game]
                if len(candidates) != 1:
                    raise OSError("A unique INZONE H9/H7 Game output is not available")
                target = candidates[0].id
            elif current in external:
                target = current
            else:
                target = self.settings.get("last_output")
                if target not in external:
                    target = self.audio.windows_fallback(outputs)
            # Recheck the checkbox after a potentially slow Windows fallback.
            if not self.settings.get("auto_switch"):
                self.applied = None
                return
            if current != target:
                self.audio.set_default(target)
            if not connected:
                self.settings.set("last_output", target)
            self.applied = connected
            label = next((d.name for d in outputs if d.id == target), target)
            self.message = f"Auto-switch: {label}"
            self.retry_at = 0
        except Exception as exc:
            self.message = f"Auto-switch error: {exc}"
            self.retry_at = time.monotonic() + 5

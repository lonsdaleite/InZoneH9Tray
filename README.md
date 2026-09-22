# InZoneH9Tray

**English** | [Русский](README.ru.md)

Windows tray battery indicator and optional automatic audio switching for Sony INZONE H9 (WH-G900N). Based on [janit88/InZoneH9Tray](https://github.com/janit88/InZoneH9Tray).

## Using the app

Run `InZoneH9Tray.exe`. Right-click its tray icon:

- **Auto-switch audio output** enables/disables automatic switching. It is off on first launch; the checkbox is saved.
- **Start with Windows** enables/disables launch when the current user signs in, without administrator rights. It is off until enabled in the menu.
- **Refresh now** requests a refresh without starting another competing worker.
- **Open data folder** opens settings and diagnostic logs.
- **Exit** stops monitoring. It leaves your current audio output selected.

With automatic switching enabled, connecting the headset selects **INZONE H9 / INZONE H7 - Game**. Disconnecting it returns to the most recently selected active output outside this headset. Manual output selections are remembered even while the checkbox is off. A manual selection while the headset stays connected is respected until the next connection transition.

Only the console and multimedia playback defaults are changed. Microphone selection and the communications default are preserved. Applications pinned to a specific device may need their own output set to Windows default.

If no remembered output is available, the app uses Windows' automatic selection among the remaining outputs. With multiple alternatives, this briefly disables the headset's active Game/Chat playback endpoints, reads Windows' choice, then restores them and applies that choice. Existing audio sessions can be interrupted during this fallback. A durable recovery file restores endpoints after an interrupted operation on the next launch. The usual remembered-output path does not disable endpoints.

## Working with or without INZONE Hub

- **Hub not running / not installed:** the app locates the USB dongle by `VID_054C&PID_0E53` and issues read-only COM queries for radio connection and battery. It releases the port after each poll.
- **Hub running:** a bundled helper reads Hub's current connection/battery fields approximately every three seconds, including when Hub is hidden in the tray. It leaves the COM port to Hub. `%APPDATA%\Sony\INZONE Hub\ActionLog.log` provides intermediate events and a fallback if direct reading is unavailable. An empty or missing log does not prevent direct status reads.
- If neither source provides a usable state, status is **unknown**, not disconnected; no automatic switch is performed.

Hub state and logging are internal interfaces, verified with Hub 1.0.19, and may change in later versions. The helper uses ClrMD to read a small, known object graph without suspending Hub, injecting code, saving a process dump, or sending data elsewhere. Reading a running process is best-effort: two matching samples, object types, device identity and initialization flags are checked; failures leave monitoring to the log. A helper call is limited to five seconds. A confirmed COM reading is preserved for up to five seconds while Hub initializes. The app does not enable telemetry or alter Hub settings. A Hub startup coinciding with a brief direct query may require Hub to retry opening the port.

The tray menu supports Windows per-monitor DPI scaling, including monitors with different scale settings.

The original H9's direct connection replies and Hub connection events have been verified on hardware. H7 shares the dongle ID but is not hardware-tested. H9 II and INZONE Buds are not supported by this protocol implementation.

## Tray display and files

| Icon | Meaning |
| --- | --- |
| Number | Last reported battery percentage |
| ON | Connected; battery percentage not yet available |
| OFF | Headset disconnected or dongle absent |
| ? | Status unknown / waiting |

Data is stored under `%LOCALAPPDATA%\InZoneH9Tray`:

- `settings.json`: checkbox and last external output ID.
- `inzone_battery.txt`: percentage, removed when unavailable.
- `inzone_battery_status.txt`: readable status and source (`Hub`, `COM`, `USB`).
- `inzone_tray_error.log`: rotating diagnostic log.
- `audio-recovery.json`: present only for pending endpoint restoration.

The app allows one instance per Windows session. It does not install a service or change drivers. **Start with Windows** registers the current EXE path in `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run` as `InZoneH9Tray`; clearing the checkbox removes that entry. Place the EXE in its permanent location before enabling it. If you move or rename it, enable the option from the new location to update the entry. An automatic launch exits quietly if the app is already running.

## Development

Requires Windows 10/11 x64, Python 3.11+ and .NET SDK 8 or newer to build the helper. The packaged EXE includes the helper runtime; users do not need to install .NET separately.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
dotnet publish hub_snapshot/HubSnapshot.csproj -c Release -o build/hub-snapshot --source https://api.nuget.org/v3/index.json
python -m unittest discover -s tests -v
python inzone_tray.py --diagnose
python inzone_tray.py
```

`--status` and `--diagnose` read device state without changing audio defaults. Close another tray instance before using them.

Build the single-file EXE with `./build.ps1` from the activated environment. It builds the helper, runs tests and embeds the helper and DPI manifest in the executable.

Output: `dist\InZoneH9Tray.exe`. Tests use simulated audio devices; they do not change real Windows outputs. Audio switching uses Windows' private `IPolicyConfig` interface through pycaw, so compatibility should be checked after major Windows updates.

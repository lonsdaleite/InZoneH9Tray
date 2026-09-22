$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
    dotnet publish hub_snapshot/HubSnapshot.csproj -c Release -o build/hub-snapshot --source https://api.nuget.org/v3/index.json
    if ($LASTEXITCODE -ne 0) { throw 'Hub snapshot build failed' }
    python -m PyInstaller --onefile --noconsole --clean --name InZoneH9Tray --manifest inzone_tray.manifest --add-binary 'build/hub-snapshot/HubSnapshot.exe;hub-snapshot' --hidden-import=pystray._win32 inzone_tray.py
    if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
} finally {
    Pop-Location
}

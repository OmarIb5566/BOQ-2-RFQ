# Installs the BOQ -> RFQ web server and worker as Windows services with NSSM (https://nssm.cc, one .exe, no installer).
# Run in an elevated PowerShell from the app folder:   .\deploy\windows\install-services.ps1 -Nssm C:\tools\nssm.exe
# Remove again with:                                     .\deploy\windows\install-services.ps1 -Remove
#
# Note: Microsoft does not support Excel automation inside a Windows service. With the services, the app uses its
# built-in renderer for BOQ snapshots (set BOQ_EXCEL_RENDERER = "html"); .xlsb files cannot be read without Excel.
param(
    [string]$Nssm = "nssm.exe",
    [string]$AppDir = (Resolve-Path "$PSScriptRoot\..\.."),
    [string]$Config = "",
    [int]$Port = 8080,
    [switch]$Remove
)

$python = Join-Path $AppDir ".venv\Scripts\python.exe"
$services = @(
    @{ Name = "BOQ2RFQ-Web";    Args = "server.py --host 127.0.0.1 --port $Port --no-browser"; Desc = "BOQ -> RFQ web app" },
    @{ Name = "BOQ2RFQ-Worker"; Args = "worker.py";                                             Desc = "BOQ -> RFQ background worker" }
)

if ($Remove) {
    foreach ($s in $services) { & $Nssm stop $s.Name; & $Nssm remove $s.Name confirm }
    return
}
if (-not (Test-Path $python)) { throw "Create the environment first: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt" }
if ($Config -eq "") { $Config = Join-Path $AppDir "boq2rfq.toml" }

foreach ($s in $services) {
    & $Nssm install $s.Name $python $s.Args
    & $Nssm set $s.Name AppDirectory $AppDir
    & $Nssm set $s.Name Description $s.Desc
    & $Nssm set $s.Name AppEnvironmentExtra "BOQ_CONFIG=$Config" "PYTHONUNBUFFERED=1" "BOQ_WORKER=external"
    & $Nssm set $s.Name Start SERVICE_AUTO_START
    & $Nssm set $s.Name AppStdout (Join-Path $AppDir "logs\$($s.Name).log")
    & $Nssm set $s.Name AppStderr (Join-Path $AppDir "logs\$($s.Name).log")
    & $Nssm set $s.Name AppRotateFiles 1
    & $Nssm set $s.Name AppRotateBytes 10485760
    & $Nssm set $s.Name AppExit Default Restart
}
New-Item -ItemType Directory -Force (Join-Path $AppDir "logs") | Out-Null
foreach ($s in $services) { & $Nssm start $s.Name }
Write-Host "Installed. Put IIS or nginx with the HTTPS certificate in front of http://127.0.0.1:$Port"

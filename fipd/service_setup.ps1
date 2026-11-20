# service_setup.ps1 — a executer EN ELEVE : cree et démarre DirectOutputService
$ErrorActionPreference = 'Stop'
$exe = 'C:\Program Files\Logitech\DirectOutput\DirectOutputService.exe'

if (-not (Get-Service DirectOutputService -ErrorAction SilentlyContinue)) {
    sc.exe create DirectOutputService binPath= $exe start= demand | Out-Null
    Write-Output 'service cree'
} else {
    Write-Output 'service deja present'
}
sc.exe start DirectOutputService
Start-Sleep -Seconds 2
Get-CimInstance Win32_Service -Filter "Name='DirectOutputService'" |
    Select-Object State,StartMode,ProcessId | Format-List

# deploy_proxy.ps1 — A EXECUTER EN ADMIN. Cycle complet :
#   sauvegarde du vrai plugin -> pose du proxy -> start service -> fenetre powercycle
#   -> stop service -> RESTAURATION du vrai plugin -> copie du log vers le workspace.
$ErrorActionPreference = 'Continue'
$dir   = 'C:\Program Files\Logitech\DirectOutput'
$real  = Join-Path $dir 'real'
$proxy = 'C:\Users\Arnaud\.zcode\workspace\1793817436471-42fb2902-0f03-4b00-8231-e3c9bbe85b78\proxy\DirectOutputSaiFip.dll'
$log   = 'C:\ProgramData\fipd_proxy_log.txt'
$wout  = 'C:\Users\Arnaud\.zcode\workspace\1793817436471-42fb2902-0f03-4b00-8231-e3c9bbe85b78\proxy\proxy_log_copie.txt'

function Restore {
    Write-Output '-- restauration du vrai plugin --'
    Copy-Item (Join-Path $real 'DirectOutputSaiFip.dll') (Join-Path $dir 'DirectOutputSaiFip.dll') -Force
    Remove-Item $real -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $log) { Copy-Item $log $wout -Force }
}

Remove-Item $log -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $real -Force | Out-Null
Copy-Item (Join-Path $dir 'DirectOutputSaiFip.dll') (Join-Path $real 'DirectOutputSaiFip.dll') -Force
Copy-Item $proxy (Join-Path $dir 'DirectOutputSaiFip.dll') -Force
Write-Output 'proxy en place.'

sc.exe start DirectOutputService | Out-Null
Start-Sleep -Seconds 2
Write-Output ''
Write-Output '=============================================================='
Write-output '  >>> DEBRANCHE LE FIP MAINTENANT, 5 s, PUIS REBRANCHE-LE <<<'
Write-Output '  (le service va re-initialiser le FIP via le proxy)'
Write-Output '=============================================================='
for ($i = 110; $i -gt 0; $i -= 10) {
    Write-Output ("  capture proxy... {0,3} s restantes" -f $i)
    Start-Sleep -Seconds 10
}
sc.exe stop DirectOutputService | Out-Null
Start-Sleep -Seconds 3
Restore
Write-Output ''
if (Test-Path $wout) {
    Write-Output ("TERMINE. Log : {0} ({1} octets)" -f $wout, (Get-Item $wout).Length)
} else {
    Write-Output 'TERMINE mais AUCUN log produit (proxy non charge ?)'
}
Start-Sleep -Seconds 8

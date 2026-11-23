<#
test_recuperation.ps1 — Test MATERIEL de la reprise automatique de fipd3 (ADMINISTRATEUR).

Perte REELLE de session : on redemarre le service Logitech DirectOutputService
(meme effet qu'un FIP debranche/rebranche : tout ce que le daemon avait
enregistre disparait cote service, et son ancien handle devient mort). Le
daemon doit retablir l'affichage SEUL, sans redemarrage.

Verdict : GET /apps expose `ok_age` (secondes depuis la derniere image ACCEPTEE
par le FIP) et `recoveries`. Un cycle reussit quand ok_age < 3 s est soutenu
pendant 10 s. Deux cycles par defaut (au 2e, l'enumeration retourne deux
handles morts EN PLUS du peripherique vivant).

Une seule elevation, un seul log. Depuis une fenetre NON elevee :

  powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"<chemin>\test_recuperation.ps1\"'"

puis lire le log : %TEMP%\fipd_recuperation.txt

-SansRedemarrage : verifie la logique du script SANS elevation et SANS toucher
au service (mesure seule ; attendu : "session conservee").
#>
param(
    [string]$Log = (Join-Path $env:TEMP 'fipd_recuperation.txt'),
    [int]$Boucles = 2,
    [int]$DelaiMaxS = 60,
    [switch]$SansRedemarrage
)
$ErrorActionPreference = 'Continue'
Start-Transcript -Path $Log -Force | Out-Null
$api = 'http://127.0.0.1:8199'

# Write-Host : present dans le transcript, sans polluer la valeur de retour
function Log([string]$Message) { Write-Host $Message }

function Etat {
    try { return Invoke-RestMethod -Uri "$api/apps" -TimeoutSec 3 } catch { return $null }
}

function Cycle([int]$n) {
    $avant = Etat
    if (-not $avant) { throw "le daemon fipd3 ne repond pas sur $api (lancez : python fipd3.py)" }
    $r0 = [int]$avant.recoveries
    Log ('CYCLE {0} AVANT  : page {1}/{2} mode={3} ok_age={4} s recoveries={5}' -f $n, ($avant.page + 1), $avant.apps.Count, $avant.mode, $avant.ok_age, $r0)
    $t0 = Get-Date
    if ($SansRedemarrage) {
        Log "CYCLE $n : SANS redemarrage (verification du script seul)"
    }
    else {
        Log ('CYCLE {0} : redemarrage du service a {1}' -f $n, $t0.ToString('HH:mm:ss'))
        Restart-Service -Name DirectOutputService -Force
        (Get-Service DirectOutputService).WaitForStatus('Running', '00:00:30')
    }
    $perteVue = $false
    $retour = $null
    $retourA = $null
    while (((Get-Date) - $t0).TotalSeconds -lt $DelaiMaxS) {
        Start-Sleep -Milliseconds 400
        $e = Etat
        if (-not $e) { continue }
        $age = [double]$e.ok_age
        $t = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
        if (-not $perteVue -and $age -gt 4) {
            $perteVue = $true
            Log ('CYCLE {0} t={1} s : PERDUE (ok_age={2} s : plus rien nest accepte)' -f $n, $t, $age)
        }
        if ($perteVue -and -not $retour -and $age -lt 3) {
            $retour = Get-Date
            Log ('CYCLE {0} t={1} s : images a nouveau acceptees (recoveries={2})' -f $n, $t, $e.recoveries)
        }
        if ($retour) {
            if ($age -ge 3) {
                Log ('CYCLE {0} t={1} s : nouvelle coupure (ok_age={2} s)' -f $n, $t, $age)
                $retour = $null
            }
            elseif (((Get-Date) - $retour).TotalSeconds -ge 10) {
                $retourA = [math]::Round(($retour - $t0).TotalSeconds, 1)
                break
            }
        }
    }
    $fin = Etat
    if ($fin) {
        Log ('CYCLE {0} APRES  : page {1}/{2} mode={3} ok_age={4} s recoveries={5} (avant : {6})' -f $n, ($fin.page + 1), $fin.apps.Count, $fin.mode, $fin.ok_age, $fin.recoveries, $r0)
    }
    if ($null -ne $retourA) { return "OK - recuperation en $retourA s" }
    if (-not $perteVue) { return 'OK - session conservee (la perte ne s est pas vue cote daemon)' }
    return 'ECHEC - le daemon ne se retablit pas seul'
}

$verdicts = @()
try {
    $svc = Get-Service DirectOutputService
    Log "SERVICE: $($svc.Name) $($svc.Status)"
    for ($n = 1; $n -le $Boucles; $n++) {
        $v = Cycle $n
        $verdicts += $v
        Log "CYCLE $n : $v"
        Start-Sleep -Seconds 3
    }
}
catch {
    $verdicts += "ERREUR : $_"
    Log $verdicts[-1]
}
$global = 'OK'
foreach ($v in $verdicts) { if (-not $v.StartsWith('OK')) { $global = 'ECHEC' } }
Log "VERDICT: $global"
Stop-Transcript | Out-Null

# Riavvia solo la GUI (equivalente di scripts/restart.sh): ferma la run e il processo
# Python, lascia attivi i container (modello e browser, con il login Prolific) e
# riparte. Serve dopo una modifica al codice Python, o quando il bridge Playwright
# si blocca (errori "BrowserStateError ... no URL for the active tab").
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\restart.ps1
#
# -Resume      rilancia la run SOLO se il watchdog del bridge (pw_bridge.py) ha lasciato
#              il marker var\resume_run.flag, cioe' se una run era in corso quando si e'
#              bloccato. Lo usa il watchdog.
# -ResumeLast  rilancia sempre l'ultima run avviata (var\last_run.json), anche a mano.
# -NoBrowser   non apre il pannello nel browser.
param(
    [switch]$NoBrowser,
    [switch]$Resume,
    [switch]$ResumeLast
)
. (Join-Path $PSScriptRoot 'common.ps1')

$flag = Join-Path $Root 'var\resume_run.flag'
$last = Join-Path $Root 'var\last_run.json'

# Leggi il marker PRIMA di fermare tutto, e scartalo se e' vecchio (una run
# interrotta da ore non va fatta ripartire da sola).
$resumeRun = [bool]$ResumeLast
if ($Resume -and (Test-Path $flag)) {
    $age = (Get-Date) - (Get-Item $flag).LastWriteTime
    if ($age.TotalMinutes -lt 15) { $resumeRun = $true }
    else { Write-Host "Marker di ripresa vecchio di $([int]$age.TotalMinutes) min: non rilancio la run." }
}
Remove-Item $flag -ErrorAction SilentlyContinue

& (Join-Path $PSScriptRoot 'stop.ps1') -KeepContainers
& (Join-Path $PSScriptRoot 'start.ps1') -NoModel -NoBrowser:$NoBrowser

if ($resumeRun) {
    if (-not (Test-Path $last)) {
        Write-Host 'Nessuna run da riprendere (var\last_run.json manca).'
    } else {
        if (-not (Wait-Http 'http://127.0.0.1:8090/health' 120)) {
            Write-Host 'ATTENZIONE: il modello non risponde, non rilancio la run.'
        } else {
            $json = Get-Content $last -Raw -Encoding UTF8
            $bytes = [Text.Encoding]::UTF8.GetBytes($json)
            try {
                Invoke-WebRequest "http://127.0.0.1:$Port/start_agent" -Method Post -Body $bytes `
                    -ContentType 'application/json; charset=utf-8' -UseBasicParsing -TimeoutSec 15 | Out-Null
                Write-Host 'Run ripresa con gli stessi parametri dell''ultima volta.'
            } catch {
                Write-Host "Non sono riuscito a rilanciare la run: $($_.Exception.Message)"
            }
        }
    }
}

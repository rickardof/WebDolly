# Spegne la GUI e i container (equivalente di pkill + docker stop).
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\stop.ps1
#
# -KeepContainers  ferma solo la GUI e la run in corso, lascia i container attivi
param([switch]$KeepContainers)
. (Join-Path $PSScriptRoot 'common.ps1')

if (Test-Gui) {
    try { Invoke-WebRequest "http://127.0.0.1:$Port/agent/stop" -Method Post -UseBasicParsing -TimeoutSec 5 | Out-Null } catch { }
}

# La GUI: prima dal pid salvato, poi da chi ascolta sulla porta.
$pids = @()
if (Test-Path $PidFile) { $pids += [int](Get-Content $PidFile | Select-Object -First 1) }
$pids += @(Get-NetTCPConnection -LocalPort ([int]$Port) -State Listen -ErrorAction SilentlyContinue |
           ForEach-Object { $_.OwningProcess })
foreach ($id in ($pids | Sort-Object -Unique)) {
    $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -match '^python') {
        Stop-Process -Id $id -Force
        Write-Host "GUI fermata (pid $id)"
    }
}
Remove-Item $PidFile -ErrorAction SilentlyContinue

if (-not $KeepContainers -and (Test-DockerEngine)) {
    Invoke-Docker stop fara-browser-pw fara-test | Out-Null
    Write-Host 'Container fermati.'
}

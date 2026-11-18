# Avvio quotidiano su Windows (equivalente di scripts/start_gui_pw.sh):
# avvia Docker Desktop se serve, i container e la GUI su http://127.0.0.1:8766.
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\start.ps1
#
# -NoBrowser   non apre il browser
# -NoModel     non avvia fara-test (modello via API: FARA_URL / FARA_MODEL / FARA_API_KEY)
# -KeepAwake   impedisce la sospensione del PC finche' la GUI e' in esecuzione
#              (su Linux lo fa systemd-inhibit, qui non esiste)
param(
    [switch]$NoBrowser,
    [switch]$NoModel,
    [switch]$KeepAwake
)
. (Join-Path $PSScriptRoot 'common.ps1')

if (-not (Test-Path $VenvPython)) { throw 'venv_pw non trovato: esegui prima scripts\windows\setup.ps1' }

Wait-DockerEngine
$containers = @('fara-browser-pw')
if (-not $NoModel) { $containers = @('fara-test') + $containers }
Write-Host "Avvio container: $($containers -join ', ')"
Invoke-Docker start @containers | Out-Null

New-Item -ItemType Directory -Force $LogDir | Out-Null
if (Test-Gui) {
    Write-Host "GUI gia' in esecuzione su :$Port"
} else {
    # Python su Windows apre i file di testo in cp1252 se non e' in modalita' UTF-8.
    $env:PYTHONUTF8 = '1'
    $p = Start-Process $VenvPython -ArgumentList 'gui_server.py', '--port', $Port `
        -WorkingDirectory (Join-Path $Root 'agent') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir 'gui_server.out') `
        -RedirectStandardError  (Join-Path $LogDir 'gui_server.err')
    Set-Content -Path $PidFile -Value $p.Id
    if (-not (Wait-Http "http://127.0.0.1:$Port/agent/status" 30)) {
        throw "La GUI non risponde. Vedi $LogDir\gui_server.err"
    }
    Write-Host "GUI avviata (pid $($p.Id))"
}

if ($KeepAwake) {
    $guiPid = if (Test-Path $PidFile) { (Get-Content $PidFile | Select-Object -First 1) } else { 0 }
    $code = @'
param($TargetPid)
Add-Type -Namespace W -Name P -MemberDefinition '[System.Runtime.InteropServices.DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f);'
while (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue) {
    [void][W.P]::SetThreadExecutionState(0x80000001)   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    Start-Sleep 30
}
[void][W.P]::SetThreadExecutionState(0x80000000)
'@
    $tmp = Join-Path $env:TEMP 'fara_keepawake.ps1'
    Set-Content -Path $tmp -Value $code -Encoding ASCII
    Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $tmp, '-TargetPid', $guiPid
    Write-Host 'Sospensione del PC bloccata finche la GUI e in esecuzione.'
}

if (-not $NoModel) {
    Write-Host 'Attendo il modello (caricamento: ~1-2 minuti)...'
    if (Wait-Http 'http://127.0.0.1:8090/health' 240) { Write-Host 'Modello pronto.' }
    else { Write-Host 'ATTENZIONE: il modello non risponde ancora. Controlla: docker logs fara-test' }
}

$url = "http://127.0.0.1:$Port"
Write-Host "Pannello: $url"
if (-not $NoBrowser) { Start-Process $url }

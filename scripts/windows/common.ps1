# Funzioni condivise dagli script Windows (setup.ps1, start.ps1, stop.ps1).
# Solo ASCII: Windows PowerShell 5.1 legge i file senza BOM come ANSI.
$ErrorActionPreference = 'Stop'

$script:Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script:Port = if ($env:FARA_PORT) { $env:FARA_PORT } else { '8766' }
$script:VenvPython = Join-Path $Root 'venv_pw\Scripts\python.exe'
$script:LogDir = Join-Path $Root 'var\logs\pw'
$script:PidFile = Join-Path $Root 'var\gui_server.pid'

function Get-DockerExe {
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $fallback = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
    if (Test-Path $fallback) { return $fallback }
    throw 'Docker non trovato. Installa Docker Desktop (winget install Docker.DockerDesktop).'
}

function Invoke-Docker {
    # Esegue docker e lancia un errore se esce con codice diverso da 0.
    # stderr non viene trattato come errore (docker ci scrive anche i progressi).
    $exe = Get-DockerExe
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $exe @args 2>&1 | ForEach-Object { "$_" }
        if ($LASTEXITCODE -ne 0) { throw "docker $($args -join ' ') ha fallito (codice $LASTEXITCODE)" }
    } finally { $ErrorActionPreference = $prev }
}

function Test-DockerEngine {
    $exe = Get-DockerExe
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $exe info --format '{{.ServerVersion}}' 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }
    finally { $ErrorActionPreference = $prev }
}

function Wait-DockerEngine([int]$TimeoutSeconds = 180) {
    if (Test-DockerEngine) { return }
    $app = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    if (-not (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue) -and (Test-Path $app)) {
        Write-Host 'Avvio Docker Desktop...'
        Start-Process $app
    }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep 3
        if (Test-DockerEngine) { return }
    }
    throw "Il motore Docker non risponde dopo $TimeoutSeconds s. Apri Docker Desktop e riprova."
}

function Test-Gui {
    try {
        Invoke-WebRequest "http://127.0.0.1:$Port/agent/status" -UseBasicParsing -TimeoutSec 2 | Out-Null
        return $true
    } catch { return $false }
}

function Wait-Http([string]$Url, [int]$TimeoutSeconds = 180) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try { Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 3 | Out-Null; return $true } catch { Start-Sleep 3 }
    }
    return $false
}

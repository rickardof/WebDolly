# Setup una tantum su Windows (equivalente di scripts/setup.sh).
# Prerequisiti: Docker Desktop (backend WSL2, con driver NVIDIA aggiornato per la GPU)
# e Python 3.12. Idempotente: si puo' rilanciare.
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
#
# Modello: di default cerca in models\ i file
#   Fara1.5-27B-Q4_K_M.gguf  +  mmproj-Fara1.5-27B-bf16.gguf (o Fara1.5-27B.mmproj-q8_0.gguf)
# Per un altro modello: -Model e -Mmproj (nomi di file dentro models\).
param(
    [string]$Model = 'Fara1.5-27B-Q4_K_M.gguf',
    [string]$Mmproj = '',
    [int]$Context = 32768,
    [switch]$SkipModel
)
. (Join-Path $PSScriptRoot 'common.ps1')
Set-Location $Root

Write-Host '== venv Python =='
if (-not (Test-Path $VenvPython)) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { & py -3.12 -m venv (Join-Path $Root 'venv_pw') }
    else {
        $local = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path $local) { & $local -m venv (Join-Path $Root 'venv_pw') }
        else { & python -m venv (Join-Path $Root 'venv_pw') }
    }
    if (-not (Test-Path $VenvPython)) { throw 'Impossibile creare venv_pw: installa Python 3.12 (winget install Python.Python.3.12).' }
}
& $VenvPython -m pip install -q -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install fallito' }

Write-Host '== Docker =='
Wait-DockerEngine
try { Invoke-Docker network create fara-net | Out-Null } catch { }   # esiste gia'
Invoke-Docker build -t fara-browser    docker/browser/    | Select-Object -Last 1
Invoke-Docker build -t fara-browser-pw docker/browser-pw/ | Select-Object -Last 1

Write-Host '== container del browser (fara-browser-pw) =='
New-Item -ItemType Directory -Force (Join-Path $Root 'var\logs\pw'), (Join-Path $Root 'var\profiles\pw') | Out-Null
try { Invoke-Docker rm -f fara-browser-pw | Out-Null } catch { }
# Chromium runs with its own sandbox; FARA_NO_SANDBOX=1 turns it off for a host
# whose kernel has no user namespaces (see docs\security.md).
$sandboxEnv = @(); if ($env:FARA_NO_SANDBOX -eq '1') { $sandboxEnv = @('-e', 'FARA_NO_SANDBOX=1') }
Invoke-Docker run -d --name fara-browser-pw --restart unless-stopped `
    --network fara-net --shm-size 64m `
    --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 1024 `
    --security-opt "seccomp=$Root\docker\seccomp-chromium.json" `
    @sandboxEnv `
    -p 127.0.0.1:9333:9333 -p 127.0.0.1:5901:5900 `
    -v "$Root\fixtures\pw:/data" `
    -v "$Root\var\profiles\pw:/home/browser/chromium-profile" `
    fara-browser-pw | Out-Null

if ($SkipModel) {
    Write-Host '== modello locale saltato (-SkipModel). Usa FARA_URL / FARA_MODEL / FARA_API_KEY.'
} else {
    if (-not $Mmproj) {
        foreach ($c in 'mmproj-Fara1.5-27B-bf16.gguf', 'Fara1.5-27B.mmproj-q8_0.gguf') {
            if (Test-Path (Join-Path $Root "models\$c")) { $Mmproj = $c; break }
        }
    }
    if (-not (Test-Path (Join-Path $Root "models\$Model"))) {
        Write-Host "== modello locale saltato: models\$Model non trovato."
    } elseif (-not $Mmproj -or -not (Test-Path (Join-Path $Root "models\$Mmproj"))) {
        Write-Host '== modello locale saltato: nessun file mmproj trovato in models\.'
    } else {
        Write-Host "== container del modello (fara-test): $Model + $Mmproj =="
        try { Invoke-Docker rm -f fara-test | Out-Null } catch { }
        Invoke-Docker run -d --name fara-test --restart unless-stopped `
            --gpus all --network fara-net `
            --security-opt no-new-privileges:true `
            -p 127.0.0.1:8090:8080 `
            -v "$Root\models:/models:ro" `
            ghcr.io/ggml-org/llama.cpp:server-cuda `
            -m "/models/$Model" --mmproj "/models/$Mmproj" `
            --host 0.0.0.0 --port 8080 `
            --reasoning-format none -ngl 99 -c $Context | Out-Null
    }
}

Write-Host ''
Write-Host 'Fatto. Avvia con: powershell -ExecutionPolicy Bypass -File scripts\windows\start.ps1'

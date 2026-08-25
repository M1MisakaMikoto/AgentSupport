param(
    [switch]$NoWait,
    [string]$WslDistro = "Ubuntu"
)

$ErrorActionPreference = "Stop"

function Get-LinuxRoot {
    $windows = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $drive = $windows.Substring(0, 1).ToLower()
    $rest = $windows.Substring(3) -replace "\\", "/"
    return "/mnt/$drive/$rest"
}

$linuxRoot = Get-LinuxRoot
$composeFile = "$linuxRoot/docker-compose.yml"
$dockerDirect = [bool](Get-Command docker -ErrorAction SilentlyContinue)

function Invoke-Compose {
    param([string[]]$ComposeArgs)
    if ($dockerDirect) {
        & docker compose @ComposeArgs
    } else {
        & wsl -d $WslDistro -- docker compose @ComposeArgs
    }
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed: $($ComposeArgs -join ' ')"
    }
}

if (-not $dockerDirect) {
    & wsl -d $WslDistro -- docker version --format "{{.Server.Version}}" *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Docker not available on PATH or via WSL ($WslDistro)."
        exit 1
    }
}

Write-Host "Starting infrastructure (postgres, redis)..."
Invoke-Compose -ComposeArgs @("-f", $composeFile, "up", "-d", "postgres", "redis")

$temporalImage = (& wsl -d $WslDistro -- docker images -q temporalio/dev-server:latest 2>$null | Out-String).Trim()
if ($temporalImage) {
    Write-Host "Starting Temporal via Compose service..."
    Invoke-Compose -ComposeArgs @("-f", $composeFile, "up", "-d", "temporal")
} else {
    $wslTemporal = (& wsl -d $WslDistro -- sh -c "command -v temporal || true" | Out-String).Trim()
    if (-not $wslTemporal) {
        & wsl -d $WslDistro -- test -x /home/pzdl0013/bin/temporal
        if ($LASTEXITCODE -eq 0) {
            $wslTemporal = "/home/pzdl0013/bin/temporal"
        }
    }
    if ($wslTemporal) {
        Write-Host "Starting Temporal dev server via WSL CLI ($wslTemporal)..."
        Start-Process -FilePath "wsl" -ArgumentList "-d", $WslDistro, "--", $wslTemporal, "server", "start-dev", "--headless", "--port", "7233", "--namespace", "default" -WindowStyle Hidden
    } else {
        Write-Warning "Temporal not started: Compose image unavailable and no WSL temporal CLI found."
        Write-Warning "Install it or run: wsl -d $WslDistro -- <path>/temporal server start-dev --headless --port 7233"
    }
}

# --no-deps: api/worker depend on temporal, which may be running outside Compose.
Write-Host "Starting temporal-worker, api and gateway..."
Invoke-Compose -ComposeArgs @("-f", $composeFile, "up", "-d", "--no-deps", "temporal-worker", "api", "gateway")

if ($NoWait) {
    Write-Host "Stack started (no readiness wait)."
    exit 0
}

Write-Host "Waiting for API readiness at http://127.0.0.1:8000/ready ..."
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:8000/ready" -TimeoutSec 3
        if ($response.status -eq "ready") {
            Write-Host "API ready."
            Write-Host "OpenAPI:  http://127.0.0.1:8000/docs"
            Write-Host "Debug UI: http://127.0.0.1:8000/debug/"
            exit 0
        }
    } catch {
        Start-Sleep -Seconds 2
    }
}

Write-Error "API did not become ready within 120s. Run 'docker compose ps' to inspect."
exit 1

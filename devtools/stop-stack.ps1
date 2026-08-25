param(
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

if (-not $dockerDirect) {
    & wsl -d $WslDistro -- docker version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Docker not available on PATH or via WSL ($WslDistro)."
        exit 1
    }
}

if ($dockerDirect) {
    & docker compose -f (Join-Path $PSScriptRoot "docker-compose.yml") down
} else {
    & wsl -d $WslDistro -- docker compose -f $composeFile down
}
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Stack stopped. Note: a WSL CLI Temporal dev server (if used) keeps running; stop it with:"
Write-Host "  wsl -d $WslDistro -- pkill -f 'temporal server'"

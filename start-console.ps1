param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$consoleUrl = "http://127.0.0.1:8010/"
$tempRoot = Join-Path $projectRoot ".tmp"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python"
}

New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

function Test-ConsoleReady {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $consoleUrl -TimeoutSec 1
    }
    catch {
        return $false
    }
    if ($response.StatusCode -eq 200 -and $response.Content -notmatch "dev-console-token") {
        throw "Port 8010 is already used by another HTTP service."
    }
    return $response.StatusCode -eq 200
}

if (-not (Test-ConsoleReady)) {
    $process = Start-Process `
        -FilePath $python `
        -ArgumentList "-m", "devtools.console", "--host", "127.0.0.1", "--port", "8010" `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $tempRoot "dev-console.out.log") `
        -RedirectStandardError (Join-Path $tempRoot "dev-console.err.log") `
        -PassThru
    Set-Content -LiteralPath (Join-Path $tempRoot "dev-console.pid") -Value $process.Id -Encoding ascii

    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        if (Test-ConsoleReady) {
            $ready = $true
            break
        }
    }
    if (-not $ready) {
        $errorLog = Join-Path $tempRoot "dev-console.err.log"
        $detail = if (Test-Path -LiteralPath $errorLog) {
            (Get-Content -LiteralPath $errorLog -Tail 12) -join [Environment]::NewLine
        }
        else {
            "No server log was created."
        }
        throw "Development console did not become ready.`n$detail"
    }
}

if (-not $NoBrowser) {
    Start-Process $consoleUrl
}

Write-Output "AgentSupport console: $consoleUrl"

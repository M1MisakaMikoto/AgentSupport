$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$docker = 'E:\Docker\resources\bin\docker.exe'
$image = 'python:3.12-slim'
$prefix = 'agent-state-lab-'
$warmName = "${prefix}warm"
$scaleNames = 1..10 | ForEach-Object { "${prefix}scale-$_" }

function Measure-Milliseconds([scriptblock]$Action) {
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    & $Action
    $watch.Stop()
    return $watch.Elapsed.TotalMilliseconds
}
function Get-Summary([double[]]$Values) {
    $sorted = $Values | Sort-Object
    $median = if ($sorted.Count % 2 -eq 0) {
        ($sorted[$sorted.Count / 2 - 1] + $sorted[$sorted.Count / 2]) / 2
    } else {
        $sorted[[Math]::Floor($sorted.Count / 2)]
    }
    return [ordered]@{
        samples = $sorted.Count
        median_ms = [Math]::Round($median, 3)
        p95_ms = [Math]::Round($sorted[[Math]::Floor(($sorted.Count - 1) * 0.95)], 3)
        min_ms = [Math]::Round($sorted[0], 3)
        max_ms = [Math]::Round($sorted[-1], 3)
    }
}

$env:PATH = "E:\Docker\resources\bin;$env:PATH"
& $docker pull $image | Out-Null

try {
    $freshRunTimes = @()
    1..10 | ForEach-Object {
        $freshRunTimes += Measure-Milliseconds {
            & $docker run --rm $image python -c 'pass' | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'fresh docker run failed' }
        }
    }

    & $docker run -d --name $warmName $image sleep infinity | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'warm container creation failed' }

    $warmExecTimes = @()
    1..10 | ForEach-Object {
        $warmExecTimes += Measure-Milliseconds {
            & $docker exec $warmName python -c 'pass' | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'warm docker exec failed' }
        }
    }

    $scaleWatch = [System.Diagnostics.Stopwatch]::StartNew()
    foreach ($name in $scaleNames) {
        & $docker run -d --name $name $image sleep infinity | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "scale container creation failed: $name" }
    }
    $scaleWatch.Stop()

    $stats = @(& $docker stats --no-stream --format '{{json .}}' $warmName $scaleNames | ForEach-Object { $_ | ConvertFrom-Json })
    $result = [ordered]@{
        docker_desktop = (& $docker version --format '{{.Server.Platform.Name}}')
        engine_version = (& $docker version --format '{{.Server.Version}}')
        image = $image
        fresh_container_per_task = Get-Summary $freshRunTimes
        warm_container_exec = Get-Summary $warmExecTimes
        sequential_scale_to_10_ms = [Math]::Round($scaleWatch.Elapsed.TotalMilliseconds, 3)
        running_container_stats = @($stats | ForEach-Object {
            [ordered]@{
                name = $_.Name
                memory = $_.MemUsage
                cpu = $_.CPUPerc
                pids = $_.PIDs
            }
        })
    }

    $output = Join-Path $root 'results\container-cost.json'
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $output -Encoding UTF8
    $result | ConvertTo-Json -Depth 8
}
finally {
    $targets = @($warmName) + $scaleNames
    foreach ($name in $targets) {
        & $docker rm -f $name 2>$null | Out-Null
    }
}

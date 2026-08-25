param(
    [Parameter(Mandatory = $true)]
    [string]$Prompt,

    [Parameter(Mandatory = $true)]
    [string]$TrajectoryFile,

    [string]$ProjectRoot = 'E:\PythonProject\WorkBranch',
    [string]$Provider = 'openai',
    [string]$Model = 'qwen3.6-plus',
    [string]$BaseUrl = 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    [string]$ApiKeyEnvironmentName = 'OPENAI_API_KEY',
    [int]$MaxSteps = 6
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$cli = Resolve-Path (Join-Path $ProjectRoot '.tools\bin\trae-cli.exe')
$workspace = Join-Path $root 'workspace'
$trajectory = Join-Path $root $TrajectoryFile

if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($ApiKeyEnvironmentName, 'Process'))) {
    throw "Required API key environment variable is empty: $ApiKeyEnvironmentName"
}

$runtimeConfig = @"
agents:
  trae_agent:
    enable_lakeview: false
    model: lab_model
    max_steps: $MaxSteps
    tools:
      - bash
      - str_replace_based_edit_tool
      - task_done
allow_mcp_servers: []
mcp_servers: {}
model_providers:
  $Provider`:
    api_key: ''
    provider: $Provider
    base_url: $BaseUrl
models:
  lab_model:
    model_provider: $Provider
    model: $Model
    max_tokens: 512
    temperature: 0
    top_p: 1
    top_k: 0
    max_retries: 1
    parallel_tool_calls: false
"@
$runtimeConfigPath = Join-Path $root 'results\runtime-config.yaml'
New-Item -ItemType Directory -Path (Split-Path -Parent $runtimeConfigPath) -Force | Out-Null
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($runtimeConfigPath, $runtimeConfig, $utf8NoBom)

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
try {
    & $cli run $Prompt `
        --working-dir $workspace `
        --config-file $runtimeConfigPath `
        --provider $Provider `
        --model $Model `
        --model-base-url $BaseUrl `
        --max-steps $MaxSteps `
        --trajectory-file $trajectory `
        --console-type simple
    if ($LASTEXITCODE -ne 0) {
        throw "trae-cli exited with code $LASTEXITCODE"
    }
}
finally {
    $stopwatch.Stop()
    [pscustomobject]@{
        prompt_chars = $Prompt.Length
        estimated_tokens = [Math]::Ceiling($Prompt.Length / 4)
        elapsed_ms = $stopwatch.ElapsedMilliseconds
        trajectory_file = $trajectory
        provider = $Provider
        model = $Model
    } | ConvertTo-Json -Compress
}

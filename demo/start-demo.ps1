# AgentSupport Skill 生成 · 会议演示一键启动
# 用法：powershell -ExecutionPolicy Bypass -File demo\start-demo.ps1
$ErrorActionPreference = 'Stop'
$proj = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path 'D:\workspace')) { New-Item -ItemType Directory -Path 'D:\workspace' | Out-Null }

Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $_.LocalPort -in 8000,8080,8900 } |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

$logDir = Join-Path $env:TEMP 'agentsupport-demo'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$envFile = Join-Path $proj '.env'
$vars = @{}
Get-Content -LiteralPath $envFile | ForEach-Object { if ($_ -match '^([A-Za-z0-9_]+)=(.*)$') { $vars[$matches[1]] = $matches[2].Trim() } }
$launcher = Join-Path $PSScriptRoot '_launcher.py'

$env:PYTHONPATH = 'src;vendor\trae-agent-src'
$env:SESSION_RUNNER_MODE = 'trae'; $env:SESSION_RUNNER_PROVIDER = 'trae'; $env:SESSION_RUNNER_VERSION = '0.1.0'
$env:SESSION_RUNNER_HEARTBEAT_SECONDS = '10'
$env:SESSION_RUNNER_WORKSPACE_ROOTS = 'D:\workspace'
$env:SESSION_RUNNER_TRAE_CONFIG = Join-Path $proj 'src\session_runner\trae_config.yaml'
$env:SESSION_RUNNER_TOKEN = $vars['AGENTSUPPORT_RUNNER_TOKEN']
$env:TRAE_PROVIDER = $vars['TRAE_PROVIDER']; $env:TRAE_MODEL = $vars['TRAE_MODEL']
$env:TRAE_MODEL_BASE_URL = $vars['TRAE_MODEL_BASE_URL']; $env:TRAE_MODEL_HOST = $vars['TRAE_MODEL_HOST']
$env:TRAE_API_KEY = $vars['TRAE_API_KEY']; $env:TRAE_MAX_STEPS = '24'
$env:HTTPS_PROXY = $vars['HTTPS_PROXY']; $env:HTTP_PROXY = $vars['HTTP_PROXY']; $env:NO_PROXY = 'localhost,127.0.0.1,api,runner'
Start-Process -FilePath (Join-Path $proj '.venv\Scripts\python.exe') -ArgumentList @('-m','uvicorn','session_runner.main:app','--host','127.0.0.1','--port','8080') -WorkingDirectory $proj -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'runner.out.log') -RedirectStandardError (Join-Path $logDir 'runner.err.log')
Start-Sleep -Seconds 5

$env:PYTHONPATH = 'src'
$env:AGENTSUPPORT_PERSISTENCE_MODE = 'memory'; $env:AGENTSUPPORT_EXECUTION_MODE = 'inline'; $env:AGENTSUPPORT_RUNTIME_DRIVER = 'memory'
$env:AGENTSUPPORT_CORE_RUNNER_URL = 'http://127.0.0.1:8080'; $env:AGENTSUPPORT_CORE_RUNNER_TIMEOUT_SECONDS = '900'
$env:AGENTSUPPORT_WORKSPACE_ROOT = Join-Path $proj 'workspace-data'
$env:AGENTSUPPORT_SKILLS_ROOT = Join-Path $proj 'skills'
$env:AGENTSUPPORT_AUTO_CREATE_SCHEMA = 'false'; $env:AGENTSUPPORT_ENABLED_SKILLS = ''
Start-Process -FilePath (Join-Path $proj '.venv\Scripts\python.exe') -ArgumentList @($launcher) -WorkingDirectory $proj -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'api.out.log') -RedirectStandardError (Join-Path $logDir 'api.err.log')
Start-Sleep -Seconds 8

Start-Process -FilePath (Join-Path $proj '.venv\Scripts\python.exe') -ArgumentList @('demo\demo_server.py') -WorkingDirectory $proj -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'demo.out.log') -RedirectStandardError (Join-Path $logDir 'demo.err.log')
Start-Sleep -Seconds 6

foreach ($u in @('http://127.0.0.1:8000/ready','http://127.0.0.1:8080/ready','http://127.0.0.1:8900/api/state')) {
  try { Invoke-WebRequest -Uri $u -TimeoutSec 5 -UseBasicParsing | Out-Null; Write-Output "OK  $u" }
  catch { Write-Output "FAIL $u  $($_.Exception.Message)" }
}
Write-Output ''
Write-Output '演示环境已启动：请打开 http://127.0.0.1:8900'
Write-Output "日志：$logDir\*.log"
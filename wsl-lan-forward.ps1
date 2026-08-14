#requires -Version 5.1
<#
.SYNOPSIS
    Expose a service running inside WSL2 (default: AgentSupport API on port 8000) to the LAN.

.DESCRIPTION
    WSL2 uses NAT, so a Docker Compose stack running inside WSL is normally only reachable
    from the Windows host via localhost. This script creates:

      1. A Windows portproxy rule: <LAN IP>:8000 -> <WSL IP>:8000
      2. A Windows Firewall inbound allow rule for TCP 8000, scoped to the local subnet.

    After running it, other machines on the LAN can open:
        http://<windows-lan-ip>:8000/docs

    WSL2 IPs change on every WSL restart, so re-run this script afterwards to refresh the
    mapping. The script self-elevates; approve the UAC prompt when it appears.

.EXAMPLE
    .\wsl-lan-forward.ps1

.EXAMPLE
    .\wsl-lan-forward.ps1 -Port 8000 -WslDistro Ubuntu -LanAddress 10.50.172.41
#>
param(
    [int]$Port = 8000,
    [string]$WslDistro = "Ubuntu",
    [string]$DocsPath = "/docs",
    [string]$LanAddress = "",
    [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"

if (-not $LogFile) {
    $LogFile = Join-Path $env:TEMP "wsl-lan-forward.log"
}

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Message
    Add-Content -LiteralPath $LogFile -Value $line -Encoding utf8
    Write-Host $line
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Remove-Item -LiteralPath $LogFile -ErrorAction SilentlyContinue
    $argList = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", ("`"{0}`"" -f $PSCommandPath),
        "-Port", "$Port",
        "-WslDistro", $WslDistro,
        "-DocsPath", $DocsPath
    )
    if ($LanAddress) {
        $argList += @("-LanAddress", $LanAddress)
    }
    $argList += @("-LogFile", ("`"{0}`"" -f $LogFile))

    Write-Host "Requesting administrator privileges (approve the UAC prompt)..."
    try {
        $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $argList -Verb RunAs -Wait -PassThru
    }
    catch {
        Write-Host "Elevation failed or was declined: $($_.Exception.Message)"
        exit 1
    }
    if (Test-Path -LiteralPath $LogFile) {
        Get-Content -LiteralPath $LogFile
    }
    exit $proc.ExitCode
}

# ---------------------------------------------------------------------------
# Elevated path
# ---------------------------------------------------------------------------
Remove-Item -LiteralPath $LogFile -ErrorAction SilentlyContinue

$ruleName = "AgentSupport WSL LAN $Port"
Write-Log "Exposing port $Port (docs path: $DocsPath) from WSL distro '$WslDistro' to the LAN."

# 1. Resolve the WSL2 IP (changes on every WSL restart).
$wslOutput = (& wsl.exe -d $WslDistro hostname -I 2>$null) -join " "
$wslIp = ($wslOutput -split "\s+") |
    Where-Object { $_ -match "^(\d{1,3}\.){3}\d{1,3}$" } |
    Select-Object -First 1
if (-not $wslIp) {
    Write-Log "ERROR: could not resolve a WSL IP for distro '$WslDistro'. Is WSL running?"
    exit 1
}
Write-Log "WSL2 IP: $wslIp"

# 2. Resolve the Windows LAN IP (interface used by the default route).
if (-not $LanAddress) {
    $defaultRoute = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Where-Object { $_.NextHop -ne "0.0.0.0" } |
        Sort-Object RouteMetric |
        Select-Object -First 1
    if ($defaultRoute) {
        $addr = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $defaultRoute.ifIndex -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notmatch "^127\." -and $_.IPAddress -notmatch "^169\.254\." } |
            Select-Object -First 1
        $LanAddress = if ($addr) { $addr.IPAddress } else { "" }
    }
}
if (-not $LanAddress) {
    $LanAddress = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch "^127\." -and $_.IPAddress -notmatch "^169\.254\." } |
        Select-Object -First 1).IPAddress
}
if (-not $LanAddress) {
    Write-Log "ERROR: could not determine a Windows LAN IPv4 address. Pass -LanAddress explicitly."
    exit 1
}
Write-Log "Windows LAN IP (portproxy listen address): $LanAddress"

# 3. (Re)create the portproxy mapping. The listen address must be the LAN IP:
#    WSL's own localhost relay already owns 127.0.0.1:$Port, so 0.0.0.0 would conflict.
foreach ($listen in @($LanAddress, "0.0.0.0")) {
    & netsh.exe interface portproxy delete v4tov4 listenaddress=$listen listenport=$Port 2>$null | Out-Null
}
& netsh.exe interface portproxy add v4tov4 listenaddress=$LanAddress listenport=$Port connectaddress=$wslIp connectport=$Port
if ($LASTEXITCODE -ne 0) {
    Write-Log "ERROR: 'netsh interface portproxy add' failed (exit code $LASTEXITCODE)."
    exit 1
}
Write-Log "portproxy: $LanAddress`:$Port -> $wslIp`:$Port"

# 4. (Re)create the firewall rule, scoped to the local subnet for safety.
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Remove-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
}
New-NetFirewallRule `
    -DisplayName $ruleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $Port `
    -RemoteAddress LocalSubnet `
    -ErrorAction Stop | Out-Null
Write-Log "firewall: inbound TCP $Port allowed (local subnet only), rule '$ruleName'"

# 5. Verify the docs endpoint through the new mapping.
$url = "http://${LanAddress}:${Port}${DocsPath}"
Start-Sleep -Milliseconds 500
$code = (& curl.exe --noproxy "*" -s -o NUL -w "%{http_code}" --connect-timeout 5 $url 2>$null)
if ($code -eq "200") {
    Write-Log "OK: $url returned HTTP 200."
}
else {
    Write-Log "WARNING: $url returned HTTP '$code' (the docs path may differ, or the WSL stack is not up)."
}
Write-Log "LAN access URL: http://${LanAddress}:${Port}/docs"
Write-Log "NOTE: this API intentionally has no authentication; the firewall rule is scoped to the local subnet. Re-run this script after WSL restarts to refresh the WSL IP."

exit 0

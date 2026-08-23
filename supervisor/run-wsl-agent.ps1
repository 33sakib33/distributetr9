param()

$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $here "lab-agent.log"
$starter = Join-Path $here "start-agent.sh"
$cfgPath = Join-Path $here "settings.json"

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "o"), $msg
    try {
        if ((Test-Path $log) -and ((Get-Item $log).Length -gt 2MB)) {
            Move-Item $log "$log.old" -Force
        }
        Add-Content -Path $log -Value $line -Encoding UTF8
    } catch {}
    Write-Host $line
}

function ConvertTo-LinuxPath([string]$p, [string]$user) {
    $p = [string]$p
    $p = $p.Trim().Trim('"').Replace('\', '/')
    if ($p.StartsWith('~/')) { $p = "/home/$user/" + $p.Substring(2) }
    if ($p -and $p -notmatch '^/') { $p = "/$p" }
    return $p
}

function ConvertTo-WslMount([string]$winPath) {
    $full = [System.IO.Path]::GetFullPath($winPath).Replace('\', '/')
    if ($full -match '^([A-Za-z]):/(.*)$') {
        return "/mnt/$($Matches[1].ToLower())/$($Matches[2])"
    }
    return $full
}

$Distro = "Ubuntu"
$Api = "http://10.10.126.226:8080"
$Config = "/etc/lab-agent/config.json"
$AgentDir = "/home/sakib/distributetr9_node"
$WslUser = "sakib"

if (Test-Path $cfgPath) {
    try {
        $cfg = Get-Content -Raw -Path $cfgPath | ConvertFrom-Json
        if ($cfg.Distro) { $Distro = [string]$cfg.Distro }
        if ($cfg.Api) { $Api = [string]$cfg.Api }
        if ($cfg.Config) { $Config = [string]$cfg.Config }
        if ($cfg.AgentDir) { $AgentDir = [string]$cfg.AgentDir }
        if ($cfg.WslUser) { $WslUser = [string]$cfg.WslUser }
    } catch {
        Write-Log "failed to read $cfgPath : $_"
    }
}

$linuxDir = ConvertTo-LinuxPath $AgentDir $WslUser
$linuxConfig = ConvertTo-LinuxPath $Config $WslUser
if (-not $linuxDir) { $linuxDir = "/home/$WslUser/distributetr9_node" }

$template = @'
#!/bin/bash
set +u
export LAB_API='__API__'
export LAB_CONFIG='__CONFIG__'
export LAB_AGENT_DIR='__DIR__'
echo "user=$(id -un) uid=$(id -u) home=$HOME pwd=$(pwd) dir=$LAB_AGENT_DIR"
echo "--- ls dir ---"
ls -la "$LAB_AGENT_DIR" 2>&1 || echo "cannot ls $LAB_AGENT_DIR"
echo "--- ls dir/agent ---"
ls -la "$LAB_AGENT_DIR/agent" 2>&1 || true
echo "--- find agent.py/run.sh ---"
find "$HOME" /opt/lab-agent "$LAB_AGENT_DIR" -maxdepth 4 \( -name 'agent.py' -o -name 'run.sh' \) 2>/dev/null | head -40
DIR=""
for CAND in "$LAB_AGENT_DIR" "$LAB_AGENT_DIR/agent" /opt/lab-agent "$HOME/distributetr9_node" "$HOME/distributetr9_node/agent" "$HOME/distributetr9/agent"; do
  echo "try $CAND"
  if [ -f "$CAND/agent.py" ]; then
    DIR="$CAND"
    break
  fi
done
if [ -z "$DIR" ]; then
  echo "lab agent not found (no agent.py in candidates)"
  exit 1
fi
export LAB_AGENT_DIR="$DIR"
echo "start in $DIR"
cd "$DIR" || exit 1
if [ -f ./run.sh ]; then
  exec bash ./run.sh
fi
exec python3 ./agent.py --config "$LAB_CONFIG" --api "$LAB_API" --executor auto
'@

$script = $template.Replace('__API__', $Api).Replace('__CONFIG__', $linuxConfig).Replace('__DIR__', $linuxDir)
$unix = (($script -replace "`r`n", "`n") -replace "`r", "`n")
if (-not $unix.EndsWith("`n")) { $unix = $unix + "`n" }
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($starter, $unix, $utf8)

function Ensure-Distro {
    cmd.exe /c "wsl.exe -d $Distro -u $WslUser -- echo ok" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "distro not ready, terminating $Distro"
        cmd.exe /c "wsl.exe --terminate $Distro" 2>$null | Out-Null
        Start-Sleep -Seconds 3
        cmd.exe /c "wsl.exe -d $Distro -u $WslUser -- echo ok" | Out-Null
    }
}

Write-Log "supervisor start distro=$Distro wslUser=$WslUser api=$Api agentDir=$linuxDir"
Start-Sleep -Seconds 8

while ($true) {
    try {
        Ensure-Distro
        $wslPath = ConvertTo-WslMount $starter
        Write-Log "starting bash $wslPath"
        $line = "wsl.exe -d $Distro -u $WslUser -- bash $wslPath"
        cmd.exe /c $line 2>&1 | ForEach-Object { Write-Log "$_" }
        Write-Log "wsl agent exited code=$LASTEXITCODE"
    } catch {
        Write-Log "supervisor error $_"
    }
    Start-Sleep -Seconds 5
}

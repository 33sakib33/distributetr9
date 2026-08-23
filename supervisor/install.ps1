param(
    [string]$Distro = "Ubuntu",
    [string]$Api = "http://192.168.68.118:8080",
    [string]$Config = "etc/lab-agent/config.json",
    [string]$AgentDir = "home/sakib/distributetr9_node",
    [string]$WslUser = "sakib"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $here "run-wsl-agent.ps1"
$taskName = "LabMlAgent"
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function ConvertTo-LinuxPath([string]$p, [string]$user) {
    $p = [string]$p
    $p = $p.Trim().Trim('"').Replace('\', '/')
    if ($p.StartsWith('~/')) { $p = "/home/$user/" + $p.Substring(2) }
    if ($p -notmatch '^/') { $p = "/$p" }
    return $p
}

$linuxDir = ConvertTo-LinuxPath $AgentDir $WslUser
$linuxConfig = ConvertTo-LinuxPath $Config $WslUser

# Do not put Linux paths on the scheduled-task command line. PowerShell treats /home as switches.
$cfg = @{
    Distro = $Distro
    Api = $Api
    Config = $linuxConfig
    AgentDir = $linuxDir
    WslUser = $WslUser
}
$cfgPath = Join-Path $here "settings.json"
$cfg | ConvertTo-Json | Set-Content -Path $cfgPath -Encoding UTF8
Write-Host "Wrote $cfgPath"
Write-Host "  distro=$Distro wslUser=$WslUser api=$Api agentDir=$linuxDir"

# SYSTEM cannot start WSL2 on most PCs. Run as the installing Windows user.
# S4U = whether or not that user is interactively logged on (no stored password).
$arg = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT45S"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType S4U -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Scheduled task $taskName installed and started as $account"
Write-Host "  log: $(Join-Path $here 'lab-agent.log')"
Write-Host "If the node stays offline: Get-Content $(Join-Path $here 'lab-agent.log') -Tail 60"

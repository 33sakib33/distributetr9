<#
.SYNOPSIS
One-command setup of a lab PC: WSL Ubuntu, Docker, the lab agent, and the
boot task that starts it without anyone logging in.

.DESCRIPTION
Run in an Administrator PowerShell. Safe to run again: it repairs or updates
an existing install. If WSL itself was just enabled, it asks for a reboot and
you run the same command again.

.EXAMPLE
# From the lab server (pilot, plain HTTP). The server fills in its own address.
powershell -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm http://10.0.0.5:8080/install/bootstrap.ps1)))"

.EXAMPLE
# From the lab server over HTTPS with the lab's own CA (LAB_TLS=internal).
curl.exe -fsSL --cacert C:\lab\lab-ca.crt -o C:\lab\bootstrap.ps1 https://lab-api.cse.uni.edu/install/bootstrap.ps1
powershell -ExecutionPolicy Bypass -File C:\lab\bootstrap.ps1 -CaCert C:\lab\lab-ca.crt

.EXAMPLE
# From a copy of the repo, e.g. on a USB stick.
supervisor\bootstrap.cmd -Api http://10.0.0.5:8080 -Registry 10.0.0.5:5001
#>
[CmdletBinding()]
param(
    [string]$Api = "__LAB_API__",
    [string]$Registry = "__LAB_REGISTRY__",
    [ValidateSet("train", "build")][string]$Role = "train",
    [ValidateSet("auto", "yes", "no")][string]$Gpu = "auto",
    [string]$CaCert = "",
    [string]$Distro = "Ubuntu",
    [string]$LinuxUser = "lab",
    [string]$Root = "C:\lab",
    # Ubuntu root filesystem to import when `wsl --install` cannot download one:
    # a local .tar.gz (e.g. from a USB stick) or a URL. Default: Canonical's 24.04 image.
    [string]$UbuntuImage = "",
    [switch]$ResetIdentity,
    [switch]$NoBootTask
)

$ErrorActionPreference = "Stop"
$env:WSL_UTF8 = "1"
$utf8 = New-Object System.Text.UTF8Encoding $false

function Step([string]$message) {
    Write-Host ""
    Write-Host "==> $message" -ForegroundColor Cyan
}

function Fail([string]$message, [int]$code = 1) {
    Write-Host "ERROR: $message" -ForegroundColor Red
    exit $code
}

# Runs a program inside the distro as root, with no shell re-parsing the arguments.
function Invoke-Wsl([string[]]$Command) {
    & wsl.exe -d $Distro -u root --exec @Command
    if ($LASTEXITCODE -ne 0) { Fail "inside $Distro failed: $($Command -join ' ')" }
}

# Windows PowerShell 5.1 turns each stderr line of a redirected native program
# into an error record, and with ErrorActionPreference=Stop the first one aborts
# the script (e.g. docker's "Unable to find image ... locally"). Run such
# commands with Continue and judge them by $LASTEXITCODE instead.
function Invoke-Native([scriptblock]$Block) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Block } finally { $ErrorActionPreference = $saved }
}

function ConvertTo-WslPath([string]$WindowsPath) {
    $full = [System.IO.Path]::GetFullPath($WindowsPath).Replace('\', '/')
    $out = & wsl.exe -d $Distro -u root --exec wslpath -a $full
    if ($LASTEXITCODE -ne 0 -or -not $out) { Fail "cannot map $WindowsPath into $Distro" }
    return ([string]$out).Trim()
}

# --- inputs -----------------------------------------------------------------

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "run this in an Administrator PowerShell"
}
if ($Api -like "__*") { $Api = "" }
if ($Registry -like "__*") { $Registry = "" }
if (-not $Api) { Fail "pass -Api with the control server address, e.g. -Api http://10.0.0.5:8080" }
$Api = $Api.TrimEnd("/")
if ($CaCert) {
    if (-not (Test-Path $CaCert)) { Fail "CA certificate not found: $CaCert" }
    $CaCert = (Resolve-Path $CaCert).Path
}
if ($Gpu -eq "auto") {
    $nvidia = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match "NVIDIA" }
    $useGpu = [bool]$nvidia
} else {
    $useGpu = ($Gpu -eq "yes")
}
New-Item -ItemType Directory -Force -Path $Root | Out-Null

Write-Host "Lab PC setup for $env:COMPUTERNAME"
Write-Host "  control API : $Api"
Write-Host "  registry    : $(if ($Registry) { $Registry } else { '(none; catalog images only)' })"
Write-Host "  role        : $Role   GPU: $(if ($useGpu) { 'yes' } else { 'no' })"

# --- WSL and the distro -----------------------------------------------------

Step "Checking WSL"
# `wsl --version` exists only in the current (Store) WSL, which GPU passthrough
# and systemd need. `wsl --update` moves an older inbox WSL onto it.
Invoke-Native { & wsl.exe --version *> $null }
if ($LASTEXITCODE -ne 0) {
    Invoke-Native { & wsl.exe --update *> $null }
    Invoke-Native { & wsl.exe --version *> $null }
}
if ($LASTEXITCODE -ne 0) {
    & wsl.exe --install --no-distribution
    Write-Host ""
    Write-Host "WSL has just been enabled. Reboot this PC, then run the same command again." -ForegroundColor Yellow
    exit 3010
}
Invoke-Native { & wsl.exe --update *> $null }

function Get-InstalledDistros {
    return @((& wsl.exe -l -q) -replace "`0", "" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

# wsl --install needs Microsoft's distribution list from raw.githubusercontent.com.
# When that is blocked, import Canonical's WSL image (or a local copy) instead.
function Import-UbuntuImage {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
    $source = $UbuntuImage
    if (-not $source) {
        $source = "https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-noble-wsl-$arch-wsl.rootfs.tar.gz"
    }
    if ($source -match "^https?://") {
        $image = Join-Path $Root "ubuntu-wsl-$arch.rootfs.tar.gz"
        if (-not (Test-Path $image)) {
            Write-Host "Downloading $source (about 350 MB)"
            & curl.exe -fL --retry 3 -o "$image.part" $source
            if ($LASTEXITCODE -ne 0) {
                Fail "could not download the Ubuntu image. Download it on another machine from $source, copy it here and rerun with -UbuntuImage <path>"
            }
            Move-Item "$image.part" $image -Force
        }
    } else {
        if (-not (Test-Path $source)) { Fail "Ubuntu image not found: $source" }
        $image = (Resolve-Path $source).Path
    }
    $target = Join-Path $Root "wsl\$Distro"
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Write-Host "Importing $image as $Distro (into $target)"
    & wsl.exe --import $Distro $target $image --version 2
    if ($LASTEXITCODE -ne 0) { Fail "wsl --import of $image failed" }
}

if ((Get-InstalledDistros) -notcontains $Distro) {
    Step "Installing $Distro"
    if (-not $UbuntuImage) {
        & wsl.exe --install -d $Distro --no-launch
        if ($LASTEXITCODE -ne 0) {
            Write-Host "wsl --install failed; retrying with --web-download" -ForegroundColor Yellow
            & wsl.exe --install -d $Distro --no-launch --web-download
        }
    }
    if ((Get-InstalledDistros) -contains $Distro) {
        # Initialise without the interactive first-run prompt; the user is created below.
        $launcherName = ($Distro.ToLower() -replace "[-.]", "") + ".exe"
        $launcher = Get-Command $launcherName -ErrorAction SilentlyContinue
        if ($launcher) { & $launcher.Source install --root | Out-Null }
    } else {
        Write-Host "Microsoft's distribution list could not be fetched; importing Ubuntu directly." -ForegroundColor Yellow
        Import-UbuntuImage
    }
}
& wsl.exe -d $Distro -u root --exec true
if ($LASTEXITCODE -ne 0) {
    Fail "$Distro did not start. Open it once from the Start menu, close it, then run this again."
}

Step "Linux user '$LinuxUser'"
$prep = Join-Path $Root "wsl-prep.sh"
$prepScript = @'
set -e
U="$1"
id -u "$U" >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo "$U"
touch /etc/wsl.conf
grep -q '^\[user\]' /etc/wsl.conf || printf '\n[user]\ndefault=%s\n' "$U" >> /etc/wsl.conf
echo "user $U ready"
'@
[System.IO.File]::WriteAllText($prep, $prepScript.Replace("`r`n", "`n"), $utf8)
Invoke-Wsl @("bash", (ConvertTo-WslPath $prep), $LinuxUser)

# --- the install package ----------------------------------------------------

$repo = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { "" }
if ($repo -and (Test-Path (Join-Path $repo "scripts\setup-lab-node.sh"))) {
    $src = $repo
    Step "Using the local copy at $src"
} else {
    Step "Downloading the lab PC package from $Api"
    $tgz = Join-Path $Root "lab-node.tar.gz"
    $curlArgs = @("-fsSL", "-o", $tgz, "$Api/install/lab-node.tar.gz")
    if ($CaCert) { $curlArgs = @("--cacert", $CaCert) + $curlArgs }
    & curl.exe @curlArgs
    if ($LASTEXITCODE -ne 0) { Fail "download failed; is $Api reachable from this PC?" }
    $src = Join-Path $Root "lab-node"
    if (Test-Path $src) { Remove-Item -Recurse -Force $src }
    New-Item -ItemType Directory -Path $src | Out-Null
    & tar.exe -xzf $tgz -C $src
    if ($LASTEXITCODE -ne 0) { Fail "could not unpack $tgz" }
}

# --- Docker and the agent inside WSL ----------------------------------------

Step "Installing Docker and the agent inside $Distro (this takes a few minutes)"
$installer = @("env", "LAB_USER=$LinuxUser", "bash", (ConvertTo-WslPath (Join-Path $src "scripts\setup-lab-node.sh")),
    "--api", $Api, "--role", $Role)
if ($Registry) { $installer += @("--registry", $Registry) }
if ($useGpu) { $installer += "--gpu" }
if ($CaCert) { $installer += @("--ca-cert", (ConvertTo-WslPath $CaCert)) }
if ($ResetIdentity) { $installer += "--reset-identity" }
Invoke-Wsl $installer

Step "Restarting WSL so the docker group takes effect"
& wsl.exe --shutdown

if ($useGpu) {
    Step "Checking the GPU from a container"
    # The NVIDIA runtime injects nvidia-smi into any container started with --gpus.
    $gpuOut = Invoke-Native { & wsl.exe -d $Distro -u $LinuxUser --exec docker run --rm --gpus all python:3.12.7-slim nvidia-smi -L 2>&1 }
    $gpuLines = @($gpuOut | ForEach-Object { "$_" } | Where-Object { $_ -match "\S" })
    if ($LASTEXITCODE -eq 0) {
        Write-Host "GPU passthrough works:"
        $gpuLines | Where-Object { $_ -match "^GPU " } | ForEach-Object { Write-Host "  $_" }
    } else {
        Write-Host "GPU passthrough did not work yet. Check the Windows NVIDIA driver; the agent reports the node unhealthy until it does." -ForegroundColor Yellow
        $gpuLines | Select-Object -Last 8 | ForEach-Object { Write-Host "  $_" }
    }
}

# --- boot task ----------------------------------------------------------------

if ($NoBootTask) {
    Write-Host ""
    Write-Host "Done. Start the agent by hand inside $Distro with: lab-agent"
    exit 0
}

Step "Installing the boot task"
$supervisor = Join-Path $Root "supervisor"
New-Item -ItemType Directory -Force -Path $supervisor | Out-Null
$from = Join-Path $src "supervisor"
if ((Resolve-Path $from).Path -ne (Resolve-Path $supervisor).Path) {
    Copy-Item (Join-Path $from "*.ps1"), (Join-Path $from "*.cmd") -Destination $supervisor -Force
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $supervisor "install.ps1") `
    -Distro $Distro -Api $Api -AgentDir "opt/lab-agent" -WslUser $LinuxUser
if ($LASTEXITCODE -ne 0) { Fail "boot task install failed" }

Step "Waiting for the agent to register with $Api"
$log = Join-Path $supervisor "lab-agent.log"
$deadline = (Get-Date).AddMinutes(4)
$registered = $null
while ((Get-Date) -lt $deadline) {
    if (Test-Path $log) {
        $registered = Select-String -Path $log -Pattern "registered node" | Select-Object -Last 1
        if ($registered) { break }
    }
    Start-Sleep -Seconds 5
}
if ($registered) {
    Write-Host $registered.Line
    Write-Host ""
    if ($registered.Line -match "class unsupported") {
        Write-Host "Registered, but this GPU is not in the supported class list, so the node is quarantined and gets no jobs." -ForegroundColor Yellow
        Write-Host "Ask the lab admin to add its class to agent/classes.json; the agent picks it up on its next update." -ForegroundColor Yellow
        exit 2
    }
    if ($registered.Line -match "healthy False") {
        Write-Host "Registered, but the agent reports itself unhealthy (CUDA check failed), so the node is quarantined." -ForegroundColor Yellow
        Write-Host "Check the Windows NVIDIA driver, then rerun this command." -ForegroundColor Yellow
        exit 2
    }
    Write-Host "Done. $env:COMPUTERNAME is a lab node. Reboot without logging in to confirm it comes back by itself." -ForegroundColor Green
} else {
    Write-Host "The agent has not registered yet. Last log lines:" -ForegroundColor Yellow
    if (Test-Path $log) { Get-Content $log -Tail 30 }
    exit 1
}

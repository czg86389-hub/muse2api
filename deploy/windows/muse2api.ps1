#requires -Version 5.1
<#
  muse2api Windows control script (start / stop / restart / status)

  ASCII-only on purpose: no chcp call, no non-ASCII bytes, so the script behaves
  the same on every Windows console codepage. User-facing text lives in README.md.

  Process ownership (used by both start and stop):

    On Windows a venv's python.exe/pythonw.exe is a launcher: it spawns the base
    interpreter, and that child is the process actually listening on the port.
    So "this repository" means: the listener's command line is a muse2api uvicorn
    server on this port, AND either the listener or one of its ancestors is an
    interpreter located in <repo>\venv\Scripts.

    A listener that fails that test is reported by PID/path and is never killed.
    Browser cleanup only touches processes started with this instance's exact
    profile directory AND this instance's CDP port.
#>
[CmdletBinding()]
param(
    [ValidateSet('menu', 'start', 'stop', 'restart', 'status', 'open', 'log')]
    [string]$Action = 'menu',

    # Allow stopping a muse2api server on this port that this script did not
    # start (for example one launched by hand). Still requires the command line
    # to be a muse2api uvicorn server; unrelated programs are never killed.
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# ---------- paths and configuration ----------

$ScriptDir = $PSScriptRoot
$Root      = (Resolve-Path -LiteralPath (Join-Path $ScriptDir '..\..')).Path
$EnvFile   = Join-Path $Root '.env'
$LogFile   = Join-Path $Root 'server.log'

function Read-DotEnv {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $s = $line.Trim()
        if (-not $s -or $s.StartsWith('#') -or -not $s.Contains('=')) { continue }
        $i = $s.IndexOf('=')
        $k = $s.Substring(0, $i).Trim()
        $v = $s.Substring($i + 1).Trim().Trim("'").Trim('"')
        if ($k) { $map[$k] = $v }
    }
    return $map
}

$DotEnv = Read-DotEnv -Path $EnvFile

function Get-Cfg {
    param([string]$Name, $Default)
    if ($DotEnv.ContainsKey($Name) -and $DotEnv[$Name]) { return $DotEnv[$Name] }
    return $Default
}

$Port      = [int](Get-Cfg 'MUSE2API_PORT' 18610)
$CdpPort   = [int](Get-Cfg 'MUSE2API_CDP_PORT' 19210)
$ListenIp  = [string](Get-Cfg 'MUSE2API_HOST' '127.0.0.1')
$ApiKey    = [string](Get-Cfg 'MUSE2API_KEY' '')
$ProfileRootRaw = [string](Get-Cfg 'MUSE2API_PROFILE_ROOT' (Join-Path $env:USERPROFILE 'muse2api-profiles'))
$ProfileDir = [System.IO.Path]::GetFullPath((Join-Path $ProfileRootRaw 'generate'))

$VenvScripts = [System.IO.Path]::GetFullPath((Join-Path $Root 'venv\Scripts'))
$PyExe       = Join-Path $VenvScripts 'python.exe'
$PywExe      = Join-Path $VenvScripts 'pythonw.exe'
$BaseUrl     = "http://127.0.0.1:$Port"
$PortPattern = '--port[\s"]+' + $Port + '(\s|"|$)'

function Get-PanelUrl {
    $url = "$BaseUrl/admin"
    if ($ApiKey) { $url = "${url}?key=$ApiKey" }
    return $url
}

# ---------- process inspection ----------

function Get-ProcessInfo {
    param([int]$ProcessId)
    return Get-CimInstance Win32_Process -Filter ("ProcessId=" + $ProcessId) -ErrorAction SilentlyContinue
}

function Get-PortOwner {
    param([int]$PortNumber)
    $conn = Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if (-not $conn) { return $null }
    $proc = Get-ProcessInfo -ProcessId $conn.OwningProcess
    if (-not $proc) { return $null }
    return [pscustomobject]@{
        ProcessId       = $proc.ProcessId
        ParentProcessId = $proc.ParentProcessId
        Name            = $proc.Name
        Path            = $proc.ExecutablePath
        CommandLine     = $proc.CommandLine
    }
}

function Test-InterpreterInThisVenv {
    param([string]$Path)
    if (-not $Path) { return $false }
    if (-not $Path.StartsWith($VenvScripts, [System.StringComparison]::OrdinalIgnoreCase)) { return $false }
    return [bool]($Path -match '\\(python|pythonw)\.exe$')
}

function Test-IsMuse2apiServer {
    <# Command line looks like a muse2api uvicorn server bound to this port. #>
    param($Proc)
    if (-not $Proc -or -not $Proc.CommandLine) { return $false }
    if ($Proc.CommandLine.IndexOf('uvicorn', [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
    if ($Proc.CommandLine.IndexOf('app:app', [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
    return [bool]($Proc.CommandLine -match $PortPattern)
}

function Get-Ancestors {
    param([int]$ProcessId, [int]$MaxDepth = 6)
    $result = @()
    $current = $ProcessId
    for ($i = 0; $i -lt $MaxDepth; $i++) {
        $proc = Get-ProcessInfo -ProcessId $current
        if (-not $proc) { break }
        if ($proc.ParentProcessId -eq 0) { break }
        if ($proc.ParentProcessId -eq $current) { break }
        $parent = Get-ProcessInfo -ProcessId $proc.ParentProcessId
        if (-not $parent) { break }
        $result += $parent
        $current = $parent.ProcessId
    }
    return $result
}

function Find-VenvLauncher {
    <# Topmost ancestor (or the listener itself) that runs this repo's venv
       interpreter. Returns $null when the listener does not belong to us. #>
    param($Owner)
    if (Test-InterpreterInThisVenv -Path $Owner.Path) { return $Owner }
    $launcher = $null
    foreach ($ancestor in (Get-Ancestors -ProcessId $Owner.ProcessId)) {
        if (Test-InterpreterInThisVenv -Path $ancestor.ExecutablePath) {
            $launcher = $ancestor
            break
        }
    }
    return $launcher
}

function Test-IsOwnService {
    param($Owner)
    if (-not $Owner) { return $false }
    if (-not (Test-IsMuse2apiServer -Proc $Owner)) { return $false }
    return [bool](Find-VenvLauncher -Owner $Owner)
}

function Format-Owner {
    param($Proc)
    $path = if ($Proc.Path) { $Proc.Path }
            elseif ($Proc.ExecutablePath) { $Proc.ExecutablePath }
            else { '<unknown>' }
    $cmd  = if ($Proc.CommandLine) { $Proc.CommandLine } else { '' }
    if ($cmd.Length -gt 160) { $cmd = $cmd.Substring(0, 160) + '...' }
    return ("PID {0} ({1})`n        {2}`n        {3}" -f $Proc.ProcessId, $Proc.Name, $path, $cmd)
}

function Test-BrowserBelongsToThisInstance {
    <# Browsers are ours only with BOTH this instance's absolute profile
       directory and this instance's CDP port on the command line. #>
    param([string]$CommandLine)
    if (-not $CommandLine) { return $false }
    $normalized = $CommandLine.Replace('/', '\')
    if ($normalized.IndexOf($ProfileDir, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
    $cdpArg = '--remote-debugging-port=' + $CdpPort
    if ($normalized.IndexOf($cdpArg, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
    return $true
}

function Clear-Orphans {
    <# Remove leftovers of THIS instance only: its own browser profile, and
       uvicorn servers started from this repository's venv on this port. #>
    $removed = 0

    foreach ($b in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                    Where-Object {
                        $_.Name -in @('chrome.exe', 'msedge.exe', 'chromium.exe') -and
                        (Test-BrowserBelongsToThisInstance -CommandLine $_.CommandLine)
                    })) {
        Stop-Process -Id $b.ProcessId -Force -ErrorAction SilentlyContinue
        $removed++
    }

    foreach ($s in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                    Where-Object {
                        $_.Name -in @('python.exe', 'pythonw.exe') -and
                        (Test-InterpreterInThisVenv -Path $_.ExecutablePath) -and
                        $_.CommandLine -and
                        $_.CommandLine.IndexOf('uvicorn', [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                        $_.CommandLine -match $PortPattern
                    })) {
        Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue
        $removed++
    }

    return $removed
}

# ---------- actions ----------

function Show-Status {
    $owner = Get-PortOwner -PortNumber $Port
    if (-not $owner) {
        Write-Host ("  State    : not running (port {0} is free)" -f $Port)
        return
    }
    if (Test-IsOwnService -Owner $owner) {
        Write-Host  "  State    : running (this repository)"
        Write-Host ("  Process  : {0}" -f (Format-Owner $owner))
        Write-Host ("  Panel    : {0}" -f (Get-PanelUrl))
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "$BaseUrl/healthz"
            Write-Host ("  Health   : HTTP {0}" -f $r.StatusCode)
        } catch {
            Write-Host  "  Health   : unreachable"
        }
        return
    }
    if (Test-IsMuse2apiServer -Proc $owner) {
        Write-Host ("  State    : a muse2api server on port {0} that this script did not start" -f $Port)
        Write-Host ("  Process  : {0}" -f (Format-Owner $owner))
        Write-Host  "             stop it with:  muse2api.bat stop -Force"
        return
    }
    Write-Host ("  State    : port {0} is used by a DIFFERENT program; this repository is not running" -f $Port)
    Write-Host ("  Occupant : {0}" -f (Format-Owner $owner))
}

function Start-Service {
    if (-not (Test-Path -LiteralPath $PyExe) -and -not (Test-Path -LiteralPath $PywExe)) {
        Write-Host "  [X] venv not found. Create it first:"
        Write-Host "        python -m venv venv"
        Write-Host "        venv\Scripts\python.exe -m pip install -r requirements.txt"
        return 2
    }

    $owner = Get-PortOwner -PortNumber $Port
    if ($owner) {
        if (Test-IsOwnService -Owner $owner) {
            Write-Host "  [i] Already running (this repository)."
            Write-Host ("  [i] Panel: {0}" -f (Get-PanelUrl))
            Start-Process (Get-PanelUrl) | Out-Null
            return 0
        }
        Write-Host ("  [X] Port {0} is already in use, and not by this repository:" -f $Port)
        Write-Host ("        {0}" -f (Format-Owner $owner))
        Write-Host  "      Refusing to start. The occupant was not touched."
        return 2
    }

    $interpreter = $PywExe
    if (-not (Test-Path -LiteralPath $interpreter)) { $interpreter = $PyExe }

    Write-Host ("Starting muse2api on port {0} ..." -f $Port)
    $inner = '""{0}" -m uvicorn app:app --host {1} --port {2} >> "{3}" 2>&1"' -f `
             $interpreter, $ListenIp, $Port, $LogFile
    Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\cmd.exe') `
                  -ArgumentList ('/c ' + $inner) `
                  -WorkingDirectory $Root `
                  -WindowStyle Hidden | Out-Null

    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 900
        $owner = Get-PortOwner -PortNumber $Port
        if (-not $owner) { continue }
        if (Test-IsOwnService -Owner $owner) {
            Write-Host ""
            Write-Host "  [OK] Started."
            Write-Host ("  [OK] Panel: {0}" -f (Get-PanelUrl))
            Write-Host ""
            Start-Process (Get-PanelUrl) | Out-Null
            return 0
        }
        Write-Host ("  [X] Port {0} was taken by a different program during startup:" -f $Port)
        Write-Host ("        {0}" -f (Format-Owner $owner))
        return 2
    }

    Write-Host ""
    Write-Host "  [X] Startup did not complete in time. See server.log in the repository root."
    Write-Host ""
    return 1
}

function Stop-Service {
    $owner = Get-PortOwner -PortNumber $Port
    if (-not $owner) {
        Write-Host "  [i] Service is not running."
        $n = Clear-Orphans
        if ($n -gt 0) { Write-Host ("  [i] Removed {0} leftover process(es) of this repository." -f $n) }
        return 0
    }

    $ownService = Test-IsOwnService -Owner $owner
    if (-not $ownService) {
        if (-not (Test-IsMuse2apiServer -Proc $owner)) {
            Write-Host ("  [X] Port {0} is used by a different program; this repository is not running:" -f $Port)
            Write-Host ("        {0}" -f (Format-Owner $owner))
            Write-Host  "      Nothing was stopped. Close that program or change MUSE2API_PORT in .env."
            return 2
        }
        if (-not $Force) {
            Write-Host ("  [!] Port {0} runs a muse2api server that this script did not start:" -f $Port)
            Write-Host ("        {0}" -f (Format-Owner $owner))
            Write-Host  "      Nothing was stopped. Re-run with -Force if you really want to stop it."
            return 2
        }
    }

    # Kill the outermost process of this instance's interpreter chain so the
    # browser children die with it.
    $target = $owner
    $launcher = Find-VenvLauncher -Owner $owner
    if ($launcher) {
        $top = $launcher
        foreach ($ancestor in (Get-Ancestors -ProcessId $launcher.ProcessId)) {
            if (Test-InterpreterInThisVenv -Path $ancestor.ExecutablePath) { $top = $ancestor } else { break }
        }
        $target = $top
    }

    Write-Host ("Stopping muse2api (PID {0}) ..." -f $target.ProcessId)
    try {
        & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /F /T /PID $target.ProcessId 2>&1 | Out-Null
    } catch {
        Write-Host "  [X] taskkill failed; run this script as Administrator."
        return 1
    }
    if ($target.ProcessId -ne $owner.ProcessId) {
        if (Get-ProcessInfo -ProcessId $owner.ProcessId) {
                    & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /F /T /PID $owner.ProcessId 2>&1 | Out-Null
        }
    }

    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 400
        if (-not (Get-PortOwner -PortNumber $Port)) { break }
    }

    $still = Get-PortOwner -PortNumber $Port
    if ($still) {
        Write-Host ("  [X] Port {0} is still held by PID {1}; try running as Administrator." -f $Port, $still.ProcessId)
        return 1
    }

    $n = Clear-Orphans
    Write-Host "  [OK] Stopped."
    if ($n -gt 0) { Write-Host ("  [OK] Also removed {0} leftover process(es) of this repository." -f $n) }
    return 0
}

function Restart-Service {
    $rc = Stop-Service
    if ($rc -ne 0) { return $rc }
    return (Start-Service)
}

function Open-Panel {
    $owner = Get-PortOwner -PortNumber $Port
    if (-not $owner -or -not (Test-IsOwnService -Owner $owner)) {
        Write-Host "  [i] Service is not running; start it first."
        return 2
    }
    Start-Process (Get-PanelUrl) | Out-Null
    return 0
}

function Open-Log {
    if (-not (Test-Path -LiteralPath $LogFile)) { New-Item -ItemType File -Path $LogFile | Out-Null }
    Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\notepad.exe') -ArgumentList $LogFile | Out-Null
    return 0
}

# ---------- entry point ----------

switch ($Action) {
    'start'   { exit (Start-Service) }
    'stop'    { exit (Stop-Service) }
    'restart' { exit (Restart-Service) }
    'status'  { Show-Status; exit 0 }
    'open'    { exit (Open-Panel) }
    'log'     { exit (Open-Log) }
}

$menuRc = 0
while ($true) {
    Clear-Host
    Write-Host "=================================================="
    Write-Host "                muse2api console"
    Write-Host "=================================================="
    Write-Host ("  Repo   : {0}" -f $Root)
    Write-Host ("  Port   : {0}   CDP: {1}" -f $Port, $CdpPort)
    Show-Status
    Write-Host "--------------------------------------------------"
    Write-Host "   1. Start service"
    Write-Host "   2. Stop service"
    Write-Host "   3. Restart service"
    Write-Host "   4. Open admin panel"
    Write-Host "   5. Open server.log"
    Write-Host "   6. Show detailed status"
    Write-Host "   0. Exit"
    Write-Host "--------------------------------------------------"
    $choice = Read-Host "Choose an option"
    switch ($choice) {
        '1' { $menuRc = Start-Service;   Read-Host "Press Enter to continue" | Out-Null }
        '2' { $menuRc = Stop-Service;    Read-Host "Press Enter to continue" | Out-Null }
        '3' { $menuRc = Restart-Service; Read-Host "Press Enter to continue" | Out-Null }
        '4' { Open-Panel | Out-Null }
        '5' { Open-Log | Out-Null }
        '6' { Show-Status; Read-Host "Press Enter to continue" | Out-Null }
        '0' { exit $menuRc }
        default { }
    }
}

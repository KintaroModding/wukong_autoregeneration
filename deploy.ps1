<#
.SYNOPSIS
    Copies the built mod (dist\<pakName>.pak) into the game's Paks\~mods folder and replaces an old file safely.

.DESCRIPTION
    The running game locks the pak files it loaded, and a file must not change while the game runs. The script checks
    the target file first; with -Wait it waits until the game is closed. The file is copied under a temporary name,
    checked, and then swapped in with one rename, so the game never sees a half-written file. The game loads mods from
    Paks\~mods only with the launch option -fileopenlog.

    Exit codes: 0 = deployed or already up to date, 2 = the game is running (without -Wait), 1 = error.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -Wait
#>
param(
    # The game's install folder. Default: $env:UE5_GAMES_DIR\<gameFolder of mod.json>.
    [string]$GameDir,
    # The mods folder. Default: <GameDir>\<paksPath of mod.json>\~mods.
    [string]$ModsDir,
    # Wait until the game releases the file instead of stopping.
    [switch]$Wait,
    [int]$WaitMinutes = 360
)

$ErrorActionPreference = "Stop"

$mod = Get-Content (Join-Path $PSScriptRoot "mod.json") -Raw | ConvertFrom-Json
if (-not $ModsDir) {
    if (-not $GameDir) {
        if (-not $env:UE5_GAMES_DIR) {
            throw "The game folder is unknown: set UE5_GAMES_DIR (the folder that holds '$($mod.gameFolder)'), or use -GameDir or -ModsDir."
        }
        $GameDir = Join-Path $env:UE5_GAMES_DIR $mod.gameFolder
    }
    $paks = Join-Path $GameDir $mod.paksPath
    if (-not (Test-Path $paks)) {
        throw "The game's Paks folder is not there: $paks"
    }
    $ModsDir = Join-Path $paks "~mods"
}
New-Item -ItemType Directory -Force $ModsDir | Out-Null

function Test-Locked([string]$Path) {
    if (-not (Test-Path $Path)) {
        return $false
    }
    try {
        $stream = [System.IO.File]::Open($Path, 'Open', 'ReadWrite', 'None')
        $stream.Close()
        return $false
    } catch {
        return $true
    }
}

function Get-Hash([string]$Path) {
    try {
        return (Get-FileHash $Path -ErrorAction Stop).Hash
    } catch {
        return $null
    }
}

$name = "$($mod.pakName).pak"
$source = Join-Path $PSScriptRoot "dist\$name"
if (-not (Test-Path $source)) {
    throw "$source not found. Build it first with build.ps1."
}
$target = Join-Path $ModsDir $name
$hash = Get-Hash $source

if ((Get-Hash $target) -eq $hash) {
    Write-Host "Already up to date: $target" -ForegroundColor Green
    exit 0
}

if (Test-Locked $target) {
    if (-not $Wait) {
        Write-Host "The game is running and uses $target." -ForegroundColor Yellow
        Write-Host "Close the game and run this again, or use the -Wait option." -ForegroundColor Yellow
        exit 2
    }
    Write-Host "The game is running. Waiting until it is closed (checking every 5 seconds)..."
    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    while (Test-Locked $target) {
        if ((Get-Date) -gt $deadline) {
            throw "The game did not release the mod file within $WaitMinutes minutes."
        }
        Start-Sleep -Seconds 5
    }
}

$temp = "$target.tmp"
Copy-Item $source $temp -Force
if ((Get-Hash $temp) -ne $hash) {
    Remove-Item $temp -Force
    throw "The copy in $temp is damaged. $target was not replaced."
}
try {
    if (Test-Path $target) {
        # [NullString]::Value: Windows PowerShell passes $null as "" to string parameters.
        [System.IO.File]::Replace($temp, $target, [NullString]::Value)
    } else {
        Move-Item $temp $target
    }
} catch {
    Remove-Item $temp -Force -ErrorAction SilentlyContinue
    throw "Could not replace $target (did the game start again?): $($_.Exception.Message)"
}
if ((Get-Hash $target) -ne $hash) {
    throw "Check failed: $target does not match $source."
}
Write-Host "Deployed $name to $ModsDir (start the game with -fileopenlog)" -ForegroundColor Green

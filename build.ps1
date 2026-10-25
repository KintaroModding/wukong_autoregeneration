<#
.SYNOPSIS
    Builds the mod (the src folder) into dist\<pakName>.pak, the file for the game's Paks\~mods folder.

.DESCRIPTION
    Black Myth: Wukong keeps its data tables as protobuf files (b1\Content\00Main\PBTable\...\*.data) inside its own
    variant of the pak format. src holds the changed tables as JSON with field names, at their game path. The build:
      1. encodes every src\**\*.json into dist\stage\**\*.data (scripts\pbtable.py with the schema
         scripts\pbschema.json) and checks it: decoded again, it must equal its source (pbtable.py verify);
      2. packs dist\stage into dist\<pakName>.pak in the game's pak format (scripts\b1pak.py pack), with the deepest
         folder that holds all tables as the mount point;
      3. checks the pak: reads every file back and compares it with its staged table (b1pak.py check).
    It needs Python 3 (no extra package). With -Deploy, deploy.ps1 then copies the pak into the game's Paks\~mods
    folder. After a good build, dist\ holds only the pak. The game loads it only with the launch option -fileopenlog.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build.ps1 -Deploy
#>
param(
    # Copy the pak to the game's Paks\~mods folder after the build (see deploy.ps1).
    [switch]$Deploy,
    # The game's install folder. Default: $env:UE5_GAMES_DIR\<gameFolder of mod.json>.
    [string]$GameDir
)

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$mod = Get-Content (Join-Path $root "mod.json") -Raw | ConvertFrom-Json
$src = Join-Path $root "src"
$dist = Join-Path $root "dist"
$stage = Join-Path $dist "stage"
$scripts = Join-Path $root "scripts"
$schema = Join-Path $scripts "pbschema.json"
$pak = Join-Path $dist "$($mod.pakName).pak"

# Python: the venv of a _tools folder above the project, else python from PATH.
function Find-Python {
    $dir = Split-Path $root -Parent
    while ($dir) {
        $candidate = Join-Path $dir "_tools\.venv\Scripts\python.exe"
        if (Test-Path $candidate) {
            return $candidate
        }
        $dir = Split-Path $dir -Parent
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "Python 3 not found. Install it from https://www.python.org and run this again."
    }
    return $python.Source
}

function Invoke-Python([string[]]$Arguments, [string]$Failure) {
    & $python -I @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Failure (exit code $LASTEXITCODE)"
    }
}

$python = Find-Python
if (-not (Test-Path $schema)) {
    throw "$schema not found: it holds the field names of the tables in src."
}
New-Item -ItemType Directory -Force $dist | Out-Null
foreach ($old in @($stage, $pak)) {
    if (Test-Path -LiteralPath $old) {
        Remove-Item -LiteralPath $old -Recurse -Force
    }
}

$sources = @(Get-ChildItem $src -Recurse -File -Filter "*.json")
if ($sources.Count -eq 0) {
    throw "No tables (*.json) in $src."
}

# 1. JSON -> protobuf tables, each checked against its source.
$staged = @()
foreach ($json in $sources) {
    $relative = $json.FullName.Substring($src.Length + 1)
    $data = Join-Path $stage ([System.IO.Path]::ChangeExtension($relative, ".data"))
    New-Item -ItemType Directory -Force (Split-Path $data -Parent) | Out-Null
    Invoke-Python @((Join-Path $scripts "pbtable.py"), "encode", $schema, $json.FullName, $data) "Encoding $relative failed"
    Invoke-Python @((Join-Path $scripts "pbtable.py"), "verify", $schema, $json.FullName, $data) "$relative does not encode back to its source"
    $staged += [pscustomobject]@{ Path = $data; Game = [System.IO.Path]::ChangeExtension($relative, ".data").Replace("\", "/") }
}

# 2. The pak: the mount point is the deepest folder that holds every table.
# @(): a one-item list would collapse into a string, and $common[0] would be its first character.
$common = @(($staged[0].Game -split "/") | Select-Object -SkipLast 1)
foreach ($item in $staged) {
    $parts = @(($item.Game -split "/") | Select-Object -SkipLast 1)
    $length = 0
    while ($length -lt $common.Count -and $length -lt $parts.Count -and $common[$length] -eq $parts[$length]) {
        $length++
    }
    $common = @($common | Select-Object -First $length)
}
$mount = "../../../" + (($common -join "/") + "/").TrimStart("/")
$files = $staged | ForEach-Object { $_.Game.Substring(($common -join "/").Length).TrimStart("/") + "=" + $_.Path }
Invoke-Python (@((Join-Path $scripts "b1pak.py"), "pack", $pak, $mount) + $files) "Packing failed"

# 3. The check: every file of the pak against its staged table.
Invoke-Python (@((Join-Path $scripts "b1pak.py"), "check", $pak) + $files) "The pak does not match the tables (see above). Nothing was deployed."

Remove-Item -LiteralPath $stage -Recurse -Force
Write-Host "Built: $(Split-Path $pak -Leaf) ($((Get-Item $pak).Length) bytes)"

if ($Deploy) {
    $deployArgs = @{}
    if ($GameDir) {
        $deployArgs.GameDir = $GameDir
    }
    & (Join-Path $root "deploy.ps1") @deployArgs
    exit $LASTEXITCODE
}

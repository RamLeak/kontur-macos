[CmdletBinding()]
param(
    [Parameter()]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$CargoHome,

    [Parameter()]
    [string]$RustupHome,

    [Parameter(Mandatory = $true)]
    [Alias('TargetDir')]
    [ValidateNotNullOrEmpty()]
    [string]$TargetDirectory,

    [Parameter(Mandatory = $true)]
    [Alias('ToolchainBinDir', 'BinDirectory')]
    [ValidateNotNullOrEmpty()]
    [string]$ToolchainBinDirectory,

    [Parameter()]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$')]
    [string]$Toolchain = 'stable-x86_64-pc-windows-msvc',

    [Parameter()]
    [Alias('BuildProfile')]
    [ValidateSet('debug', 'release')]
    [string]$Profile = 'release'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-AbsolutePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$BasePath
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'A path value is required.'
    }

    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) {
        $Path
    }
    else {
        Join-Path -Path $BasePath -ChildPath $Path
    }

    return [System.IO.Path]::GetFullPath($candidate)
}

function Test-PathWithin {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $rootFull = [System.IO.Path]::GetFullPath($Root)
    $rootPrefix = if ($rootFull.EndsWith([System.IO.Path]::DirectorySeparatorChar) -or
        $rootFull.EndsWith([System.IO.Path]::AltDirectorySeparatorChar)) {
        $rootFull
    }
    else {
        $rootFull + [System.IO.Path]::DirectorySeparatorChar
    }

    return $pathFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase) -or
        $pathFull.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-NotFilesystemRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $rootPath = [System.IO.Path]::GetPathRoot($fullPath)
    if ($fullPath.TrimEnd([char[]]@('\', '/')).Equals(
            $rootPath.TrimEnd([char[]]@('\', '/')),
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must not be a filesystem root: $fullPath"
    }
}

function Assert-ExistingDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label does not exist as a directory: $Path"
    }

    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a junction or symlink: $Path"
    }
}

function Assert-ExistingFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label does not exist as a file: $Path"
    }

    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a junction or symlink: $Path"
    }
}

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}

$projectRootFull = Resolve-AbsolutePath -Path $ProjectRoot -BasePath (Get-Location).Path
$legacySourceRoot = [System.IO.Path]::GetFullPath('C:\Projects\Kontur')
if (Test-PathWithin -Path $projectRootFull -Root $legacySourceRoot) {
    throw "ProjectRoot must not be the read-only legacy source tree: $legacySourceRoot"
}
Assert-ExistingDirectory -Path $projectRootFull -Label 'ProjectRoot'

$manifestPath = Join-Path $projectRootFull 'native\Cargo.toml'
$lockPath = Join-Path $projectRootFull 'native\Cargo.lock'
$staticWebRoot = Join-Path $projectRootFull 'web\out'
$targetRoot = Resolve-AbsolutePath -Path $TargetDirectory -BasePath $projectRootFull
if (Test-PathWithin -Path $targetRoot -Root $legacySourceRoot) {
    throw "TargetDirectory must not point into the read-only legacy source tree: $legacySourceRoot"
}
Assert-NotFilesystemRoot -Path $targetRoot -Label 'TargetDirectory'

Assert-ExistingFile -Path $manifestPath -Label 'native Cargo manifest'
Assert-ExistingFile -Path $lockPath -Label 'native Cargo.lock'
Assert-ExistingDirectory -Path $staticWebRoot -Label 'static web/out'

Assert-ExistingDirectory -Path $targetRoot -Label 'TargetDirectory'

$rustupHomeWasExplicit = -not [string]::IsNullOrWhiteSpace($RustupHome)
$cargoHomeFull = Resolve-AbsolutePath -Path $CargoHome -BasePath $projectRootFull
$rustupHomeFull = if ($rustupHomeWasExplicit) {
    Resolve-AbsolutePath -Path $RustupHome -BasePath $projectRootFull
}
else {
    $null
}

if (Test-PathWithin -Path $cargoHomeFull -Root $legacySourceRoot) {
    throw "CargoHome must not point into the read-only legacy source/cache tree: $legacySourceRoot"
}
Assert-NotFilesystemRoot -Path $cargoHomeFull -Label 'CargoHome'
Assert-ExistingDirectory -Path $cargoHomeFull -Label 'CargoHome'

if ($rustupHomeWasExplicit) {
    if (Test-PathWithin -Path $rustupHomeFull -Root $legacySourceRoot) {
        throw "RustupHome must not point into the read-only legacy source/cache tree: $legacySourceRoot"
    }
    Assert-ExistingDirectory -Path $rustupHomeFull -Label 'RustupHome'
}

$toolchainBinFull = Resolve-AbsolutePath -Path $ToolchainBinDirectory -BasePath $projectRootFull
$approvedToolchainBin = [System.IO.Path]::GetFullPath('C:\Projects\Kontur\desktop\.tools\rustup\toolchains\1.90.0-x86_64-pc-windows-msvc\bin')
$toolchainBinIsApproved = $toolchainBinFull.TrimEnd([char[]]@('\', '/')).Equals(
    $approvedToolchainBin.TrimEnd([char[]]@('\', '/')),
    [System.StringComparison]::OrdinalIgnoreCase
)
if ((Test-PathWithin -Path $toolchainBinFull -Root $legacySourceRoot) -and
    -not $toolchainBinIsApproved) {
    throw "ToolchainBinDirectory must not point into the read-only legacy source tree: $legacySourceRoot"
}
Assert-NotFilesystemRoot -Path $toolchainBinFull -Label 'ToolchainBinDirectory'
Assert-ExistingDirectory -Path $toolchainBinFull -Label 'ToolchainBinDirectory'
$cargoCommand = Join-Path $toolchainBinFull 'cargo.exe'
$rustcCommand = Join-Path $toolchainBinFull 'rustc.exe'
Assert-ExistingFile -Path $cargoCommand -Label 'toolchain cargo.exe'
Assert-ExistingFile -Path $rustcCommand -Label 'toolchain rustc.exe'

$profileName = $Profile.ToLowerInvariant()
$profileDirectory = switch ($profileName) {
    'dev' { 'debug'; break }
    'debug' { 'debug'; break }
    'release' { 'release'; break }
    default { $profileName }
}
$outputExe = Join-Path $targetRoot (Join-Path $profileDirectory 'kontur-local.exe')

$environmentValues = [ordered]@{
    CARGO_HOME = $cargoHomeFull
    RUSTUP_TOOLCHAIN = $Toolchain
    CARGO_TARGET_DIR = $targetRoot
    CARGO_NET_OFFLINE = 'true'
    CARGO_TERM_COLOR = 'never'
    CARGO_BUILD_JOBS = '1'
    CARGO_INCREMENTAL = '0'
    RUSTC = $rustcCommand
    # This helper embeds the reviewed production identity and restores the caller
    # environment afterwards. ROOT uses a separate explicit command for QA.
    TAURI_CONFIG = $null
}
if ($rustupHomeWasExplicit) {
    $environmentValues['RUSTUP_HOME'] = $rustupHomeFull
}
$savedEnvironment = @{}
foreach ($entry in $environmentValues.GetEnumerator()) {
    $savedEnvironment[$entry.Key] = [System.Environment]::GetEnvironmentVariable($entry.Key, 'Process')
    if ($null -eq $entry.Value) {
        Remove-Item -LiteralPath ('Env:' + $entry.Key) -ErrorAction SilentlyContinue
    }
    else {
        [System.Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, 'Process')
    }
}

$buildExitCode = 1
try {
    $cargoArguments = @(
        'build',
        '--offline',
        '--locked',
        '--manifest-path',
        $manifestPath,
        '--bin',
        'kontur-local'
    )
    if ($profileName -eq 'release') {
        $cargoArguments += '--release'
    }
    elseif ($profileName -ne 'dev' -and $profileName -ne 'debug') {
        $cargoArguments += @('--profile', $profileName)
    }

    Push-Location -LiteralPath $projectRootFull
    try {
        $cargoOutput = & $cargoCommand @cargoArguments 2>&1
        $buildExitCode = $LASTEXITCODE
        $cargoOutput | ForEach-Object { Write-Output $_ }
    }
    finally {
        Pop-Location
    }
}
finally {
    foreach ($entry in $environmentValues.GetEnumerator()) {
        $oldValue = $savedEnvironment[$entry.Key]
        if ($null -eq $oldValue) {
            Remove-Item -LiteralPath ('Env:' + $entry.Key) -ErrorAction SilentlyContinue
        }
        else {
            [System.Environment]::SetEnvironmentVariable($entry.Key, [string]$oldValue, 'Process')
        }
    }
}

if ($buildExitCode -ne 0) {
    throw "Offline Cargo build failed with exit code $buildExitCode."
}

Assert-ExistingFile -Path $outputExe -Label 'kontur-local build output'
$outputHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $outputExe).Hash
Write-Output "Built: $outputExe"
Write-Output "SHA256: $outputHash"

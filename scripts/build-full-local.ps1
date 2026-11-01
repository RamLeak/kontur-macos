[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$PreparedResourcesRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$StaticWebRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$CargoHome,

    [Parameter(Mandatory = $true)]
    [Alias('TargetDir')]
    [ValidateNotNullOrEmpty()]
    [string]$TargetDirectory,

    [Parameter(Mandatory = $true)]
    [Alias('ToolchainBinDir', 'BinDirectory')]
    [ValidateNotNullOrEmpty()]
    [string]$ToolchainBinDirectory,

    [Parameter()]
    [string]$ProjectRoot,

    [Parameter()]
    [string]$RustupHome,

    [Parameter()]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$')]
    [string]$Toolchain = 'stable-x86_64-pc-windows-msvc',

    [Parameter()]
    [ValidateSet('debug', 'release')]
    [string]$Profile = 'release',

    [Parameter()]
    [string]$NativeHelperPath,

    [Parameter()]
    [switch]$Build
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$legacySourceRoot = [System.IO.Path]::GetFullPath('C:\Projects\Kontur')
$expectedProductName = -join @(
    [char]0x041A; [char]0x043E; [char]0x043D; [char]0x0442; [char]0x0443; [char]0x0440
    [char]0x0020
    [char]0x041B; [char]0x043E; [char]0x043A; [char]0x0430; [char]0x043B; [char]0x044C
    [char]0x043D; [char]0x044B; [char]0x0439
)

function Resolve-AbsolutePath {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$BasePath)
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'A path value is required.' }
    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path -Path $BasePath -ChildPath $Path }
    return [System.IO.Path]::GetFullPath($candidate)
}

function Test-PathWithin {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Root)
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $rootFull = [System.IO.Path]::GetFullPath($Root)
    $rootPrefix = if ($rootFull.EndsWith([System.IO.Path]::DirectorySeparatorChar) -or
        $rootFull.EndsWith([System.IO.Path]::AltDirectorySeparatorChar)) { $rootFull } else {
        $rootFull + [System.IO.Path]::DirectorySeparatorChar
    }
    return $pathFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase) -or
        $pathFull.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-NotFilesystemRoot {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $rootPath = [System.IO.Path]::GetPathRoot($fullPath)
    if ($fullPath.TrimEnd([char[]]@('\', '/')).Equals($rootPath.TrimEnd([char[]]@('\', '/')),
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ($Label + ' must not be a filesystem root: ' + $fullPath)
    }
}

function Assert-ExistingDirectory {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw ($Label + ' does not exist as a directory: ' + $Path) }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw ($Label + ' must not be a junction or symlink: ' + $Path)
    }
}

function Assert-ExistingFile {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw ($Label + ' does not exist as a file: ' + $Path) }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw ($Label + ' must not be a junction or symlink: ' + $Path)
    }
}

function Get-PlainFilesUnderRoot {
    param([Parameter(Mandatory = $true)][string]$Root)
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    Assert-ExistingDirectory -Path $rootFull -Label 'plain source root'
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $files = New-Object 'System.Collections.Generic.List[object]'
    $stack.Push($rootFull)
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        $currentItem = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw ('Source tree must not contain a junction or symlink: ' + $current)
        }
        foreach ($child in @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw ('Source tree must not contain a junction or symlink: ' + $child.FullName)
            }
            $relative = $child.FullName.Substring($rootFull.Length).TrimStart([char[]]@('\', '/')).Replace('\', '/')
            if ($child.PSIsContainer) {
                $stack.Push($child.FullName)
            }
            else {
                [void]$files.Add([pscustomobject]@{ Item = $child; RelativePath = $relative })
            }
        }
    }
    return $files.ToArray()
}

function Normalize-ManifestRelativePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $normalized = $Path.Replace('\', '/')
    if ([string]::IsNullOrWhiteSpace($normalized) -or $normalized.StartsWith('/') -or
        $normalized -match '^[A-Za-z]:') {
        throw ('Manifest path is not relative: ' + $Path)
    }
    $parts = $normalized.Split('/')
    foreach ($part in $parts) {
        if ([string]::IsNullOrWhiteSpace($part) -or $part -eq '.' -or $part -eq '..' -or
            $part.IndexOfAny([char[]]@([char]0x0000, [char]0x0001, [char]0x0002, [char]0x0003)) -ge 0) {
            throw ('Manifest path contains unsafe components: ' + $Path)
        }
    }
    return ($parts -join '/')
}

function Assert-JsonObjectShape {
    param(
        [Parameter(Mandatory = $true)][object]$Object,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$Required,
        [Parameter()][string[]]$Optional = @()
    )
    if ($null -eq $Object -or $Object -is [System.Array]) { throw ($Label + ' must be a JSON object.') }
    $actual = @($Object.PSObject.Properties | ForEach-Object { $_.Name })
    $allowed = @($Required) + @($Optional)
    foreach ($name in $Required) {
        if ($actual -notcontains $name) { throw ($Label + ' is missing required field: ' + $name) }
    }
    foreach ($name in $actual) {
        if ($allowed -notcontains $name) { throw ($Label + ' has unexpected field: ' + $name) }
    }
}

function Assert-PreparedResourceManifest {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ResourcesRoot
    )
    Assert-ExistingFile -Path $ManifestPath -Label 'full local resources manifest'
    $manifestInfo = Get-Item -LiteralPath $ManifestPath -Force
    if ($manifestInfo.Length -gt 64MB) { throw 'Full local resources manifest is unexpectedly large.' }
    try {
        $manifest = ([System.IO.File]::ReadAllText($ManifestPath) | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        throw ('Full local resources manifest is not valid JSON: ' + $ManifestPath)
    }
    Assert-JsonObjectShape -Object $manifest -Label 'full local resources manifest' -Required @(
        'format', 'productName', 'version', 'identifier', 'python', 'backend',
        'directProductionRequirements', 'skippedConditionalRequirements', 'distributions', 'files'
    )
    if ($manifest.format -ne 'kontur-full-local-resources-v1' -or
        $manifest.productName -ne $expectedProductName -or
        $manifest.version -ne '0.1.0' -or
        $manifest.identifier -ne 'app.kontur.offline.desktop') {
        throw 'Full local resources manifest identity does not match the production application.'
    }
    Assert-JsonObjectShape -Object $manifest.python -Label 'manifest python' -Required @(
        'implementation', 'version', 'invocation', 'runtimeSource'
    )
    if ($manifest.python.implementation -ne 'CPython' -or
        $manifest.python.version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$' -or
        $manifest.python.invocation -ne 'python.exe -I -B -u') {
        throw 'Full local resources manifest Python identity is invalid.'
    }
    Assert-JsonObjectShape -Object $manifest.backend -Label 'manifest backend' -Required @('status', 'source') -Optional @(
        'entrypoint', 'testsExcluded'
    )
    if ($manifest.backend.status -ne 'prepared') {
        throw 'Full local resources manifest does not prove a prepared backend.'
    }
    Assert-JsonObjectShape -Object $manifest.backend -Label 'prepared manifest backend' -Required @(
        'status', 'source', 'entrypoint', 'testsExcluded'
    )
    if ($manifest.backend.entrypoint -ne 'resources/backend/offline_bridge.py' -or
        $manifest.backend.testsExcluded -ne $true) {
        throw 'Prepared manifest backend identity is invalid.'
    }

    $manifestRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $ManifestPath)).TrimEnd([char[]]@('\', '/'))
    $resourcesFull = [System.IO.Path]::GetFullPath($ResourcesRoot).TrimEnd([char[]]@('\', '/'))
    if (-not (Test-PathWithin -Path $resourcesFull -Root $projectRootFull)) {
        throw 'Prepared resources root is outside the offline project.'
    }
    $manifestFiles = @($manifest.files)
    if ($manifestFiles.Count -eq 0) { throw 'Full local resources manifest has no file entries.' }
    $manifestPaths = @{}
    foreach ($entry in $manifestFiles) {
        Assert-JsonObjectShape -Object $entry -Label 'manifest file entry' -Required @('path', 'sha256', 'size', 'source')
        if ($entry.path -isnot [string] -or $entry.sha256 -isnot [string] -or $entry.source -isnot [string]) {
            throw 'Manifest file entry has an invalid field type.'
        }
        $relative = Normalize-ManifestRelativePath -Path $entry.path
        if (-not $relative.StartsWith('resources/', [System.StringComparison]::OrdinalIgnoreCase)) {
            throw ('Manifest file is outside resources/: ' + $relative)
        }
        $key = $relative.ToLowerInvariant()
        if ($manifestPaths.ContainsKey($key)) { throw ('Manifest contains duplicate file path: ' + $relative) }
        if ($entry.sha256 -notmatch '^[A-Fa-f0-9]{64}$') { throw ('Manifest SHA256 is invalid: ' + $relative) }
        [long]$expectedSize = 0
        if (-not [long]::TryParse([string]$entry.size, [Globalization.NumberStyles]::Integer,
                [Globalization.CultureInfo]::InvariantCulture, [ref]$expectedSize) -or $expectedSize -lt 0) {
            throw ('Manifest file size is invalid: ' + $relative)
        }
        $sourcePath = Resolve-AbsolutePath -Path $relative -BasePath $manifestRoot
        if (-not (Test-PathWithin -Path $sourcePath -Root $resourcesFull)) {
            throw ('Manifest file escapes prepared resources: ' + $relative)
        }
        Assert-ExistingFile -Path $sourcePath -Label ('manifest payload ' + $relative)
        $sourceInfo = Get-Item -LiteralPath $sourcePath -Force
        if ($sourceInfo.Length -ne $expectedSize) { throw ('Manifest size mismatch: ' + $relative) }
        $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourcePath).Hash
        if (-not $actualHash.Equals($entry.sha256, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw ('Manifest SHA256 mismatch: ' + $relative)
        }
        $manifestPaths[$key] = $relative
    }

    $actualFiles = @(Get-PlainFilesUnderRoot -Root $resourcesFull)
    foreach ($actual in $actualFiles) {
        $relative = 'resources/' + $actual.RelativePath
        $key = $relative.ToLowerInvariant()
        if (-not $manifestPaths.ContainsKey($key)) { throw ('Unmanifested prepared resource file: ' + $relative) }
    }
    if ($actualFiles.Count -ne $manifestPaths.Count) {
        throw ('Prepared resources file count differs from manifest: actual=' + $actualFiles.Count + ' manifest=' + $manifestPaths.Count)
    }
    $catalogKey = 'resources/catalog/catalog.sqlite3'
    if (-not $manifestPaths.ContainsKey($catalogKey)) {
        throw 'Prepared catalog/catalog.sqlite3 is not covered by the resource manifest.'
    }
    Assert-ExistingFile -Path (Join-Path $resourcesFull 'catalog\catalog.sqlite3') -Label 'prepared resources/catalog/catalog.sqlite3'
    $entrypointKey = 'resources/backend/offline_bridge.py'
    if (-not $manifestPaths.ContainsKey($entrypointKey)) {
        throw 'Prepared backend entrypoint is not covered by the resource manifest.'
    }
    return $manifest
}

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$projectRootFull = Resolve-AbsolutePath -Path $ProjectRoot -BasePath (Get-Location).Path
if (Test-PathWithin -Path $projectRootFull -Root $legacySourceRoot) {
    throw ('ProjectRoot must not be the read-only legacy source tree: ' + $legacySourceRoot)
}
Assert-ExistingDirectory -Path $projectRootFull -Label 'ProjectRoot'

$resourcesFull = Resolve-AbsolutePath -Path $PreparedResourcesRoot -BasePath $projectRootFull
$staticFull = Resolve-AbsolutePath -Path $StaticWebRoot -BasePath $projectRootFull
$cargoHomeFull = Resolve-AbsolutePath -Path $CargoHome -BasePath $projectRootFull
$targetFull = Resolve-AbsolutePath -Path $TargetDirectory -BasePath $projectRootFull
if ((Test-PathWithin -Path $resourcesFull -Root $legacySourceRoot) -or
    (-not (Test-PathWithin -Path $resourcesFull -Root $projectRootFull))) {
    throw 'PreparedResourcesRoot must be inside the offline project and outside the legacy source tree.'
}
if (Test-PathWithin -Path $staticFull -Root $legacySourceRoot) {
    throw ('StaticWebRoot must not point into the read-only legacy source tree: ' + $legacySourceRoot)
}
if ((Test-PathWithin -Path $cargoHomeFull -Root $legacySourceRoot) -or
    (Test-PathWithin -Path $targetFull -Root $legacySourceRoot)) {
    throw ('CargoHome and TargetDirectory must not point into the read-only legacy source tree: ' + $legacySourceRoot)
}
if ((Test-PathWithin -Path $cargoHomeFull -Root $projectRootFull) -or
    (Test-PathWithin -Path $targetFull -Root $projectRootFull)) {
    throw 'CargoHome and TargetDirectory must be isolated existing directories outside the project source tree.'
}
Assert-NotFilesystemRoot -Path $cargoHomeFull -Label 'CargoHome'
Assert-NotFilesystemRoot -Path $targetFull -Label 'TargetDirectory'
Assert-ExistingDirectory -Path $resourcesFull -Label 'PreparedResourcesRoot'
Assert-ExistingDirectory -Path $staticFull -Label 'StaticWebRoot'
Assert-ExistingDirectory -Path $cargoHomeFull -Label 'CargoHome'
Assert-ExistingDirectory -Path $targetFull -Label 'TargetDirectory'

$fullFrontendRoot = Join-Path $projectRootFull 'full\frontend'
$expectedStaticFull = [System.IO.Path]::GetFullPath((Join-Path $fullFrontendRoot 'out'))
Assert-ExistingDirectory -Path $fullFrontendRoot -Label 'full frontend source'
if (-not $staticFull.Equals($expectedStaticFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw ('StaticWebRoot must be the full frontend static output: ' + $expectedStaticFull)
}
Assert-ExistingDirectory -Path $expectedStaticFull -Label 'full frontend static output'
Assert-ExistingFile -Path (Join-Path $expectedStaticFull 'index.html') -Label 'full frontend index.html'
[void](Get-PlainFilesUnderRoot -Root $expectedStaticFull)

$nativeRootFull = Join-Path $projectRootFull 'native'
$nativeConfigPath = Join-Path $nativeRootFull 'tauri.conf.json'
Assert-ExistingDirectory -Path $nativeRootFull -Label 'native source'
Assert-ExistingFile -Path $nativeConfigPath -Label 'native Tauri config'
try {
    $tauriConfig = ([System.IO.File]::ReadAllText($nativeConfigPath) | ConvertFrom-Json -ErrorAction Stop)
}
catch {
    throw ('Native Tauri config is not valid JSON: ' + $nativeConfigPath)
}
Assert-JsonObjectShape -Object $tauriConfig -Label 'native Tauri config' -Required @(
    'productName', 'version', 'identifier', 'build'
) -Optional @('$schema', 'app', 'bundle')
if ($tauriConfig.productName -ne $expectedProductName -or
    $tauriConfig.version -ne '0.1.0' -or
    $tauriConfig.identifier -ne 'app.kontur.offline.desktop') {
    throw 'Native Tauri config identity does not match the production application.'
}
Assert-JsonObjectShape -Object $tauriConfig.build -Label 'native Tauri build config' -Required @('frontendDist') -Optional @(
    'devUrl', 'beforeDevCommand', 'beforeBuildCommand'
)
$baseFrontendDist = [string]$tauriConfig.build.frontendDist
if ([string]::IsNullOrWhiteSpace($baseFrontendDist) -or [System.IO.Path]::IsPathRooted($baseFrontendDist)) {
    throw 'Native Tauri frontendDist must be a relative path.'
}
$expectedFrontendDist = '../full/frontend/out'
if (-not $baseFrontendDist.Equals($expectedFrontendDist, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw ('Native Tauri frontendDist must be exactly ' + $expectedFrontendDist + '; legacy web/out or another base path is refused.')
}
$baseFrontendResolved = Resolve-AbsolutePath -Path $baseFrontendDist -BasePath $nativeRootFull
if (-not $baseFrontendResolved.Equals($expectedStaticFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Native Tauri frontendDist does not resolve to full/frontend/out.'
}

$preparedPython = Join-Path $resourcesFull 'python'
$preparedBackend = Join-Path $resourcesFull 'backend'
Assert-ExistingDirectory -Path $preparedPython -Label 'prepared resources/python'
Assert-ExistingDirectory -Path $preparedBackend -Label 'prepared resources/backend'
Assert-ExistingFile -Path (Join-Path $preparedPython 'python.exe') -Label 'prepared resources/python/python.exe'
Assert-ExistingDirectory -Path (Join-Path $preparedPython 'Lib\site-packages') -Label 'prepared resources/python/Lib/site-packages'
Assert-ExistingFile -Path (Join-Path $preparedBackend 'offline_bridge.py') -Label 'prepared resources/backend/offline_bridge.py'
Assert-ExistingDirectory -Path (Join-Path $preparedBackend 'app') -Label 'prepared resources/backend/app'
$resourcesManifest = Join-Path (Split-Path -Parent $resourcesFull) 'full-local-resources-manifest.json'
$resourcesManifestObject = Assert-PreparedResourceManifest -ManifestPath $resourcesManifest -ResourcesRoot $resourcesFull

if (-not [string]::IsNullOrWhiteSpace($NativeHelperPath)) {
    $nativeHelperFull = Resolve-AbsolutePath -Path $NativeHelperPath -BasePath $projectRootFull
    $defaultHelperFull = [System.IO.Path]::GetFullPath((Join-Path $projectRootFull 'scripts\build-offline.ps1'))
    if (-not $nativeHelperFull.Equals($defaultHelperFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'NativeHelperPath is compatibility-only and may not select another build helper.'
    }
    Assert-ExistingFile -Path $nativeHelperFull -Label 'compatibility native build helper'
}

$toolchainFull = Resolve-AbsolutePath -Path $ToolchainBinDirectory -BasePath $projectRootFull
$approvedToolchainFull = [System.IO.Path]::GetFullPath('C:\Projects\Kontur\desktop\.tools\rustup\toolchains\1.90.0-x86_64-pc-windows-msvc\bin')
$toolchainIsApproved = $toolchainFull.TrimEnd([char[]]@('\', '/')).Equals(
    $approvedToolchainFull.TrimEnd([char[]]@('\', '/')), [System.StringComparison]::OrdinalIgnoreCase)
if ((Test-PathWithin -Path $toolchainFull -Root $legacySourceRoot) -and (-not $toolchainIsApproved)) {
    throw ('ToolchainBinDirectory may use only the explicitly approved read-only toolchain under the legacy tree: ' + $approvedToolchainFull)
}
Assert-ExistingDirectory -Path $toolchainFull -Label 'ToolchainBinDirectory'
Assert-ExistingFile -Path (Join-Path $toolchainFull 'cargo.exe') -Label 'toolchain cargo.exe'
Assert-ExistingFile -Path (Join-Path $toolchainFull 'rustc.exe') -Label 'toolchain rustc.exe'

$rustupHomeFull = $null
if (-not [string]::IsNullOrWhiteSpace($RustupHome)) {
    $rustupHomeFull = Resolve-AbsolutePath -Path $RustupHome -BasePath $projectRootFull
    if ((Test-PathWithin -Path $rustupHomeFull -Root $legacySourceRoot) -or
        (Test-PathWithin -Path $rustupHomeFull -Root $projectRootFull)) {
        throw 'RustupHome must be an existing isolated directory outside the project and legacy source trees.'
    }
    Assert-ExistingDirectory -Path $rustupHomeFull -Label 'RustupHome'
}

$cargoManifestPath = Join-Path $nativeRootFull 'Cargo.toml'
$cargoLockPath = Join-Path $nativeRootFull 'Cargo.lock'
Assert-ExistingFile -Path $cargoManifestPath -Label 'native Cargo manifest'
Assert-ExistingFile -Path $cargoLockPath -Label 'native Cargo.lock'

if (-not $Build) {
    Write-Output 'Full-local build validation passed; no build was started.'
    Write-Output ('Prepared resources: ' + $resourcesFull)
    Write-Output ('Static full/frontend/out: ' + $staticFull)
    Write-Output ('Native Tauri frontendDist: ' + $baseFrontendDist)
    Write-Output 'Resource manifest identity, hashes, sizes, paths, backend gate, and completeness passed.'
    Write-Output ('Profile: ' + $Profile)
    Write-Output 'Pass -Build only after the prepared resources, full frontend static output, and isolated toolchain paths are reviewed.'
    return
}

$cargoCommand = Join-Path $toolchainFull 'cargo.exe'
$rustcCommand = Join-Path $toolchainFull 'rustc.exe'
$environmentValues = [ordered]@{
    CARGO_HOME = $cargoHomeFull
    RUSTUP_TOOLCHAIN = $Toolchain
    CARGO_TARGET_DIR = $targetFull
    CARGO_NET_OFFLINE = 'true'
    CARGO_TERM_COLOR = 'never'
    CARGO_BUILD_JOBS = '1'
    CARGO_INCREMENTAL = '0'
    RUSTC = $rustcCommand
    # Use the already-validated native/tauri.conf.json; do not mask a wrong
    # frontendDist with an inline caller or QA configuration.
    TAURI_CONFIG = $null
}
if ($null -ne $rustupHomeFull) { $environmentValues['RUSTUP_HOME'] = $rustupHomeFull }

$savedEnvironment = @{}
$buildExitCode = 1
try {
    foreach ($entry in $environmentValues.GetEnumerator()) {
        $savedEnvironment[$entry.Key] = [System.Environment]::GetEnvironmentVariable($entry.Key, 'Process')
        if ($null -eq $entry.Value) {
            Remove-Item -LiteralPath ('Env:' + $entry.Key) -ErrorAction SilentlyContinue
        }
        else {
            [System.Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, 'Process')
        }
    }
    $cargoArguments = @(
        'build',
        '--offline',
        '--locked',
        '--manifest-path',
        $cargoManifestPath,
        '--bin',
        'kontur-local'
    )
    if ($Profile -eq 'release') { $cargoArguments += '--release' }

    $locationPushed = $false
    Push-Location -LiteralPath $projectRootFull
    $locationPushed = $true
    try {
        $cargoOutput = & $cargoCommand @cargoArguments 2>&1
        $buildExitCode = $LASTEXITCODE
        $cargoOutput | ForEach-Object { Write-Output $_ }
    }
    finally {
        if ($locationPushed) { Pop-Location }
    }
}
finally {
    foreach ($entry in $environmentValues.GetEnumerator()) {
        if (-not $savedEnvironment.ContainsKey($entry.Key)) { continue }
        $oldValue = $savedEnvironment[$entry.Key]
        if ($null -eq $oldValue) {
            Remove-Item -LiteralPath ('Env:' + $entry.Key) -ErrorAction SilentlyContinue
        }
        else {
            [System.Environment]::SetEnvironmentVariable($entry.Key, [string]$oldValue, 'Process')
        }
    }
}

if ($buildExitCode -ne 0) { throw ('Full-local offline Cargo build failed with exit code ' + $buildExitCode + '.') }

$profileDirectory = if ($Profile -eq 'release') { 'release' } else { 'debug' }
$outputExe = Join-Path $targetFull (Join-Path $profileDirectory 'kontur-local.exe')
Assert-ExistingFile -Path $outputExe -Label 'kontur-local build output'
Write-Output ('Full-local native build: ' + $outputExe)
Write-Output ('SHA256: ' + (Get-FileHash -Algorithm SHA256 -LiteralPath $outputExe).Hash)

[CmdletBinding()]
param(
    [string]$ManifestPath,
    [string]$OutputDirectory,
    [switch]$DownloadMissing
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:MaxPartBytes = [int64]64MB
$script:MaxManifestBytes = [int64]1MB
$script:MaxParts = 99
$script:CopyBufferBytes = 1MB
$script:MaxWindowsPathLength = 240
$script:ProductionRepository = 'RamLeak/kontur-offline'
$script:ProductionTag = 'offline-2026-11-01'
$script:ProductionArchiveName = 'Kontur-Local-Windows-Offline.zip'
$script:ProductionArchiveBytes = [int64]1297102998
$script:ProductionArchiveSha256 = '42aec3208f7bf942023422bfb16ed85830759ac05a24f59ae43cc4640451ebb2'
$script:ProductionMemberCount = [int64]3252
$script:ProductionUncompressedBytes = [int64]2725976057
$script:ProductionLargestMemberBytes = [int64]2632617984
$script:ProductionExecutablePath = 'kontur-local.exe'
$script:ProductionExecutableSha256 = '7b2008f7da21335f8ca1dd53d2c099a14c484f2884751ade596144d8d1251612'
$script:ProductionResourcesManifestPath = 'full-local-resources-manifest.json'
$script:ProductionResourcesManifestSha256 = '4d989ce834eded14d13e0ba5370b4824bd6d1802199b4229c659f7c9d8e2a392'
$script:ProductionPartsManifestSha256 = '95cba4c765d8191bd1970a79cdd3b5c32ab7cbd3d476bcbca793c418f1a6807f'

function Resolve-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$BasePath)
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'A path value is required.' }
    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path -Path $BasePath -ChildPath $Path }
    return [System.IO.Path]::GetFullPath($candidate)
}

function Test-PathWithin {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Root)
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $rootFull = [System.IO.Path]::GetFullPath($Root)
    $rootPrefix = if ($rootFull.EndsWith([System.IO.Path]::DirectorySeparatorChar) -or $rootFull.EndsWith([System.IO.Path]::AltDirectorySeparatorChar)) { $rootFull } else { $rootFull + [System.IO.Path]::DirectorySeparatorChar }
    return $pathFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase) -or $pathFull.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-NotFilesystemRoot {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $rootPath = [System.IO.Path]::GetPathRoot($fullPath)
    if ($fullPath.TrimEnd([char[]]@('\', '/')).Equals($rootPath.TrimEnd([char[]]@('\', '/')), [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must not be a filesystem root: $fullPath"
    }
}

function Assert-NotReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label must not be a reparse point: $Path" }
    }
}

function Assert-ExistingDirectory {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw "$Label does not exist as a directory: $Path" }
    Assert-NotReparsePoint -Path $Path -Label $Label
}

function Assert-ExistingFile {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "$Label does not exist as a file: $Path" }
    Assert-NotReparsePoint -Path $Path -Label $Label
}

function Assert-ExactProperties {
    param([Parameter(Mandatory = $true)]$Object, [Parameter(Mandatory = $true)][string[]]$Names, [Parameter(Mandatory = $true)][string]$Label)
    if ($null -eq $Object -or $Object -is [System.Array] -or $Object -is [string]) { throw "$Label must be a JSON object." }
    $actual = @($Object.PSObject.Properties | ForEach-Object { $_.Name })
    $expected = @($Names | Sort-Object)
    $actualSorted = @($actual | Sort-Object)
    if ($actual.Count -ne $expected.Count) { throw "$Label has an unexpected schema." }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if ($actualSorted[$index] -cne $expected[$index]) { throw "$Label has an unexpected schema." }
    }
}

function Assert-NonEmptyString {
    param($Value, [Parameter(Mandatory = $true)][string]$Label, [int]$Maximum = 4096)
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value) -or $Value.Length -gt $Maximum) { throw "$Label must be a bounded non-empty string." }
    if ($Value.IndexOfAny([char[]]@([char]0, [char]9, [char]10, [char]13)) -ge 0) { throw "$Label contains a control character." }
    return [string]$Value
}

function Get-ManifestInteger {
    param($Value, [Parameter(Mandatory = $true)][string]$Label, [int64]$Minimum = 0)
    if ($null -eq $Value -or $Value -is [bool]) { throw "$Label must be an integer." }
    $typeName = $Value.GetType().Name
    $integerTypes = @('Byte', 'SByte', 'Int16', 'UInt16', 'Int32', 'UInt32', 'Int64', 'UInt64')
    if ($integerTypes -notcontains $typeName) { throw "$Label must be an integer." }
    try { $number = [int64]$Value } catch { throw "$Label is outside the supported integer range." }
    if ($number -lt $Minimum) { throw "$Label is outside the supported range." }
    return $number
}

function Normalize-Sha256 {
    param($Value, [Parameter(Mandatory = $true)][string]$Label)
    $text = Assert-NonEmptyString -Value $Value -Label $Label -Maximum 64
    if ($text -notmatch '^[0-9a-fA-F]{64}$') { throw "$Label must be exactly 64 hexadecimal characters." }
    return $text.ToLowerInvariant()
}

function Assert-SafeLeafName {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][string]$Label)
    if ($Name.Length -lt 1 -or $Name.Length -gt 255 -or $Name -eq '.' -or $Name -eq '..') { throw "$Label is not a safe filename." }
    if ($Name -ne [System.IO.Path]::GetFileName($Name) -or $Name -match '[\\/:\x00-\x1f<>"|?*]' -or $Name -match '[\. ]$') { throw "$Label is not a safe filename." }
    return $Name
}

function Get-SafeArchiveEntry {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][string]$Label)
    if ($Name.Length -lt 1 -or $Name.Length -gt 2048 -or $Name.Contains('\') -or $Name.StartsWith('/') -or [System.IO.Path]::IsPathRooted($Name)) { throw "$Label has an unsafe archive path." }
    $directory = $Name.EndsWith('/')
    $rawSegments = $Name.Split('/')
    $segments = New-Object System.Collections.Generic.List[string]
    for ($index = 0; $index -lt $rawSegments.Count; $index++) {
        $segment = $rawSegments[$index]
        if ($segment.Length -eq 0 -and $directory -and $index -eq ($rawSegments.Count - 1)) { continue }
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -eq '.' -or $segment -eq '..' -or $segment.Contains(':')) { throw "$Label has an unsafe archive path." }
        if ($segment.IndexOfAny([char[]]@([char]0, [char]9, [char]10, [char]13)) -ge 0) { throw "$Label has an unsafe archive path." }
        foreach ($invalid in [System.IO.Path]::GetInvalidFileNameChars()) {
            if ($segment.IndexOf($invalid) -ge 0) { throw "$Label has an unsafe archive path." }
        }
        [void]$segments.Add($segment)
    }
    if ($segments.Count -eq 0) { throw "$Label has an unsafe archive path." }
    return [pscustomobject]@{
        Original = $Name
        Segments = @($segments.ToArray())
        Relative = ($segments -join '\')
        Key = ($segments -join '\').ToLowerInvariant()
        IsDirectory = $directory
    }
}

function Test-ZipSymlink {
    param([Parameter(Mandatory = $true)]$Entry)
    $property = @($Entry.PSObject.Properties | Where-Object { $_.Name -eq 'ExternalAttributes' })
    if ($property.Count -eq 0) { return $false }
    try { $attributes = [int64]$property[0].Value } catch { return $false }
    return ((($attributes -shr 16) -band 0xF000) -eq 0xA000)
}

function Copy-StreamBounded {
    param([Parameter(Mandatory = $true)][System.IO.Stream]$Source, [Parameter(Mandatory = $true)][System.IO.Stream]$Destination)
    $buffer = New-Object byte[] $script:CopyBufferBytes
    while ($true) {
        $read = $Source.Read($buffer, 0, $buffer.Length)
        if ($read -le 0) { break }
        $Destination.Write($buffer, 0, $read)
    }
}

function Assert-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Expected, [Parameter(Mandatory = $true)][string]$Label)
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    if ($actual -cne $Expected.ToLowerInvariant()) { throw "$Label SHA-256 does not match the manifest." }
}

function Read-PartsManifest {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-ExistingFile -Path $Path -Label 'Parts manifest'
    $manifestInfo = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($manifestInfo.Length -gt $script:MaxManifestBytes) { throw 'Parts manifest is too large.' }
    $manifestSha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    try { $raw = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) | ConvertFrom-Json } catch { throw 'Parts manifest is not valid JSON.' }
    Assert-ExactProperties -Object $raw -Names @('schemaVersion', 'repository', 'tag', 'archive', 'parts') -Label 'Manifest'
    $schemaVersion = Get-ManifestInteger -Value $raw.schemaVersion -Label 'schemaVersion' -Minimum 1
    if ($schemaVersion -ne 1) { throw 'Unsupported parts manifest schemaVersion.' }
    $repository = Assert-NonEmptyString -Value $raw.repository -Label 'repository' -Maximum 200
    $tag = Assert-NonEmptyString -Value $raw.tag -Label 'tag' -Maximum 128
    if ($repository -match '[\\\x00-\x1f]' -or $tag -match '[\\/\x00-\x1f]') { throw 'Manifest repository or tag is unsafe.' }

    $archive = $raw.archive
    Assert-ExactProperties -Object $archive -Names @('name', 'bytes', 'sha256', 'memberCount', 'uncompressedBytes', 'largestMemberBytes', 'executablePath', 'executableSha256', 'resourcesManifestPath', 'resourcesManifestSha256') -Label 'archive'
    $archiveName = Assert-SafeLeafName -Name (Assert-NonEmptyString -Value $archive.name -Label 'archive.name' -Maximum 255) -Label 'archive.name'
    $archiveBytes = Get-ManifestInteger -Value $archive.bytes -Label 'archive.bytes' -Minimum 1
    $archiveSha256 = Normalize-Sha256 -Value $archive.sha256 -Label 'archive.sha256'
    $memberCount = Get-ManifestInteger -Value $archive.memberCount -Label 'archive.memberCount' -Minimum 1
    $uncompressedBytes = Get-ManifestInteger -Value $archive.uncompressedBytes -Label 'archive.uncompressedBytes' -Minimum 1
    $largestMemberBytes = Get-ManifestInteger -Value $archive.largestMemberBytes -Label 'archive.largestMemberBytes' -Minimum 1
    $executablePath = Get-SafeArchiveEntry -Name (Assert-NonEmptyString -Value $archive.executablePath -Label 'archive.executablePath' -Maximum 2048) -Label 'archive.executablePath'
    $resourcesManifestPath = Get-SafeArchiveEntry -Name (Assert-NonEmptyString -Value $archive.resourcesManifestPath -Label 'archive.resourcesManifestPath' -Maximum 2048) -Label 'archive.resourcesManifestPath'
    if ($executablePath.IsDirectory -or $resourcesManifestPath.IsDirectory) { throw 'Expected executable and resource manifest paths must be files.' }
    $executableSha256 = Normalize-Sha256 -Value $archive.executableSha256 -Label 'archive.executableSha256'
    $resourcesManifestSha256 = Normalize-Sha256 -Value $archive.resourcesManifestSha256 -Label 'archive.resourcesManifestSha256'

    $rawParts = @($raw.parts)
    if ($rawParts.Count -lt 1 -or $rawParts.Count -gt $script:MaxParts) { throw 'Manifest parts must contain between 1 and 99 entries.' }
    $parts = @()
    $sum = [int64]0
    $partNumber = 1
    foreach ($rawPart in $rawParts) {
        Assert-ExactProperties -Object $rawPart -Names @('name', 'bytes', 'sha256') -Label ('parts[' + ($partNumber - 1) + ']')
        $partName = Assert-SafeLeafName -Name (Assert-NonEmptyString -Value $rawPart.name -Label 'part.name' -Maximum 255) -Label 'part.name'
        $partBytes = Get-ManifestInteger -Value $rawPart.bytes -Label 'part.bytes' -Minimum 1
        if ($partBytes -gt $script:MaxPartBytes) { throw 'A part exceeds the 64 MiB bound.' }
        $partSha256 = Normalize-Sha256 -Value $rawPart.sha256 -Label 'part.sha256'
        $expectedPartName = $archiveName + '.part' + $partNumber.ToString('00', [System.Globalization.CultureInfo]::InvariantCulture)
        if ($partName -cne $expectedPartName) { throw 'Manifest part names must be contiguous archive.partNN files.' }
        $sum = $sum + $partBytes
        if ($sum -gt $archiveBytes) { throw 'Manifest part sizes exceed archive.bytes.' }
        $parts += [pscustomobject]@{ Name = $partName; Bytes = $partBytes; Sha256 = $partSha256 }
        $partNumber++
    }
    if ($sum -ne $archiveBytes) { throw 'Manifest part sizes do not sum to archive.bytes.' }

    $production = $repository -ceq $script:ProductionRepository -and $tag -ceq $script:ProductionTag
    if ($production) {
        if ($manifestSha256 -cne $script:ProductionPartsManifestSha256 -or $archiveName -cne $script:ProductionArchiveName -or $archiveBytes -ne $script:ProductionArchiveBytes -or $archiveSha256 -cne $script:ProductionArchiveSha256 -or $memberCount -ne $script:ProductionMemberCount -or $uncompressedBytes -ne $script:ProductionUncompressedBytes -or $largestMemberBytes -ne $script:ProductionLargestMemberBytes -or $executablePath.Original -cne $script:ProductionExecutablePath -or $executableSha256 -cne $script:ProductionExecutableSha256 -or $resourcesManifestPath.Original -cne $script:ProductionResourcesManifestPath -or $resourcesManifestSha256 -cne $script:ProductionResourcesManifestSha256 -or $parts.Count -ne 20) {
            throw 'Production manifest values do not match the pinned offline release contract.'
        }
    }
    return [pscustomobject]@{
        Path = $Path
        Sha256 = $manifestSha256
        Directory = (Split-Path -Parent $Path)
        Repository = $repository
        Tag = $tag
        ArchiveName = $archiveName
        ArchiveBytes = $archiveBytes
        ArchiveSha256 = $archiveSha256
        MemberCount = $memberCount
        UncompressedBytes = $uncompressedBytes
        LargestMemberBytes = $largestMemberBytes
        ExecutablePath = $executablePath
        ExecutableSha256 = $executableSha256
        ResourcesManifestPath = $resourcesManifestPath
        ResourcesManifestSha256 = $resourcesManifestSha256
        Parts = $parts
        Production = $production
    }
}

function Assert-PinnedProductionManifest {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-ExistingFile -Path $Path -Label 'Production parts manifest'
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    if ($actual -cne $script:ProductionPartsManifestSha256) {
        throw 'Production parts manifest SHA-256 does not match the pinned offline release contract.'
    }
}

function Get-GhCommand {
    $command = Get-Command -Name 'gh.exe' -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $command) { return $null }
    return $command.Source
}

function Write-ManualDownloadInstructions {
    param([Parameter(Mandatory = $true)]$Manifest)
    Write-Output ('Missing parts. Download all exact files from: https://github.com/' + $Manifest.Repository + '/releases/tag/' + $Manifest.Tag)
    Write-Output ('The parts must be placed next to: ' + $Manifest.Path)
    Write-Output 'If gh.exe is installed, re-run with -DownloadMissing to use normal gh release download.'
}

function Ensure-Parts {
    param([Parameter(Mandatory = $true)]$Manifest, [Parameter(Mandatory = $true)][bool]$DownloadMissing)
    $missing = @()
    foreach ($part in $Manifest.Parts) {
        $partPath = Join-Path $Manifest.Directory $part.Name
        if (-not (Test-Path -LiteralPath $partPath -PathType Leaf)) { $missing += $part }
    }
    if ($missing.Count -gt 0) {
        $gh = Get-GhCommand
        if (-not $DownloadMissing) {
            Write-ManualDownloadInstructions -Manifest $Manifest
            if ($null -ne $gh) { Write-Output 'gh.exe was found; the download was not started without the explicit -DownloadMissing switch.' }
            throw 'Required archive parts are missing.'
        }
        if ($null -eq $gh) {
            Write-ManualDownloadInstructions -Manifest $Manifest
            throw 'gh.exe is not installed; download the parts manually.'
        }
        $arguments = @('release', 'download', $Manifest.Tag, '--repo', $Manifest.Repository)
        foreach ($part in $missing) { $arguments += @('--pattern', $part.Name) }
        $arguments += @('--dir', $Manifest.Directory)
        & $gh @arguments
        if ($LASTEXITCODE -ne 0) { throw ('gh release download failed with exit code ' + $LASTEXITCODE + '.') }
    }
    foreach ($part in $Manifest.Parts) {
        $partPath = Join-Path $Manifest.Directory $part.Name
        Assert-ExistingFile -Path $partPath -Label ('Archive part ' + $part.Name)
        $size = (Get-Item -LiteralPath $partPath -Force -ErrorAction Stop).Length
        if ([int64]$size -ne $part.Bytes) { throw ('Archive part size mismatch: ' + $part.Name) }
        Assert-FileSha256 -Path $partPath -Expected $part.Sha256 -Label ('Archive part ' + $part.Name)
    }
}

function New-UniqueOutputRoot {
    param([Parameter(Mandatory = $true)][string]$OutputDirectory)
    Assert-ExistingDirectory -Path $OutputDirectory -Label 'Output directory'
    Assert-NotFilesystemRoot -Path $OutputDirectory -Label 'Output directory'
    for ($attempt = 0; $attempt -lt 12; $attempt++) {
        $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ', [System.Globalization.CultureInfo]::InvariantCulture)
        $name = 'Kontur-offline-' + $stamp + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
        $candidate = Join-Path $OutputDirectory $name
        if (Test-Path -LiteralPath $candidate) { continue }
        [void](New-Item -ItemType Directory -Path $candidate -ErrorAction Stop)
        Assert-NotReparsePoint -Path $candidate -Label 'New output directory'
        return $candidate
    }
    throw 'Could not allocate a new output directory without overwriting an existing path.'
}

function Assemble-Archive {
    param([Parameter(Mandatory = $true)]$Manifest, [Parameter(Mandatory = $true)][string]$OutputRoot)
    $zipPath = Join-Path $OutputRoot $Manifest.ArchiveName
    $temporaryPath = $zipPath + '.partial'
    $output = $null
    try {
        $output = New-Object System.IO.FileStream($temporaryPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None, $script:CopyBufferBytes, [System.IO.FileOptions]::SequentialScan)
        foreach ($part in $Manifest.Parts) {
            $partPath = Join-Path $Manifest.Directory $part.Name
            $input = $null
            try {
                $input = New-Object System.IO.FileStream($partPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read, $script:CopyBufferBytes, [System.IO.FileOptions]::SequentialScan)
                Copy-StreamBounded -Source $input -Destination $output
            }
            finally {
                if ($null -ne $input) { $input.Dispose() }
            }
        }
    }
    finally {
        if ($null -ne $output) { $output.Dispose() }
    }
    $temporarySize = (Get-Item -LiteralPath $temporaryPath -Force -ErrorAction Stop).Length
    if ([int64]$temporarySize -ne $Manifest.ArchiveBytes) { throw 'Assembled archive size does not match the manifest.' }
    Assert-FileSha256 -Path $temporaryPath -Expected $Manifest.ArchiveSha256 -Label 'Assembled archive'
    [System.IO.File]::Move($temporaryPath, $zipPath)
    return $zipPath
}

function Ensure-SafeDirectoryTree {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $false)][string[]]$Segments = @())
    $current = $Root
    foreach ($segment in $Segments) {
        $current = Join-Path $current $segment
        if (Test-Path -LiteralPath $current) {
            if (-not (Test-Path -LiteralPath $current -PathType Container)) { throw 'Archive entry collides with a file path.' }
            Assert-NotReparsePoint -Path $current -Label 'Extraction directory'
        }
        else {
            [void](New-Item -ItemType Directory -Path $current -ErrorAction Stop)
            Assert-NotReparsePoint -Path $current -Label 'Extraction directory'
        }
    }
    return $current
}

function Get-ExtractionPath {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)]$SafeEntry)
    $destination = $Root
    foreach ($segment in $SafeEntry.Segments) { $destination = Join-Path $destination $segment }
    $fullDestination = [System.IO.Path]::GetFullPath($destination)
    if (-not (Test-PathWithin -Path $fullDestination -Root $Root)) { throw 'Archive entry escapes the extraction root.' }
    return $fullDestination
}

function Assert-ExtractionPathLength {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    if ($Path.Length -ge $script:MaxWindowsPathLength) {
        throw "$Label exceeds the Windows PowerShell 5 path limit; choose a shorter output directory."
    }
}

function Extract-ValidatedArchive {
    param([Parameter(Mandatory = $true)]$Manifest, [Parameter(Mandatory = $true)][string]$ZipPath, [Parameter(Mandatory = $true)][string]$OutputRoot)
    Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    $extractRoot = Join-Path $OutputRoot 'extracted'
    Assert-ExtractionPathLength -Path $extractRoot -Label 'Extraction root'
    $zip = $null
    try {
        $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
        if ([int64]$zip.Entries.Count -ne $Manifest.MemberCount) { throw 'ZIP member count does not match the manifest.' }
        $total = [int64]0
        $largest = [int64]0
        $seen = @{}
        $validated = @()
        foreach ($entry in $zip.Entries) {
            $safeEntry = Get-SafeArchiveEntry -Name $entry.FullName -Label 'ZIP entry'
            if (Test-ZipSymlink -Entry $entry) { throw 'ZIP symlink entries are not allowed.' }
            if ($seen.ContainsKey($safeEntry.Key)) { throw 'ZIP contains duplicate paths after Windows normalization.' }
            $seen[$safeEntry.Key] = $true
            $length = [int64]$entry.Length
            if ($length -lt 0 -or $length -gt $Manifest.LargestMemberBytes) { throw 'ZIP member exceeds the manifest member bound.' }
            $total = $total + $length
            if ($total -gt $Manifest.UncompressedBytes) { throw 'ZIP uncompressed size exceeds the manifest bound.' }
            if ($length -gt $largest) { $largest = $length }
            Assert-ExtractionPathLength -Path (Get-ExtractionPath -Root $extractRoot -SafeEntry $safeEntry) -Label 'ZIP extraction path'
            $validated += [pscustomobject]@{ Entry = $entry; Safe = $safeEntry; Length = $length }
        }
        if ($total -ne $Manifest.UncompressedBytes -or $largest -ne $Manifest.LargestMemberBytes) { throw 'ZIP uncompressed size or largest member does not match the manifest.' }
        [void](New-Item -ItemType Directory -Path $extractRoot -ErrorAction Stop)
        Assert-NotReparsePoint -Path $extractRoot -Label 'Extraction root'
        foreach ($item in $validated) {
            $destination = Get-ExtractionPath -Root $extractRoot -SafeEntry $item.Safe
            if ($item.Safe.IsDirectory) {
                if ($item.Length -ne 0) { throw 'ZIP directory entry has data.' }
                [void](Ensure-SafeDirectoryTree -Root $extractRoot -Segments $item.Safe.Segments)
                continue
            }
            $parentSegments = @()
            if ($item.Safe.Segments.Count -gt 1) { $parentSegments = @($item.Safe.Segments[0..($item.Safe.Segments.Count - 2)]) }
            [void](Ensure-SafeDirectoryTree -Root $extractRoot -Segments $parentSegments)
            if (Test-Path -LiteralPath $destination) { throw 'ZIP file path collides with an existing entry.' }
            $input = $null
            $output = $null
            try {
                $input = $item.Entry.Open()
                $output = New-Object System.IO.FileStream($destination, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None, $script:CopyBufferBytes, [System.IO.FileOptions]::SequentialScan)
                Copy-StreamBounded -Source $input -Destination $output
            }
            finally {
                if ($null -ne $output) { $output.Dispose() }
                if ($null -ne $input) { $input.Dispose() }
            }
            $written = (Get-Item -LiteralPath $destination -Force -ErrorAction Stop).Length
            if ([int64]$written -ne $item.Length) { throw 'Extracted member length does not match the ZIP entry.' }
            Assert-NotReparsePoint -Path $destination -Label 'Extracted file'
        }
    }
    finally {
        if ($null -ne $zip) { $zip.Dispose() }
    }
    $executablePath = Get-ExtractionPath -Root $extractRoot -SafeEntry $Manifest.ExecutablePath
    $resourcesPath = Get-ExtractionPath -Root $extractRoot -SafeEntry $Manifest.ResourcesManifestPath
    Assert-ExistingFile -Path $executablePath -Label 'Extracted executable'
    Assert-ExistingFile -Path $resourcesPath -Label 'Extracted resource manifest'
    Assert-FileSha256 -Path $executablePath -Expected $Manifest.ExecutableSha256 -Label 'Extracted executable'
    Assert-FileSha256 -Path $resourcesPath -Expected $Manifest.ResourcesManifestSha256 -Label 'Extracted resource manifest'
    return $extractRoot
}

function Invoke-Assembly {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$OutputDirectory,
        [Parameter(Mandatory = $false)][bool]$DownloadMissing = $false
    )
    $manifestFullPath = Resolve-FullPath -Path $ManifestPath -BasePath (Get-Location).Path
    $outputDirectoryFullPath = Resolve-FullPath -Path $OutputDirectory -BasePath (Get-Location).Path
    Assert-PinnedProductionManifest -Path $manifestFullPath
    $manifest = Read-PartsManifest -Path $manifestFullPath
    Assert-ExistingDirectory -Path $manifest.Directory -Label 'Manifest directory'
    Ensure-Parts -Manifest $manifest -DownloadMissing $DownloadMissing
    $outputRoot = New-UniqueOutputRoot -OutputDirectory $outputDirectoryFullPath
    $zip = Assemble-Archive -Manifest $manifest -OutputRoot $outputRoot
    $extracted = Extract-ValidatedArchive -Manifest $manifest -ZipPath $zip -OutputRoot $outputRoot
    [pscustomobject]@{
        result = 'PASS'
        archive = $zip
        extracted = $extracted
        archiveSha256 = $manifest.ArchiveSha256
        executableSha256 = $manifest.ExecutableSha256
        resourcesManifestSha256 = $manifest.ResourcesManifestSha256
    } | ConvertTo-Json -Compress
}

if ($MyInvocation.InvocationName -ne '.') {
    if ([string]::IsNullOrWhiteSpace($ManifestPath)) { $ManifestPath = Join-Path (Get-Location).Path 'Kontur-Local-Windows-Offline.parts.json' }
    if ([string]::IsNullOrWhiteSpace($OutputDirectory)) { $OutputDirectory = (Get-Location).Path }
    Invoke-Assembly -ManifestPath $ManifestPath -OutputDirectory $OutputDirectory -DownloadMissing ([bool]$DownloadMissing)
}

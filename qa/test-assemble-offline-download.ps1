[CmdletBinding()]
param([switch]$SkipNested)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$helper = Join-Path $repo 'scripts\assemble-offline-download.ps1'
$cmd = Join-Path $repo 'scripts\assemble-offline-download.cmd'
$artifactRoot = Join-Path $repo 'artifacts'

function Assert-QA {
    param([bool]$Condition, [Parameter(Mandatory = $true)][string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-QAThrows {
    param([Parameter(Mandatory = $true)][scriptblock]$Action, [Parameter(Mandatory = $true)][string]$Message, [string]$Pattern)
    $caught = $false
    try { & $Action } catch {
        $caught = $true
        if (-not [string]::IsNullOrWhiteSpace($Pattern) -and $_.Exception.Message -notmatch $Pattern) { throw ($Message + ': unexpected error: ' + $_.Exception.Message) }
    }
    if (-not $caught) { throw ($Message + ': expected failure did not occur.') }
}

function Read-Text([string]$Path) { return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) }

function Get-Sha([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() }

function Write-SyntheticZip {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)]$Entries)
    Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    $zip = $null
    try {
        $zip = [System.IO.Compression.ZipFile]::Open($Path, [System.IO.Compression.ZipArchiveMode]::Create)
        foreach ($item in $Entries) {
            $entry = $zip.CreateEntry($item.Name)
            $stream = $null
            try {
                $stream = $entry.Open()
                $stream.Write($item.Bytes, 0, $item.Bytes.Length)
            }
            finally { if ($null -ne $stream) { $stream.Dispose() } }
        }
    }
    finally { if ($null -ne $zip) { $zip.Dispose() } }
}

function Split-SyntheticZip {
    param([Parameter(Mandatory = $true)][string]$ZipPath, [Parameter(Mandatory = $true)][string]$Directory, [Parameter(Mandatory = $true)][string]$ArchiveName)
    $bytes = [System.IO.File]::ReadAllBytes($ZipPath)
    $half = [int][Math]::Ceiling($bytes.Length / 2.0)
    $part01 = New-Object byte[] $half
    [Array]::Copy($bytes, 0, $part01, 0, $half)
    $part02Length = $bytes.Length - $half
    $part02 = New-Object byte[] $part02Length
    [Array]::Copy($bytes, $half, $part02, 0, $part02Length)
    $path01 = Join-Path $Directory ($ArchiveName + '.part01')
    $path02 = Join-Path $Directory ($ArchiveName + '.part02')
    [System.IO.File]::WriteAllBytes($path01, $part01)
    [System.IO.File]::WriteAllBytes($path02, $part02)
    return @(
        [pscustomobject]@{ name = [System.IO.Path]::GetFileName($path01); bytes = $part01.Length; sha256 = Get-Sha $path01 },
        [pscustomobject]@{ name = [System.IO.Path]::GetFileName($path02); bytes = $part02.Length; sha256 = Get-Sha $path02 }
    )
}

function New-SyntheticManifest {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$ZipPath, [Parameter(Mandatory = $true)]$Entries, [Parameter(Mandatory = $true)]$Parts, [string]$ExecutablePath = 'kontur-local.exe', [string]$ResourcesPath = 'full-local-resources-manifest.json')
    $largest = 0
    $uncompressed = 0
    foreach ($entry in $Entries) {
        $uncompressed += $entry.Bytes.Length
        if ($entry.Bytes.Length -gt $largest) { $largest = $entry.Bytes.Length }
    }
    $manifest = [ordered]@{
        schemaVersion = 1
        repository = 'Synthetic/fixture'
        tag = 'qa'
        archive = [ordered]@{
            name = [System.IO.Path]::GetFileName($ZipPath)
            bytes = (Get-Item -LiteralPath $ZipPath).Length
            sha256 = Get-Sha $ZipPath
            memberCount = $Entries.Count
            uncompressedBytes = $uncompressed
            largestMemberBytes = $largest
            executablePath = $ExecutablePath
            executableSha256 = (Get-Sha (Join-Path (Split-Path -Parent $ZipPath) $ExecutablePath))
            resourcesManifestPath = $ResourcesPath
            resourcesManifestSha256 = (Get-Sha (Join-Path (Split-Path -Parent $ZipPath) $ResourcesPath))
        }
        parts = @($Parts)
    }
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding UTF8
    return $manifest
}

function Invoke-Helper {
    param([Parameter(Mandatory = $true)][string]$Manifest, [Parameter(Mandatory = $true)][string]$Output)
    $manifestObject = Read-PartsManifest -Path $Manifest
    Assert-ExistingDirectory -Path $manifestObject.Directory -Label 'Manifest directory'
    Ensure-Parts -Manifest $manifestObject -DownloadMissing $false
    $outputRoot = New-UniqueOutputRoot -OutputDirectory $Output
    $zip = Assemble-Archive -Manifest $manifestObject -OutputRoot $outputRoot
    $extracted = Extract-ValidatedArchive -Manifest $manifestObject -ZipPath $zip -OutputRoot $outputRoot
    return [pscustomobject]@{
        result = 'PASS'
        archive = $zip
        extracted = $extracted
        archiveSha256 = $manifestObject.ArchiveSha256
        executableSha256 = $manifestObject.ExecutableSha256
        resourcesManifestSha256 = $manifestObject.ResourcesManifestSha256
    }
}

function Test-HelperAst {
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($helper, [ref]$tokens, [ref]$errors)
    Assert-QA ($errors.Count -eq 0) 'PowerShell helper AST parsing failed.'
    $definitions = @($ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.FunctionDefinitionAst] })
    Assert-QA ($definitions.Count -ge 15) 'Helper function surface unexpectedly disappeared.'
    $cmdText = Read-Text $cmd
    Assert-QA ($cmdText -match '(?im)^cd /d "%~dp0"') '.cmd does not set its own working directory.'
    Assert-QA ($cmdText -match '-NoProfile') '.cmd must not load a PowerShell profile.'
    Assert-QA ($cmdText -match '-ExecutionPolicy Bypass') '.cmd must use process-scoped execution policy only.'
    Assert-QA ($cmdText -match '-File .*assemble-offline-download\.ps1') '.cmd does not invoke the helper file.'
    Assert-QA ($cmdText -match 'assemble-offline-download\.ps1" -DownloadMissing') '.cmd must enable explicit missing-part download mode.'
    Assert-QA ((Read-Text $helper) -match '\$MyInvocation\.InvocationName -ne ''\.''') 'Helper public entrypoint is not separated from the dot-source test seam.'
    return $ast
}

. $helper
Test-HelperAst | Out-Null

$fixture = Join-Path $artifactRoot ('assemble-offline-qa-' + [guid]::NewGuid().ToString('N'))
$fixtureOwned = $false
try {
    [void](New-Item -ItemType Directory -Path $fixture -ErrorAction Stop)
    $fixtureOwned = $true
    $archiveName = 'Synthetic.zip'
    $exeBytes = [System.Text.Encoding]::UTF8.GetBytes('synthetic executable bytes')
    $resourceBytes = [System.Text.Encoding]::UTF8.GetBytes('{"synthetic":true}')
    $dataBytes = [System.Text.Encoding]::UTF8.GetBytes('small fixture payload')
    $entries = @(
        [pscustomobject]@{ Name = 'kontur-local.exe'; Bytes = $exeBytes },
        [pscustomobject]@{ Name = 'full-local-resources-manifest.json'; Bytes = $resourceBytes },
        [pscustomobject]@{ Name = 'nested/data.txt'; Bytes = $dataBytes }
    )
    [System.IO.File]::WriteAllBytes((Join-Path $fixture 'kontur-local.exe'), $exeBytes)
    [System.IO.File]::WriteAllBytes((Join-Path $fixture 'full-local-resources-manifest.json'), $resourceBytes)
    $zipSource = Join-Path $fixture $archiveName
    Write-SyntheticZip -Path $zipSource -Entries $entries
    $parts = Split-SyntheticZip -ZipPath $zipSource -Directory $fixture -ArchiveName $archiveName
    $manifestPath = Join-Path $fixture 'Synthetic.parts.json'
    New-SyntheticManifest -Path $manifestPath -ZipPath $zipSource -Entries $entries -Parts $parts | Out-Null

    $depthMemberName = 'resources/' + ('x' * 95)
    Assert-QA ($depthMemberName.Length -eq 105) 'Path-depth fixture member must be 105 characters.'
    $depthEntry = Get-SafeArchiveEntry -Name $depthMemberName -Label 'Path-depth fixture'
    $portableDepthPath = Get-ExtractionPath -Root 'C:\Kontur\extracted' -SafeEntry $depthEntry
    Assert-QA ($portableDepthPath.Length -lt $script:MaxWindowsPathLength) 'Portable 105-character member unexpectedly exceeds the path bound.'
    Assert-ExtractionPathLength -Path $portableDepthPath -Label 'Portable path-depth fixture'
    $tooDeepRoot = Join-Path $fixture ('depth-' + ('r' * 100) + '\extracted')
    $tooDeepPath = Get-ExtractionPath -Root $tooDeepRoot -SafeEntry $depthEntry
    Assert-QA ($tooDeepPath.Length -ge $script:MaxWindowsPathLength) 'Deep path fixture did not cross the path bound.'
    Assert-QAThrows { Assert-ExtractionPathLength -Path $tooDeepPath -Label 'Deep path fixture' } 'Deep path was accepted' 'Windows PowerShell 5 path limit'

    $publicGuard = Join-Path $fixture 'public-guard'
    [void](New-Item -ItemType Directory -Path $publicGuard)
    $publicGuardObject = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $publicGuardObject.repository = 'attacker/example'
    $publicGuardObject.tag = 'not-the-pinned-release'
    $publicGuardManifest = Join-Path $publicGuard 'Synthetic.parts.json'
    $publicGuardObject | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $publicGuardManifest -Encoding UTF8
    Copy-Item -LiteralPath (Join-Path $fixture ($archiveName + '.part01')) -Destination $publicGuard
    $fakeGh = Join-Path $publicGuard 'gh.exe'
    $ghSentinel = Join-Path $publicGuard 'gh-invocation-sentinel.txt'
    [System.IO.File]::WriteAllText($fakeGh, 'This file must never be executed by the pinned-manifest guard.', [System.Text.Encoding]::ASCII)
    $oldPath = $env:PATH
    try {
        $env:PATH = $publicGuard + ';' + $oldPath
        Assert-QAThrows { & $helper -ManifestPath $publicGuardManifest -OutputDirectory $publicGuard -DownloadMissing } 'Public helper accepted an unpinned repository/tag manifest' 'pinned offline release contract'
    }
    finally { $env:PATH = $oldPath }
    Assert-QA (-not (Test-Path -LiteralPath $ghSentinel)) 'Pinned-manifest rejection reached the gh download seam.'

    $existingZip = Join-Path $fixture 'existing.zip'
    [System.IO.File]::WriteAllText($existingZip, 'do not replace', [System.Text.Encoding]::UTF8)
    $existingDirectory = Join-Path $fixture 'existing-target'
    [void](New-Item -ItemType Directory -Path $existingDirectory)
    $existingSentinel = Join-Path $existingDirectory 'sentinel.txt'
    [System.IO.File]::WriteAllText($existingSentinel, 'keep', [System.Text.Encoding]::UTF8)

    $result = Invoke-Helper -Manifest $manifestPath -Output $fixture
    Assert-QA ($result.result -eq 'PASS') 'Synthetic helper result was not PASS.'
    Assert-QA (Test-Path -LiteralPath $result.archive -PathType Leaf) 'Assembled ZIP is missing.'
    Assert-QA ((Get-Sha $result.archive) -eq (Get-Sha $zipSource)) 'Assembled ZIP differs from the source fixture.'
    Assert-QA (Test-Path -LiteralPath (Join-Path $result.extracted 'nested\data.txt') -PathType Leaf) 'Synthetic nested file was not extracted.'
    Assert-QA ((Get-Sha (Join-Path $result.extracted 'kontur-local.exe')) -eq (Get-Sha (Join-Path $fixture 'kontur-local.exe'))) 'Synthetic executable hash was not preserved.'
    Assert-QA (([System.IO.File]::ReadAllText($existingZip, [System.Text.Encoding]::UTF8)).Contains('do not replace')) 'Existing ZIP was changed.'
    Assert-QA (([System.IO.File]::ReadAllText($existingSentinel, [System.Text.Encoding]::UTF8)).Contains('keep')) 'Existing output directory was changed.'

    $badHash = Join-Path $fixture 'bad-hash'
    [void](New-Item -ItemType Directory -Path $badHash)
    Copy-Item -LiteralPath (Join-Path $fixture ($archiveName + '.part01')) -Destination $badHash
    Copy-Item -LiteralPath (Join-Path $fixture ($archiveName + '.part02')) -Destination $badHash
    $badHashManifest = Join-Path $badHash 'Synthetic.parts.json'
    $badHashObject = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $badHashObject.parts[0].sha256 = ('0' * 64)
    $badHashObject | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $badHashManifest -Encoding UTF8
    Assert-QAThrows { Invoke-Helper -Manifest $badHashManifest -Output $badHash } 'Bad part hash was accepted' 'SHA-256'

    $badSchema = Join-Path $fixture 'bad-schema'
    [void](New-Item -ItemType Directory -Path $badSchema)
    $badSchemaManifest = Join-Path $badSchema 'Synthetic.parts.json'
    $badSchemaObject = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    Add-Member -InputObject $badSchemaObject -MemberType NoteProperty -Name unexpected -Value $true
    $badSchemaObject | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $badSchemaManifest -Encoding UTF8
    Assert-QAThrows { Invoke-Helper -Manifest $badSchemaManifest -Output $badSchema } 'Unknown manifest field was accepted' 'unexpected schema'

    $badPath = Join-Path $fixture 'bad-path'
    [void](New-Item -ItemType Directory -Path $badPath)
    $badEntries = @(
        [pscustomobject]@{ Name = '../escape.txt'; Bytes = [System.Text.Encoding]::UTF8.GetBytes('escape') },
        [pscustomobject]@{ Name = 'kontur-local.exe'; Bytes = $exeBytes }
    )
    $badZip = Join-Path $badPath 'Unsafe.zip'
    [System.IO.File]::WriteAllBytes((Join-Path $badPath 'kontur-local.exe'), $exeBytes)
    Write-SyntheticZip -Path $badZip -Entries $badEntries
    $badParts = Split-SyntheticZip -ZipPath $badZip -Directory $badPath -ArchiveName 'Unsafe.zip'
    $badManifest = Join-Path $badPath 'Unsafe.parts.json'
    New-SyntheticManifest -Path $badManifest -ZipPath $badZip -Entries $badEntries -Parts $badParts -ResourcesPath 'kontur-local.exe' | Out-Null
    $escapePath = Join-Path $fixture 'escape.txt'
    Assert-QAThrows { Invoke-Helper -Manifest $badManifest -Output $badPath } 'Unsafe ZIP path was accepted' 'unsafe archive path'
    Assert-QA (-not (Test-Path -LiteralPath $escapePath)) 'Unsafe ZIP path escaped the fixture root.'

    $missing = Join-Path $fixture 'missing'
    [void](New-Item -ItemType Directory -Path $missing)
    Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $missing 'Synthetic.parts.json')
    Copy-Item -LiteralPath (Join-Path $fixture ($archiveName + '.part01')) -Destination $missing
    Assert-QAThrows { Invoke-Helper -Manifest (Join-Path $missing 'Synthetic.parts.json') -Output $missing } 'Missing part was not rejected' 'Required archive parts'

    [pscustomobject]@{
        result = 'PASS'
        powershell = $PSVersionTable.PSVersion.ToString()
        checks = @('ps-ast', 'cmd-contract', 'pinned-public-entrypoint', 'path-depth-guard', 'assemble-extract', 'badpart-reject', 'schema-reject', 'path-reject', 'existing-target-preserved', 'missing-part-instructions')
    } | ConvertTo-Json -Compress
}
finally {
    if ($fixtureOwned -and (Test-Path -LiteralPath $fixture)) {
        $resolvedFixture = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $fixture).ProviderPath)
        $artifactPrefix = [System.IO.Path]::GetFullPath($artifactRoot).TrimEnd('\') + '\'
        if (-not $resolvedFixture.StartsWith($artifactPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'QA cleanup target escaped artifacts.' }
        if ((Get-Item -LiteralPath $resolvedFixture -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'QA cleanup refuses a reparse point.' }
        Remove-Item -LiteralPath $resolvedFixture -Recurse -Force
    }
}

if (-not $SkipNested -and $PSVersionTable.PSEdition -eq 'Core') {
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf) {
        & $windowsPowerShell -NoLogo -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -SkipNested
        if ($LASTEXITCODE -ne 0) { throw 'Windows PowerShell 5 functional QA failed.' }
    }
    else { Write-Output 'PS5 functional QA unavailable; PS5 executable was not found.' }
}

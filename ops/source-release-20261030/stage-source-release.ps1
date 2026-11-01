[CmdletBinding()]
param(
    [string]$RepositoryRoot,
    [string]$Destination,
    [switch]$ManifestOnly,
    [string]$ManifestPath,
    [long]$MaxFileBytes = 8388608
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
} else {
    $RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
}

if (-not (Test-Path -LiteralPath (Join-Path $RepositoryRoot 'native') -PathType Container)) {
    throw "RepositoryRoot does not look like kontur-offline: native/ is missing."
}

if ($MaxFileBytes -le 0) {
    throw 'MaxFileBytes must be positive.'
}

if ($ManifestOnly -and -not [string]::IsNullOrWhiteSpace($Destination)) {
    throw 'Do not pass Destination together with ManifestOnly.'
}

if (-not $ManifestOnly -and [string]::IsNullOrWhiteSpace($Destination)) {
    throw 'Destination is required unless ManifestOnly is selected.'
}

$RepositoryRoot = $RepositoryRoot.TrimEnd('\')
$ManifestPath = if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    Join-Path $PSScriptRoot 'SOURCE_MANIFEST.tsv'
} else {
    [System.IO.Path]::GetFullPath($ManifestPath)
}

$AllowedExtensions = @(
    '.cjs', '.cmd', '.css', '.html', '.ico', '.js', '.json', '.lock', '.mjs', '.md', '.otf',
    '.png', '.ps1', '.py', '.rs', '.scss', '.svg', '.toml', '.ts', '.tsx',
    '.ttf', '.txt', '.woff', '.woff2'
)

$ForbiddenPathPattern = '(^|/)(node_modules|\.next|out|pyodide|runtime|archives?|snapshots?|logs?|receipts?|installers?|binaries?|database|databases|sqlite|dpapi|sessions?|cookies?|tokens?|\.qa|pytest_cache|ruff_cache|__pycache__)(/|$)|(^|/)\.env[^/]*(/|$)|(^|/)[^/]*(\.pem|\.key|\.p12|\.pfx|\.db|\.sqlite3?|\.zip|\.exe|\.dll|\.so|\.dylib|\.msi|\.log|\.bak)$'

$TextExtensions = @(
    '.cjs', '.cmd', '.css', '.html', '.js', '.json', '.lock', '.mjs', '.md', '.ps1', '.py',
    '.rs', '.scss', '.toml', '.ts', '.tsx', '.txt'
)

$ExclusionCounts = [ordered]@{}
$MissingRoots = New-Object 'System.Collections.Generic.List[string]'
$CandidateMap = @{}

function Add-Exclusion {
    param([Parameter(Mandatory = $true)][string]$Reason)

    if (-not $ExclusionCounts.Contains($Reason)) {
        $ExclusionCounts[$Reason] = 0
    }
    $ExclusionCounts[$Reason] = [int]$ExclusionCounts[$Reason] + 1
}

function Test-ReparsePoint {
    param([Parameter(Mandatory = $true)][System.IO.FileSystemInfo]$Item)

    return (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Get-RelativeUnixPath {
    param([Parameter(Mandatory = $true)][string]$FullPath)

    $full = [System.IO.Path]::GetFullPath($FullPath)
    $prefix = $RepositoryRoot.TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escaped RepositoryRoot: $full"
    }
    return $full.Substring($prefix.Length).Replace('\', '/')
}

function Add-Candidate {
    param([Parameter(Mandatory = $true)][string]$FullPath)

    $item = Get-Item -LiteralPath $FullPath -Force
    if (Test-ReparsePoint -Item $item) {
        throw "Reparse point rejected: $(Get-RelativeUnixPath -FullPath $item.FullName)"
    }

    $relative = Get-RelativeUnixPath -FullPath $item.FullName
    if ($relative -match $ForbiddenPathPattern) {
        Add-Exclusion -Reason 'path-policy'
        return
    }

    $extension = [System.IO.Path]::GetExtension($relative).ToLowerInvariant()
    if ($AllowedExtensions -notcontains $extension -and $item.Name -ne '.gitignore') {
        Add-Exclusion -Reason 'extension-policy'
        return
    }

    if ($item.Length -gt $MaxFileBytes) {
        throw "Unexpectedly large source file rejected: $relative ($($item.Length) bytes)"
    }

    if (-not $CandidateMap.ContainsKey($relative)) {
        $CandidateMap[$relative] = $item.FullName
    }
}

function Add-ExplicitFile {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $fullPath = Join-Path $RepositoryRoot ($RelativePath.Replace('/', '\'))
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        Add-Exclusion -Reason 'missing-explicit-file'
        return
    }
    Add-Candidate -FullPath $fullPath
}

function Add-SafeTree {
    param(
        [Parameter(Mandatory = $true)][string]$RelativeRoot,
        [switch]$Required
    )

    $absoluteRoot = Join-Path $RepositoryRoot ($RelativeRoot.Replace('/', '\'))
    if (-not (Test-Path -LiteralPath $absoluteRoot -PathType Container)) {
        if ($Required) {
            [void]$MissingRoots.Add($RelativeRoot)
        } else {
            Add-Exclusion -Reason 'missing-optional-root'
        }
        return
    }

    $rootItem = Get-Item -LiteralPath $absoluteRoot -Force
    if (Test-ReparsePoint -Item $rootItem) {
        throw "Reparse point rejected at tree root: $RelativeRoot"
    }

    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($rootItem.FullName)
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        $currentItem = Get-Item -LiteralPath $current -Force
        if (Test-ReparsePoint -Item $currentItem) {
            throw "Reparse point rejected: $(Get-RelativeUnixPath -FullPath $currentItem.FullName)"
        }

        foreach ($entry in Get-ChildItem -LiteralPath $currentItem.FullName -Force) {
            if (Test-ReparsePoint -Item $entry) {
                throw "Reparse point rejected: $(Get-RelativeUnixPath -FullPath $entry.FullName)"
            }

            $relative = Get-RelativeUnixPath -FullPath $entry.FullName
            if ($relative -match $ForbiddenPathPattern -or
                $relative -match '^(full/frontend|web)/public/python-runtime(?:/|$)' -or
                $relative -match '^full/backend/tests/fixtures(?:/|$)') {
                Add-Exclusion -Reason 'path-policy'
                continue
            }

            if ($entry.PSIsContainer) {
                $stack.Push($entry.FullName)
            } else {
                Add-Candidate -FullPath $entry.FullName
            }
        }
    }
}

# Explicit tree roots keep traversal away from runtime binaries, caches, QA
# outputs, profile data, and the original server tree.
$TreeRoots = @(
    @{ Path = 'native/src'; Required = $true },
    @{ Path = 'native/tests'; Required = $true },
    @{ Path = 'native/capabilities'; Required = $true },
    @{ Path = 'native/permissions'; Required = $false },
    @{ Path = 'native/icons'; Required = $true },
    @{ Path = 'full/frontend/app'; Required = $true },
    @{ Path = 'full/frontend/components'; Required = $true },
    @{ Path = 'full/frontend/lib'; Required = $true },
    @{ Path = 'full/frontend/tests'; Required = $true },
    @{ Path = 'full/frontend/verification'; Required = $false },
    @{ Path = 'full/frontend/reference'; Required = $false },
    @{ Path = 'full/frontend/public'; Required = $true },
    @{ Path = 'full/frontend/public/icons'; Required = $true },
    @{ Path = 'full/frontend/public/assets'; Required = $false },
    @{ Path = 'full/frontend/public/fonts'; Required = $false },
    @{ Path = 'full/frontend/assets'; Required = $false },
    @{ Path = 'full/frontend/fonts'; Required = $false },
    @{ Path = 'full/frontend/icons'; Required = $false },
    @{ Path = 'full/frontend/scripts'; Required = $false },
    @{ Path = 'full/backend/app'; Required = $true },
    @{ Path = 'full/backend/tests'; Required = $true },
    @{ Path = 'web/app'; Required = $false },
    @{ Path = 'web/components'; Required = $false },
    @{ Path = 'web/lib'; Required = $false },
    @{ Path = 'web/tests'; Required = $false },
    @{ Path = 'web/public'; Required = $false }
)

foreach ($tree in $TreeRoots) {
    Add-SafeTree -RelativeRoot $tree.Path -Required:$tree.Required
}

$ExplicitFiles = @(
    'README.md', 'HANDOFF_RU.md', '.gitignore',
    'native/tauri.conf.json',
    'native/build.rs',
    'native/Cargo.toml',
    'native/Cargo.lock',
    'full/frontend/next-env.d.ts',
    'full/frontend/next.config.ts',
    'full/frontend/tsconfig.json',
    'full/frontend/package.json',
    'full/frontend/package-lock.json',
    'full/backend/local_codex.py',
    'full/backend/offline_app.py',
    'full/backend/offline_bridge.py',
    'full/backend/offline_catalog.py',
    'full/backend/offline_protocol.py',
    'full/backend/offline_state.py',
    'full/backend/offline_transfer.py',
    'full/backend/offline_ai.py',
    'full/backend/offline_ai_adapter.py',
    'full/backend/offline_ai_gateway.py',
    'full/backend/offline_ai_opencode.py',
    'full/backend/offline_ai_providers.py',
    'full/backend/offline_ai_secrets.py',
    'full/backend/offline_ai_settings.py',
    'full/backend/pyproject.toml',
    'full/backend/requirements.lock',
    'full/backend/LOCAL_ADAPTER.md',
    'full/backend/LOCAL_CODEX.md',
    'full/backend/TRANSFER_SCHEMA.md',
    'full/backend/tests/fixtures/import_kege_current_bank.py',
    'full/frontend/STATIC_EXPORT_MANIFEST.json',
    'full/frontend/FRONTEND_INTEGRATION_MANIFEST.json',
    'full/frontend/SOURCE_COPY_MANIFEST.json',
    'full/frontend/PYODIDE_ASSETS.json',
    'assets/branding/kontur-user-icon.png',
    'assets/branding/icon.ico',
    'web/next-env.d.ts', 'web/next.config.ts', 'web/tsconfig.json', 'web/package.json', 'web/README.md',
    'scripts/build-offline.ps1',
    'qa/run-native-full-check.ps1',
    'qa/prepare_native_fixtures.py',
    'qa/prepare_full_resources.py',
    'qa/test-install-marker.ps1',
    'qa/exporter_transfer.py',
    'exporter/__init__.py',
    'exporter/kontur_transfer.py',
    'qa/full-parity/runtime-handoff.md',
    'docs/FULL_RESOURCE_PACKAGING.md',
    'docs/PYTHON_RUNTIME_COMPONENT.md',
    'docs/FULL_PARITY_MATRIX.md',
    'docs/FULL_PARITY_STATUS.md',
    'ops/source-release-20261030/stage-source-release.ps1',
    'ops/source-release-20261030/README.md',
    'scripts/build-full-local.ps1',
    'scripts/package-python-runtime.ps1',
    'scripts/package-offline.ps1',
    'scripts/package-install-local.ps1',
    'scripts/assemble-offline-download.ps1',
    'scripts/assemble-offline-download.cmd',
    'qa/test-assemble-offline-download.ps1',
    'docs/DOWNLOAD_WINDOWS_RU.md',
    'docs/FULL_PARITY_CONTRACT.md',
    'docs/CLIENT_STATE_CONTRACT.md',
    'docs/TRANSFER_FORMAT.md',
    'docs/CATALOG_SEED_V2.md',
    'docs/OFFLINE_AI_CONNECTIONS.md',
    'docs/OPENDESIGN_PROMPT_RU.md',
    'docs/CONTRACT.md',
    'ops/catalog-offline-20261029-v4/export_catalog.py',
    'ops/catalog-offline-20261029/export_catalog.py',
    'ops/catalog-offline-20261029/test_export_catalog.py',
    'ops/catalog-offline-20261029-v4/test_export_catalog.py',
    'ops/catalog-offline-20261029-v4/PLAN_RU.md',
    'ops/catalog-offline-20261029-v4/CATALOG_SEED_FORMAT.md',
    'ops/catalog-assets-offline-20261030/download_assets.py',
    'ops/catalog-assets-offline-20261030/test_download_assets.py',
    'ops/catalog-assets-offline-20261030/test_download_assets_security.py',
    'ops/catalog-assets-offline-20261030/test_downloader_contract.py',
    'ops/catalog-assets-offline-20261030/test_extended_budget.py',
    'ops/catalog-assets-offline-20261030/inspect_asset_headers.py',
    'ops/catalog-assets-offline-20261030/test_inspect_asset_headers.py',
    'ops/catalog-assets-offline-20261030/PLAN_RU.md',
    'ops/catalog-seed-finalizer-20261030/finalize_catalog.py',
    'ops/catalog-seed-finalizer-20261030/test_finalize_catalog.py',
    'ops/catalog-seed-finalizer-20261030/PLAN_RU.md'
)

foreach ($relativePath in $ExplicitFiles) {
    Add-ExplicitFile -RelativePath $relativePath
}

$RequiredFiles = @(
    'native/build.rs', 'native/Cargo.toml', 'native/Cargo.lock',
    'full/frontend/package.json', 'full/frontend/package-lock.json',
    'full/backend/app/auth.py', 'full/backend/local_codex.py',
    'full/backend/offline_app.py', 'full/backend/offline_bridge.py',
    'full/backend/offline_ai.py', 'full/backend/offline_ai_adapter.py',
    'full/backend/offline_ai_gateway.py', 'full/backend/offline_ai_opencode.py',
    'full/backend/offline_ai_providers.py', 'full/backend/offline_ai_secrets.py',
    'full/backend/offline_ai_settings.py',
    'scripts/build-full-local.ps1', 'scripts/package-python-runtime.ps1',
    'scripts/package-offline.ps1', 'scripts/package-install-local.ps1',
    'docs/FULL_PARITY_CONTRACT.md', 'docs/CLIENT_STATE_CONTRACT.md',
    'docs/TRANSFER_FORMAT.md', 'docs/CATALOG_SEED_V2.md'
)

foreach ($required in $RequiredFiles) {
    if (-not $CandidateMap.ContainsKey($required)) {
        throw "Required allowlisted source is missing or rejected: $required"
    }
}

if ($MissingRoots.Count -gt 0) {
    throw "Required source roots are missing: $($MissingRoots -join ', ')"
}

if ($CandidateMap.Count -eq 0) {
    throw 'No allowlisted source files were found.'
}

function Get-SecretMatches {
    $patterns = @(
        @{ Name = 'private-key-block'; Pattern = '-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----' },
        @{ Name = 'jwt-like-token'; Pattern = '\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b' },
        @{ Name = 'openai-like-token'; Pattern = '\bsk-[A-Za-z0-9_-]{20,}\b' },
        @{ Name = 'github-like-token'; Pattern = '\bgh[pousr]_[A-Za-z0-9_]{20,}\b' },
        @{ Name = 'google-like-token'; Pattern = '\bAIza[A-Za-z0-9_-]{30,}\b' },
        @{ Name = 'slack-like-token'; Pattern = '\bxox[baprs]-[A-Za-z0-9-]{20,}\b' },
        @{ Name = 'assigned-secret'; Pattern = '(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)\s*[:=]\s*["''][A-Za-z0-9_./+=-]{24,}["'']' }
    )
    $matches = New-Object 'System.Collections.Generic.List[object]'
    foreach ($entry in $CandidateMap.GetEnumerator()) {
        $relative = [string]$entry.Key
        $extension = [System.IO.Path]::GetExtension($relative).ToLowerInvariant()
        if ($TextExtensions -notcontains $extension) {
            continue
        }

        $text = [string](Get-Content -LiteralPath ([string]$entry.Value) -Raw -Encoding UTF8)
        foreach ($rule in $patterns) {
            $count = [System.Text.RegularExpressions.Regex]::Matches(
                $text,
                [string]$rule.Pattern,
                [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
            ).Count
            if ($count -gt 0) {
                [void]$matches.Add([pscustomobject]@{
                    Category = [string]$rule.Name
                    Path = $relative
                    Count = $count
                })
            }
        }
    }
    return $matches.ToArray()
}

$SecretMatches = @(Get-SecretMatches)

function Get-FileRows {
    param([Parameter(Mandatory = $true)][string]$Root)

    $rows = New-Object 'System.Collections.Generic.List[object]'
    foreach ($relative in ($CandidateMap.Keys | Sort-Object)) {
        $fullPath = Join-Path $Root ($relative.Replace('/', '\'))
        $item = Get-Item -LiteralPath $fullPath -Force
        if (Test-ReparsePoint -Item $item) {
            throw "Reparse point rejected in staged source: $relative"
        }
        [void]$rows.Add([pscustomobject]@{
            Path = $relative
            Bytes = [int64]$item.Length
            Sha256 = ((Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLowerInvariant())
        })
    }
    return $rows.ToArray()
}

function Write-SecretReport {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Rows
    )

    $lines = New-Object 'System.Collections.Generic.List[string]'
    [void]$lines.Add("category`tpath`tcount")
    if ($Rows.Count -eq 0) {
        [void]$lines.Add('# no matches')
    } else {
        foreach ($row in ($Rows | Sort-Object Category, Path)) {
            [void]$lines.Add("$($row.Category)`t$($row.Path)`t$($row.Count)")
        }
    }
    Set-Content -LiteralPath $Path -Value $lines -Encoding UTF8
}

function Write-ReleaseMetadata {
    param(
        [Parameter(Mandatory = $true)][string]$OutputDirectory,
        [Parameter(Mandatory = $true)][object[]]$Rows,
        [Parameter(Mandatory = $true)][string]$Prefix
    )

    $fileLines = New-Object 'System.Collections.Generic.List[string]'
    [void]$fileLines.Add("path`tbytes`tsha256")
    foreach ($row in ($Rows | Sort-Object Path)) {
        [void]$fileLines.Add("$($row.Path)`t$($row.Bytes)`t$($row.Sha256)")
    }
    Set-Content -LiteralPath (Join-Path $OutputDirectory "${Prefix}_FILELIST.tsv") -Value $fileLines -Encoding UTF8

    $hashLines = New-Object 'System.Collections.Generic.List[string]'
    foreach ($row in ($Rows | Sort-Object Path)) {
        [void]$hashLines.Add("$($row.Sha256)  $($row.Path)")
    }
    Set-Content -LiteralPath (Join-Path $OutputDirectory "${Prefix}_SHA256SUMS.txt") -Value $hashLines -Encoding UTF8

    $summary = [ordered]@{
        schemaVersion = 1
        sourceOnly = $true
        fileCount = $Rows.Count
        byteCount = [int64](($Rows | Measure-Object -Property Bytes -Sum).Sum)
        maxFileBytes = $MaxFileBytes
        repositoryRoot = 'kontur-offline'
        originalServerRead = $false
        networkUsed = $false
        gitMutation = $false
    }
    $summary | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $OutputDirectory "${Prefix}_SUMMARY.json") -Encoding UTF8

    $exclusionLines = New-Object 'System.Collections.Generic.List[string]'
    [void]$exclusionLines.Add('# Source-release exclusions')
    [void]$exclusionLines.Add('')
    [void]$exclusionLines.Add('This package contains source only. Runtime binaries, catalogs, user data, and QA output are intentionally omitted.')
    [void]$exclusionLines.Add('')
    [void]$exclusionLines.Add('Counts are reported without reading excluded file contents:')
    foreach ($entry in $ExclusionCounts.GetEnumerator() | Sort-Object Name) {
        [void]$exclusionLines.Add("- $($entry.Key): $($entry.Value)")
    }
    if ($ExclusionCounts.Count -eq 0) {
        [void]$exclusionLines.Add('- none observed')
    }
    [void]$exclusionLines.Add('')
    [void]$exclusionLines.Add('Policy: no .env*, keys, private-key material, DPAPI/auth/session/cookie data, databases, snapshots, logs, receipts, installers, archives, node_modules, .next, out, Pyodide/runtime binaries, or real task/bank/answer assets are staged.')
    Set-Content -LiteralPath (Join-Path $OutputDirectory "${Prefix}_EXCLUSIONS.md") -Value $exclusionLines -Encoding UTF8

    Write-SecretReport -Path (Join-Path $OutputDirectory "${Prefix}_SECRET_SCAN.tsv") -Rows $SecretMatches
}

if ($SecretMatches.Count -gt 0) {
    $rejectReport = Join-Path $PSScriptRoot 'SOURCE_SECRET_SCAN_REJECTED.tsv'
    Write-SecretReport -Path $rejectReport -Rows $SecretMatches
    throw "Secret scan rejected $($SecretMatches.Count) finding(s); see $rejectReport (values are never written)."
}

if ($ManifestOnly) {
    $manifestDirectory = Split-Path -Parent $ManifestPath
    if (-not (Test-Path -LiteralPath $manifestDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $manifestDirectory -Force | Out-Null
    }

    $rows = @(Get-FileRows -Root $RepositoryRoot)
    $manifestLines = New-Object 'System.Collections.Generic.List[string]'
    [void]$manifestLines.Add("path`tbytes`tsha256")
    foreach ($row in ($rows | Sort-Object Path)) {
        [void]$manifestLines.Add("$($row.Path)`t$($row.Bytes)`t$($row.Sha256)")
    }
    Set-Content -LiteralPath $ManifestPath -Value $manifestLines -Encoding UTF8
    Write-ReleaseMetadata -OutputDirectory $manifestDirectory -Rows $rows -Prefix 'SOURCE'
    Write-Output "ManifestOnly: $($rows.Count) files, $((($rows | Measure-Object -Property Bytes -Sum).Sum)) bytes"
    exit 0
}

$destinationFull = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationFull) {
    throw "Destination must be a new directory: $destinationFull"
}

$destinationParent = Split-Path -Parent $destinationFull
if (-not (Test-Path -LiteralPath $destinationParent -PathType Container)) {
    New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
}
$parentItem = Get-Item -LiteralPath $destinationParent -Force
if (Test-ReparsePoint -Item $parentItem) {
    throw "Destination parent is a reparse point: $destinationParent"
}

New-Item -ItemType Directory -Path $destinationFull -Force | Out-Null
foreach ($relative in ($CandidateMap.Keys | Sort-Object)) {
    $sourcePath = [string]$CandidateMap[$relative]
    $targetPath = Join-Path $destinationFull ($relative.Replace('/', '\'))
    $targetParent = Split-Path -Parent $targetPath
    if (-not (Test-Path -LiteralPath $targetParent -PathType Container)) {
        New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
    }
    Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Force
}

$rows = @(Get-FileRows -Root $destinationFull)
foreach ($row in $rows) {
    if ($row.Path -match $ForbiddenPathPattern) {
        throw "Forbidden path reached staged output: $($row.Path)"
    }
}

Write-ReleaseMetadata -OutputDirectory $destinationFull -Rows $rows -Prefix 'RELEASE'
Write-Output "Staged: $destinationFull"
Write-Output "Files: $($rows.Count); bytes: $((($rows | Measure-Object -Property Bytes -Sum).Sum))"

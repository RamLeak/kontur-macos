[CmdletBinding()]
param(
    [Parameter()]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [Alias('ExePath')]
    [ValidateNotNullOrEmpty()]
    [string]$ExecutablePath,

    [Parameter()]
    [Alias('DestinationRoot', 'OutputDirectory')]
    [string]$Destination,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$IconPath,

    [Parameter()]
    [Alias('PreparedResourcesDirectory', 'PreparedResourcesRoot', 'PreparedResources', 'ResourcesRoot')]
    [string]$ResourcesDirectory,

    [Parameter()]
    [Alias('ApprovedResourcesManifest', 'ApprovedResourcesManifestPath', 'FullResourcesManifestPath', 'ResourcesManifest')]
    [string]$ResourcesManifestPath,

    [Parameter()]
    [Alias('ResourcesManifestHash', 'ApprovedResourcesManifestSha256')]
    [ValidatePattern('^[0-9A-Fa-f]{64}$')]
    [string]$ResourcesManifestSha256
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$displayName = -join @(
    [char]0x041A; [char]0x043E; [char]0x043D; [char]0x0442; [char]0x0443; [char]0x0440
    [char]0x0020
    [char]0x041B; [char]0x043E; [char]0x043A; [char]0x0430; [char]0x043B; [char]0x044C
    [char]0x043D; [char]0x044B; [char]0x0439
)

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

function Assert-NearestExistingDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $candidate = [System.IO.Path]::GetFullPath($Path)
    while (-not (Test-Path -LiteralPath $candidate)) {
        $parent = Split-Path -Parent $candidate
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent.Equals($candidate, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Could not resolve an existing parent for ${Label}: $Path"
        }
        $candidate = $parent
    }
    Assert-ExistingDirectory -Path $candidate -Label "$Label ancestor"
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Content
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Normalize-ResourceRelativePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $normalized = $Path.Replace('\', '/')
    if ([string]::IsNullOrWhiteSpace($normalized) -or $normalized.StartsWith('/') -or
        $normalized -match '^[A-Za-z]:' -or $normalized -match '[\x00-\x1F]') {
        throw "$Label contains an unsafe relative path: $Path"
    }

    $parts = $normalized.Split('/')
    foreach ($part in $parts) {
        if ([string]::IsNullOrWhiteSpace($part) -or $part -eq '.' -or $part -eq '..' -or
            $part.IndexOf([char]0) -ge 0 -or
            $part.IndexOfAny([char[]]@([char]0x0001, [char]0x0002, [char]0x0003)) -ge 0) {
            throw "$Label contains an unsafe relative path: $Path"
        }
    }

    return ($parts -join '/')
}

function Test-ForbiddenResourcePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RelativePath
    )

    $parts = $RelativePath.Replace('\', '/').Split('/')
    foreach ($part in $parts) {
        if ($part -match '^(?i:\.env)') {
            return $true
        }
        if ($part -in @('__pycache__', '.git', '.hg', '.svn', '.venv', 'venv',
                'auth', 'auth-data', 'auth_data', 'credential', 'credentials',
                'profile', 'profiles', 'cache', 'caches')) {
            return $true
        }
    }

    $leaf = $parts[$parts.Count - 1]
    return $leaf -match '^(?i:\.env(?:\..*)?)$' -or
        $leaf -match '^(?i:(sitecustomize|usercustomize)\.py)$' -or
        $leaf -match '(?i:\.pth)$' -or
        $leaf -match '^(?i:(direct_url\.json|INSTALLER|REQUESTED|pyvenv\.cfg))$' -or
        $leaf -match '^(?i:__editable__.*)$' -or
        $leaf -match '(?i:\.egg-link)$' -or
        $leaf -match '^(?i:(auth|auth[-_]?token|credentials?|cookies?|session|session[-_]?token|tokens?|access[_-]?token|refresh[_-]?token|csrf[-_]?token|profiles?|caches?)\.(json|toml|sqlite3?|db|bin|dat|txt))$' -or
        $leaf -match '^(?i:(id_(rsa|ed25519|ecdsa|dsa)|private[_-]?key)\.(key|json|bin|dat)|id_(rsa|ed25519|ecdsa|dsa)|.*\.key)$'
}

function Get-RelativePathFromRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    if (-not (Test-PathWithin -Path $pathFull -Root $rootFull) -or
        $pathFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside its prepared root: $Path"
    }

    return $pathFull.Substring($rootFull.Length).TrimStart([char[]]@('\', '/')).Replace('\', '/')
}

function Get-SafeResourceFilesUnderRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    Assert-ExistingDirectory -Path $Root -Label 'prepared resources directory'
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $files = New-Object 'System.Collections.Generic.List[object]'
    $stack.Push($rootFull)
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        $currentItem = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Prepared resources tree must not contain a junction or symlink: $current"
        }

        foreach ($child in @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Prepared resources tree must not contain a junction or symlink: $($child.FullName)"
            }
            $relative = Normalize-ResourceRelativePath -Path (Get-RelativePathFromRoot -Root $rootFull -Path $child.FullName) -Label 'prepared resources tree'
            if (Test-ForbiddenResourcePath -RelativePath $relative) {
                throw "Prepared resources tree contains a forbidden payload path: $relative"
            }
            if ($child.PSIsContainer) {
                $stack.Push($child.FullName)
            }
            else {
                [void]$files.Add([pscustomobject]@{
                    Item = $child
                    RelativePath = $relative
                })
            }
        }
    }

    return @($files.ToArray())
}

function Get-RequiredStringProperty {
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Object,

        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $property.Value -isnot [string] -or
        [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        throw "$Label must contain a non-empty string property '$Name'."
    }

    return [string]$property.Value
}

function Normalize-Sha256 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if ($Value -notmatch '^[0-9A-Fa-f]{64}$') {
        throw "$Label must be a 64-character SHA-256 value."
    }

    return $Value.ToUpperInvariant()
}

function Assert-NoMachineSourcePath {
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Object,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    foreach ($property in @($Object.PSObject.Properties)) {
        $value = $property.Value
        if ($value -is [string]) {
            if ($value -match '^[A-Za-z]:[\\/]' -or $value.StartsWith('\\\\') -or
                $value.StartsWith('/')) {
                throw "$Label contains a machine source path in '$($property.Name)'."
            }
        }
        elseif ($value -is [psobject] -and $value -isnot [System.Array]) {
            Assert-NoMachineSourcePath -Object $value -Label $Label
        }
        elseif ($value -is [System.Array]) {
            foreach ($item in $value) {
                if ($item -is [string]) {
                    if ($item -match '^[A-Za-z]:[\\/]' -or $item.StartsWith('\\\\') -or
                        $item.StartsWith('/')) {
                        throw "$Label contains a machine source path in '$($property.Name)'."
                    }
                }
                elseif ($item -is [psobject]) {
                    Assert-NoMachineSourcePath -Object $item -Label $Label
                }
            }
        }
    }
}

function Read-JsonObject {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    Assert-ExistingFile -Path $Path -Label $Label
    try {
        $json = [System.IO.File]::ReadAllText($Path) | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "$Label is not valid JSON: $($_.Exception.Message)"
    }
    if ($null -eq $json -or $json -is [System.Array]) {
        throw "$Label must contain a JSON object."
    }
    return $json
}

function Read-ValidatedResourceManifest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ResourcesRoot,

        [Parameter(Mandatory = $true)]
        [string]$ManifestPath,

        [Parameter()]
        [AllowEmptyString()]
        [string]$ExpectedManifestHash
    )

    $resourceFiles = @(Get-SafeResourceFilesUnderRoot -Root $ResourcesRoot)
    $resourcesFull = [System.IO.Path]::GetFullPath($ResourcesRoot).TrimEnd([char[]]@('\', '/'))
    $manifestFull = [System.IO.Path]::GetFullPath($ManifestPath)
    if (Test-PathWithin -Path $manifestFull -Root $resourcesFull) {
        throw 'Approved resources manifest must be outside the prepared resources directory.'
    }
    if (Test-ForbiddenResourcePath -RelativePath ([System.IO.Path]::GetFileName($manifestFull))) {
        throw 'Approved resources manifest uses a forbidden payload filename.'
    }
    Assert-ExistingFile -Path $manifestFull -Label 'approved resources manifest'
    $manifestHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $manifestFull).Hash.ToUpperInvariant()
    if (-not [string]::IsNullOrWhiteSpace($ExpectedManifestHash) -and
        $manifestHash -ne (Normalize-Sha256 -Value $ExpectedManifestHash -Label 'ResourcesManifestSha256')) {
        throw 'Approved resources manifest SHA-256 does not match ResourcesManifestSha256.'
    }

    $manifest = Read-JsonObject -Path $manifestFull -Label 'approved resources manifest'
    $format = Get-RequiredStringProperty -Object $manifest -Name 'format' -Label 'approved resources manifest'
    if ($format -notin @('kontur-full-local-resources-v1', 'kontur-full-local-resources-v2',
            'kontur-local-resources-v1', 'kontur-local-resources-v2')) {
        throw "Unsupported approved resources manifest format: $format"
    }
    if ((Get-RequiredStringProperty -Object $manifest -Name 'productName' -Label 'approved resources manifest') -ne $displayName -or
        (Get-RequiredStringProperty -Object $manifest -Name 'version' -Label 'approved resources manifest') -ne '0.1.0' -or
        (Get-RequiredStringProperty -Object $manifest -Name 'identifier' -Label 'approved resources manifest') -ne 'app.kontur.offline.desktop') {
        throw 'Approved resources manifest identity does not match the production application.'
    }
    Assert-NoMachineSourcePath -Object $manifest -Label 'approved resources manifest'

    $rootProperty = $manifest.PSObject.Properties['root']
    if ($null -ne $rootProperty -and ($rootProperty.Value -isnot [string] -or
            $rootProperty.Value -ne 'resources')) {
        throw "Approved resources manifest root must be 'resources'."
    }
    $backendProperty = $manifest.PSObject.Properties['backend']
    if ($null -ne $backendProperty) {
        if ($backendProperty.Value -is [System.Array] -or $null -eq $backendProperty.Value) {
            throw 'Approved resources manifest backend metadata must be an object.'
        }
        $backendEntrypoint = Get-RequiredStringProperty -Object $backendProperty.Value -Name 'entrypoint' -Label 'approved resources manifest backend'
        if ($backendEntrypoint -notin @('resources/backend/offline_bridge.py', 'backend/offline_bridge.py')) {
            throw 'Approved resources manifest backend entrypoint is invalid.'
        }
        $backendStatus = $backendProperty.Value.PSObject.Properties['status']
        if ($null -ne $backendStatus -and $backendStatus.Value -ne 'prepared') {
            throw 'Approved resources manifest backend is not marked prepared.'
        }
    }
    $pythonProperty = $manifest.PSObject.Properties['python']
    if ($null -ne $pythonProperty) {
        if ($pythonProperty.Value -is [System.Array] -or $null -eq $pythonProperty.Value) {
            throw 'Approved resources manifest Python metadata must be an object.'
        }
        if ((Get-RequiredStringProperty -Object $pythonProperty.Value -Name 'implementation' -Label 'approved resources manifest Python') -ne 'CPython' -or
            (Get-RequiredStringProperty -Object $pythonProperty.Value -Name 'invocation' -Label 'approved resources manifest Python') -ne 'python.exe -I -B -u') {
            throw 'Approved resources manifest Python identity is invalid.'
        }
    }
    $filesProperty = $manifest.PSObject.Properties['files']
    if ($null -eq $filesProperty -or $filesProperty.Value -isnot [System.Array]) {
        throw 'Approved resources manifest must contain a files array.'
    }

    $manifestFiles = @($filesProperty.Value)
    if ($manifestFiles.Count -eq 0) { throw 'Approved resources manifest has no file entries.' }
    $manifestByPath = @{}
    foreach ($entry in $manifestFiles) {
        $pathProperty = $entry.PSObject.Properties['path']
        $hashProperty = $entry.PSObject.Properties['sha256']
        $sizeProperty = $entry.PSObject.Properties['size']
        if ($null -eq $pathProperty -or $pathProperty.Value -isnot [string] -or
            $null -eq $hashProperty -or $hashProperty.Value -isnot [string] -or
            $null -eq $sizeProperty) {
            throw 'Approved resources manifest file entries must contain path, sha256, and size.'
        }
        $rawPath = Normalize-ResourceRelativePath -Path ([string]$pathProperty.Value) -Label 'approved resources manifest file'
        if ($rawPath.StartsWith('resources/', [System.StringComparison]::OrdinalIgnoreCase)) {
            $relative = $rawPath.Substring(10)
        }
        else {
            $relative = $rawPath
        }
        $relative = Normalize-ResourceRelativePath -Path $relative -Label 'approved resources manifest file'
        if (Test-ForbiddenResourcePath -RelativePath $relative) {
            throw "Approved resources manifest contains a forbidden payload path: $relative"
        }
        $key = $relative.ToLowerInvariant()
        if ($manifestByPath.ContainsKey($key)) {
            throw "Approved resources manifest contains duplicate file path: $relative"
        }
        $hash = Normalize-Sha256 -Value ([string]$hashProperty.Value) -Label "approved resources hash for $relative"
        [long]$size = 0
        if (-not [long]::TryParse([string]$sizeProperty.Value,
                [Globalization.NumberStyles]::Integer,
                [Globalization.CultureInfo]::InvariantCulture,
                [ref]$size) -or $size -lt 0) {
            throw "Approved resources size is invalid: $relative"
        }
        $manifestByPath[$key] = [pscustomobject]@{
            RelativePath = $relative
            Hash = $hash
            Size = $size
        }
    }

    $actualByPath = @{}
    foreach ($file in $resourceFiles) {
        $actualByPath[$file.RelativePath.ToLowerInvariant()] = $file
    }
    foreach ($key in $manifestByPath.Keys) {
        if (-not $actualByPath.ContainsKey($key)) {
            throw "Approved resources manifest file is missing from the prepared tree: $($manifestByPath[$key].RelativePath)"
        }
    }
    foreach ($key in $actualByPath.Keys) {
        if (-not $manifestByPath.ContainsKey($key)) {
            throw "Prepared resources tree contains an unmanifested file: $($actualByPath[$key].RelativePath)"
        }
    }
    if ($manifestByPath.Count -ne $actualByPath.Count) {
        throw 'Prepared resources tree file set differs from the approved resources manifest.'
    }

    foreach ($required in @('python/python.exe', 'backend/offline_bridge.py', 'catalog/catalog.sqlite3')) {
        if (-not $manifestByPath.ContainsKey($required)) {
            throw "Approved resources manifest is missing required resource: $required"
        }
    }

    $validatedEntries = New-Object 'System.Collections.Generic.List[object]'
    foreach ($key in ($manifestByPath.Keys | Sort-Object)) {
        $entry = $manifestByPath[$key]
        $sourcePath = Join-Path $resourcesFull ($entry.RelativePath.Replace('/', '\'))
        Assert-ExistingFile -Path $sourcePath -Label "approved resource $($entry.RelativePath)"
        $sourceInfo = Get-Item -LiteralPath $sourcePath -Force -ErrorAction Stop
        if ($sourceInfo.Length -ne $entry.Size) {
            throw "Approved resource size does not match: $($entry.RelativePath)"
        }
        $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourcePath).Hash.ToUpperInvariant()
        if ($actualHash -ne $entry.Hash) {
            throw "Approved resource SHA-256 does not match: $($entry.RelativePath)"
        }
        [void]$validatedEntries.Add([pscustomobject]@{
            RelativePath = $entry.RelativePath
            SourcePath = $sourcePath
            Hash = $actualHash
            Size = [long]$entry.Size
        })
    }

    return [pscustomobject]@{
        Manifest = $manifest
        ManifestPath = $manifestFull
        ManifestHash = $manifestHash
        ResourcesRoot = $resourcesFull
        Entries = @($validatedEntries.ToArray())
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

$artifactsRoot = Join-Path $projectRootFull 'artifacts'
if (-not (Test-Path -LiteralPath $artifactsRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $artifactsRoot -Force | Out-Null
}
else {
    Assert-ExistingDirectory -Path $artifactsRoot -Label 'artifacts directory'
}

$executableFull = Resolve-AbsolutePath -Path $ExecutablePath -BasePath $projectRootFull
if (Test-PathWithin -Path $executableFull -Root $legacySourceRoot) {
    throw "ExecutablePath must not point into the read-only legacy source tree: $legacySourceRoot"
}
Assert-ExistingFile -Path $executableFull -Label 'kontur-local executable'
if ([System.IO.Path]::GetExtension($executableFull).ToLowerInvariant() -ne '.exe') {
    throw "ExecutablePath must point to a .exe file: $executableFull"
}

if ([string]::IsNullOrWhiteSpace($IconPath)) {
    $IconPath = 'native\icons\icon.ico'
}
$iconFull = Resolve-AbsolutePath -Path $IconPath -BasePath $projectRootFull
if (Test-PathWithin -Path $iconFull -Root $legacySourceRoot) {
    throw "IconPath must not point into the read-only legacy source tree: $legacySourceRoot"
}
Assert-ExistingFile -Path $iconFull -Label 'approved icon'
if ([System.IO.Path]::GetExtension($iconFull).ToLowerInvariant() -ne '.ico') {
    throw "IconPath must point to an .ico file: $iconFull"
}

$hasResourcesDirectory = -not [string]::IsNullOrWhiteSpace($ResourcesDirectory)
$hasResourcesManifest = -not [string]::IsNullOrWhiteSpace($ResourcesManifestPath)
if ($hasResourcesDirectory -ne $hasResourcesManifest) {
    throw 'ResourcesDirectory and ResourcesManifestPath must be supplied together for a full-resource package.'
}
if ($hasResourcesDirectory -and [string]::IsNullOrWhiteSpace($ResourcesManifestSha256)) {
    throw 'ResourcesManifestSha256 is required when creating a v2 full-resource package.'
}
$resourceValidation = $null
$resourceManifestPackageName = $null
if ($hasResourcesDirectory) {
    $resourcesInputFull = Resolve-AbsolutePath -Path $ResourcesDirectory -BasePath $projectRootFull
    if ((Test-PathWithin -Path $resourcesInputFull -Root $legacySourceRoot) -or
        (-not (Test-PathWithin -Path $resourcesInputFull -Root $projectRootFull))) {
        throw 'ResourcesDirectory must be a prepared tree inside the offline project and outside the read-only legacy source tree.'
    }
    Assert-ExistingDirectory -Path $resourcesInputFull -Label 'ResourcesDirectory'
    $directPythonRoot = Join-Path $resourcesInputFull 'python'
    $nestedResourcesRoot = Join-Path $resourcesInputFull 'resources'
    if (Test-Path -LiteralPath $directPythonRoot -PathType Container) {
        $resourcesRootFull = $resourcesInputFull
    }
    elseif (Test-Path -LiteralPath (Join-Path $nestedResourcesRoot 'python') -PathType Container) {
        $resourcesRootFull = [System.IO.Path]::GetFullPath($nestedResourcesRoot)
    }
    else {
        throw 'ResourcesDirectory must contain resources\python, resources\backend, and resources\catalog trees.'
    }
    Assert-ExistingDirectory -Path $resourcesRootFull -Label 'prepared resources root'
    $resourcesManifestFull = Resolve-AbsolutePath -Path $ResourcesManifestPath -BasePath $projectRootFull
    if ((Test-PathWithin -Path $resourcesManifestFull -Root $legacySourceRoot) -or
        (-not (Test-PathWithin -Path $resourcesManifestFull -Root $projectRootFull))) {
        throw 'ResourcesManifestPath must be inside the offline project and outside the read-only legacy source tree.'
    }
    $resourceValidation = Read-ValidatedResourceManifest -ResourcesRoot $resourcesRootFull `
        -ManifestPath $resourcesManifestFull -ExpectedManifestHash $ResourcesManifestSha256
    $resourceManifestPackageName = [System.IO.Path]::GetFileName($resourceValidation.ManifestPath)
    if ($resourceManifestPackageName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*\.json$' -or
        $resourceManifestPackageName -ieq 'package-manifest.json') {
        throw 'ResourcesManifestPath must name a non-package JSON manifest file.'
    }
}

if ([string]::IsNullOrWhiteSpace($Destination)) {
    if ($null -eq $resourceValidation) {
        $Destination = 'artifacts\portable\kontur-local-v0.1.0'
    }
    else {
        $Destination = 'artifacts\portable\kontur-local-full-v0.1.0'
    }
}
$destinationFull = Resolve-AbsolutePath -Path $Destination -BasePath $projectRootFull
$portableRoot = Join-Path $artifactsRoot 'portable'
if (-not (Test-PathWithin -Path $destinationFull -Root $artifactsRoot) -or
    $destinationFull.Equals($artifactsRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination must be a new child directory under the offline artifacts directory: $destinationFull"
}
if (Test-Path -LiteralPath $destinationFull) {
    throw "Destination already exists; refusing to overwrite it: $destinationFull"
}
if ($destinationFull.Equals($portableRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination must be a package directory below artifacts\portable, not the parent itself: $destinationFull"
}

if (-not (Test-Path -LiteralPath $portableRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $portableRoot -Force | Out-Null
}
else {
    Assert-ExistingDirectory -Path $portableRoot -Label 'portable artifacts directory'
}

$stagingRoot = Join-Path $artifactsRoot ('.portable-staging-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
$stagingRetained = $true
try {
    Copy-Item -LiteralPath $executableFull -Destination (Join-Path $stagingRoot 'kontur-local.exe')
    Copy-Item -LiteralPath $iconFull -Destination (Join-Path $stagingRoot 'icon.ico')

    if ($null -ne $resourceValidation) {
        $stagedResourcesRoot = Join-Path $stagingRoot 'resources'
        New-Item -ItemType Directory -Path $stagedResourcesRoot -Force | Out-Null
        foreach ($resourceEntry in @($resourceValidation.Entries)) {
            $destinationPath = Join-Path $stagedResourcesRoot ($resourceEntry.RelativePath.Replace('/', '\'))
            $destinationParent = Split-Path -Parent $destinationPath
            if (-not (Test-Path -LiteralPath $destinationParent -PathType Container)) {
                New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
            }
            Copy-Item -LiteralPath $resourceEntry.SourcePath -Destination $destinationPath
        }
        Copy-Item -LiteralPath $resourceValidation.ManifestPath -Destination (
            Join-Path $stagingRoot $resourceManifestPackageName)
    }

    $launcherContent = @'
@echo off
setlocal
set "APP=%~dp0kontur-local.exe"
if not exist "%APP%" (
  echo kontur-local.exe was not found beside this launcher.
  exit /b 2
)
pushd "%~dp0"
start "Kontur Local" "%APP%"
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
'@
    Write-Utf8NoBom -Path (Join-Path $stagingRoot 'package-launch-kontur-local.cmd') -Content $launcherContent

    $shortcutContent = @'
[CmdletBinding()]
param(
    [Parameter()]
    [string]$ShortcutPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$displayName = -join @(
    [char]0x041A; [char]0x043E; [char]0x043D; [char]0x0442; [char]0x0443; [char]0x0440
    [char]0x0020
    [char]0x041B; [char]0x043E; [char]0x043A; [char]0x0430; [char]0x043B; [char]0x044C
    [char]0x043D; [char]0x044B; [char]0x0439
)

$packageRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$executablePath = Join-Path $packageRoot 'kontur-local.exe'
$iconPath = Join-Path $packageRoot 'icon.ico'
if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $iconPath -PathType Leaf)) {
    throw 'The portable package is incomplete; keep this helper beside kontur-local.exe and icon.ico.'
}

$desktopRoot = [Environment]::GetFolderPath('Desktop')
if ([string]::IsNullOrWhiteSpace($desktopRoot)) {
    throw 'The current user Desktop folder could not be resolved.'
}
$desktopRoot = [System.IO.Path]::GetFullPath($desktopRoot)
$desktopItem = Get-Item -LiteralPath $desktopRoot -Force -ErrorAction Stop
if (($desktopItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'The current user Desktop directory must not be a junction or symlink.'
}
if ([string]::IsNullOrWhiteSpace($ShortcutPath)) {
    $ShortcutPath = Join-Path $desktopRoot ($displayName + '.lnk')
}
elseif (-not [System.IO.Path]::IsPathRooted($ShortcutPath)) {
    $ShortcutPath = Join-Path $desktopRoot $ShortcutPath
}
else {
    $ShortcutPath = [System.IO.Path]::GetFullPath($ShortcutPath)
}
$shortcutParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $ShortcutPath))
if (-not $shortcutParent.Equals($desktopRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'ShortcutPath must be a direct child of the current user Desktop directory.'
}
if ([System.IO.Path]::GetExtension($ShortcutPath).ToLowerInvariant() -ne '.lnk') {
    throw 'ShortcutPath must use the .lnk extension.'
}
$existingShortcut = Get-Item -LiteralPath $ShortcutPath -Force -ErrorAction SilentlyContinue
if ($null -ne $existingShortcut) {
    throw "Refusing to overwrite an existing shortcut: $ShortcutPath"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $executablePath
$shortcut.WorkingDirectory = $packageRoot
$shortcut.IconLocation = "$iconPath,0"
$shortcut.Description = $displayName
$shortcut.Save()
Write-Output "Created per-user shortcut: $ShortcutPath"
'@
    Write-Utf8NoBom -Path (Join-Path $stagingRoot 'package-install-local-shortcut.ps1') -Content $shortcutContent

    $executableHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $stagingRoot 'kontur-local.exe')).Hash
    $iconHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $stagingRoot 'icon.ico')).Hash
    $metadata = if ($null -eq $resourceValidation) {
        [ordered]@{
            format = 'kontur-local-portable-v1'
            productName = $displayName
            version = '0.1.0'
            identifier = 'app.kontur.offline.desktop'
            executable = 'kontur-local.exe'
            icon = 'icon.ico'
            launcher = 'package-launch-kontur-local.cmd'
            shortcutHelper = 'package-install-local-shortcut.ps1'
            sha256 = [ordered]@{
                executable = $executableHash
                icon = $iconHash
            }
        }
    }
    else {
        [ordered]@{
            format = 'kontur-local-portable-v2'
            productName = $displayName
            version = '0.1.0'
            identifier = 'app.kontur.offline.desktop'
            executable = 'kontur-local.exe'
            icon = 'icon.ico'
            launcher = 'package-launch-kontur-local.cmd'
            shortcutHelper = 'package-install-local-shortcut.ps1'
            resourcesDirectory = 'resources'
            resourcesManifest = $resourceManifestPackageName
            resourcesManifestSha256 = $resourceValidation.ManifestHash
            sha256 = [ordered]@{
                executable = $executableHash
                icon = $iconHash
            }
        }
    }
    $metadataJson = $metadata | ConvertTo-Json -Depth 4
    Write-Utf8NoBom -Path (Join-Path $stagingRoot 'package-manifest.json') -Content $metadataJson

    $destinationParent = Split-Path -Parent $destinationFull
    if (-not (Test-Path -LiteralPath $destinationParent -PathType Container)) {
        Assert-NearestExistingDirectory -Path $destinationParent -Label 'package destination'
        New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
    }
    else {
        Assert-ExistingDirectory -Path $destinationParent -Label 'package destination parent'
    }
    Move-Item -LiteralPath $stagingRoot -Destination $destinationFull
    $stagingRetained = $false
}
finally {
    if ($stagingRetained -and (Test-Path -LiteralPath $stagingRoot)) {
        Write-Warning "Packaging stopped; partial staging was retained for inspection: $stagingRoot"
    }
}

Write-Output "Portable package: $destinationFull"
Write-Output "Executable SHA256: $executableHash"
Write-Output "Icon SHA256: $iconHash"
if ($null -ne $resourceValidation) {
    Write-Output "Resources manifest SHA256: $($resourceValidation.ManifestHash)"
    Write-Output "Resource files: $($resourceValidation.Entries.Count)"
}

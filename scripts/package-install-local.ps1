[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$PackageDirectory,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9A-Fa-f]{64}$')]
    [string]$ExpectedExecutableSha256,

    [Parameter()]
    [Alias('ExpectedResourcesManifestHash')]
    [ValidatePattern('^[0-9A-Fa-f]{64}$')]
    [string]$ExpectedResourcesManifestSha256
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$displayName = -join @(
    [char]0x041A; [char]0x043E; [char]0x043D; [char]0x0442; [char]0x0443; [char]0x0440
    [char]0x0020
    [char]0x041B; [char]0x043E; [char]0x043A; [char]0x0430; [char]0x043B; [char]0x044C
    [char]0x043D; [char]0x044B; [char]0x0439
)

$productIdentifier = 'app.kontur.offline.desktop'
$productVersion = '0.1.0'
$installMarkerName = 'install-marker.json'

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

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
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

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
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
        if ([string]::IsNullOrWhiteSpace($parent) -or
            $parent.Equals($candidate, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Could not resolve an existing parent for ${Label}: $Path"
        }
        $candidate = $parent
    }

    Assert-ExistingDirectory -Path $candidate -Label "$Label ancestor"
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

function Read-JsonObject {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    Assert-ExistingFile -Path $Path -Label $Label
    try {
        $json = [System.IO.File]::ReadAllText($Path) | ConvertFrom-Json
    }
    catch {
        throw "$Label is not valid JSON: $($_.Exception.Message)"
    }

    if ($null -eq $json -or $json -is [System.Array]) {
        throw "$Label must contain a JSON object."
    }

    return $json
}

function Assert-V2ResourceManifestPinAdmission {
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Manifest,

        [Parameter()]
        [AllowEmptyString()]
        [string]$ExpectedResourceManifestHash
    )

    $format = Get-RequiredStringProperty -Object $Manifest -Name 'format' -Label 'portable package manifest'
    if ($format -eq 'kontur-local-portable-v2' -and
        [string]::IsNullOrWhiteSpace($ExpectedResourceManifestHash)) {
        throw 'ExpectedResourcesManifestSha256 is required when admitting a v2 full-resource package.'
    }
}

function Assert-ExactJsonPropertySet {
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Object,

        [Parameter(Mandatory = $true)]
        [string[]]$ExpectedNames,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $actualNames = @($Object.PSObject.Properties.Name)
    $missing = @($ExpectedNames | Where-Object { $_ -notin $actualNames })
    $unexpected = @($actualNames | Where-Object { $_ -notin $ExpectedNames })
    if ($missing.Count -gt 0 -or $unexpected.Count -gt 0 -or
        $actualNames.Count -ne $ExpectedNames.Count) {
        throw "$Label has an unexpected property set. Missing: $($missing -join ', '); unexpected: $($unexpected -join ', ')."
    }
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
        throw "Path is outside its owned root: $Path"
    }
    return $pathFull.Substring($rootFull.Length).TrimStart([char[]]@('\', '/')).Replace('\', '/')
}

function Get-SafeFilesUnderRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    Assert-ExistingDirectory -Path $Root -Label $Label
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $files = New-Object 'System.Collections.Generic.List[object]'
    $stack.Push($rootFull)
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        $currentItem = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label must not contain a junction or symlink: $current"
        }
        foreach ($child in @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label must not contain a junction or symlink: $($child.FullName)"
            }
            $relative = Normalize-ResourceRelativePath -Path (Get-RelativePathFromRoot -Root $rootFull -Path $child.FullName) -Label $Label
            if (Test-ForbiddenResourcePath -RelativePath $relative) {
                throw "$Label contains a forbidden payload path: $relative"
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

function Read-ValidatedResourceManifest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PackageRoot,

        [Parameter(Mandatory = $true)]
        [string]$ManifestPath,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedManifestHash,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $resourcesRoot = Join-Path $PackageRoot 'resources'
    $resourceFiles = @(Get-SafeFilesUnderRoot -Root $resourcesRoot -Label "$Label resources")
    $packageFull = [System.IO.Path]::GetFullPath($PackageRoot).TrimEnd([char[]]@('\', '/'))
    $resourcesFull = [System.IO.Path]::GetFullPath($resourcesRoot).TrimEnd([char[]]@('\', '/'))
    $manifestFull = [System.IO.Path]::GetFullPath($ManifestPath)
    if (-not (Test-PathWithin -Path $manifestFull -Root $packageFull) -or
        (Test-PathWithin -Path $manifestFull -Root $resourcesFull)) {
        throw "$Label path must be a direct package file outside resources/."
    }
    $manifestRelative = Get-RelativePathFromRoot -Root $packageFull -Path $manifestFull
    if ($manifestRelative.IndexOf('/') -ge 0 -or
        [System.IO.Path]::GetExtension($manifestRelative).ToLowerInvariant() -ne '.json') {
        throw "$Label path must be a direct .json package file."
    }
    if (Test-ForbiddenResourcePath -RelativePath $manifestRelative) {
        throw "$Label path uses a forbidden payload filename."
    }
    Assert-ExistingFile -Path $manifestFull -Label $Label
    $manifestHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $manifestFull).Hash.ToUpperInvariant()
    if ($manifestHash -ne (Normalize-Sha256 -Value $ExpectedManifestHash -Label "$Label SHA-256")) {
        throw "$Label SHA-256 does not match the public package manifest pin."
    }
    $manifest = Read-JsonObject -Path $manifestFull -Label $Label
    $format = Get-RequiredStringProperty -Object $manifest -Name 'format' -Label $Label
    if ($format -notin @('kontur-full-local-resources-v1', 'kontur-full-local-resources-v2',
            'kontur-local-resources-v1', 'kontur-local-resources-v2')) {
        throw "Unsupported $Label format: $format"
    }
    if ((Get-RequiredStringProperty -Object $manifest -Name 'productName' -Label $Label) -ne $displayName -or
        (Get-RequiredStringProperty -Object $manifest -Name 'version' -Label $Label) -ne $productVersion -or
        (Get-RequiredStringProperty -Object $manifest -Name 'identifier' -Label $Label) -ne $productIdentifier) {
        throw "$Label identity does not match the production application."
    }
    Assert-NoMachineSourcePath -Object $manifest -Label $Label
    $rootProperty = $manifest.PSObject.Properties['root']
    if ($null -ne $rootProperty -and ($rootProperty.Value -isnot [string] -or
            $rootProperty.Value -ne 'resources')) {
        throw "$Label root must be 'resources'."
    }
    $backendProperty = $manifest.PSObject.Properties['backend']
    if ($null -ne $backendProperty) {
        if ($backendProperty.Value -is [System.Array] -or $null -eq $backendProperty.Value) {
            throw "$Label backend metadata must be an object."
        }
        $backendEntrypoint = Get-RequiredStringProperty -Object $backendProperty.Value -Name 'entrypoint' -Label "$Label backend"
        if ($backendEntrypoint -notin @('resources/backend/offline_bridge.py', 'backend/offline_bridge.py')) {
            throw "$Label backend entrypoint is invalid."
        }
        $backendStatus = $backendProperty.Value.PSObject.Properties['status']
        if ($null -ne $backendStatus -and $backendStatus.Value -ne 'prepared') {
            throw "$Label backend is not marked prepared."
        }
    }
    $pythonProperty = $manifest.PSObject.Properties['python']
    if ($null -ne $pythonProperty) {
        if ($pythonProperty.Value -is [System.Array] -or $null -eq $pythonProperty.Value) {
            throw "$Label Python metadata must be an object."
        }
        if ((Get-RequiredStringProperty -Object $pythonProperty.Value -Name 'implementation' -Label "$Label Python") -ne 'CPython' -or
            (Get-RequiredStringProperty -Object $pythonProperty.Value -Name 'invocation' -Label "$Label Python") -ne 'python.exe -I -B -u') {
            throw "$Label Python identity is invalid."
        }
    }
    $filesProperty = $manifest.PSObject.Properties['files']
    if ($null -eq $filesProperty -or $filesProperty.Value -isnot [System.Array]) {
        throw "$Label must contain a files array."
    }
    $manifestByPath = @{}
    foreach ($entry in @($filesProperty.Value)) {
        $pathProperty = $entry.PSObject.Properties['path']
        $hashProperty = $entry.PSObject.Properties['sha256']
        $sizeProperty = $entry.PSObject.Properties['size']
        if ($null -eq $pathProperty -or $pathProperty.Value -isnot [string] -or
            $null -eq $hashProperty -or $hashProperty.Value -isnot [string] -or
            $null -eq $sizeProperty) {
            throw "$Label file entries must contain path, sha256, and size."
        }
        $rawPath = Normalize-ResourceRelativePath -Path ([string]$pathProperty.Value) -Label "$Label file"
        if ($rawPath.StartsWith('resources/', [System.StringComparison]::OrdinalIgnoreCase)) {
            $relative = $rawPath.Substring(10)
        }
        else {
            $relative = $rawPath
        }
        $relative = Normalize-ResourceRelativePath -Path $relative -Label "$Label file"
        if (Test-ForbiddenResourcePath -RelativePath $relative) {
            throw "$Label contains a forbidden payload path: $relative"
        }
        $key = $relative.ToLowerInvariant()
        if ($manifestByPath.ContainsKey($key)) { throw "$Label contains duplicate file path: $relative" }
        $hash = Normalize-Sha256 -Value ([string]$hashProperty.Value) -Label "$Label hash for $relative"
        [long]$size = 0
        if (-not [long]::TryParse([string]$sizeProperty.Value,
                [Globalization.NumberStyles]::Integer,
                [Globalization.CultureInfo]::InvariantCulture,
                [ref]$size) -or $size -lt 0) {
            throw "$Label size is invalid: $relative"
        }
        $manifestByPath[$key] = [pscustomobject]@{
            RelativePath = $relative
            Hash = $hash
            Size = $size
        }
    }
    $actualByPath = @{}
    foreach ($file in $resourceFiles) { $actualByPath[$file.RelativePath.ToLowerInvariant()] = $file }
    foreach ($key in $manifestByPath.Keys) {
        if (-not $actualByPath.ContainsKey($key)) { throw "$Label file is missing from the package: $($manifestByPath[$key].RelativePath)" }
    }
    foreach ($key in $actualByPath.Keys) {
        if (-not $manifestByPath.ContainsKey($key)) { throw "Package contains an unmanifested resource file: $($actualByPath[$key].RelativePath)" }
    }
    if ($manifestByPath.Count -ne $actualByPath.Count) { throw "$Label file set differs from the package resources tree." }
    foreach ($required in @('python/python.exe', 'backend/offline_bridge.py', 'catalog/catalog.sqlite3')) {
        if (-not $manifestByPath.ContainsKey($required)) { throw "$Label is missing required resource: $required" }
    }

    $validatedEntries = New-Object 'System.Collections.Generic.List[object]'
    foreach ($key in ($manifestByPath.Keys | Sort-Object)) {
        $entry = $manifestByPath[$key]
        $sourcePath = Join-Path $resourcesFull ($entry.RelativePath.Replace('/', '\'))
        Assert-ExistingFile -Path $sourcePath -Label "$Label resource $($entry.RelativePath)"
        $sourceInfo = Get-Item -LiteralPath $sourcePath -Force -ErrorAction Stop
        if ($sourceInfo.Length -ne $entry.Size) { throw "$Label resource size mismatch: $($entry.RelativePath)" }
        $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourcePath).Hash.ToUpperInvariant()
        if ($actualHash -ne $entry.Hash) { throw "$Label resource SHA-256 mismatch: $($entry.RelativePath)" }
        [void]$validatedEntries.Add([pscustomobject]@{
            RelativePath = $entry.RelativePath
            PackagePath = $sourcePath
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

function Read-ValidatedPackageDirectoryV2 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedHash,

        [Parameter(Mandatory = $true)]
        [string]$Label,

        [Parameter()]
        [AllowEmptyString()]
        [string]$ExpectedResourceManifestHash,

        [Parameter(Mandatory = $true)]
        [psobject]$Manifest
    )

    Assert-ExactJsonPropertySet -Object $Manifest -ExpectedNames @(
        'format', 'productName', 'version', 'identifier', 'executable', 'icon',
        'launcher', 'shortcutHelper', 'resourcesDirectory', 'resourcesManifest',
        'resourcesManifestSha256', 'sha256'
    ) -Label "$Label manifest"
    if ((Get-RequiredStringProperty -Object $Manifest -Name 'format' -Label "$Label manifest") -ne 'kontur-local-portable-v2' -or
        (Get-RequiredStringProperty -Object $Manifest -Name 'productName' -Label "$Label manifest") -ne $displayName -or
        (Get-RequiredStringProperty -Object $Manifest -Name 'version' -Label "$Label manifest") -ne $productVersion -or
        (Get-RequiredStringProperty -Object $Manifest -Name 'identifier' -Label "$Label manifest") -ne $productIdentifier -or
        (Get-RequiredStringProperty -Object $Manifest -Name 'executable' -Label "$Label manifest") -ne 'kontur-local.exe' -or
        (Get-RequiredStringProperty -Object $Manifest -Name 'icon' -Label "$Label manifest") -ne 'icon.ico' -or
        (Get-RequiredStringProperty -Object $Manifest -Name 'launcher' -Label "$Label manifest") -ne 'package-launch-kontur-local.cmd' -or
        (Get-RequiredStringProperty -Object $Manifest -Name 'shortcutHelper' -Label "$Label manifest") -ne 'package-install-local-shortcut.ps1' -or
        (Get-RequiredStringProperty -Object $Manifest -Name 'resourcesDirectory' -Label "$Label manifest") -ne 'resources') {
        throw "$Label manifest has an unsupported product identity or file mapping."
    }
    Assert-NoMachineSourcePath -Object $Manifest -Label "$Label manifest"

    $shaProperty = $Manifest.PSObject.Properties['sha256']
    if ($null -eq $shaProperty -or $shaProperty.Value -is [System.Array]) {
        throw "$Label manifest must contain a sha256 object."
    }
    $manifestExecutableHash = Normalize-Sha256 -Value (
        Get-RequiredStringProperty -Object $shaProperty.Value -Name 'executable' -Label "$Label manifest sha256") -Label "$Label manifest executable hash"
    $manifestIconHash = Normalize-Sha256 -Value (
        Get-RequiredStringProperty -Object $shaProperty.Value -Name 'icon' -Label "$Label manifest sha256") -Label "$Label manifest icon hash"
    $expectedHash = Normalize-Sha256 -Value $ExpectedHash -Label 'ExpectedExecutableSha256'
    if ($manifestExecutableHash -ne $expectedHash) {
        throw "$Label manifest executable hash does not match ExpectedExecutableSha256."
    }
    Assert-ExactJsonPropertySet -Object $shaProperty.Value -ExpectedNames @('executable', 'icon') -Label "$Label manifest sha256"

    $manifestResourceHash = Normalize-Sha256 -Value (
        Get-RequiredStringProperty -Object $Manifest -Name 'resourcesManifestSha256' -Label "$Label manifest") -Label "$Label manifest resources hash"
    if (-not [string]::IsNullOrWhiteSpace($ExpectedResourceManifestHash) -and
        $manifestResourceHash -ne (Normalize-Sha256 -Value $ExpectedResourceManifestHash -Label 'ExpectedResourcesManifestSha256')) {
        throw "$Label resources manifest hash does not match ExpectedResourcesManifestSha256."
    }
    $resourceManifestName = Get-RequiredStringProperty -Object $Manifest -Name 'resourcesManifest' -Label "$Label manifest"
    $resourceManifestRelative = Normalize-ResourceRelativePath -Path $resourceManifestName -Label "$Label resources manifest"
    if ($resourceManifestRelative.IndexOf('/') -ge 0 -or
        [System.IO.Path]::GetExtension($resourceManifestRelative).ToLowerInvariant() -ne '.json' -or
        $resourceManifestRelative -ieq 'package-manifest.json') {
        throw "$Label resources manifest must be a direct JSON package file."
    }

    $manifestPath = Join-Path $Root 'package-manifest.json'
    $executablePath = Join-Path $Root 'kontur-local.exe'
    $iconPath = Join-Path $Root 'icon.ico'
    $launcherPath = Join-Path $Root 'package-launch-kontur-local.cmd'
    $shortcutHelperPath = Join-Path $Root 'package-install-local-shortcut.ps1'
    $resourceManifestPath = Join-Path $Root $resourceManifestRelative
    Assert-ExistingFile -Path $executablePath -Label "$Label executable"
    Assert-ExistingFile -Path $iconPath -Label "$Label icon"
    Assert-ExistingFile -Path $launcherPath -Label "$Label launcher"
    Assert-ExistingFile -Path $shortcutHelperPath -Label "$Label shortcut helper"
    $actualExecutableHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $executablePath).Hash.ToUpperInvariant()
    $actualIconHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $iconPath).Hash.ToUpperInvariant()
    if ($actualExecutableHash -ne $expectedHash -or $actualExecutableHash -ne $manifestExecutableHash) {
        throw "$Label executable SHA-256 does not match the expected identity."
    }
    if ($actualIconHash -ne $manifestIconHash) { throw "$Label icon SHA-256 does not match its manifest." }

    $resourcePackage = Read-ValidatedResourceManifest -PackageRoot $Root -ManifestPath $resourceManifestPath `
        -ExpectedManifestHash $manifestResourceHash -Label "$Label resources manifest"
    $packageFiles = @(Get-SafeFilesUnderRoot -Root $Root -Label $Label)
    $expectedFiles = @{}
    foreach ($known in @('package-manifest.json', 'kontur-local.exe', 'icon.ico',
            'package-launch-kontur-local.cmd', 'package-install-local-shortcut.ps1',
            $resourceManifestRelative)) {
        $expectedFiles[$known.ToLowerInvariant()] = $known
    }
    foreach ($entry in @($resourcePackage.Entries)) {
        $expectedFiles[('resources/' + $entry.RelativePath).ToLowerInvariant()] = 'resources/' + $entry.RelativePath
    }
    foreach ($file in $packageFiles) {
        $key = $file.RelativePath.ToLowerInvariant()
        if (-not $expectedFiles.ContainsKey($key)) {
            throw "$Label contains an unexpected file: $($file.RelativePath)"
        }
    }
    foreach ($key in $expectedFiles.Keys) {
        if (-not (@($packageFiles | Where-Object { $_.RelativePath.ToLowerInvariant() -eq $key }).Count -gt 0)) {
            throw "$Label is missing an expected file: $($expectedFiles[$key])"
        }
    }
    if ($packageFiles.Count -ne $expectedFiles.Count) { throw "$Label file set is not exact." }

    return [pscustomobject]@{
        Format = 'v2'
        Root = $Root
        ManifestPath = $manifestPath
        ExecutablePath = $executablePath
        IconPath = $iconPath
        LauncherPath = $launcherPath
        ShortcutHelperPath = $shortcutHelperPath
        ResourceManifestPath = $resourceManifestPath
        ResourceManifestHash = $manifestResourceHash
        ResourceEntries = @($resourcePackage.Entries)
        ExecutableHash = $actualExecutableHash
        IconHash = $actualIconHash
    }
}

function Read-ValidatedPackageDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedHash,

        [Parameter(Mandatory = $true)]
        [string]$Label,

        [Parameter()]
        [AllowEmptyString()]
        [string]$ExpectedResourcesManifestHash
    )

    Assert-ExistingDirectory -Path $Root -Label $Label
    $packageFilesBeforeRead = @(Get-SafeFilesUnderRoot -Root $Root -Label $Label)
    $manifestPath = Join-Path $Root 'package-manifest.json'
    $executablePath = Join-Path $Root 'kontur-local.exe'
    $iconPath = Join-Path $Root 'icon.ico'
    $manifest = Read-JsonObject -Path $manifestPath -Label "$Label manifest"

    $format = Get-RequiredStringProperty -Object $manifest -Name 'format' -Label "$Label manifest"
    if ($format -eq 'kontur-local-portable-v2') {
        return Read-ValidatedPackageDirectoryV2 -Root $Root -ExpectedHash $ExpectedHash -Label $Label `
            -ExpectedResourceManifestHash $ExpectedResourcesManifestHash -Manifest $manifest
    }
    $productName = Get-RequiredStringProperty -Object $manifest -Name 'productName' -Label "$Label manifest"
    $version = Get-RequiredStringProperty -Object $manifest -Name 'version' -Label "$Label manifest"
    $identifier = Get-RequiredStringProperty -Object $manifest -Name 'identifier' -Label "$Label manifest"
    $executableName = Get-RequiredStringProperty -Object $manifest -Name 'executable' -Label "$Label manifest"
    $iconName = Get-RequiredStringProperty -Object $manifest -Name 'icon' -Label "$Label manifest"
    if ($format -ne 'kontur-local-portable-v1' -or $productName -ne $displayName -or
        $version -ne $productVersion -or $identifier -ne $productIdentifier -or
        $executableName -ne 'kontur-local.exe' -or $iconName -ne 'icon.ico') {
        throw "$Label manifest has an unsupported product identity or file mapping."
    }

    $shaProperty = $manifest.PSObject.Properties['sha256']
    if ($null -eq $shaProperty -or $shaProperty.Value -is [System.Array]) {
        throw "$Label manifest must contain a sha256 object."
    }
    $manifestExecutableHash = Normalize-Sha256 -Value (Get-RequiredStringProperty -Object $shaProperty.Value -Name 'executable' -Label "$Label manifest sha256") -Label "$Label manifest executable hash"
    $manifestIconHash = Normalize-Sha256 -Value (Get-RequiredStringProperty -Object $shaProperty.Value -Name 'icon' -Label "$Label manifest sha256") -Label "$Label manifest icon hash"
    $expectedHash = Normalize-Sha256 -Value $ExpectedHash -Label 'ExpectedExecutableSha256'
    if ($manifestExecutableHash -ne $expectedHash) {
        throw "$Label manifest executable hash does not match ExpectedExecutableSha256."
    }

    Assert-ExistingFile -Path $executablePath -Label "$Label executable"
    Assert-ExistingFile -Path $iconPath -Label "$Label icon"
    $actualExecutableHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $executablePath).Hash.ToUpperInvariant()
    $actualIconHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $iconPath).Hash.ToUpperInvariant()
    if ($actualExecutableHash -ne $expectedHash -or $actualExecutableHash -ne $manifestExecutableHash) {
        throw "$Label executable SHA-256 does not match the expected identity."
    }
    if ($actualIconHash -ne $manifestIconHash) {
        throw "$Label icon SHA-256 does not match its manifest."
    }

    return [pscustomobject]@{
        Format = 'v1'
        Root = $Root
        ManifestPath = $manifestPath
        ExecutablePath = $executablePath
        IconPath = $iconPath
        ExecutableHash = $actualExecutableHash
        IconHash = $actualIconHash
    }
}

function New-OwnedInstallMarker {
    param(
        [Parameter(Mandatory = $true)]
        [string]$InstallRoot,

        [Parameter(Mandatory = $true)]
        [string]$ExecutableHash,

        [Parameter(Mandatory = $true)]
        [string]$IconHash,

        [Parameter()]
        [AllowEmptyString()]
        [string]$ResourceManifestPath,

        [Parameter()]
        [AllowEmptyString()]
        [string]$ResourceManifestHash
    )

    $executableSha = Normalize-Sha256 -Value $ExecutableHash -Label 'marker executable hash'
    $iconSha = Normalize-Sha256 -Value $IconHash -Label 'marker icon hash'
    if ([string]::IsNullOrWhiteSpace($ResourceManifestPath)) {
        return [ordered]@{
            markerFormat = 'kontur-local-install-v1'
            productName = $displayName
            version = $productVersion
            identifier = $productIdentifier
            installRoot = $InstallRoot
            versionsDirectory = 'versions'
            currentVersionDirectory = Join-Path 'versions' $executableSha
            executable = 'kontur-local.exe'
            icon = 'icon.ico'
            packageManifest = 'package-manifest.json'
            executableSha256 = $executableSha
            iconSha256 = $iconSha
        }
    }

    $resourceSha = Normalize-Sha256 -Value $ResourceManifestHash -Label 'marker resources manifest hash'
    $resourceName = Normalize-ResourceRelativePath -Path $ResourceManifestPath -Label 'marker resources manifest'
    if ($resourceName.IndexOf('/') -ge 0 -or
        [System.IO.Path]::GetExtension($resourceName).ToLowerInvariant() -ne '.json' -or
        $resourceName -ieq 'package-manifest.json') {
        throw 'Marker resources manifest must be a direct package file.'
    }
    return [ordered]@{
        markerFormat = 'kontur-local-install-v2'
        productName = $displayName
        version = $productVersion
        identifier = $productIdentifier
        installRoot = $InstallRoot
        versionsDirectory = 'versions'
        currentVersionDirectory = Join-Path 'versions' $executableSha
        executable = 'kontur-local.exe'
        icon = 'icon.ico'
        packageManifest = 'package-manifest.json'
        resourcesDirectory = 'resources'
        resourcesManifest = $resourceName
        resourcesManifestSha256 = $resourceSha
        executableSha256 = $executableSha
        iconSha256 = $iconSha
    }
}

function Read-ValidatedOwnedMarker {
    param(
        [Parameter(Mandatory = $true)]
        [string]$MarkerPath,

        [Parameter(Mandatory = $true)]
        [string]$InstallRoot
    )

    $marker = Read-JsonObject -Path $MarkerPath -Label 'owned install marker'
    $markerFormat = Get-RequiredStringProperty -Object $marker -Name 'markerFormat' -Label 'owned install marker'
    if ($markerFormat -eq 'kontur-local-install-v2') {
        Assert-ExactJsonPropertySet -Object $marker -ExpectedNames @(
            'markerFormat', 'productName', 'version', 'identifier', 'installRoot',
            'versionsDirectory', 'currentVersionDirectory', 'executable', 'icon',
            'packageManifest', 'resourcesDirectory', 'resourcesManifest',
            'resourcesManifestSha256', 'executableSha256', 'iconSha256'
        ) -Label 'owned install marker'
        if ((Get-RequiredStringProperty -Object $marker -Name 'productName' -Label 'owned install marker') -ne $displayName -or
            (Get-RequiredStringProperty -Object $marker -Name 'version' -Label 'owned install marker') -ne $productVersion -or
            (Get-RequiredStringProperty -Object $marker -Name 'identifier' -Label 'owned install marker') -ne $productIdentifier -or
            (Get-RequiredStringProperty -Object $marker -Name 'versionsDirectory' -Label 'owned install marker') -ne 'versions' -or
            (Get-RequiredStringProperty -Object $marker -Name 'executable' -Label 'owned install marker') -ne 'kontur-local.exe' -or
            (Get-RequiredStringProperty -Object $marker -Name 'icon' -Label 'owned install marker') -ne 'icon.ico' -or
            (Get-RequiredStringProperty -Object $marker -Name 'packageManifest' -Label 'owned install marker') -ne 'package-manifest.json' -or
            (Get-RequiredStringProperty -Object $marker -Name 'resourcesDirectory' -Label 'owned install marker') -ne 'resources') {
            throw 'The existing install marker is not owned by this application version.'
        }
        $markerResourceName = Normalize-ResourceRelativePath -Path (
            Get-RequiredStringProperty -Object $marker -Name 'resourcesManifest' -Label 'owned install marker') -Label 'owned marker resources manifest'
        if ($markerResourceName.IndexOf('/') -ge 0 -or
            [System.IO.Path]::GetExtension($markerResourceName).ToLowerInvariant() -ne '.json' -or
            $markerResourceName -ieq 'package-manifest.json') {
            throw 'The existing install marker has an invalid resources manifest path.'
        }
        $markerResourceHash = Normalize-Sha256 -Value (
            Get-RequiredStringProperty -Object $marker -Name 'resourcesManifestSha256' -Label 'owned install marker') -Label 'owned marker resources manifest hash'
    }
    elseif ($markerFormat -eq 'kontur-local-install-v1') {
        Assert-ExactJsonPropertySet -Object $marker -ExpectedNames @(
            'markerFormat', 'productName', 'version', 'identifier', 'installRoot',
            'versionsDirectory', 'currentVersionDirectory', 'executable', 'icon',
            'packageManifest', 'executableSha256', 'iconSha256'
        ) -Label 'owned install marker'
        $markerResourceName = $null
        $markerResourceHash = $null
    }
    else {
        throw 'The existing install marker is not owned by this application version.'
    }

    if ($markerFormat -notin @('kontur-local-install-v1', 'kontur-local-install-v2') -or
        (Get-RequiredStringProperty -Object $marker -Name 'productName' -Label 'owned install marker') -ne $displayName -or
        (Get-RequiredStringProperty -Object $marker -Name 'version' -Label 'owned install marker') -ne $productVersion -or
        (Get-RequiredStringProperty -Object $marker -Name 'identifier' -Label 'owned install marker') -ne $productIdentifier -or
        (Get-RequiredStringProperty -Object $marker -Name 'versionsDirectory' -Label 'owned install marker') -ne 'versions' -or
        (Get-RequiredStringProperty -Object $marker -Name 'executable' -Label 'owned install marker') -ne 'kontur-local.exe' -or
        (Get-RequiredStringProperty -Object $marker -Name 'icon' -Label 'owned install marker') -ne 'icon.ico' -or
        (Get-RequiredStringProperty -Object $marker -Name 'packageManifest' -Label 'owned install marker') -ne 'package-manifest.json') {
        throw 'The existing install marker is not owned by this application version.'
    }

    $markerInstallRoot = Resolve-AbsolutePath -Path (Get-RequiredStringProperty -Object $marker -Name 'installRoot' -Label 'owned install marker') -BasePath $InstallRoot
    $normalizedInstallRoot = $InstallRoot.TrimEnd([char[]]@('\', '/'))
    if (-not $markerInstallRoot.TrimEnd([char[]]@('\', '/')).Equals($normalizedInstallRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'The existing install marker belongs to a different install root.'
    }

    $markerHash = Normalize-Sha256 -Value (Get-RequiredStringProperty -Object $marker -Name 'executableSha256' -Label 'owned install marker') -Label 'owned marker executable hash'
    $markerIconHash = Normalize-Sha256 -Value (Get-RequiredStringProperty -Object $marker -Name 'iconSha256' -Label 'owned install marker') -Label 'owned marker icon hash'
    $versionsRoot = Join-Path $InstallRoot 'versions'
    $currentVersionPath = Resolve-AbsolutePath -Path (Get-RequiredStringProperty -Object $marker -Name 'currentVersionDirectory' -Label 'owned install marker') -BasePath $InstallRoot
    if (-not (Test-PathWithin -Path $currentVersionPath -Root $versionsRoot) -or
        $currentVersionPath.TrimEnd([char[]]@('\', '/')).Equals($versionsRoot.TrimEnd([char[]]@('\', '/')), [System.StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $currentVersionPath) -ne $markerHash) {
        throw 'The existing install marker points outside its owned version directory.'
    }

    return [pscustomobject]@{
        Marker = $marker
        MarkerFormat = $markerFormat
        ExecutableHash = $markerHash
        IconHash = $markerIconHash
        ResourceManifestName = $markerResourceName
        ResourceManifestHash = $markerResourceHash
        CurrentVersionPath = $currentVersionPath
    }
}

function Write-OwnedInstallMarkerAtomically {
    param(
        [Parameter(Mandatory = $true)]
        [string]$MarkerPath,

        [Parameter(Mandatory = $true)]
        [string]$InstallRoot,

        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$Marker
    )

    if (-not (Test-PathWithin -Path $MarkerPath -Root $InstallRoot)) {
        throw "Install marker path escaped the install root: $MarkerPath"
    }
    $markerParent = Split-Path -Parent $MarkerPath
    Assert-ExistingDirectory -Path $markerParent -Label 'install marker parent'
    $temporaryPath = Join-Path $markerParent ('.install-marker.tmp-' + [Guid]::NewGuid().ToString('N') + '.json')
    if (-not (Test-PathWithin -Path $temporaryPath -Root $InstallRoot)) {
        throw 'Install marker temporary path escaped the install root.'
    }

    $encoding = New-Object System.Text.UTF8Encoding($false)
    $json = $Marker | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText($temporaryPath, $json, $encoding)
    try {
        if (Test-Path -LiteralPath $MarkerPath) {
            [void](Read-ValidatedOwnedMarker -MarkerPath $MarkerPath -InstallRoot $InstallRoot)
            [System.IO.File]::Replace($temporaryPath, $MarkerPath, [System.Management.Automation.Language.NullString]::Value, $true)
        }
        else {
            [System.IO.File]::Move($temporaryPath, $MarkerPath)
        }
    }
    catch {
        throw "Atomic install marker update failed; temporary marker was retained: $temporaryPath. $($_.Exception.Message)"
    }
}

function Set-OwnedDesktopShortcut {
    param(
        [Parameter(Mandatory = $true)]
        [string]$DesktopRoot,

        [Parameter(Mandatory = $true)]
        [string]$InstallRoot,

        [Parameter(Mandatory = $true)]
        [string]$VersionsRoot,

        [Parameter(Mandatory = $true)]
        [string]$CurrentVersionPath,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedHash
    )

    Assert-ExistingDirectory -Path $DesktopRoot -Label 'current user Desktop'
    $shortcutPath = Join-Path $DesktopRoot ($displayName + '.lnk')
    $installedExecutable = Join-Path $CurrentVersionPath 'kontur-local.exe'
    Assert-ExistingFile -Path $installedExecutable -Label 'installed executable'
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $installedExecutable).Hash.ToUpperInvariant() -ne $ExpectedHash) {
        throw 'The installed executable no longer matches the expected SHA-256.'
    }

    $shell = New-Object -ComObject WScript.Shell
    $shortcutItem = Get-Item -LiteralPath $shortcutPath -Force -ErrorAction SilentlyContinue
    if ($null -ne $shortcutItem) {
        if (($shortcutItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            ($shortcutItem.Attributes -band [System.IO.FileAttributes]::Directory) -ne 0) {
            throw 'The existing Desktop shortcut path is not a regular owned-file candidate.'
        }

        try {
            $shortcut = $shell.CreateShortcut($shortcutPath)
            $targetPath = [string]$shortcut.TargetPath
        }
        catch {
            throw "The existing Desktop shortcut could not be inspected; refusing to overwrite it: $($_.Exception.Message)"
        }
        if ([string]::IsNullOrWhiteSpace($targetPath)) {
            throw 'The existing Desktop shortcut has no target; refusing to overwrite it.'
        }
        $targetFull = Resolve-AbsolutePath -Path $targetPath -BasePath $DesktopRoot
        if (-not (Test-PathWithin -Path $targetFull -Root $InstallRoot) -or
            -not (Test-PathWithin -Path (Split-Path -Parent $targetFull) -Root $VersionsRoot) -or
            [System.IO.Path]::GetFileName($targetFull) -ne 'kontur-local.exe') {
            throw 'The existing Desktop shortcut target is outside a verified owned install version; refusing to overwrite it.'
        }
        $targetVersionPath = Split-Path -Parent $targetFull
        $targetVersionHash = Normalize-Sha256 -Value (Split-Path -Leaf $targetVersionPath) -Label 'existing shortcut version'
        [void](Read-ValidatedPackageDirectory -Root $targetVersionPath -ExpectedHash $targetVersionHash -Label 'existing shortcut target version')
    }
    else {
        $shortcut = $shell.CreateShortcut($shortcutPath)
    }

    $shortcut.TargetPath = $installedExecutable
    $shortcut.Arguments = ''
    $shortcut.WindowStyle = 1
    $shortcut.WorkingDirectory = $CurrentVersionPath
    $shortcut.IconLocation = "$(Join-Path $CurrentVersionPath 'icon.ico'),0"
    $shortcut.Description = $displayName
    $shortcut.Save()
    return $shortcutPath
}

$expectedHash = Normalize-Sha256 -Value $ExpectedExecutableSha256 -Label 'ExpectedExecutableSha256'
$packageRoot = Resolve-AbsolutePath -Path $PackageDirectory -BasePath (Get-Location).Path
Assert-ExistingDirectory -Path $packageRoot -Label 'PackageDirectory'

# Full-resource admission is explicitly two-factor: the trusted executable SHA
# and the caller-supplied approved resource-manifest SHA. Do this before any
# LocalAppData/install-root inspection or target writes. Internal validation of
# already-owned versions may rely on their public manifest/owned marker pin.
$packageAdmissionManifestPath = Join-Path $packageRoot 'package-manifest.json'
$packageAdmissionManifest = Read-JsonObject -Path $packageAdmissionManifestPath -Label 'portable package manifest'
Assert-V2ResourceManifestPinAdmission -Manifest $packageAdmissionManifest `
    -ExpectedResourceManifestHash $ExpectedResourcesManifestSha256

$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
if ([string]::IsNullOrWhiteSpace($localAppData)) {
    throw 'The current user LocalApplicationData directory could not be resolved.'
}
$localAppData = [System.IO.Path]::GetFullPath($localAppData)
Assert-ExistingDirectory -Path $localAppData -Label 'LocalApplicationData'
$programsRoot = Join-Path $localAppData 'Programs'
$installRoot = Join-Path $programsRoot 'KonturLocal'
$stateRoot = Join-Path $localAppData $productIdentifier
Assert-NotFilesystemRoot -Path $installRoot -Label 'install root'
if (Test-PathWithin -Path $packageRoot -Root $installRoot) {
    throw 'PackageDirectory must remain separate from the fixed install root; the source package is never modified.'
}

# The identity chain is explicit manifest + expected SHA-256 + measured files.
# A PE binary is not signed here, so appID cannot be cryptographically derived from its bytes.
$package = Read-ValidatedPackageDirectory -Root $packageRoot -ExpectedHash $expectedHash `
    -ExpectedResourcesManifestHash $ExpectedResourcesManifestSha256 -Label 'portable package'

$installRootExists = Test-Path -LiteralPath $installRoot
if ($installRootExists) {
    Assert-ExistingDirectory -Path $installRoot -Label 'existing install root'
    $markerPath = Join-Path $installRoot $installMarkerName
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw 'Existing install root has no valid owned install marker; refusing to clobber it.'
    }
    [void](Read-ValidatedOwnedMarker -MarkerPath $markerPath -InstallRoot $installRoot)
}
else {
    Assert-NearestExistingDirectory -Path $programsRoot -Label 'install root parent'
    if (-not (Test-Path -LiteralPath $programsRoot -PathType Container)) {
        New-Item -ItemType Directory -Path $programsRoot | Out-Null
    }
    else {
        Assert-ExistingDirectory -Path $programsRoot -Label 'install root parent'
    }
    New-Item -ItemType Directory -Path $installRoot | Out-Null
}

$markerPath = Join-Path $installRoot $installMarkerName
$marker = if ($package.Format -eq 'v2') {
    New-OwnedInstallMarker -InstallRoot $installRoot -ExecutableHash $expectedHash -IconHash $package.IconHash `
        -ResourceManifestPath ([System.IO.Path]::GetFileName($package.ResourceManifestPath)) -ResourceManifestHash $package.ResourceManifestHash
}
else {
    New-OwnedInstallMarker -InstallRoot $installRoot -ExecutableHash $expectedHash -IconHash $package.IconHash
}

$versionsRoot = Join-Path $installRoot 'versions'
if (Test-Path -LiteralPath $versionsRoot) {
    Assert-ExistingDirectory -Path $versionsRoot -Label 'install versions directory'
}
else {
    New-Item -ItemType Directory -Path $versionsRoot | Out-Null
}

$versionPath = Join-Path $versionsRoot $expectedHash
if (-not (Test-PathWithin -Path $versionPath -Root $versionsRoot)) {
    throw 'Versioned install path escaped the owned versions directory.'
}

if (Test-Path -LiteralPath $versionPath) {
    Assert-ExistingDirectory -Path $versionPath -Label 'existing matching install version'
    $existingVersion = Read-ValidatedPackageDirectory -Root $versionPath -ExpectedHash $expectedHash `
        -ExpectedResourcesManifestHash $(if ($package.Format -eq 'v2') { $package.ResourceManifestHash } else { $null }) `
        -Label 'existing matching install version'
    if ($existingVersion.IconHash -ne $package.IconHash) {
        throw 'The existing matching executable version has a different icon hash; refusing to reuse or overwrite it.'
    }
    if ($package.Format -eq 'v2' -and ($existingVersion.Format -ne 'v2' -or
            $existingVersion.ResourceManifestHash -ne $package.ResourceManifestHash -or
            [System.IO.Path]::GetFileName($existingVersion.ResourceManifestPath) -ne
            [System.IO.Path]::GetFileName($package.ResourceManifestPath))) {
        throw 'The existing matching executable version has a different resource manifest/tree; refusing to reuse or overwrite it. Resource-only updates require a new executable/release SHA.'
    }
}
else {
    $stagingPath = Join-Path $versionsRoot ('.staging-' + $expectedHash + '-' + [Guid]::NewGuid().ToString('N'))
    if (-not (Test-PathWithin -Path $stagingPath -Root $versionsRoot)) {
        throw 'Install staging path escaped the owned versions directory.'
    }
    New-Item -ItemType Directory -Path $stagingPath | Out-Null
    Copy-Item -LiteralPath $package.ExecutablePath -Destination (Join-Path $stagingPath 'kontur-local.exe')
    Copy-Item -LiteralPath $package.IconPath -Destination (Join-Path $stagingPath 'icon.ico')
    Copy-Item -LiteralPath $package.ManifestPath -Destination (Join-Path $stagingPath 'package-manifest.json')
    if ($package.Format -eq 'v2') {
        Copy-Item -LiteralPath $package.LauncherPath -Destination (Join-Path $stagingPath 'package-launch-kontur-local.cmd')
        Copy-Item -LiteralPath $package.ShortcutHelperPath -Destination (Join-Path $stagingPath 'package-install-local-shortcut.ps1')
        Copy-Item -LiteralPath $package.ResourceManifestPath -Destination (Join-Path $stagingPath ([System.IO.Path]::GetFileName($package.ResourceManifestPath)))
        $stagedResourcesRoot = Join-Path $stagingPath 'resources'
        New-Item -ItemType Directory -Path $stagedResourcesRoot -Force | Out-Null
        foreach ($resourceEntry in @($package.ResourceEntries)) {
            $stagedPath = Join-Path $stagedResourcesRoot ($resourceEntry.RelativePath.Replace('/', '\'))
            $stagedParent = Split-Path -Parent $stagedPath
            if (-not (Test-Path -LiteralPath $stagedParent -PathType Container)) {
                New-Item -ItemType Directory -Path $stagedParent -Force | Out-Null
            }
            Copy-Item -LiteralPath $resourceEntry.PackagePath -Destination $stagedPath
        }
    }
    [void](Read-ValidatedPackageDirectory -Root $stagingPath -ExpectedHash $expectedHash `
        -ExpectedResourcesManifestHash $(if ($package.Format -eq 'v2') { $package.ResourceManifestHash } else { $null }) `
        -Label 'staged install version')
    if (Test-Path -LiteralPath $versionPath) {
        throw 'The matching install version appeared during staging; refusing to overwrite it.'
    }
    Move-Item -LiteralPath $stagingPath -Destination $versionPath
    [void](Read-ValidatedPackageDirectory -Root $versionPath -ExpectedHash $expectedHash `
        -ExpectedResourcesManifestHash $(if ($package.Format -eq 'v2') { $package.ResourceManifestHash } else { $null }) `
        -Label 'installed version')
}

Write-OwnedInstallMarkerAtomically -MarkerPath $markerPath -InstallRoot $installRoot -Marker $marker
[void](Read-ValidatedOwnedMarker -MarkerPath $markerPath -InstallRoot $installRoot)
$desktopRoot = [Environment]::GetFolderPath('Desktop')
if ([string]::IsNullOrWhiteSpace($desktopRoot)) {
    throw 'The current user Desktop directory could not be resolved; install completed without changing a shortcut.'
}
$desktopRoot = [System.IO.Path]::GetFullPath($desktopRoot)
$shortcutPath = Set-OwnedDesktopShortcut -DesktopRoot $desktopRoot -InstallRoot $installRoot -VersionsRoot $versionsRoot -CurrentVersionPath $versionPath -ExpectedHash $expectedHash

Write-Output "Installed Kontur Local version ${productVersion}: $versionPath"
Write-Output "Executable SHA256: $expectedHash"
Write-Output "Desktop shortcut: $shortcutPath"
Write-Output "Preserved app state: $stateRoot"

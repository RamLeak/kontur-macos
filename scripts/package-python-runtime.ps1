[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$RuntimeRoot,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$VenvRoot,
    [Parameter()]
    [string]$BackendRoot,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Destination,
    [Parameter()]
    [string]$ProjectRoot,
    [Parameter()]
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')]
    [string]$PythonVersion = '3.12.14',
    [Parameter()]
    [switch]$Package
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$displayName = -join @(
    [char]0x041A; [char]0x043E; [char]0x043D; [char]0x0442; [char]0x0443; [char]0x0440
    [char]0x0020
    [char]0x041B; [char]0x043E; [char]0x043A; [char]0x0430; [char]0x043B; [char]0x044C
    [char]0x043D; [char]0x044B; [char]0x0439
)
$legacySourceRoot = [System.IO.Path]::GetFullPath('C:\Projects\Kontur')

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

function Assert-NearestExistingDirectory {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $candidate = [System.IO.Path]::GetFullPath($Path)
    while (-not (Test-Path -LiteralPath $candidate)) {
        $parent = Split-Path -Parent $candidate
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent.Equals($candidate, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw ('Could not resolve an existing parent for ' + $Label + ': ' + $Path)
        }
        $candidate = $parent
    }
    Assert-ExistingDirectory -Path $candidate -Label ($Label + ' ancestor')
}

function Write-Utf8NoBom {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Content)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Get-NormalizedRelativePath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    $normalized = $RelativePath.Replace('\', '/')
    if ([string]::IsNullOrWhiteSpace($normalized) -or $normalized.StartsWith('/') -or $normalized -match '^[A-Za-z]:') {
        throw ('Unsafe relative path: ' + $RelativePath)
    }
    $parts = $normalized.Split('/')
    foreach ($part in $parts) {
        if ([string]::IsNullOrWhiteSpace($part) -or $part -eq '.' -or $part -eq '..' -or
            $part.IndexOf([char]0) -ge 0 -or
            $part.IndexOfAny([char[]]@([char]0x0000, [char]0x0001, [char]0x0002, [char]0x0003)) -ge 0) {
            throw ('Unsafe relative path: ' + $RelativePath)
        }
    }
    return ($parts -join '/')
}

function Get-RelativePathFromRoot {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$Path)
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    if (-not (Test-PathWithin -Path $pathFull -Root $rootFull)) { throw ('Path is outside its source root: ' + $Path) }
    if ($pathFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) { return '' }
    return $pathFull.Substring($rootFull.Length).TrimStart([char[]]@('\', '/')).Replace('\', '/')
}

function Test-ForbiddenPackageRelativePath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    $parts = $RelativePath.Replace('\', '/').Split('/')
    foreach ($part in $parts) {
        if ($part -in @('__pycache__', '.git', '.hg', '.svn', '.venv', 'venv', 'auth-data', 'auth_data')) { return $true }
    }
    $leaf = $parts[$parts.Count - 1]
    if ($leaf -match '^(?i:\.env(?:\..*)?)$' -or
        $leaf -match '^(?i:(sitecustomize|usercustomize)\.py)$' -or
        $leaf -match '^(?i:__editable__.*)$' -or
        $leaf -match '(?i:\.egg-link)$' -or $leaf -match '(?i:\.pth)$' -or
        $leaf -match '^(?i:(auth|auth[-_]?token|credentials|cookies?|session|session[-_]?token|tokens?|access[_-]?token|refresh[_-]?token|csrf[-_]?token)\.(json|toml|sqlite3?|db|bin|dat|txt))$' -or
        $leaf -match '^(?i:direct_url\.json|pyvenv\.cfg|INSTALLER|REQUESTED)$' -or
        $leaf -match '^(?i:(id_(rsa|ed25519|ecdsa|dsa)|private[_-]?key)\.(key|json|bin|dat)|id_(rsa|ed25519|ecdsa|dsa)|.*\.key)$') { return $true }
    return $false
}

function Get-SafeFilesUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter()][AllowEmptyString()][string]$SkipRelativeRegex
    )
    $rootFull = [System.IO.Path]::GetFullPath($Root)
    Assert-ExistingDirectory -Path $rootFull -Label 'source directory'
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($rootFull)
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        $currentItem = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw ('Source directory must not contain a junction or symlink: ' + $current)
        }
        foreach ($child in @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw ('Source tree must not contain a junction or symlink: ' + $child.FullName)
            }
            $relative = Get-RelativePathFromRoot -Root $rootFull -Path $child.FullName
            if (-not [string]::IsNullOrWhiteSpace($SkipRelativeRegex) -and $relative -match $SkipRelativeRegex) { continue }
            if (Test-ForbiddenPackageRelativePath -RelativePath $relative) { continue }
            if ($child.PSIsContainer) {
                $stack.Push($child.FullName)
            }
            else { [pscustomobject]@{ Item = $child; RelativePath = $relative } }
        }
    }
}

function ConvertTo-DistributionName {
    param([Parameter(Mandatory = $true)][string]$Name)
    $trimmed = $Name.Trim()
    if ([string]::IsNullOrWhiteSpace($trimmed)) { throw 'A distribution name is empty.' }
    return ([regex]::Replace($trimmed, '[-_.]+', '-')).ToLowerInvariant()
}

function Get-MetadataHeaderLines {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][AllowEmptyString()][string[]]$Lines)
    $headers = New-Object 'System.Collections.Generic.List[string]'
    foreach ($line in $Lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { break }
        [void]$headers.Add($line)
    }
    return $headers.ToArray()
}

function Get-MetadataValues {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][AllowEmptyString()][string[]]$Lines,
        [Parameter(Mandatory = $true)][string]$Header
    )
    $prefix = $Header + ':'
    $values = New-Object 'System.Collections.Generic.List[string]'
    foreach ($line in $Lines) {
        if ($line.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            [void]$values.Add($line.Substring($prefix.Length).Trim())
        }
    }
    return $values.ToArray()
}

function ConvertTo-Requirement {
    param([Parameter(Mandatory = $true)][string]$Line)
    if ($Line -notmatch '^Requires-Dist:\s*(?<body>.+)$') { throw ('Malformed Requires-Dist line: ' + $Line) }
    $body = $matches['body'].Trim()
    $marker = ''
    $semicolon = $body.IndexOf(';')
    if ($semicolon -ge 0) {
        $marker = $body.Substring($semicolon + 1).Trim()
        $body = $body.Substring(0, $semicolon).Trim()
    }
    if ($body -notmatch '^(?<name>[A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[(?<extras>[^\]]+)\])?(?<spec>.*)$') {
        throw ('Malformed dependency declaration: ' + $Line)
    }
    $dependencyMatch = $matches.Clone()
    $extras = New-Object 'System.Collections.Generic.List[string]'
    if (-not [string]::IsNullOrWhiteSpace($dependencyMatch['extras'])) {
        foreach ($extra in $dependencyMatch['extras'].Split(',')) {
            $extraName = $extra.Trim().ToLowerInvariant()
            if ($extraName -notmatch '^[a-z0-9][a-z0-9._-]*$') { throw ('Malformed dependency extra: ' + $Line) }
            [void]$extras.Add($extraName)
        }
    }
    return [pscustomobject]@{
        Raw = $Line
        Name = $dependencyMatch['name'].Trim()
        NormalizedName = ConvertTo-DistributionName -Name $dependencyMatch['name']
        Extras = $extras.ToArray()
        Specifier = $dependencyMatch['spec'].Trim()
        Marker = $marker
    }
}

function ConvertTo-VersionParts {
    param([Parameter(Mandatory = $true)][string]$Version)
    $clean = $Version.Trim()
    if ($clean.StartsWith('v', [System.StringComparison]::OrdinalIgnoreCase)) { $clean = $clean.Substring(1) }
    if ($clean -notmatch '^[0-9]+(?:\.[0-9]+)*(?:\.\*)?$') {
        throw ('Unsupported non-numeric version for offline validation: ' + $Version)
    }
    $clean = $clean.TrimEnd('*').TrimEnd('.')
    $parts = New-Object 'System.Collections.Generic.List[int]'
    foreach ($part in $clean.Split('.')) {
        [int]$number = 0
        if (-not [int]::TryParse($part, [Globalization.NumberStyles]::None,
                [Globalization.CultureInfo]::InvariantCulture, [ref]$number)) {
            throw ('Unsupported version component: ' + $Version)
        }
        [void]$parts.Add($number)
    }
    return $parts.ToArray()
}

function Compare-OfflineVersions {
    param([Parameter(Mandatory = $true)][string]$Left, [Parameter(Mandatory = $true)][string]$Right)
    $leftParts = @(ConvertTo-VersionParts -Version $Left)
    $rightParts = @(ConvertTo-VersionParts -Version $Right)
    $count = [Math]::Max($leftParts.Count, $rightParts.Count)
    for ($index = 0; $index -lt $count; $index++) {
        $leftValue = if ($index -lt $leftParts.Count) { $leftParts[$index] } else { 0 }
        $rightValue = if ($index -lt $rightParts.Count) { $rightParts[$index] } else { 0 }
        if ($leftValue -lt $rightValue) { return -1 }
        if ($leftValue -gt $rightValue) { return 1 }
    }
    return 0
}

function Test-VersionSpecifier {
    param(
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter()][AllowEmptyString()][string]$Specifier
    )
    if ([string]::IsNullOrWhiteSpace($Specifier)) { return $true }
    foreach ($clause in $Specifier.Split(',')) {
        $piece = $clause.Trim()
        if ([string]::IsNullOrWhiteSpace($piece)) { continue }
        if ($piece -notmatch '^(?<operator>===|==|!=|~=|>=|<=|>|<)\s*(?<required>[0-9][0-9A-Za-z.*+-]*)$') {
            throw ('Unsupported version specifier: ' + $Specifier)
        }
        $operator = $matches['operator']
        $required = $matches['required']
        $hasWildcard = $required.EndsWith('*')
        if ($hasWildcard -and $operator -notin @('==', '!=')) {
            throw ('Wildcard version is only supported with == or !=: ' + $Specifier)
        }
        if ($hasWildcard) {
            $prefix = $required.TrimEnd('*')
            $isMatch = $Version.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
            if (($operator -eq '==' -and -not $isMatch) -or ($operator -eq '!=' -and $isMatch)) { return $false }
            continue
        }
        $comparison = Compare-OfflineVersions -Left $Version -Right $required
        switch ($operator) {
            '===' { if ($comparison -ne 0) { return $false } }
            '==' { if ($comparison -ne 0) { return $false } }
            '!=' { if ($comparison -eq 0) { return $false } }
            '>' { if ($comparison -le 0) { return $false } }
            '>=' { if ($comparison -lt 0) { return $false } }
            '<' { if ($comparison -ge 0) { return $false } }
            '<=' { if ($comparison -gt 0) { return $false } }
            '~=' {
                if ($comparison -lt 0) { return $false }
                $requiredParts = @(ConvertTo-VersionParts -Version $required)
                if ($requiredParts.Count -le 1) { $upper = @($requiredParts[0] + 1) }
                else { $upper = @($requiredParts[0], $requiredParts[1] + 1) }
                if ((Compare-OfflineVersions -Left $Version -Right ($upper -join '.')) -ge 0) { return $false }
            }
        }
    }
    return $true
}

function Remove-OuterMarkerParentheses {
    param([Parameter(Mandatory = $true)][string]$Expression)
    $result = $Expression.Trim()
    while ($result.StartsWith('(') -and $result.EndsWith(')')) {
        $depth = 0
        $quote = [char]0
        $encloses = $true
        for ($index = 0; $index -lt $result.Length; $index++) {
            $character = $result[$index]
            if ($quote -ne [char]0) {
                if ($character -eq $quote) { $quote = [char]0 }
                continue
            }
            if ($character -eq [char]0x0027 -or $character -eq [char]0x0022) { $quote = $character; continue }
            if ($character -eq '(') { $depth++ }
            elseif ($character -eq ')') {
                $depth--
                if ($depth -eq 0 -and $index -lt ($result.Length - 1)) { $encloses = $false; break }
                if ($depth -lt 0) { throw ('Malformed environment marker: ' + $Expression) }
            }
        }
        if (-not $encloses -or $depth -ne 0 -or $quote -ne [char]0) { break }
        $result = $result.Substring(1, $result.Length - 2).Trim()
    }
    return $result
}

function Split-TopLevelMarker {
    param(
        [Parameter(Mandatory = $true)][string]$Expression,
        [Parameter(Mandatory = $true)][ValidateSet('and', 'or')][string]$Operator
    )
    $parts = New-Object 'System.Collections.Generic.List[string]'
    $depth = 0
    $quote = [char]0
    $start = 0
    $needle = ' ' + $Operator + ' '
    for ($index = 0; $index -lt $Expression.Length; $index++) {
        $character = $Expression[$index]
        if ($quote -ne [char]0) {
            if ($character -eq $quote) { $quote = [char]0 }
            continue
        }
        if ($character -eq [char]0x0027 -or $character -eq [char]0x0022) { $quote = $character; continue }
        if ($character -eq '(') { $depth++; continue }
        if ($character -eq ')') {
            $depth--
            if ($depth -lt 0) { throw ('Malformed environment marker: ' + $Expression) }
            continue
        }
        if ($depth -eq 0 -and ($index + $needle.Length) -le $Expression.Length -and
            $Expression.Substring($index, $needle.Length).Equals($needle, [System.StringComparison]::OrdinalIgnoreCase)) {
            [void]$parts.Add($Expression.Substring($start, $index - $start).Trim())
            $start = $index + $needle.Length
            $index = $start - 1
        }
    }
    if ($quote -ne [char]0 -or $depth -ne 0) { throw ('Malformed environment marker: ' + $Expression) }
    if ($parts.Count -eq 0) { return @() }
    [void]$parts.Add($Expression.Substring($start).Trim())
    return $parts.ToArray()
}

function Test-MarkerLeaf {
    param([Parameter(Mandatory = $true)][string]$Expression, [Parameter()][string[]]$Extras = @())
    $leaf = $Expression.Trim()
    $pattern = '^(?<left>[A-Za-z_][A-Za-z0-9_]*)\s*(?<operator>==|!=|<=|>=|<|>)\s*(?<quote>["''])(?<value>.*?)\k<quote>$'
    if ($leaf -notmatch $pattern) { throw ('Unsupported environment marker: ' + $Expression) }
    $left = $matches['left'].ToLowerInvariant()
    $operator = $matches['operator']
    $value = $matches['value']
    switch ($left) {
        'extra' {
            $has = @($Extras) -contains $value.ToLowerInvariant()
            if ($operator -eq '==') { return $has }
            if ($operator -eq '!=') { return (-not $has) }
            throw ('Unsupported extra marker operator: ' + $Expression)
        }
        'sys_platform' {
            $actual = 'win32'
            if ($operator -eq '==') { return $actual -eq $value }
            if ($operator -eq '!=') { return $actual -ne $value }
        }
        'platform_system' {
            $actual = 'Windows'
            if ($operator -eq '==') { return $actual -eq $value }
            if ($operator -eq '!=') { return $actual -ne $value }
        }
        'platform_python_implementation' {
            $actual = 'CPython'
            if ($operator -eq '==') { return $actual -eq $value }
            if ($operator -eq '!=') { return $actual -ne $value }
        }
        'python_version' {
            $actual = $script:OfflinePythonVersion.Substring(0, $script:OfflinePythonVersion.LastIndexOf('.'))
            return Test-VersionSpecifier -Version $actual -Specifier ($operator + $value)
        }
        'python_full_version' { return Test-VersionSpecifier -Version $script:OfflinePythonVersion -Specifier ($operator + $value) }
        default { throw ('Unsupported environment marker variable: ' + $left) }
    }
    throw ('Unsupported environment marker operator: ' + $Expression)
}

function Test-EnvironmentMarker {
    param([Parameter()][AllowEmptyString()][string]$Expression, [Parameter()][string[]]$Extras = @())
    if ([string]::IsNullOrWhiteSpace($Expression)) { return $true }
    $trimmed = Remove-OuterMarkerParentheses -Expression $Expression
    $orParts = @(Split-TopLevelMarker -Expression $trimmed -Operator 'or')
    if ($orParts.Count -gt 1) {
        foreach ($part in $orParts) { if (Test-EnvironmentMarker -Expression $part -Extras $Extras) { return $true } }
        return $false
    }
    $andParts = @(Split-TopLevelMarker -Expression $trimmed -Operator 'and')
    if ($andParts.Count -gt 1) {
        foreach ($part in $andParts) { if (-not (Test-EnvironmentMarker -Expression $part -Extras $Extras)) { return $false } }
        return $true
    }
    return Test-MarkerLeaf -Expression $trimmed -Extras $Extras
}

function ConvertFrom-RecordLine {
    param([Parameter(Mandatory = $true)][string]$Line)
    $fields = New-Object 'System.Collections.Generic.List[string]'
    $builder = New-Object System.Text.StringBuilder
    $quoted = $false
    for ($index = 0; $index -lt $Line.Length; $index++) {
        $character = $Line[$index]
        if ($quoted) {
            if ($character -eq [char]0x0022) {
                if (($index + 1) -lt $Line.Length -and $Line[$index + 1] -eq [char]0x0022) {
                    [void]$builder.Append([char]0x0022)
                    $index++
                }
                else { $quoted = $false }
            }
            else { [void]$builder.Append($character) }
            continue
        }
        if ($character -eq [char]0x0022) { $quoted = $true }
        elseif ($character -eq [char]0x002C) {
            [void]$fields.Add($builder.ToString())
            [void]$builder.Clear()
        }
        else { [void]$builder.Append($character) }
    }
    if ($quoted) { throw ('Malformed RECORD CSV line: ' + $Line) }
    [void]$fields.Add($builder.ToString())
    if ($fields.Count -ne 3) { throw ('RECORD line must contain exactly three fields: ' + $Line) }
    return $fields.ToArray()
}

function ConvertTo-Base64UrlBytes {
    param([Parameter(Mandatory = $true)][string]$Value)
    $base64 = $Value.Replace('-', '+').Replace('_', '/')
    while (($base64.Length % 4) -ne 0) { $base64 += '=' }
    try { return [Convert]::FromBase64String($base64) }
    catch { throw ('Malformed RECORD hash: ' + $Value) }
}

function ConvertTo-LowerHex {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    $builder = New-Object System.Text.StringBuilder
    foreach ($byte in $Bytes) { [void]$builder.Append($byte.ToString('x2', [Globalization.CultureInfo]::InvariantCulture)) }
    return $builder.ToString()
}

function Get-DistributionCatalog {
    param([Parameter(Mandatory = $true)][string]$SitePackagesRoot)
    Assert-ExistingDirectory -Path $SitePackagesRoot -Label 'venv site-packages'
    $catalog = New-Object 'System.Collections.Generic.List[object]'
    $directories = @(Get-ChildItem -LiteralPath $SitePackagesRoot -Directory -Force -ErrorAction Stop |
        Where-Object { $_.Name -match '\.dist-info$' } | Sort-Object Name)
    foreach ($directory in $directories) {
        if (($directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw ('Distribution metadata must not be a junction or symlink: ' + $directory.FullName)
        }
        $metadataPath = Join-Path $directory.FullName 'METADATA'
        $wheelPath = Join-Path $directory.FullName 'WHEEL'
        $recordPath = Join-Path $directory.FullName 'RECORD'
        Assert-ExistingFile -Path $metadataPath -Label ($directory.Name + ' METADATA')
        Assert-ExistingFile -Path $wheelPath -Label ($directory.Name + ' WHEEL')
        Assert-ExistingFile -Path $recordPath -Label ($directory.Name + ' RECORD')
        $metadataLines = [System.IO.File]::ReadAllLines($metadataPath)
        $metadataHeaderLines = @(Get-MetadataHeaderLines -Lines $metadataLines)
        $wheelLines = [System.IO.File]::ReadAllLines($wheelPath)
        $nameValues = @(Get-MetadataValues -Lines $metadataHeaderLines -Header 'Name')
        $versionValues = @(Get-MetadataValues -Lines $metadataHeaderLines -Header 'Version')
        if ($nameValues.Count -ne 1 -or $versionValues.Count -ne 1) {
            throw ('Distribution metadata must contain exactly one Name and Version: ' + $directory.Name)
        }
        $requiresPythonValues = @(Get-MetadataValues -Lines $metadataHeaderLines -Header 'Requires-Python')
        $rootValues = @(Get-MetadataValues -Lines $wheelLines -Header 'Root-Is-Purelib')
        $rootIsPurelib = $null
        if ($rootValues.Count -eq 1) {
            if ($rootValues[0] -eq 'true') { $rootIsPurelib = $true }
            elseif ($rootValues[0] -eq 'false') { $rootIsPurelib = $false }
            else { throw ('Unsupported Root-Is-Purelib value: ' + $directory.Name) }
        }
        $requirements = New-Object 'System.Collections.Generic.List[object]'
        foreach ($line in $metadataHeaderLines | Where-Object { $_ -match '^Requires-Dist:' }) {
            [void]$requirements.Add((ConvertTo-Requirement -Line $line))
        }
        $licenseEvidence = New-Object 'System.Collections.Generic.List[string]'
        foreach ($line in $metadataHeaderLines) {
            if ($line -match '^(License|License-Expression|License-File):' -or
                $line -match '^Classifier:\s+License\s+::') { [void]$licenseEvidence.Add($line) }
        }
        $tags = @(Get-MetadataValues -Lines $wheelLines -Header 'Tag')
        if ($tags.Count -eq 0) { throw ('WHEEL has no Tag entries: ' + $directory.Name) }
        [void]$catalog.Add([pscustomobject]@{
            DirectoryName = $directory.Name
            DirectoryPath = $directory.FullName
            Name = $nameValues[0]
            NormalizedName = ConvertTo-DistributionName -Name $nameValues[0]
            Version = $versionValues[0]
            RequiresPython = if ($requiresPythonValues.Count -eq 1) { $requiresPythonValues[0] } else { '' }
            Requirements = $requirements.ToArray()
            MetadataLines = $metadataLines
            WheelLines = $wheelLines
            Tags = $tags
            RootIsPurelib = $rootIsPurelib
            RecordPath = $recordPath
            LicenseFilesDeclared = @(Get-MetadataValues -Lines $metadataHeaderLines -Header 'License-File')
            LicenseEvidence = $licenseEvidence.ToArray()
        })
    }
    if ($catalog.Count -eq 0) { throw ('No distribution metadata found in: ' + $SitePackagesRoot) }
    return $catalog.ToArray()
}

function Get-DistributionByName {
    param([Parameter(Mandatory = $true)][hashtable]$CatalogByName, [Parameter(Mandatory = $true)][string]$NormalizedName)
    if (-not $CatalogByName.ContainsKey($NormalizedName)) {
        throw ('Required distribution is missing from the read-only venv: ' + $NormalizedName)
    }
    $matches = @($CatalogByName[$NormalizedName].ToArray())
    if ($matches.Count -ne 1) { throw ('Distribution name is ambiguous in the read-only venv: ' + $NormalizedName) }
    return $matches[0]
}

function Get-ExtraKey {
    param([Parameter()][string[]]$Extras = @())
    return ((@($Extras) | ForEach-Object { $_.ToLowerInvariant() } | Sort-Object -Unique) -join ',')
}

function Test-RequiredWheelCompatibility {
    param([Parameter(Mandatory = $true)][pscustomobject]$Distribution, [Parameter(Mandatory = $true)][string]$RequestedPythonVersion)
    if (-not [string]::IsNullOrWhiteSpace($Distribution.RequiresPython) -and
        -not (Test-VersionSpecifier -Version $RequestedPythonVersion -Specifier $Distribution.RequiresPython)) {
        throw ($Distribution.Name + ' ' + $Distribution.Version + ' does not support Python ' + $RequestedPythonVersion +
            ' according to Requires-Python: ' + $Distribution.RequiresPython)
    }
    $pythonParts = $RequestedPythonVersion.Split('.')
    $cpTag = 'cp' + $pythonParts[0] + $pythonParts[1]
    $matching = New-Object 'System.Collections.Generic.List[string]'
    foreach ($tag in $Distribution.Tags) {
        $tagParts = $tag.Split('-')
        if ($tagParts.Count -ne 3) { throw ('Malformed wheel tag in ' + $Distribution.Name + ': ' + $tag) }
        $pythonTag = $tagParts[0]
        $abiTag = $tagParts[1]
        $platformTag = $tagParts[2]
        $isPure = $platformTag -eq 'any' -and $abiTag -eq 'none' -and
            ($pythonTag -eq 'py3' -or $pythonTag -eq 'py2.py3' -or $pythonTag -eq 'py2')
        $isNative = $platformTag -eq 'win_amd64' -and
            (($pythonTag -eq $cpTag -and $abiTag -eq $cpTag) -or
             ($pythonTag -eq $cpTag -and $abiTag -eq 'abi3') -or
             ($pythonTag -match '^cp[0-9]+$' -and $abiTag -eq 'abi3' -and
                (Compare-OfflineVersions -Left $pythonTag.Substring(2) -Right $cpTag.Substring(2)) -le 0))
        if ($isPure -or $isNative) { [void]$matching.Add($tag) }
    }
    if ($matching.Count -eq 0) {
        throw ('No compatible Windows/Python ' + $RequestedPythonVersion + ' wheel tag for ' +
            $Distribution.Name + ' ' + $Distribution.Version + ': ' + ($Distribution.Tags -join ', '))
    }
    return $matching.ToArray()
}

function Read-DistributionRecord {
    param(
        [Parameter(Mandatory = $true)][pscustomobject]$Distribution,
        [Parameter(Mandatory = $true)][string]$SitePackagesRoot,
        [Parameter(Mandatory = $true)][string]$VenvRoot
    )
    $entries = New-Object 'System.Collections.Generic.List[object]'
    foreach ($line in [System.IO.File]::ReadAllLines($Distribution.RecordPath)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $fields = @(ConvertFrom-RecordLine -Line $line)
        $rawRecordPath = $fields[0].Replace('\', '/')
        $isExternalScript = $rawRecordPath -match '^\.\./\.\./Scripts/(?<script>.+)$'
        if ($isExternalScript) {
            $scriptRoot = Join-Path $VenvRoot 'Scripts'
            Assert-ExistingDirectory -Path $scriptRoot -Label 'venv Scripts'
            $scriptRelative = Get-NormalizedRelativePath -RelativePath $matches['script']
            $sourcePath = [System.IO.Path]::GetFullPath((Join-Path $scriptRoot ($scriptRelative.Replace('/', '\'))))
            if (-not (Test-PathWithin -Path $sourcePath -Root $scriptRoot)) {
                throw ('RECORD script path escapes the venv Scripts directory: ' + $Distribution.Name + ' -> ' + $rawRecordPath)
            }
            $relative = 'external/Scripts/' + $scriptRelative
            $skipped = $true
        }
        else {
            $relative = Get-NormalizedRelativePath -RelativePath $rawRecordPath
            $sourcePath = [System.IO.Path]::GetFullPath((Join-Path $SitePackagesRoot ($relative.Replace('/', '\'))))
            if (-not (Test-PathWithin -Path $sourcePath -Root $SitePackagesRoot)) {
                throw ('RECORD path escapes site-packages: ' + $Distribution.Name + ' -> ' + $relative)
            }
            $skipped = Test-ForbiddenPackageRelativePath -RelativePath $relative
        }
        Assert-ExistingFile -Path $sourcePath -Label ($Distribution.Name + ' RECORD file')
        if ($skipped) {
            [void]$entries.Add([pscustomobject]@{
                    RelativePath = $relative
                    SourcePath = $sourcePath
                    RecordHash = ''
                    Skipped = $true
                })
            continue
        }
        $actualHash = ''
        if (-not [string]::IsNullOrWhiteSpace($fields[1])) {
            $hashParts = $fields[1].Split('=', 2)
            if ($hashParts.Count -ne 2 -or $hashParts[0].ToLowerInvariant() -notin @('sha256', 'sha384', 'sha512')) {
                throw ('Unsupported RECORD hash algorithm: ' + $Distribution.Name + ' -> ' + $relative)
            }
            $algorithm = $hashParts[0].ToUpperInvariant()
            $actualHash = (Get-FileHash -Algorithm $algorithm -LiteralPath $sourcePath).Hash.ToLowerInvariant()
            $expectedHash = ConvertTo-LowerHex -Bytes (ConvertTo-Base64UrlBytes -Value $hashParts[1])
            if ($actualHash -ne $expectedHash) { throw ('RECORD hash mismatch: ' + $Distribution.Name + ' -> ' + $relative) }
        }
        if (-not [string]::IsNullOrWhiteSpace($fields[2])) {
            [long]$expectedSize = 0
            if (-not [long]::TryParse($fields[2], [Globalization.NumberStyles]::Integer,
                    [Globalization.CultureInfo]::InvariantCulture, [ref]$expectedSize)) {
                throw ('Malformed RECORD size: ' + $Distribution.Name + ' -> ' + $relative)
            }
            if ((Get-Item -LiteralPath $sourcePath -Force).Length -ne $expectedSize) {
                throw ('RECORD size mismatch: ' + $Distribution.Name + ' -> ' + $relative)
            }
        }
        [void]$entries.Add([pscustomobject]@{
            RelativePath = $relative
            SourcePath = $sourcePath
            RecordHash = $actualHash
            Skipped = $skipped
        })
    }
    if ($entries.Count -eq 0) { throw ('RECORD is empty: ' + $Distribution.Name) }
    $declaredLicenseFiles = @()
    foreach ($license in $Distribution.LicenseFilesDeclared) {
        $licensePath = Get-NormalizedRelativePath -RelativePath $license
        $licenseCandidates = @(
            $licensePath
            $Distribution.DirectoryName + '/licenses/' + $licensePath
            $Distribution.DirectoryName + '/' + $licensePath
        ) | Sort-Object -Unique
        $licenseEntry = @($entries | Where-Object {
                $licenseCandidates -contains $_.RelativePath
            })
        if ($licenseEntry.Count -ne 1 -or $licenseEntry[0].Skipped) {
            throw ('Declared license file is not safely present in RECORD: ' + $Distribution.Name + ' -> ' + $license)
        }
        $declaredLicenseFiles += $licenseEntry[0].RelativePath
    }
    if ($declaredLicenseFiles.Count -eq 0 -and $Distribution.LicenseEvidence.Count -eq 0) {
        throw ('Distribution has no license metadata or license file declaration: ' + $Distribution.Name)
    }
    return [pscustomobject]@{
        Distribution = $Distribution
        Entries = $entries.ToArray()
        LicenseFiles = $declaredLicenseFiles
        SkippedFiles = @($entries | Where-Object { $_.Skipped } | ForEach-Object { $_.RelativePath })
    }
}

function Read-ProductionRequirements {
    param([Parameter(Mandatory = $true)][string]$ManifestPath)
    Assert-ExistingFile -Path $ManifestPath -Label 'backend production manifest'
    $lines = [System.IO.File]::ReadAllLines($ManifestPath)
    $requirements = New-Object 'System.Collections.Generic.List[object]'
    $inDependencies = $false
    $closedDependencies = $false
    foreach ($line in $lines) {
        $trimmed = $line.Trim()
        if (-not $inDependencies) {
            if ($trimmed -match '^dependencies\s*=\s*\[$') { $inDependencies = $true }
            continue
        }
        if ($trimmed -eq ']') { $closedDependencies = $true; break }
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith('#')) { continue }
        if ($trimmed -notmatch '^(?<quote>["''])(?<value>.+)\k<quote>,?$') {
            throw ('Unsupported backend dependencies syntax in: ' + $ManifestPath)
        }
        $rawRequirement = $matches['value']
        if ($rawRequirement.IndexOf('\') -ge 0) {
            throw ('Escaped backend dependency syntax is not allowed in: ' + $ManifestPath)
        }
        [void]$requirements.Add((ConvertTo-Requirement -Line ('Requires-Dist: ' + $rawRequirement)))
    }
    if (-not $inDependencies -or -not $closedDependencies -or $requirements.Count -eq 0) {
        throw ('Backend production dependencies array is missing or malformed: ' + $ManifestPath)
    }
    return $requirements.ToArray()
}

function Get-DependencyClosure {
    param(
        [Parameter(Mandatory = $true)][pscustomobject[]]$Catalog,
        [Parameter(Mandatory = $true)][string]$RequestedPythonVersion,
        [Parameter(Mandatory = $true)][pscustomobject[]]$ProductionRequirements
    )
    $catalogByName = @{}
    foreach ($distribution in $Catalog) {
        if (-not $catalogByName.ContainsKey($distribution.NormalizedName)) {
            $catalogByName[$distribution.NormalizedName] = New-Object 'System.Collections.Generic.List[object]'
        }
        [void]$catalogByName[$distribution.NormalizedName].Add($distribution)
    }
    $applicationDistribution = Get-DistributionByName -CatalogByName $catalogByName -NormalizedName (ConvertTo-DistributionName -Name 'ege-informatics-workspace-backend')
    $expectedRoots = @('fastapi', 'pydantic', 'sqlalchemy', 'uvicorn', 'openpyxl', 'xlrd', 'odfpy')
    $directRequirements = New-Object 'System.Collections.Generic.List[object]'
    foreach ($requirement in $ProductionRequirements) {
        if (Test-EnvironmentMarker -Expression $requirement.Marker -Extras @()) { [void]$directRequirements.Add($requirement) }
    }
    $actualRoots = @($directRequirements | ForEach-Object { $_.NormalizedName } | Sort-Object -Unique)
    if (($actualRoots -join ',') -ne (($expectedRoots | Sort-Object) -join ',')) {
        throw ('Backend production dependency roots changed; review is required. Found: ' + ($actualRoots -join ','))
    }
    $uvicornRequirements = @($directRequirements | Where-Object { $_.NormalizedName -eq 'uvicorn' })
    if ($uvicornRequirements.Count -ne 1 -or @($uvicornRequirements[0].Extras) -notcontains 'standard') {
        throw 'Backend must explicitly select uvicorn[standard]; optional server dependencies will not be guessed.'
    }
    $selected = @{}
    $extrasByName = @{}
    $queue = New-Object 'System.Collections.Generic.List[object]'
    $processed = @{}
    $skippedOptional = New-Object 'System.Collections.Generic.List[object]'
    foreach ($requirement in $directRequirements) {
        $distribution = Get-DistributionByName -CatalogByName $catalogByName -NormalizedName $requirement.NormalizedName
        if (-not [string]::IsNullOrWhiteSpace($requirement.Specifier) -and
            -not (Test-VersionSpecifier -Version $distribution.Version -Specifier $requirement.Specifier)) {
            throw ('Installed version does not satisfy backend dependency: ' + $requirement.Raw)
        }
        if (-not $extrasByName.ContainsKey($requirement.NormalizedName)) {
            $extrasByName[$requirement.NormalizedName] = New-Object 'System.Collections.Generic.List[string]'
        }
        foreach ($extra in $requirement.Extras) {
            if (-not @($extrasByName[$requirement.NormalizedName].ToArray()) -contains $extra) { [void]$extrasByName[$requirement.NormalizedName].Add($extra) }
        }
        [void]$queue.Add([pscustomobject]@{ Distribution = $distribution; Extras = @($extrasByName[$requirement.NormalizedName].ToArray()) })
    }
    $queueIndex = 0
    while ($queueIndex -lt $queue.Count) {
        $item = $queue[$queueIndex]
        $queueIndex++
        $distribution = $item.Distribution
        $extras = @($item.Extras)
        $processedKey = $distribution.NormalizedName + '|' + (Get-ExtraKey -Extras $extras)
        if ($processed.ContainsKey($processedKey)) { continue }
        $processed[$processedKey] = $true
        $selected[$distribution.NormalizedName] = $distribution
        [void](Test-RequiredWheelCompatibility -Distribution $distribution -RequestedPythonVersion $RequestedPythonVersion)
        foreach ($requirement in $distribution.Requirements) {
            $active = Test-EnvironmentMarker -Expression $requirement.Marker -Extras $extras
            if (-not $active) {
                if (-not [string]::IsNullOrWhiteSpace($requirement.Marker)) {
                    [void]$skippedOptional.Add([pscustomobject]@{ Distribution = $distribution.Name; Requirement = $requirement.Raw })
                }
                continue
            }
            $dependency = Get-DistributionByName -CatalogByName $catalogByName -NormalizedName $requirement.NormalizedName
            if (-not [string]::IsNullOrWhiteSpace($requirement.Specifier) -and
                -not (Test-VersionSpecifier -Version $dependency.Version -Specifier $requirement.Specifier)) {
                throw ('Installed version does not satisfy dependency: ' + $requirement.Raw)
            }
            if (-not $extrasByName.ContainsKey($requirement.NormalizedName)) {
                $extrasByName[$requirement.NormalizedName] = New-Object 'System.Collections.Generic.List[string]'
            }
            $extrasChanged = $false
            foreach ($extra in $requirement.Extras) {
                if (-not @($extrasByName[$requirement.NormalizedName].ToArray()) -contains $extra) {
                    [void]$extrasByName[$requirement.NormalizedName].Add($extra)
                    $extrasChanged = $true
                }
            }
            $targetExtras = @($extrasByName[$requirement.NormalizedName].ToArray())
            if ($extrasChanged -or -not $selected.ContainsKey($requirement.NormalizedName)) {
                [void]$queue.Add([pscustomobject]@{ Distribution = $dependency; Extras = $targetExtras })
            }
        }
    }
    return [pscustomobject]@{
        Catalog = $Catalog
        ApplicationDistribution = $applicationDistribution
        DirectRequirements = $directRequirements.ToArray()
        Selected = @($selected.Values | Sort-Object NormalizedName)
        SelectedExtras = $extrasByName
        SkippedOptional = $skippedOptional.ToArray()
    }
}

function Add-PayloadFile {
    param(
        [Parameter(Mandatory = $true)][hashtable]$ManifestFiles,
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$SourceLabel
    )
    Assert-ExistingFile -Path $SourcePath -Label 'payload source'
    $relative = Get-NormalizedRelativePath -RelativePath $RelativePath
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $SourcePath).Hash.ToUpperInvariant()
    $length = [int64](Get-Item -LiteralPath $SourcePath -Force).Length
    if ($ManifestFiles.ContainsKey($relative)) {
        $existing = $ManifestFiles[$relative]
        if ($existing.sha256 -ne $hash -or $existing.size -ne $length) { throw ('Payload path collision with different content: ' + $relative) }
        return
    }
    $destinationPath = [System.IO.Path]::GetFullPath((Join-Path $StageRoot ($relative.Replace('/', '\'))))
    if (-not (Test-PathWithin -Path $destinationPath -Root $StageRoot)) { throw ('Payload destination escapes staging root: ' + $relative) }
    $destinationParent = Split-Path -Parent $destinationPath
    if (-not (Test-Path -LiteralPath $destinationParent -PathType Container)) {
        Assert-NearestExistingDirectory -Path $destinationParent -Label 'payload destination'
        New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
    }
    else { Assert-ExistingDirectory -Path $destinationParent -Label 'payload destination parent' }
    [System.IO.File]::Copy($SourcePath, $destinationPath, $false)
    $ManifestFiles[$relative] = [pscustomobject]@{ path = $relative; sha256 = $hash; size = $length; source = $SourceLabel }
}

function Copy-SafeTree {
    param(
        [Parameter(Mandatory = $true)][hashtable]$ManifestFiles,
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$DestinationPrefix,
        [Parameter(Mandatory = $true)][string]$SourceLabel,
        [Parameter()][string]$SkipRelativeRegex,
        [Parameter()][string[]]$AllowedExtensions,
        [Parameter()][switch]$SkipDisallowedExtensions
    )
    foreach ($entry in @(Get-SafeFilesUnderRoot -Root $SourceRoot -SkipRelativeRegex $SkipRelativeRegex)) {
        $relative = $entry.RelativePath
        if (-not [string]::IsNullOrWhiteSpace($SkipRelativeRegex) -and $relative -match $SkipRelativeRegex) { continue }
        if (Test-ForbiddenPackageRelativePath -RelativePath $relative) { continue }
        if ($null -ne $AllowedExtensions -and $AllowedExtensions.Count -gt 0) {
            $extension = [System.IO.Path]::GetExtension($entry.Item.Name).ToLowerInvariant()
            if ($AllowedExtensions -notcontains $extension) {
                if ($SkipDisallowedExtensions) { continue }
                throw ('Unexpected backend payload file extension; review it explicitly: ' + $relative)
            }
        }
        $destinationRelative = (Get-NormalizedRelativePath -RelativePath $DestinationPrefix) + '/' +
            (Get-NormalizedRelativePath -RelativePath $relative)
        Add-PayloadFile -ManifestFiles $ManifestFiles -StageRoot $StageRoot -SourcePath $entry.Item.FullName `
            -RelativePath $destinationRelative -SourceLabel $SourceLabel
    }
}

function Test-AllowedBackendRootFileName {
    param([Parameter(Mandatory = $true)][string]$Name)
    return ($Name -match '^(?i:offline.*\.py|local_codex\.py)$')
}

function Assert-BackendLayout {
    param([Parameter(Mandatory = $true)][string]$Root)
    Assert-ExistingDirectory -Path $Root -Label 'prepared backend root'
    Assert-ExistingDirectory -Path (Join-Path $Root 'app') -Label 'prepared backend app'
    Assert-ExistingFile -Path (Join-Path $Root 'offline_bridge.py') -Label 'prepared backend offline_bridge.py'
    $allowedRootFiles = @(Get-ChildItem -LiteralPath $Root -File -Force -ErrorAction Stop |
        Where-Object { Test-AllowedBackendRootFileName -Name $_.Name })
    foreach ($file in $allowedRootFiles) {
        if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw ('Prepared backend entrypoint must not be a junction or symlink: ' + $file.FullName)
        }
    }
    if (-not ($allowedRootFiles | Where-Object { $_.Name -ieq 'offline_bridge.py' })) {
        throw 'Prepared backend root has no allowed offline_bridge.py entrypoint.'
    }
}

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$projectRootFull = Resolve-AbsolutePath -Path $ProjectRoot -BasePath (Get-Location).Path
if (Test-PathWithin -Path $projectRootFull -Root $legacySourceRoot) {
    throw ('ProjectRoot must not be the read-only legacy source tree: ' + $legacySourceRoot)
}
Assert-ExistingDirectory -Path $projectRootFull -Label 'ProjectRoot'
$runtimeRootFull = Resolve-AbsolutePath -Path $RuntimeRoot -BasePath $projectRootFull
$venvRootFull = Resolve-AbsolutePath -Path $VenvRoot -BasePath $projectRootFull
$backendRootFull = $null
if (-not [string]::IsNullOrWhiteSpace($BackendRoot)) {
    $backendRootFull = Resolve-AbsolutePath -Path $BackendRoot -BasePath $projectRootFull
}
$destinationFull = Resolve-AbsolutePath -Path $Destination -BasePath $projectRootFull
Assert-ExistingDirectory -Path $runtimeRootFull -Label 'RuntimeRoot'
Assert-ExistingDirectory -Path $venvRootFull -Label 'VenvRoot'
if ($null -ne $backendRootFull) {
    Assert-ExistingDirectory -Path $backendRootFull -Label 'BackendRoot'
    if (Test-PathWithin -Path $backendRootFull -Root $legacySourceRoot) {
        throw ('BackendRoot must not point into the read-only legacy source tree: ' + $legacySourceRoot)
    }
    if (-not (Test-PathWithin -Path $backendRootFull -Root $projectRootFull)) {
        throw 'BackendRoot must be a prepared tree inside the offline project.'
    }
}
Assert-NotFilesystemRoot -Path $destinationFull -Label 'Destination'
$artifactsRoot = Join-Path $projectRootFull 'artifacts'
Assert-ExistingDirectory -Path $artifactsRoot -Label 'artifacts directory'
if (-not (Test-PathWithin -Path $destinationFull -Root $artifactsRoot) -or
    $destinationFull.Equals($artifactsRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw ('Destination must be a new child under the offline artifacts directory: ' + $destinationFull)
}
if (Test-Path -LiteralPath $destinationFull) { throw ('Destination already exists; refusing to overwrite it: ' + $destinationFull) }
Assert-NearestExistingDirectory -Path (Split-Path -Parent $destinationFull) -Label 'destination'

$runtimePython = Join-Path $runtimeRootFull 'python.exe'
$pythonVersionParts = $PythonVersion.Split('.')
$pythonRuntimeDllName = 'python' + $pythonVersionParts[0] + $pythonVersionParts[1] + '.dll'
Assert-ExistingFile -Path $runtimePython -Label 'runtime python.exe'
Assert-ExistingFile -Path (Join-Path $runtimeRootFull $pythonRuntimeDllName) -Label ('runtime ' + $pythonRuntimeDllName)
foreach ($requiredRuntimeFile in @('python3.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE.txt')) {
    Assert-ExistingFile -Path (Join-Path $runtimeRootFull $requiredRuntimeFile) -Label ('runtime ' + $requiredRuntimeFile)
}
$versionInfo = (Get-Item -LiteralPath $runtimePython -Force).VersionInfo
if ([string]::IsNullOrWhiteSpace($versionInfo.FileVersion) -or
    -not $versionInfo.FileVersion.StartsWith($PythonVersion, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw ('Runtime python.exe version does not match requested Python ' + $PythonVersion + ': ' + $versionInfo.FileVersion)
}
$pyvenvConfig = Join-Path $venvRootFull 'pyvenv.cfg'
Assert-ExistingFile -Path $pyvenvConfig -Label 'venv pyvenv.cfg'
$venvVersionLines = @([System.IO.File]::ReadAllLines($pyvenvConfig) | Where-Object { $_ -match '^version\s*=\s*(.+)$' })
if ($venvVersionLines.Count -ne 1 -or $venvVersionLines[0] -notmatch [regex]::Escape($PythonVersion)) {
    throw ('Venv Python version does not match requested Python ' + $PythonVersion)
}

if ($null -ne $backendRootFull) { Assert-BackendLayout -Root $backendRootFull }
$sitePackagesRoot = Join-Path $venvRootFull 'Lib\site-packages'
$catalog = @(Get-DistributionCatalog -SitePackagesRoot $sitePackagesRoot)
$script:OfflinePythonVersion = $PythonVersion
$productionManifestPath = if ($null -ne $backendRootFull) {
    Join-Path $backendRootFull 'pyproject.toml'
}
else {
    Join-Path $legacySourceRoot 'backend\pyproject.toml'
}
$productionRequirements = @(Read-ProductionRequirements -ManifestPath $productionManifestPath)
$closure = Get-DependencyClosure -Catalog $catalog -RequestedPythonVersion $PythonVersion `
    -ProductionRequirements $productionRequirements
$recordReports = @{}
foreach ($distribution in $closure.Selected) {
    $recordReports[$distribution.NormalizedName] = Read-DistributionRecord -Distribution $distribution -SitePackagesRoot $sitePackagesRoot -VenvRoot $venvRootFull
}

if (-not $Package) {
    Write-Output ('Validated CPython runtime: ' + $PythonVersion)
    Write-Output ('Validated backend roots: ' + (($closure.DirectRequirements | ForEach-Object { $_.Name }) -join ', '))
    Write-Output ('Validated Windows production closure (' + $closure.Selected.Count + ' distributions):')
    foreach ($distribution in $closure.Selected) {
        $extras = @($closure.SelectedExtras[$distribution.NormalizedName].ToArray())
        $matching = @(Test-RequiredWheelCompatibility -Distribution $distribution -RequestedPythonVersion $PythonVersion)
        $extraText = if ($extras.Count -gt 0) { $extras -join ',' } else { 'base' }
        Write-Output ('  ' + $distribution.Name + ' ' + $distribution.Version + ' [' + $extraText + '] tags=' + ($matching -join ','))
    }
    if ($closure.SkippedOptional.Count -gt 0) { Write-Output ('Skipped conditional/optional requirements: ' + $closure.SkippedOptional.Count) }
    if ($null -eq $backendRootFull) { Write-Output 'Backend packaging is deferred; no BackendRoot was supplied.' }
    else { Write-Output ('Backend source validated: ' + $backendRootFull) }
    Write-Output 'Packaging is disabled; pass -Package only after the prepared backend and destination have been reviewed.'
    return
}

$stagingRoot = Join-Path $artifactsRoot ('.full-local-staging-' + [Guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $stagingRoot) { throw ('Unexpected staging collision: ' + $stagingRoot) }
New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
$stagingRetained = $true
$manifestFiles = @{}
try {
    New-Item -ItemType Directory -Path (Join-Path $stagingRoot 'resources\python') -Force | Out-Null
    if ($null -ne $backendRootFull) {
        New-Item -ItemType Directory -Path (Join-Path $stagingRoot 'resources\backend') -Force | Out-Null
    }
    foreach ($runtimeFile in @('python.exe', 'python3.dll', $pythonRuntimeDllName, 'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE.txt')) {
        Add-PayloadFile -ManifestFiles $manifestFiles -StageRoot $stagingRoot -SourcePath (Join-Path $runtimeRootFull $runtimeFile) `
            -RelativePath ('resources/python/' + $runtimeFile) -SourceLabel 'prepared-cpython-runtime'
    }
    foreach ($runtimeDirectory in @('DLLs', 'Lib', 'tcl')) {
        $runtimeDirectoryFull = Join-Path $runtimeRootFull $runtimeDirectory
        Assert-ExistingDirectory -Path $runtimeDirectoryFull -Label ('runtime ' + $runtimeDirectory)
        $runtimeSkipRegex = '(^|/)(__pycache__)(/|$)'
        if ($runtimeDirectory -eq 'Lib') { $runtimeSkipRegex = '(^|/)(__pycache__|site-packages)(/|$)' }
        Copy-SafeTree -ManifestFiles $manifestFiles -StageRoot $stagingRoot -SourceRoot $runtimeDirectoryFull `
            -DestinationPrefix ('resources/python/' + $runtimeDirectory) -SourceLabel 'prepared-cpython-runtime' `
            -SkipRelativeRegex $runtimeSkipRegex
    }
    foreach ($distribution in $closure.Selected) {
        $report = $recordReports[$distribution.NormalizedName]
        foreach ($entry in $report.Entries) {
            if ($entry.Skipped) { continue }
            Add-PayloadFile -ManifestFiles $manifestFiles -StageRoot $stagingRoot -SourcePath $entry.SourcePath `
                -RelativePath ('resources/python/Lib/site-packages/' + $entry.RelativePath) `
                -SourceLabel ('venv-record:' + $distribution.Name + '-' + $distribution.Version)
        }
    }
    if ($null -ne $backendRootFull) {
        $backendRootFiles = @(Get-ChildItem -LiteralPath $backendRootFull -File -Force -ErrorAction Stop |
            Where-Object { Test-AllowedBackendRootFileName -Name $_.Name })
        foreach ($rootFile in $backendRootFiles) {
            Add-PayloadFile -ManifestFiles $manifestFiles -StageRoot $stagingRoot -SourcePath $rootFile.FullName `
                -RelativePath ('resources/backend/' + $rootFile.Name) -SourceLabel 'prepared-backend'
        }
        $backendAppSkipRegex = '(^|/)(__pycache__|tests|fixtures|profile)(/|$)|(^|/)\.env[^/]*(/|$)'
        Copy-SafeTree -ManifestFiles $manifestFiles -StageRoot $stagingRoot -SourceRoot (Join-Path $backendRootFull 'app') `
            -DestinationPrefix 'resources/backend/app' -SourceLabel 'prepared-backend' `
            -SkipRelativeRegex $backendAppSkipRegex -AllowedExtensions @('.py') -SkipDisallowedExtensions
    }

    $distributionManifest = New-Object 'System.Collections.Generic.List[object]'
    foreach ($distribution in $closure.Selected) {
        $report = $recordReports[$distribution.NormalizedName]
        $extras = @($closure.SelectedExtras[$distribution.NormalizedName].ToArray() | Sort-Object)
        [void]$distributionManifest.Add([ordered]@{
            name = $distribution.Name
            normalizedName = $distribution.NormalizedName
            version = $distribution.Version
            extras = $extras
            rootIsPurelib = $distribution.RootIsPurelib
            wheelTags = @($distribution.Tags)
            matchingWheelTags = @(Test-RequiredWheelCompatibility -Distribution $distribution -RequestedPythonVersion $PythonVersion)
            requiresPython = $distribution.RequiresPython
            licenseEvidence = @($distribution.LicenseEvidence)
            licenseFiles = @($report.LicenseFiles | ForEach-Object { 'resources/python/Lib/site-packages/' + $_ })
            skippedForbiddenRecordFiles = @($report.SkippedFiles)
        })
    }
    $manifest = [ordered]@{
        format = 'kontur-full-local-resources-v1'
        productName = $displayName
        version = '0.1.0'
        identifier = 'app.kontur.offline.desktop'
        python = [ordered]@{
            implementation = 'CPython'
            version = $PythonVersion
            invocation = 'python.exe -I -B -u'
            runtimeSource = 'prepared local runtime; no download/install'
        }
        backend = if ($null -ne $backendRootFull) {
            [ordered]@{
                status = 'prepared'
                source = 'prepared full/backend'
                entrypoint = 'resources/backend/offline_bridge.py'
                testsExcluded = $true
            }
        }
        else {
            [ordered]@{
                status = 'deferred'
                source = 'phase-1 full/backend not supplied'
            }
        }
        directProductionRequirements = @($closure.DirectRequirements | ForEach-Object { $_.Raw })
        skippedConditionalRequirements = @($closure.SkippedOptional | ForEach-Object {
            [ordered]@{ distribution = $_.Distribution; requirement = $_.Requirement }
        })
        distributions = @($distributionManifest.ToArray())
        files = @($manifestFiles.Values | Sort-Object path)
    }
    $manifestPath = Join-Path $stagingRoot 'full-local-resources-manifest.json'
    Write-Utf8NoBom -Path $manifestPath -Content ($manifest | ConvertTo-Json -Depth 12)
    $destinationParent = Split-Path -Parent $destinationFull
    if (-not (Test-Path -LiteralPath $destinationParent -PathType Container)) {
        Assert-NearestExistingDirectory -Path $destinationParent -Label 'destination'
        New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
    }
    else { Assert-ExistingDirectory -Path $destinationParent -Label 'destination parent' }
    if (Test-Path -LiteralPath $destinationFull) { throw ('Destination appeared during packaging; refusing to overwrite it: ' + $destinationFull) }
    Move-Item -LiteralPath $stagingRoot -Destination $destinationFull
    $stagingRetained = $false
}
finally {
    if ($stagingRetained -and (Test-Path -LiteralPath $stagingRoot)) {
        Write-Warning ('Packaging stopped; partial staging was retained for inspection: ' + $stagingRoot)
    }
}

Write-Output ('Prepared full local resources: ' + $destinationFull)
Write-Output ('Payload files hashed: ' + $manifestFiles.Count)
Write-Output ('Manifest: ' + (Join-Path $destinationFull 'full-local-resources-manifest.json'))

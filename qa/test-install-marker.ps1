[CmdletBinding()]
param([string]$SourceScript = (Join-Path $PSScriptRoot '../scripts/package-install-local.ps1'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath
$currentSource = Join-Path $repo 'scripts/package-install-local.ps1'
$oldSource = Join-Path $repo 'artifacts/package-install-before-marker-fix.ps1'
$source = (Resolve-Path -LiteralPath $SourceScript).ProviderPath
if ($source -notin @($currentSource, $oldSource)) { throw 'Only the trusted installer or frozen regression source is admitted.' }
if ($source -eq $oldSource -and (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne
    'e6cfd5c9fc96440db8bf24e8780220e21156bde32e3d8899c360a199b73bbfd9') { throw 'Frozen installer hash mismatch.' }
if ((Get-Item -LiteralPath $source).Length -gt 262144) { throw 'Installer exceeds the bounded AST fixture size.' }
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Installer AST parse failed.' }
$assignments = @($ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.AssignmentStatementAst] })
function Get-MainAssignment([string]$Name) {
    $found = @($assignments | Where-Object { $_.Left.Extent.Text -eq ('$' + $Name) })
    if ($found.Count -ne 1) { throw "Expected one real top-level assignment: $Name" }
    return $found[0]
}
# Define the actual installer functions; never dot-source or invoke its main block.
foreach ($definition in $ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.FunctionDefinitionAst] }) {
    Invoke-Expression $definition.Extent.Text
}
foreach ($name in @('displayName', 'productIdentifier', 'productVersion')) {
    Invoke-Expression (Get-MainAssignment $name).Extent.Text
}
$markerAst = Get-MainAssignment 'marker'
if ($markerAst.Right -isnot [System.Management.Automation.Language.IfStatementAst]) { throw 'Real marker assignment is no longer conditional.' }
$checks = 0
function Assert-MarkerTest([bool]$Condition, [string]$Label) {
    if (-not $Condition) { throw $Label }; $script:checks++
}
$artifacts = [System.IO.Path]::GetFullPath((Join-Path $repo 'artifacts'))
$fixture = Join-Path $artifacts ('install-marker-regression-' + [guid]::NewGuid().ToString('N'))
$packageRoot = Join-Path $fixture 'package'; $installRoot = Join-Path $fixture 'install'
$resourceName = 'full-local-resources-manifest.json'; $resourcePath = Join-Path $packageRoot $resourceName
$v2Path = Join-Path $installRoot 'marker-v2.json'; $v1Path = Join-Path $installRoot 'marker-v1.json'
$fixtureOwned = $false
try {
    [void](New-Item -ItemType Directory -Path $fixture)
    $fixtureOwned = $true
    [void](New-Item -ItemType Directory -Path $packageRoot)
    [void](New-Item -ItemType Directory -Path $installRoot)
    Set-Content -LiteralPath $resourcePath -Value '{"synthetic":true}' -Encoding utf8
    $expectedHash = 'A' * 64; $iconHash = 'C' * 64
    $resourceHash = (Get-FileHash -LiteralPath $resourcePath -Algorithm SHA256).Hash
    $package = [pscustomobject]@{ Format = 'v2'; IconHash = $iconHash; ResourceManifestPath = $resourcePath; ResourceManifestHash = $resourceHash }
    Assert-MarkerTest ([System.IO.Path]::IsPathFullyQualified($package.ResourceManifestPath)) 'Fixture manifest must be absolute.'
    # This executes the extracted main callsite, including the production v2/v1 branch.
    Invoke-Expression $markerAst.Extent.Text
    $marker | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $v2Path -Encoding utf8
    $read = Read-ValidatedOwnedMarker -MarkerPath $v2Path -InstallRoot $installRoot
    Assert-MarkerTest ($read.MarkerFormat -ceq 'kontur-local-install-v2') 'v2 marker format did not round-trip.'
    Assert-MarkerTest ($read.ResourceManifestName -ceq $resourceName) 'v2 marker must store the exact relative manifest leaf.'
    Assert-MarkerTest ($read.ResourceManifestHash -ceq $resourceHash) 'v2 manifest SHA changed.'
    Assert-MarkerTest ($read.Marker.currentVersionDirectory -ceq ('versions\' + $expectedHash)) 'v2 version directory changed.'
    Assert-MarkerTest ($read.CurrentVersionPath -ceq (Join-Path $installRoot ('versions\' + $expectedHash))) 'v2 resolved version path changed.'
    $package.Format = 'v1'
    Invoke-Expression $markerAst.Extent.Text
    $marker | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $v1Path -Encoding utf8
    $read = Read-ValidatedOwnedMarker -MarkerPath $v1Path -InstallRoot $installRoot
    Assert-MarkerTest ($read.MarkerFormat -ceq 'kontur-local-install-v1' -and $read.ExecutableHash -ceq $expectedHash) 'v1 marker compatibility failed.'
    Assert-MarkerTest ($null -eq $read.ResourceManifestName -and $null -eq $read.ResourceManifestHash) 'v1 gained v2 manifest fields.'
    foreach ($unsafe in @($resourcePath, '../escape.json', 'sub/../escape.json')) {
        $rejected = $false
        try { [void](New-OwnedInstallMarker -InstallRoot $installRoot -ExecutableHash $expectedHash -IconHash $iconHash -ResourceManifestPath $unsafe -ResourceManifestHash $resourceHash) }
        catch { $rejected = $_.Exception.Message -like '*unsafe relative path*' }
        Assert-MarkerTest $rejected 'Producer must reject direct absolute/traversal manifest input.'
    }
    [pscustomobject]@{ result = 'PASS'; checks = $checks; actualMainAssignment = $true; v1Supported = $true } | ConvertTo-Json -Compress
}
finally {
    if ($fixtureOwned -and (Test-Path -LiteralPath $fixture)) {
        $resolved = (Resolve-Path -LiteralPath $fixture).ProviderPath
        if ($resolved -ne $fixture -or -not $resolved.StartsWith($artifacts + '\', [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe owned fixture cleanup target.' }
        foreach ($directory in @($fixture, $packageRoot, $installRoot)) {
            if ((Test-Path -LiteralPath $directory) -and ((Get-Item -LiteralPath $directory -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { throw 'Fixture cleanup refuses reparse points.' }
        }
        foreach ($file in @($resourcePath, $v2Path, $v1Path)) { if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force } }
        foreach ($directory in @($installRoot, $packageRoot, $fixture)) { if (Test-Path -LiteralPath $directory) { [System.IO.Directory]::Delete($directory, $false) } }
    }
}

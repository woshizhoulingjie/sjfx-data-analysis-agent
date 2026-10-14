param(
    [string]$InstallDir = (Join-Path $env:ProgramFiles 'SJFX'),
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'SJFX'),
    [switch]$PurgeData
)
$ErrorActionPreference = 'Stop'
function Assert-SafeRemovalPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'Refusing to remove an empty path' }
    if (-not [IO.Path]::IsPathRooted($Path) -or ([IO.Path]::GetPathRoot($Path)).Length -le 2) {
        throw "Removal paths must be absolute: $Path"
    }
    $full = [IO.Path]::GetFullPath($Path)
    $normalized = $full.TrimEnd('\', '/')
    $root = ([IO.Path]::GetPathRoot($full)).TrimEnd('\', '/')
    if (-not $normalized -or $normalized -eq $root) {
        throw "Refusing to remove a filesystem root: $full"
    }
    foreach ($protected in @($env:USERPROFILE, $env:WINDIR, $env:ProgramFiles, $env:ProgramData)) {
        if ($protected -and $normalized -eq ([IO.Path]::GetFullPath($protected)).TrimEnd('\', '/')) {
            throw "Refusing to remove a protected system directory: $full"
        }
    }
    # GetFullPath normalizes spelling but does not resolve junctions. Reject
    # existing link components so distinct paths cannot alias the same tree.
    $component = $full
    while ($component) {
        $entry = $null
        try { $entry = Get-Item -LiteralPath $component -Force -ErrorAction Stop }
        catch [System.Management.Automation.ItemNotFoundException] { }
        if ($entry -and ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing to use a path containing a reparse point: $component"
        }
        $component = [IO.Path]::GetDirectoryName($component)
    }
}
Assert-SafeRemovalPath $InstallDir
Assert-SafeRemovalPath $DataDir
$InstallDir = ([IO.Path]::GetFullPath($InstallDir)).TrimEnd('\', '/')
$DataDir = ([IO.Path]::GetFullPath($DataDir)).TrimEnd('\', '/')
if ($InstallDir.Equals($DataDir, [StringComparison]::OrdinalIgnoreCase) -or
    $InstallDir.StartsWith($DataDir + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $DataDir.StartsWith($InstallDir + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Install and data paths must not overlap: $InstallDir and $DataDir"
}
if (Test-Path -LiteralPath $InstallDir) {
    $envFile = Join-Path $DataDir 'config\sjfx.env'
    $python = Join-Path $InstallDir 'runtime\python.exe'
    $controller = Join-Path $InstallDir 'app\sjfxctl.py'
    if ((Test-Path -LiteralPath $envFile) -and (Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $controller)) {
        & $python -B $controller --env-file $envFile stop --timeout 30
        if ($LASTEXITCODE -ne 0) { throw "The SJFX instance did not stop cleanly; refusing to remove $InstallDir" }
    }
}
if (Test-Path -LiteralPath $InstallDir) { Remove-Item -LiteralPath $InstallDir -Recurse -Force }
if ($PurgeData -and (Test-Path -LiteralPath $DataDir)) { Remove-Item -LiteralPath $DataDir -Recurse -Force }
if ($PurgeData) { Write-Output "SJFX program files and user data removed." }
else { Write-Output "SJFX program files removed; user data retained at $DataDir." }

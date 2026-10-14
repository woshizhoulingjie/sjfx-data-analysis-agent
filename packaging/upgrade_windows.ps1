param(
    [string]$PackageDir = (Split-Path -Parent $MyInvocation.MyCommand.Path),
    [string]$InstallDir = (Join-Path $env:ProgramFiles 'SJFX'),
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'SJFX')
)
$ErrorActionPreference = 'Stop'
& (Join-Path $PackageDir 'install_windows.ps1') -InstallDir $InstallDir -DataDir $DataDir
Write-Output "SJFX upgraded. Previous install is retained as a .backup-* directory; user data remains at $DataDir."

param(
  [ValidateSet('Preflight','ConfigureFiles','EnablePolicy','Restore','CheckUninstall')][string]$Action='Preflight',
  [Parameter(Mandatory=$true)][string]$HelperBinary,
  [Parameter(Mandatory=$true)][string]$StateDirectory,
  [Parameter(Mandatory=$true)][string]$TokenFile
)
$ErrorActionPreference='Stop'
# No RunAs, scheduled task or persistent elevated Web is created. The operator
# explicitly invokes a maintenance action in an already privileged terminal.
$base=@('--local','--data',[IO.Path]::GetFullPath($StateDirectory),'--local-token-file',[IO.Path]::GetFullPath($TokenFile))
if($Action -ne 'Preflight' -and $Action -ne 'CheckUninstall') {
  $principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
  if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw '此限定审计配置操作需要已授权的管理员终端；脚本不会自动提权。'}
}
switch($Action) {
  'Preflight' { & $HelperBinary @base --audit-preflight }
  'ConfigureFiles' { & $HelperBinary @base --configure-access-audit }
  'EnablePolicy' { & $HelperBinary @base --enable-audit-policy }
  'CheckUninstall' { & $HelperBinary @base --check-uninstall }
  'Restore' {
    & $HelperBinary @base --check-uninstall
    if($LASTEXITCODE -ne 0){throw '仍有未恢复处置；保存状态并先恢复处置。'}
    & $HelperBinary @base --restore-audit
    if($LASTEXITCODE -ne 0){throw '选定文件SACL恢复未完成；原备份保留。'}
    & $HelperBinary @base --restore-audit-policy
  }
}
if($LASTEXITCODE -ne 0){throw '审计维护操作失败；查看独立助手状态目录 assistant.log。'}

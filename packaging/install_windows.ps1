param(
    [string]$InstallDir = (Join-Path $env:ProgramFiles 'SJFX'),
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'SJFX')
)

$ErrorActionPreference = 'Stop'
function Protect-PrivatePath([string]$Path) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $isDirectory = [IO.Directory]::Exists($Path)
    if ($isDirectory) { $acl = [IO.Directory]::GetAccessControl($Path) }
    else { $acl = [IO.File]::GetAccessControl($Path) }
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
    if ($isDirectory) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    } else {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow')
    }
    $acl.AddAccessRule($rule)
    if ($isDirectory) { [IO.Directory]::SetAccessControl($Path, $acl) }
    else { [IO.File]::SetAccessControl($Path, $acl) }
}
function Move-BundledPath([string]$Value, [string]$OldRoot, [string]$NewRoot) {
    if ($Value.Equals($OldRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $Value.StartsWith($OldRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $Value.StartsWith($OldRoot + '/', [StringComparison]::OrdinalIgnoreCase)) {
        return $NewRoot + $Value.Substring($OldRoot.Length)
    }
    return $Value
}
function Assert-SafeInstallPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'Install and data paths cannot be empty' }
    if (-not [IO.Path]::IsPathRooted($Path) -or ([IO.Path]::GetPathRoot($Path)).Length -le 2) {
        throw "Install and data paths must be absolute: $Path"
    }
    $full = [IO.Path]::GetFullPath($Path)
    $normalized = $full.TrimEnd('\', '/')
    $root = ([IO.Path]::GetPathRoot($full)).TrimEnd('\', '/')
    if (-not $normalized -or $normalized -eq $root) { throw "Refusing to use a filesystem root: $full" }
    foreach ($protected in @($env:USERPROFILE, $env:WINDIR, $env:ProgramFiles, $env:ProgramData)) {
        if ($protected -and $normalized -eq ([IO.Path]::GetFullPath($protected)).TrimEnd('\', '/')) {
            throw "Refusing to use a protected system directory: $full"
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
Assert-SafeInstallPath $InstallDir
Assert-SafeInstallPath $DataDir
$InstallDir = ([IO.Path]::GetFullPath($InstallDir)).TrimEnd('\', '/')
$DataDir = ([IO.Path]::GetFullPath($DataDir)).TrimEnd('\', '/')
if ($InstallDir.Equals($DataDir, [StringComparison]::OrdinalIgnoreCase) -or
    $InstallDir.StartsWith($DataDir + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $DataDir.StartsWith($InstallDir + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Install and data paths must not overlap: $InstallDir and $DataDir"
}
$PackageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Required = @('app', 'runtime', 'models', 'tools', 'manifest.json')
foreach ($name in $Required) {
    if (-not (Test-Path -LiteralPath (Join-Path $PackageDir $name))) {
        throw "Offline bundle is incomplete: $name"
    }
}
$python = Join-Path $PackageDir 'runtime\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Offline bundle is missing runtime\python.exe"
}
$verify = Join-Path $PackageDir 'app\packaging\verify_bundle.py'
if (-not (Test-Path -LiteralPath $verify)) {
    throw "Offline bundle is missing its manifest verifier"
}
& $python -B $verify --root $PackageDir
if ($LASTEXITCODE -ne 0) {
    throw "Offline bundle manifest verification failed"
}

# Ask the currently installed controller to exit before replacing its code or
# runtime. This also avoids Windows sharing violations during the directory
# move. A first install has no existing configuration, so there is nothing to
# stop.
$oldEnv = Join-Path $DataDir 'config\sjfx.env'
$oldInstallRoot = ''
$oldRootFile = Join-Path $DataDir 'config\install-root.txt'
if (Test-Path -LiteralPath $oldRootFile) { $oldInstallRoot = (Get-Content -LiteralPath $oldRootFile -Raw -Encoding UTF8).Trim() }
$oldStopRoot = $InstallDir
if ($oldInstallRoot) {
    Assert-SafeInstallPath $oldInstallRoot
    $oldStopRoot = ([IO.Path]::GetFullPath($oldInstallRoot)).TrimEnd('\', '/')
    if ($oldStopRoot.Equals($DataDir, [StringComparison]::OrdinalIgnoreCase) -or
        $oldStopRoot.StartsWith($DataDir + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $DataDir.StartsWith($oldStopRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Recorded previous install path overlaps install or data path: $oldStopRoot"
    }
    if (-not $oldStopRoot.Equals($InstallDir, [StringComparison]::OrdinalIgnoreCase) -and
        ($oldStopRoot.StartsWith($InstallDir + '\', [StringComparison]::OrdinalIgnoreCase) -or
         $InstallDir.StartsWith($oldStopRoot + '\', [StringComparison]::OrdinalIgnoreCase))) {
        throw "Recorded previous install path overlaps new install path: $oldStopRoot and $InstallDir"
    }
}
$oldPython = Join-Path $oldStopRoot 'runtime\python.exe'
$oldCtl = Join-Path $oldStopRoot 'app\sjfxctl.py'
$stateDir = Join-Path $DataDir 'state'
if (Test-Path -LiteralPath $oldEnv) {
    foreach ($line in (Get-Content -LiteralPath $oldEnv -Encoding UTF8)) {
        if ($line -match '^\s*SJFX_STATE_DIR\s*=(.*)$') {
            $value = $matches[1].Trim().Trim('"').Trim("'")
            if ($value) {
                if (-not [IO.Path]::IsPathRooted($value)) { $value = Join-Path (Join-Path $oldStopRoot 'app') $value }
                $stateDir = [IO.Path]::GetFullPath($value)
            }
            break
        }
    }
}
$modelSettingsPath = Join-Path $stateDir 'model_settings.json'
if ((Test-Path -LiteralPath $oldEnv) -and (Test-Path -LiteralPath $oldPython) -and (Test-Path -LiteralPath $oldCtl)) {
    & $oldPython -B $oldCtl --env-file $oldEnv stop --timeout 30
    if ($LASTEXITCODE -ne 0) {
        throw "The existing SJFX instance did not stop cleanly; refusing to replace $InstallDir"
    }
}

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
Protect-PrivatePath $DataDir
$dataConfig = Join-Path $DataDir 'config'
New-Item -ItemType Directory -Force -Path $dataConfig | Out-Null
Protect-PrivatePath $dataConfig
$parent = Split-Path -Parent $InstallDir
New-Item -ItemType Directory -Force -Path $parent | Out-Null
$stage = Join-Path $parent ('.sjfx-stage-' + [guid]::NewGuid().ToString('N'))
$backup = $null
$metadataBackup = $null
$clamDatabase = Join-Path $DataDir 'models\protection\clamav'
$clamStage = $null
$clamDatabaseInitializing = $false
$oldInstallMoved = $false
$installMoved = $false
try {
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    $metadataBackup = Join-Path $DataDir ('.sjfx-metadata-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $metadataBackup | Out-Null
    $metadataFiles = @(
        @{ Path = (Join-Path $dataConfig 'sjfx.env'); Name = 'sjfx.env' },
        @{ Path = (Join-Path $dataConfig 'install-root.txt'); Name = 'install-root.txt' },
        @{ Path = (Join-Path $DataDir 'install-receipt.json'); Name = 'install-receipt.json' },
        @{ Path = $modelSettingsPath; Name = 'model-settings.json' }
    )
    foreach ($item in $metadataFiles) {
        $snapshot = Join-Path $metadataBackup $item.Name
        if (Test-Path -LiteralPath $item.Path) {
            Copy-Item -LiteralPath $item.Path -Destination $snapshot -Force
        } else {
            New-Item -ItemType File -Path ($snapshot + '.absent') | Out-Null
        }
    }
    # Signatures are writable instance state. Preserve any existing database,
    # including a database the user has updated from a newer offline source.
    $bundledClamDatabase = Join-Path $PackageDir 'models\protection\clamav'
    if (Test-Path -LiteralPath $bundledClamDatabase -PathType Container) {
        Assert-SafeInstallPath $clamDatabase
        if (-not (Test-Path -LiteralPath $clamDatabase)) {
            $clamParent = Split-Path -Parent $clamDatabase
            New-Item -ItemType Directory -Force -Path $clamParent | Out-Null
            Protect-PrivatePath $clamParent
            $clamStage = Join-Path $clamParent ('.sjfx-clam-' + [guid]::NewGuid().ToString('N'))
            Copy-Item -LiteralPath $bundledClamDatabase -Destination $clamStage -Recurse -Force
            Get-ChildItem -LiteralPath $clamStage -Recurse -Force | ForEach-Object {
                if (-not $_.PSIsContainer) { $_.IsReadOnly = $false }
            }
            $clamDatabaseInitializing = $true
            Move-Item -LiteralPath $clamStage -Destination $clamDatabase
        } elseif (-not (Test-Path -LiteralPath $clamDatabase -PathType Container)) {
            throw "The instance ClamAV database path is not a directory: $clamDatabase"
        }
    }
    foreach ($name in @('app', 'runtime', 'models', 'tools', 'config')) {
        Copy-Item -LiteralPath (Join-Path $PackageDir $name) -Destination (Join-Path $stage $name) -Recurse -Force
    }
    foreach ($name in @('INSTALL.md', 'install_windows.ps1', 'install_linux.sh',
                        'upgrade_windows.ps1', 'upgrade_linux.sh',
                        'uninstall_windows.ps1', 'uninstall_linux.sh')) {
        $source = Join-Path $PackageDir $name
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $stage $name) -Force
        }
    }
    Copy-Item -LiteralPath (Join-Path $PackageDir 'manifest.json') -Destination (Join-Path $stage 'manifest.json') -Force
    Copy-Item -LiteralPath (Join-Path $PackageDir 'start_sjfx.bat') -Destination (Join-Path $stage 'start_sjfx.bat') -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $InstallDir) {
        $backup = "$InstallDir.backup-$(Get-Date -Format yyyyMMdd-HHmmss)-$([guid]::NewGuid().ToString('N'))"
        Move-Item -LiteralPath $InstallDir -Destination $backup
        $oldInstallMoved = $true
    }
    Move-Item -LiteralPath $stage -Destination $InstallDir
    $installMoved = $true
# The application and bundled inputs are immutable.  Runtime state is kept in
# $DataDir and is intentionally outside this tree.
Get-ChildItem -LiteralPath $InstallDir -Recurse -Force | ForEach-Object {
    if (-not $_.PSIsContainer) { $_.IsReadOnly = $true }
}

$envFile = Join-Path $dataConfig 'sjfx.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    $template = Join-Path $InstallDir 'config\sjfx.env.template'
    $content = Get-Content -LiteralPath $template -Raw -Encoding UTF8
    $content = $content.Replace('__DATA_DIR__', $DataDir).Replace('__INSTALL_DIR__', $InstallDir)
    $bytes = New-Object byte[] 48
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    $token = [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_').TrimEnd('=')
    $content += "SJFX_API_ACCESS_TOKEN=$token`n"
    Set-Content -LiteralPath $envFile -Value $content -Encoding UTF8 -NoNewline
} elseif ($oldInstallRoot -and -not $oldStopRoot.Equals($InstallDir, [StringComparison]::OrdinalIgnoreCase)) {
    $lines = Get-Content -LiteralPath $envFile -Encoding UTF8
    $keys = @('SJFX_MODELS_DIR', 'MODELS_DIR', 'SJFX_TOOLS_DIR', 'TOOLS_DIR',
              'SJFX_DOCLING_ARTIFACTS_DIR', 'DOCLING_ARTIFACTS_DIR', 'DOCLING_CACHE_DIR',
              'SJFX_RAPIDOCR_MODEL_DIR', 'RAPIDOCR_MODEL_DIR', 'SJFX_EMBED_MODEL_PATH',
              'VLLM_EMBED_MODEL_PATH', 'SJFX_EMBED_TOKENIZER_PATH', 'LLM_TOKENIZER_PATH',
              'SJFX_MANAGED_EMBED_MODEL_PATH',
              'CONVERSATION_TOKENIZER_PATH', 'TRANSLATION_QUICKMT_MODEL_DIR',
              'TRANSLATION_MODEL_PATH', 'TRANSLATION_CT2_MODEL_PATH', 'VLLM_MODEL_PATH',
              'SJFX_DESKTOP_HELPER_BINARY', 'SJFX_PROTECTION_CLAM',
              'SJFX_PROTECTION_CLAM_DATABASE', 'SJFX_PROTECTION_CLAM_CERTS',
              'SJFX_PROTECTION_YARA', 'SJFX_PROTECTION_YARA_RULES',
              'SJFX_PROTECTION_RSPAMD', 'SJFX_MSG_PYTHON')
    $updated = foreach ($line in $lines) {
        if ($line -match '^([^#=]+)=(.*)$' -and ($keys -contains $matches[1].Trim())) {
            $value = $matches[2].Trim()
            $quote = ''
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $quote = $value.Substring(0, 1)
                $value = $value.Substring(1, $value.Length - 2)
            }
            $updatedValue = Move-BundledPath $value $oldStopRoot $InstallDir
            if ($updatedValue -ne $value) {
                $line = $matches[1] + '=' + $quote + $updatedValue + $quote
            }
        }
        $line
    }
    Set-Content -LiteralPath $envFile -Value ($updated -join "`n") -Encoding UTF8 -NoNewline
    if (Test-Path -LiteralPath $modelSettingsPath) {
        $settings = Get-Content -LiteralPath $modelSettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $changed = $false
        if (-not $settings.env_overrides -or $settings.env_overrides -is [string] -or $settings.env_overrides -is [array]) {
            throw 'Persisted model settings have invalid overrides'
        }
        foreach ($key in @('SJFX_EMBED_TOKENIZER_PATH', 'LLM_TOKENIZER_PATH', 'CONVERSATION_TOKENIZER_PATH')) {
            $property = $settings.env_overrides.PSObject.Properties[$key]
            if ($property -and $property.Value -is [string]) {
                $updatedValue = Move-BundledPath $property.Value $oldStopRoot $InstallDir
                if ($updatedValue -ne $property.Value) { $property.Value = $updatedValue; $changed = $true }
            }
        }
        if ($changed) {
            [IO.File]::WriteAllText($modelSettingsPath, (($settings | ConvertTo-Json -Depth 32) + "`n"), [Text.UTF8Encoding]::new($false))
        }
    }
}
# Upgrade older configurations by filling only new local runtime settings.
# Do not replace existing resource values or generation/embedding selections.
$runtimeKeys = @('SJFX_LOCAL_AGENT_ENDPOINT', 'SJFX_DESKTOP_ACCESS_AUDIT', 'SJFX_DESKTOP_HELPER_BINARY',
                 'SJFX_PROTECTION_CLAM', 'SJFX_PROTECTION_CLAM_DATABASE', 'SJFX_PROTECTION_CLAM_CERTS',
                 'SJFX_PROTECTION_SCAN_WORKDIR', 'SJFX_MANAGED_EMBEDDING', 'SJFX_MANAGED_EMBED_PORT',
                 'SJFX_MANAGED_EMBED_MODEL_PATH', 'SJFX_OFFLINE_INSTALLATION')
$existingText = Get-Content -LiteralPath $envFile -Raw -Encoding UTF8
$presentKeys = @{}
foreach ($line in ($existingText -split "`r?`n")) {
    if ($line -match '^\s*([^#=]+)=(.*)$') { $presentKeys[$matches[1].Trim()] = $true }
}
$missingLines = @()
foreach ($line in (Get-Content -LiteralPath (Join-Path $InstallDir 'config\sjfx.env.template') -Encoding UTF8)) {
    if ($line -match '^\s*([^#=]+)=(.*)$') {
        $key = $matches[1].Trim()
        if (($runtimeKeys -contains $key) -and -not $presentKeys.ContainsKey($key)) {
            $missingLines += $line.Replace('__DATA_DIR__', $DataDir).Replace('__INSTALL_DIR__', $InstallDir)
            $presentKeys[$key] = $true
        }
    }
}
if ($missingLines.Count) {
    if ($existingText -and -not $existingText.EndsWith("`n")) { $existingText += "`n" }
    Set-Content -LiteralPath $envFile -Value ($existingText + ($missingLines -join "`n") + "`n") -Encoding UTF8 -NoNewline
}
Protect-PrivatePath $envFile
Set-Content -LiteralPath (Join-Path $dataConfig 'install-root.txt') -Value $InstallDir -Encoding UTF8 -NoNewline
foreach ($path in @('state', 'outputs', 'parse_temp', 'logs', 'cache\huggingface', 'outputs\_uploads')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataDir $path) | Out-Null
    Protect-PrivatePath (Join-Path $DataDir $path)
}
$record = [ordered]@{ installed_at = (Get-Date).ToUniversalTime().ToString('o'); install_dir = $InstallDir; data_dir = $DataDir; backup_dir = $backup; manifest = (Join-Path $InstallDir 'manifest.json') }
Set-Content -LiteralPath (Join-Path $DataDir 'install-receipt.json') -Value ($record | ConvertTo-Json) -Encoding UTF8
Write-Output "SJFX installed. Configuration: $envFile"
Write-Output "For a new instance, initialize the administrator: & `"$InstallDir\runtime\python.exe`" -B -X utf8 `"$InstallDir\app\sjfxctl.py`" --env-file `"$envFile`" init-admin"
Write-Output "Start with: $InstallDir\start_sjfx.bat --env-file `"$envFile`""
if ($metadataBackup -and (Test-Path -LiteralPath $metadataBackup)) {
    Remove-Item -LiteralPath $metadataBackup -Recurse -Force -ErrorAction SilentlyContinue
}
} catch {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    if ($installMoved -and (Test-Path -LiteralPath $InstallDir)) {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($oldInstallMoved -and $backup -and -not (Test-Path -LiteralPath $InstallDir) -and (Test-Path -LiteralPath $backup)) {
        Move-Item -LiteralPath $backup -Destination $InstallDir -ErrorAction SilentlyContinue
    }
    if ($metadataBackup -and (Test-Path -LiteralPath $metadataBackup)) {
        foreach ($item in $metadataFiles) {
            $snapshot = Join-Path $metadataBackup $item.Name
            $absent = $snapshot + '.absent'
            if (Test-Path -LiteralPath $absent) {
                Remove-Item -LiteralPath $item.Path -Force -ErrorAction SilentlyContinue
            } elseif (Test-Path -LiteralPath $snapshot) {
                Copy-Item -LiteralPath $snapshot -Destination $item.Path -Force -ErrorAction SilentlyContinue
            }
        }
        Remove-Item -LiteralPath $metadataBackup -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($clamDatabaseInitializing -and $clamStage -and -not (Test-Path -LiteralPath $clamStage) -and (Test-Path -LiteralPath $clamDatabase)) {
        Remove-Item -LiteralPath $clamDatabase -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($clamStage -and (Test-Path -LiteralPath $clamStage)) {
        Remove-Item -LiteralPath $clamStage -Recurse -Force -ErrorAction SilentlyContinue
    }
    throw
}

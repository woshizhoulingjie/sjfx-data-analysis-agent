param([ValidateSet('configure','read','restore','preflight')][string]$Mode)
$ErrorActionPreference='Stop'
[Console]::InputEncoding=[Text.UTF8Encoding]::new()
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$payload=[Console]::In.ReadToEnd()|ConvertFrom-Json
$section=[Security.AccessControl.AccessControlSections]::Audit
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public sealed class SjfxAuditPrivilege : IDisposable {
  [StructLayout(LayoutKind.Sequential)] struct Luid { public uint Low; public int High; }
  [StructLayout(LayoutKind.Sequential)] struct Privileges { public uint Count; public Luid Id; public uint Attributes; }
  [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr handle);
  [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool LookupPrivilegeValue(string system, string name, out Luid id);
  [DllImport("advapi32.dll", SetLastError=true)] static extern bool AdjustTokenPrivileges(IntPtr token, bool all, ref Privileges state, uint size, out Privileges previous, out uint returned);
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool ConvertStringSecurityDescriptorToSecurityDescriptor(string sddl, uint revision, out IntPtr descriptor, out uint size);
  [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetSecurityDescriptorSacl(IntPtr descriptor, out bool present, out IntPtr sacl, out bool defaulted);
  [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetSecurityDescriptorControl(IntPtr descriptor, out ushort control, out uint revision);
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode)] static extern uint SetNamedSecurityInfo(string name, uint kind, uint information, IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);
  [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
  public static void SetSacl(string path, string sddl) {
    IntPtr descriptor; uint size;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1, out descriptor, out size)) throw new Win32Exception(Marshal.GetLastWin32Error());
    try {
      bool present, defaulted; IntPtr sacl; ushort control; uint revision;
      if (!GetSecurityDescriptorSacl(descriptor, out present, out sacl, out defaulted)) throw new Win32Exception(Marshal.GetLastWin32Error());
      if (!GetSecurityDescriptorControl(descriptor, out control, out revision)) throw new Win32Exception(Marshal.GetLastWin32Error());
      // Explicitly preserve the saved SACL inheritance flag, including empty SACLs.
      uint information=8U | ((control & 0x2000)!=0 ? 0x40000000U : 0x10000000U);
      uint error=SetNamedSecurityInfo(path, 1, information, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, present ? sacl : IntPtr.Zero);
      if (error!=0) throw new Win32Exception((int)error);
    } finally { LocalFree(descriptor); }
  }
  IntPtr token; Privileges previous; bool adjusted;
  public SjfxAuditPrivilege() {
    try {
      if (!OpenProcessToken(GetCurrentProcess(), 0x28, out token)) throw new Win32Exception(Marshal.GetLastWin32Error());
      Luid id;
      if (!LookupPrivilegeValue(null, "SeSecurityPrivilege", out id)) throw new Win32Exception(Marshal.GetLastWin32Error());
      var requested = new Privileges { Count=1, Id=id, Attributes=2 }; uint returned;
      bool ok=AdjustTokenPrivileges(token, false, ref requested, (uint)Marshal.SizeOf(typeof(Privileges)), out previous, out returned);
      int error=Marshal.GetLastWin32Error();
      if (!ok || error!=0) throw new Win32Exception(error);
      adjusted=true;
    } catch { if (token!=IntPtr.Zero) { CloseHandle(token); token=IntPtr.Zero; } throw; }
  }
  public void Dispose() {
    if (token==IntPtr.Zero) return;
    try {
      if (adjusted) {
        Privileges unused; uint returned;
        bool ok=AdjustTokenPrivileges(token, false, ref previous, (uint)Marshal.SizeOf(typeof(Privileges)), out unused, out returned);
        int error=Marshal.GetLastWin32Error();
        if (!ok || error!=0) throw new Win32Exception(error);
      }
    } finally { CloseHandle(token); token=IntPtr.Zero; }
  }
}
'@
# The audit PowerShell child has its own token; parent privilege state is not proof.
$privilege=[SjfxAuditPrivilege]::new()
try {
if($Mode -eq 'preflight') {
  $event=Get-WinEvent -LogName Security -MaxEvents 1
  @{security_log_readable=$true;latest_record_id=$event.RecordId}|ConvertTo-Json -Compress
} elseif($Mode -eq 'configure') {
  $results=@()
  foreach($file in $payload.files) {
    $item=Get-Item -LiteralPath $file.path -Force
    if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw '拒绝为链接或占位文件设置审计'}
    $acl=[IO.File]::GetAccessControl($file.path,$section)
    $before=$acl.GetSecurityDescriptorSddlForm($section)
    $sid=[Security.Principal.SecurityIdentifier]::new('S-1-1-0')
    $rights=[Security.AccessControl.FileSystemRights]::ReadData -bor [Security.AccessControl.FileSystemRights]::WriteData -bor [Security.AccessControl.FileSystemRights]::AppendData -bor [Security.AccessControl.FileSystemRights]::Delete
    $flags=[Security.AccessControl.AuditFlags]::Success -bor [Security.AccessControl.AuditFlags]::Failure
    $rule=[Security.AccessControl.FileSystemAuditRule]::new($sid,$rights,$flags)
    $acl.AddAuditRule($rule)
    $after=$acl.GetSecurityDescriptorSddlForm($section)
    # Caller persists this plan before requesting apply; ACLs are never changed in this branch.
    $results+=@{path=$file.path;before=$before;after=$after;binding_id=$file.binding_id}
  }
  @{plans=$results}|ConvertTo-Json -Depth 6 -Compress
} elseif($Mode -eq 'restore') {
  foreach($file in $payload.files) {
    $acl=[IO.File]::GetAccessControl($file.path,$section)
    $current=$acl.GetSecurityDescriptorSddlForm($section)
    if($current -ceq $file.target){continue}
    if($current -cne $file.expected){throw '文件审计权限在配置后发生变化，拒绝覆盖其他设置'}
    [SjfxAuditPrivilege]::SetSacl($file.path,$file.target)
    $verified=[IO.File]::GetAccessControl($file.path,$section).GetSecurityDescriptorSddlForm($section)
    if($verified -cne $file.target){throw 'SACL restore readback differs from saved target; keep backup'}
  }
  @{ok=$true}|ConvertTo-Json -Compress
} else {
  $cursor=[long]$payload.cursor
  $last=Get-WinEvent -LogName Security -MaxEvents 1
  $oldest=Get-WinEvent -LogName Security -Oldest -MaxEvents 1
  $gap=($cursor -gt 0 -and ($cursor -gt $last.RecordId -or $cursor -lt ($oldest.RecordId-1)))
  if($cursor -eq 0 -or $gap){$cursor=$last.RecordId}
  $query=[Diagnostics.Eventing.Reader.EventLogQuery]::new('Security',[Diagnostics.Eventing.Reader.PathType]::LogName,"*[System[(EventID=4663 or EventID=4656) and EventRecordID > $cursor]]")
  $reader=[Diagnostics.Eventing.Reader.EventLogReader]::new($query)
  $selected=@{};foreach($file in $payload.files){$selected[$file.path]=$file.binding_id}
  $out=@();$count=0;$next=$cursor
  try {
    while($count -lt 2000 -and ($event=$reader.ReadEvent())) {
      try {
        $count++;$next=$event.RecordId
        $xml=[xml]$event.ToXml();$data=@{}
        foreach($node in $xml.Event.EventData.Data){$data[$node.Name]=$node.'#text'}
        if($data.ObjectType -ne 'File' -or -not $selected.ContainsKey($data.ObjectName)){continue}
        # 4663 reports use of access rights, not a guaranteed one-event-per-open trace.
        $out+=@{record_id=$event.RecordId;binding_id=$selected[$data.ObjectName];path=$data.ObjectName;process=$data.ProcessName;process_id=$data.ProcessId;user=($data.SubjectDomainName+'\'+$data.SubjectUserName);mask=$data.AccessMask;event_id=$event.Id;keywords=[string]$xml.Event.System.Keywords;occurred=$event.TimeCreated.ToUniversalTime().ToString('o')}
      } finally {$event.Dispose()}
    }
  } finally {$reader.Dispose()}
  if($count -lt 2000){$next=$last.RecordId}
  @{events=$out;cursor=$next;gap=$gap}|ConvertTo-Json -Depth 6 -Compress
}
} finally { $privilege.Dispose() }

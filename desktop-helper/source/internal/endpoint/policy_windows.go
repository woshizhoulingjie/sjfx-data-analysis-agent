//go:build windows

package endpoint

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"golang.org/x/sys/windows"
	"os"
	"path/filepath"
	"sync"
	"unsafe"
)

type auditPolicyInfo struct {
	Subcategory windows.GUID
	Flags       uint32
	Category    windows.GUID
}

var auditAPI = windows.NewLazySystemDLL("advapi32.dll")
var auditPolicyPrivilegeMu sync.Mutex

// Only enable a privilege already assigned to this process token. This does
// not elevate an ordinary token or rely on a parent's token state. Serialize
// process-token scopes so one query cannot restore another query's privilege.
func acquireAuditSecurityPrivilege() (func() error, error) {
	var token windows.Token
	if err := windows.OpenProcessToken(windows.CurrentProcess(), windows.TOKEN_ADJUST_PRIVILEGES|windows.TOKEN_QUERY, &token); err != nil {
		return nil, err
	}
	name, _ := windows.UTF16PtrFromString("SeSecurityPrivilege")
	var luid windows.LUID
	if err := windows.LookupPrivilegeValue(nil, name, &luid); err != nil {
		token.Close()
		return nil, err
	}
	next := windows.Tokenprivileges{PrivilegeCount: 1, Privileges: [1]windows.LUIDAndAttributes{{Luid: luid, Attributes: windows.SE_PRIVILEGE_ENABLED}}}
	var prior windows.Tokenprivileges
	var length uint32
	// The x/sys wrapper only checks BOOL failure and silently drops the
	// successful-call ERROR_NOT_ALL_ASSIGNED case. Capture native LastError
	// in the same syscall rather than reading another OS thread's LastError.
	adjust := auditAPI.NewProc("AdjustTokenPrivileges")
	ok, _, err := adjust.Call(uintptr(token), 0, uintptr(unsafe.Pointer(&next)), uintptr(unsafe.Sizeof(prior)), uintptr(unsafe.Pointer(&prior)), uintptr(unsafe.Pointer(&length)))
	if uint32(ok) == 0 || errors.Is(err, windows.ERROR_NOT_ALL_ASSIGNED) {
		token.Close()
		if errors.Is(err, windows.ERROR_NOT_ALL_ASSIGNED) {
			return nil, fmt.Errorf("SeSecurityPrivilege未分配给当前进程令牌: %w", err)
		}
		return nil, err
	}
	return func() error {
		defer token.Close()
		// PreviousState can be empty when the privilege was already enabled.
		// Restoring that exact state preserves the caller's original token.
		ok, _, err := adjust.Call(uintptr(token), 0, uintptr(unsafe.Pointer(&prior)), 0, 0, 0)
		if uint32(ok) == 0 || errors.Is(err, windows.ERROR_NOT_ALL_ASSIGNED) {
			return fmt.Errorf("恢复SeSecurityPrivilege原状态失败: %w", err)
		}
		return nil
	}, nil
}

func queryAuditPolicyScoped(acquire func() (func() error, error), query func() (uint32, error)) (flags uint32, err error) {
	restore, err := acquire()
	if err != nil {
		return 0, err
	}
	defer func() {
		if restoreErr := restore(); restoreErr != nil {
			err = errors.Join(err, restoreErr)
		}
	}()
	return query()
}

func GetAuditPolicy() (uint32, error) {
	auditPolicyPrivilegeMu.Lock()
	defer auditPolicyPrivilegeMu.Unlock()
	return queryAuditPolicyScoped(acquireAuditSecurityPrivilege, queryAuditPolicyNative)
}

func queryAuditPolicyNative() (uint32, error) {
	guid, err := windows.GUIDFromString("{0CCE921D-69AE-11D9-BED3-505054503030}")
	if err != nil {
		return 0, err
	}
	var buffer *auditPolicyInfo
	ok, _, e := auditAPI.NewProc("AuditQuerySystemPolicy").Call(uintptr(unsafe.Pointer(&guid)), 1, uintptr(unsafe.Pointer(&buffer)))
	// Audit* returns BOOLEAN (one byte), not Win32 BOOL. Upper return-register
	// bits are unspecified on failure and must not be mistaken for success.
	if ok&0xff == 0 || buffer == nil {
		if buffer == nil && ok&0xff != 0 {
			return 0, fmt.Errorf("Windows未返回可用文件系统审计策略信息")
		}
		return 0, e
	}
	defer auditAPI.NewProc("AuditFree").Call(uintptr(unsafe.Pointer(buffer)))
	return buffer.Flags, nil
}
func SetAuditPolicy(flags uint32) error {
	if flags > 4 {
		return fmt.Errorf("invalid audit flags")
	}
	auditPolicyPrivilegeMu.Lock()
	defer auditPolicyPrivilegeMu.Unlock()
	restore, err := acquireAuditSecurityPrivilege()
	if err != nil {
		return err
	}
	// Named return handling lives in the small scoped wrapper below so a
	// failed restoration cannot be mistaken for a successful maintenance call.
	return setAuditPolicyScoped(flags, restore)
}

func setAuditPolicyScoped(flags uint32, restore func() error) (err error) {
	defer func() {
		if restoreErr := restore(); restoreErr != nil {
			err = errors.Join(err, restoreErr)
		}
	}()
	guid, _ := windows.GUIDFromString("{0CCE921D-69AE-11D9-BED3-505054503030}")
	requested := flags & 3
	if requested == 0 {
		flags = 4
	} // Zero means UNCHANGED to AuditSetSystemPolicy, not disabled.
	info := auditPolicyInfo{Subcategory: guid, Flags: flags}
	ok, _, err := auditAPI.NewProc("AuditSetSystemPolicy").Call(uintptr(unsafe.Pointer(&info)), 1)
	if ok&0xff == 0 {
		return err
	}
	// The current scope already enabled our own token. Avoid reacquiring the
	// process mutex while still verifying the actual applied native setting.
	actual, e := queryAuditPolicyNative()
	if e != nil {
		return e
	}
	if actual&3 != requested {
		return fmt.Errorf("审计策略回读不一致：请求 %d，实际 %d", requested, actual)
	}
	return nil
}
func EnableAuditPolicy(dir string) error {
	flags, err := GetAuditPolicy()
	if err != nil {
		return err
	}
	path := filepath.Join(dir, "audit-policy-backup.json")
	// Exclusive creation preserves the true pre-installation value on retries.
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil && !os.IsExist(err) {
		return err
	}
	if err == nil {
		err = json.NewEncoder(f).Encode(map[string]any{"original": flags, "applied": 3, "subcategory": "File System"})
		if err == nil {
			err = f.Sync()
		}
		f.Close()
		if err != nil {
			return err
		}
	}
	return SetAuditPolicy(3)
}

func AuditPreflight(ctx context.Context) map[string]any {
	flags, err := GetAuditPolicy()
	var report map[string]any
	readErr := runAudit(ctx, "preflight", map[string]any{}, &report)
	if report == nil {
		report = map[string]any{}
	}
	report["audit_policy_flags"] = flags
	if err != nil {
		report["audit_policy_query_error"] = err.Error()
	}
	report["audit_policy_success_failure_enabled"] = err == nil && flags&3 == 3
	report["security_log_readable"] = readErr == nil
	report["coverage_complete"] = false
	report["settings_changed"] = false
	return report
}

func RestoreAuditPolicy(dir string) error {
	path := filepath.Join(dir, "audit-policy-backup.json")
	raw, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	var backup struct {
		Original uint32 `json:"original"`
		Applied  uint32 `json:"applied"`
	}
	if err = json.Unmarshal(raw, &backup); err != nil {
		return err
	}
	flags, err := GetAuditPolicy()
	if err != nil {
		return err
	}
	if flags&3 != backup.Original&3 {
		if flags&3 != backup.Applied&3 {
			return fmt.Errorf("全局审计策略在安装后改变，拒绝覆盖其他设置；保留原始备份")
		}
		if err = SetAuditPolicy(backup.Original); err != nil {
			return err
		}
	}
	return os.Rename(path, path+".restored")
}

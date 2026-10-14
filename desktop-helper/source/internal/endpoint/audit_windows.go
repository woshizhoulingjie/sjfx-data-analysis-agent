//go:build windows

package endpoint

import (
	"bytes"
	"context"
	_ "embed"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf16"
)

//go:embed access_audit.ps1
var auditScript string

func (r *Runtime) runAccessAudit(ctx context.Context, mode string, input, output any) error {
	if r.auditRunner != nil {
		return r.auditRunner(ctx, mode, input, output)
	}
	return runAudit(ctx, mode, input, output)
}

func runAudit(ctx context.Context, mode string, input, output any) error {
	// Script is constant and encoded as UTF-16. All paths and state use JSON stdin.
	script := "& {" + auditScript + "} -Mode '" + mode + "'"
	words := utf16.Encode([]rune(script))
	raw := make([]byte, len(words)*2)
	for i, w := range words {
		binary.LittleEndian.PutUint16(raw[i*2:], w)
	}
	body, err := json.Marshal(input)
	if err != nil {
		return err
	}
	cmd := exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.StdEncoding.EncodeToString(raw))
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	cmd.Stdin = bytes.NewReader(body)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return fmt.Errorf("Windows 访问审计失败（需要管理员权限及文件系统审计策略）：%v %s", err, stderr.String())
	}
	return json.Unmarshal(bytes.TrimPrefix(out, []byte{239, 187, 191}), output)
}

type AuditPlan struct {
	Path      string `json:"path"`
	Before    string `json:"before"`
	After     string `json:"after"`
	BindingID string `json:"binding_id"`
	Identity  string `json:"identity"`
}

func (r *Runtime) ConfigureAccessAudit(ctx context.Context, bindings []Binding) error {
	if !r.AccessAudit {
		return nil
	}
	if r.auditConfigured == nil {
		r.auditConfigured = map[string]bool{}
	}
	var pending []Binding
	keys := map[string]string{}
	var plans []AuditPlan
	var newFiles []map[string]string
	for _, b := range bindings {
		if b.State == "unavailable" || b.Disposition == "quarantine" || b.Disposition == "read_lock" {
			continue
		}
		key := "audit:" + b.ID + ":" + b.Identity
		if r.auditConfigured[key] {
			continue
		}
		pending = append(pending, b)
	}
	if len(pending) == 0 {
		r.auditPending.Store(0)
		return nil
	}
	batch := pending
	if len(batch) > 200 {
		batch = batch[:200]
	}
	for _, b := range batch {
		key := "audit:" + b.ID + ":" + b.Identity
		keys[b.ID] = key
		var raw string
		var p AuditPlan
		if err := r.DB.DB.QueryRow("SELECT value FROM state WHERE key=?", key).Scan(&raw); err == nil {
			if err = json.Unmarshal([]byte(raw), &p); err != nil {
				return err
			}
			p.Path = b.Path
			plans = append(plans, p)
		} else {
			newFiles = append(newFiles, map[string]string{"path": b.Path, "binding_id": b.ID})
		}
	}
	if len(newFiles) > 0 {
		var reply struct {
			Plans []AuditPlan `json:"plans"`
		}
		if err := r.runAccessAudit(ctx, "configure", map[string]any{"files": newFiles}, &reply); err != nil {
			return err
		}
		if len(reply.Plans) != len(newFiles) {
			return fmt.Errorf("文件审计计划不完整")
		}
		seen := map[string]bool{}
		for _, p := range reply.Plans {
			if keys[p.BindingID] == "" || seen[p.BindingID] {
				return fmt.Errorf("审计计划成员无效或重复")
			}
			seen[p.BindingID] = true
			for _, b := range batch {
				if b.ID == p.BindingID {
					if p.Path != b.Path {
						return fmt.Errorf("审计计划路径不一致")
					}
					p.Identity = b.Identity
				}
			}
			raw, _ := json.Marshal(p)
			if _, err := r.DB.DB.Exec("INSERT INTO state VALUES(?,?)", keys[p.BindingID], string(raw)); err != nil {
				return err
			}
			plans = append(plans, p)
		}
	}
	apply := []map[string]string{}
	for _, p := range plans {
		identity, err := FileIdentity(p.Path)
		if err != nil || identity != p.Identity {
			return fmt.Errorf("应用审计前文件实例已变化")
		}
		apply = append(apply, map[string]string{"path": p.Path, "expected": p.Before, "target": p.After})
	}
	var reply any
	if err := r.runAccessAudit(ctx, "restore", map[string]any{"files": apply}, &reply); err != nil {
		return err
	}
	for _, p := range plans {
		r.auditConfigured[keys[p.BindingID]] = true
	}
	r.auditPending.Store(int64(len(pending) - len(batch)))
	return nil
}
func (r *Runtime) RestoreAccessAudit(ctx context.Context) error {
	rows, err := r.DB.DB.Query("SELECT key,value FROM state WHERE key LIKE 'audit:%'")
	if err != nil {
		return err
	}
	type saved struct {
		key  string
		plan AuditPlan
	}
	var plans []saved
	for rows.Next() {
		var key, raw string
		if err = rows.Scan(&key, &raw); err != nil {
			rows.Close()
			return err
		}
		var p AuditPlan
		if err = json.Unmarshal([]byte(raw), &p); err != nil {
			rows.Close()
			return err
		}
		plans = append(plans, saved{key, p})
	}
	rows.Close()
	bindings, err := r.DB.Bindings()
	if err != nil {
		return err
	}
	unresolved := 0
	for _, item := range plans {
		p := item.plan
		for _, b := range bindings {
			if b.ID == p.BindingID && b.Identity == p.Identity {
				p.Path = b.Path
			}
		}
		actual, e := FileIdentity(p.Path)
		if e != nil || actual != p.Identity {
			unresolved++
			continue
		}
		var reply any
		if e = r.runAccessAudit(ctx, "restore", map[string]any{"files": []map[string]string{{"path": p.Path, "expected": p.After, "target": p.Before}}}, &reply); e != nil {
			return e
		}
		if _, e = r.DB.DB.Exec("DELETE FROM state WHERE key=?", item.key); e != nil {
			return e
		}
	}
	if unresolved > 0 {
		return fmt.Errorf("仍有%d条原文件SACL备份未恢复：路径不可访问或实例已更换；保留备份，禁止覆盖其他文件", unresolved)
	}
	return nil
}

// Idempotent same-target requests read back each SACL without changing it.
// A cached successful installation is not proof that current ACLs still match.
func (r *Runtime) VerifyAccessAudit(ctx context.Context, bindings []Binding) error {
	if len(bindings) == 0 {
		return fmt.Errorf("尚无已登记文件，不能声称文件访问审计覆盖完整")
	}
	files := []map[string]string{}
	for _, b := range bindings {
		if b.State == "unavailable" || b.Disposition == "quarantine" || b.Disposition == "read_lock" {
			return fmt.Errorf("部分已登记文件当前不可核验SACL覆盖")
		}
		key := "audit:" + b.ID + ":" + b.Identity
		if !r.auditConfigured[key] {
			continue
		}
		var raw string
		var plan AuditPlan
		if err := r.DB.DB.QueryRow("SELECT value FROM state WHERE key=?", key).Scan(&raw); err != nil {
			return err
		}
		if err := json.Unmarshal([]byte(raw), &plan); err != nil {
			return err
		}
		identity, err := FileIdentity(b.Path)
		if err != nil || identity != b.Identity {
			return fmt.Errorf("已登记文件实例变化，原SACL覆盖失效")
		}
		files = append(files, map[string]string{"path": b.Path, "expected": plan.After, "target": plan.After})
	}
	if len(files) == 0 {
		return nil
	} // Pending configuration is separately unhealthy.
	var reply any
	return r.runAccessAudit(ctx, "restore", map[string]any{"files": files}, &reply)
}

func (r *Runtime) CollectAccess(ctx context.Context) error {
	if r.AccessAudit {
		getPolicy := GetAuditPolicy
		if r.auditPolicy != nil {
			getPolicy = r.auditPolicy
		}
		flags, err := getPolicy()
		if err != nil {
			return err
		}
		if flags&3 != 3 {
			return fmt.Errorf("Windows 文件系统成功/失败审计未启用，请运行管理员安装步骤")
		}
	}
	if !r.AccessAudit {
		return nil
	}
	bindings, err := r.DB.Bindings()
	if err != nil {
		return err
	}
	files := []map[string]string{}
	for _, b := range bindings {
		files = append(files, map[string]string{"path": b.Path, "binding_id": b.ID})
	}
	var cursor string
	_ = r.DB.DB.QueryRow("SELECT value FROM state WHERE key='audit_cursor'").Scan(&cursor)
	n, _ := strconv.ParseInt(cursor, 10, 64)
	var response struct {
		Cursor int64 `json:"cursor"`
		Gap    bool  `json:"gap"`
		Events []struct {
			RecordID  int64  `json:"record_id"`
			BindingID string `json:"binding_id"`
			Path      string `json:"path"`
			Process   string `json:"process"`
			ProcessID string `json:"process_id"`
			Mask      string `json:"mask"`
			User      string `json:"user"`
			EventID   int    `json:"event_id"`
			Keywords  string `json:"keywords"`
			Occurred  string `json:"occurred"`
		} `json:"events"`
	}
	if err = r.runAccessAudit(ctx, "read", map[string]any{"cursor": n, "files": files}, &response); err != nil {
		return err
	}
	if response.Gap {
		r.auditHealthy.Store(false)
		if err = r.MarkGap("Windows 安全日志已被清空或覆盖，访问事件存在缺口"); err != nil {
			return err
		}
	}
	current, loadErr := r.DB.Bindings()
	if loadErr != nil {
		return loadErr
	}
	for _, e := range response.Events {
		when, _ := time.Parse(time.RFC3339, e.Occurred)
		mask, _ := strconv.ParseUint(strings.TrimPrefix(e.Mask, "0x"), 16, 32)
		action := "file.access_right_used"
		if e.EventID == 4656 {
			action = "file.access_requested"
		} else if mask&1 != 0 {
			action = "file.read"
		} else if mask&6 != 0 {
			action = "file.write"
		} else if mask&0x10000 != 0 {
			action = "file.delete_right_used"
		}
		ev := Event{EventID: fmt.Sprintf("security-%d-%d", when.Unix(), e.RecordID), BindingID: e.BindingID, Action: action, Path: e.Path, Process: e.Process, Occurred: float64(when.UnixMilli()) / 1000, Detail: fmt.Sprintf("Windows %d; mask=%s; user=%s; keywords=%s", e.EventID, e.Mask, e.User, e.Keywords)}
		ev.User, ev.ProcessID = e.User, e.ProcessID
		ev.AccessMask, ev.NativeEventID, ev.NativeRecordID = e.Mask, e.EventID, e.RecordID
		// Security logs have paths and processes, not content hashes. These are
		// current binding references, never a claim that an old event read this version.
		ev.VersionEvidence = "current_binding_at_collection_not_event_content_proof"
		for _, b := range current {
			if b.ID == e.BindingID {
				ev.Identity, ev.SHA256, ev.VersionID, ev.RegistrationID, ev.GapEpoch, ev.Size = b.Identity, b.SHA256, b.VersionID, b.RegistrationID, b.GapEpoch, b.Size
			}
		}
		if err = r.DB.Enqueue(ev); err != nil {
			return err
		}
	}
	_, err = r.DB.DB.Exec("INSERT INTO state VALUES('audit_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", strconv.FormatInt(response.Cursor, 10))
	return err
}

//go:build windows

package endpoint

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"golang.org/x/sys/windows"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

type Action struct {
	ID                   string  `json:"id"`
	Binding              string  `json:"binding"`
	Kind                 string  `json:"kind"`
	SHA256               string  `json:"expected_sha256"`
	Path                 string  `json:"local_path"`
	Identity             string  `json:"local_identity"`
	RestoreID            string  `json:"restore_action_id"`
	VersionID            string  `json:"expected_version_id"`
	RegistrationID       string  `json:"registration_id"`
	RegistrationRevision int64   `json:"registration_revision"`
	ManifestGeneration   int64   `json:"manifest_generation"`
	GapEpoch             int64   `json:"gap_epoch"`
	DeviceID             string  `json:"device_id"`
	ExpiresAt            float64 `json:"expires_at"`
}
type Journal struct {
	Action      Action         `json:"action"`
	Original    string         `json:"original"`
	Destination string         `json:"destination"`
	Active      bool           `json:"active"`
	Intent      bool           `json:"intent"`
	Result      map[string]any `json:"result"`
	Supersedes  string         `json:"supersedes,omitempty"`
}

func (d *Database) Journals() ([]Journal, error) {
	rows, err := d.DB.Query("SELECT payload FROM actions")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Journal
	for rows.Next() {
		var s string
		if err = rows.Scan(&s); err != nil {
			return nil, err
		}
		var j Journal
		if err = json.Unmarshal([]byte(s), &j); err != nil {
			return nil, err
		}
		out = append(out, j)
	}
	return out, rows.Err()
}
func result(status, detail string) map[string]any {
	return map[string]any{"status": status, "detail": detail}
}
func actionActual(a Action, path, identity, sha string) map[string]any {
	return map[string]any{"action_id": a.ID, "local_path": path, "local_identity": identity, "sha256": sha}
}
func actionEffect(kind string) string {
	switch kind {
	case "quarantine":
		return "quarantine"
	case "deny_read":
		return "read_lock"
	case "protect_delete":
		return "delete_lock"
	}
	return kind
}
func (r *Runtime) validateFrozen(a Action, b Binding) error {
	if a.ExpiresAt <= float64(time.Now().UnixMilli())/1000 {
		return fmt.Errorf("指令已过期")
	}
	if a.DeviceID == "" || a.DeviceID != r.Client.GetIdentity().DeviceID || a.DeviceID != b.DeviceID {
		return fmt.Errorf("指令设备不一致")
	}
	if a.RegistrationID == "" || a.RegistrationID != b.RegistrationID || a.RegistrationRevision != b.RegistrationRevision || a.ManifestGeneration != b.ManifestGeneration || a.GapEpoch != b.GapEpoch {
		return fmt.Errorf("登记/清单/缺口代次已变化")
	}
	if a.VersionID == "" || a.VersionID != b.VersionID || a.SHA256 != b.SHA256 {
		return fmt.Errorf("绑定内容版本已变化")
	}
	if !withinRoot(b.LocalRoot, a.Path) {
		return fmt.Errorf("目标不在登记本地根目录")
	}
	for _, e := range r.Epochs() {
		if e.RegistrationID == b.RegistrationID && e.GapEpoch != a.GapEpoch {
			return fmt.Errorf("本地缺口代次已变化")
		}
	}
	return nil
}
func (r *Runtime) Execute(a Action) (out map[string]any) {
	r.operation.Lock()
	defer r.operation.Unlock()
	defer func() {
		if out != nil {
			out["target"] = a
			out["effect"] = actionEffect(a.Kind)
		}
	}()
	if !r.protectionEnabled.Load() && a.Kind != "restore" {
		return result("stale", "保护总开关关闭，未执行新处置")
	}
	var previous Journal
	if err := r.DB.LoadAction(a.ID, &previous); err == nil && previous.Result != nil {
		if previous.Action != a {
			return result("stale", "重复动作编号的冻结目标不同")
		}
		return previous.Result
	} else if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return result("failed", err.Error())
	}
	if a.Kind != "restore" && a.Kind != "quarantine" && a.Kind != "deny_read" && a.Kind != "protect_delete" {
		return result("unsupported", "不支持的文件处置")
	}
	bindings, err := r.DB.Bindings()
	if err != nil {
		return result("failed", err.Error())
	}
	var binding *Binding
	for i := range bindings {
		if bindings[i].ID == a.Binding {
			binding = &bindings[i]
			break
		}
	}
	if binding == nil || !strings.EqualFold(binding.Path, a.Path) || binding.Identity != a.Identity {
		return result("stale", "关联身份或路径已变化，请刷新后重新下发")
	}
	if a.Kind == "restore" {
		if a.ExpiresAt <= float64(time.Now().UnixMilli())/1000 || a.DeviceID == "" || a.DeviceID != binding.DeviceID || a.DeviceID != r.Client.GetIdentity().DeviceID {
			return result("stale", "恢复指令已过期或设备不一致")
		}
		return r.restore(a)
	}
	if err = r.validateFrozen(a, *binding); err != nil {
		return result("stale", err.Error())
	}
	journals, err := r.DB.Journals()
	if err != nil {
		return result("failed", err.Error())
	}
	var upgrade *Journal
	for _, j := range journals {
		if (j.Active || j.Intent) && j.Action.Binding == a.Binding {
			if j.Active && j.Action.Kind == "protect_delete" && (a.Kind == "quarantine" || a.Kind == "deny_read") {
				copy := j
				upgrade = &copy
				continue
			}
			return result("failed", "该文件已有处置，请先恢复")
		}
	}
	snap, err := Snapshot(a.Path)
	if err != nil {
		return result("failed", err.Error())
	}
	if snap.Identity != a.Identity || snap.SHA256 != a.SHA256 {
		return result("stale", "文件版本已变化，未执行处置")
	}
	j := Journal{Action: a, Original: a.Path, Destination: a.Path, Intent: true}
	if upgrade != nil {
		j.Supersedes = upgrade.Action.ID
	}
	if a.Kind == "quarantine" {
		j.Destination = filepath.Join(filepath.Dir(a.Path), ".sjfx-quarantine", ID()+".blocked")
	}
	if err = r.DB.SaveAction(a.ID, j); err != nil {
		return result("failed", err.Error())
	}
	// Validate and persist intent before releasing an older delete lock. The old
	// journal stays active until a successful stronger disposition commits.
	if a.Kind == "quarantine" {
		if err = os.MkdirAll(filepath.Dir(j.Destination), 0700); err != nil {
			j.Intent = false
			j.Result = result("failed", err.Error())
			_ = r.DB.SaveAction(a.ID, j)
			return j.Result
		}
	}
	upgradeReleased, committed := false, false
	defer func() {
		if upgrade == nil || !upgradeReleased || committed {
			return
		}
		h, e := HoldProtection(upgrade.Destination, "protect_delete")
		if e == nil {
			identity, identityErr := HandleIdentity(h)
			if identityErr != nil || identity != upgrade.Action.Identity {
				_ = CloseProtection(uintptr(h))
				e = fmt.Errorf("旧锁目标已被替换，未锁定新实例")
			} else {
				r.holds[upgrade.Action.ID] = uintptr(h)
			}
		}
		if e != nil {
			r.recoveryFailed.Store(true)
			r.failure(fmt.Errorf("升级失败且旧锁恢复受阻：%w", e))
		}
	}()
	if upgrade != nil {
		if h, ok := r.holds[upgrade.Action.ID]; ok {
			if err = CloseProtection(h); err != nil {
				return result("failed", err.Error())
			}
			delete(r.holds, upgrade.Action.ID)
		}
		upgradeReleased = true
	}
	var actualIdentity, actualSHA string
	finish := func(status, detail string) map[string]any {
		j.Result = result(status, detail)
		j.Result["effect"] = actionEffect(a.Kind)
		j.Result["target"] = a
		if status == "completed" {
			j.Result["actual"] = actionActual(a, j.Destination, actualIdentity, actualSHA)
		}
		j.Intent = false
		if e := r.DB.SaveAction(a.ID, j); e != nil {
			r.failure(e)
			return result("failed", "保存处置记录失败："+e.Error())
		}
		return j.Result
	}
	h, err := HoldProtection(a.Path, a.Kind)
	if err != nil {
		return finish("failed", err.Error())
	}
	// Verify the identity of the held object, closing the path replacement race.
	actual, err := HandleIdentity(h)
	lockedHash, hashErr := HashHandle(h)
	if err != nil || actual != a.Identity || hashErr != nil || lockedHash != a.SHA256 {
		_ = CloseProtection(uintptr(h))
		return finish("stale", "锁定时文件身份发生变化")
	}
	if err = r.validateFrozen(a, *binding); err != nil {
		_ = CloseProtection(uintptr(h))
		return finish("stale", err.Error())
	}
	if a.Kind == "quarantine" {
		if err = RenameHeld(h, j.Destination); err != nil {
			_ = CloseProtection(uintptr(h))
			return finish("failed", err.Error())
		}
	}
	actualIdentity, actualSHA = actual, lockedHash
	r.holds[a.ID] = uintptr(h)
	j.Active = true
	binding.Disposition = actionEffect(a.Kind)
	if a.Kind == "quarantine" {
		binding.Path = j.Destination
	}
	j.Intent = false
	j.Result = result("completed", "文件保护已生效；助手退出期间文件句柄锁会释放")
	j.Result["actual"] = actionActual(a, j.Destination, actualIdentity, actualSHA)
	j.Result["target"], j.Result["effect"] = a, actionEffect(a.Kind)
	if err = r.DB.commitDisposition(*binding, j, upgrade); err != nil {
		// Keep the durable intent and actual lock for recovery; do not falsely
		// return completed when the binding/journal transaction failed.
		r.recoveryFailed.Store(true)
		r.failure(err)
		committed = true
		return result("failed", "处置已生效但提交失败，须恢复核对："+err.Error())
	}
	committed = true
	return j.Result
}

func (d *Database) commitDisposition(b Binding, j Journal, old *Journal) (outErr error) {
	defer func() { d.noteCapacity(outErr) }()
	tx, err := d.DB.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if old != nil {
		old.Active, old.Intent = false, false
		if j.Action.Kind != "restore" {
			old.Result = result("superseded", "删保护被已生效的更强处置替代")
		}
		raw, _ := json.Marshal(old)
		if _, err = tx.Exec("UPDATE actions SET payload=? WHERE id=?", string(raw), old.Action.ID); err != nil {
			return err
		}
	}
	br, _ := json.Marshal(b)
	jr, _ := json.Marshal(j)
	if _, err = tx.Exec("UPDATE bindings SET payload=? WHERE id=?", string(br), b.ID); err != nil {
		return err
	}
	if _, err = tx.Exec("UPDATE actions SET payload=? WHERE id=?", string(jr), j.Action.ID); err != nil {
		return err
	}
	actionName := "file." + b.Disposition
	if j.Action.Kind == "restore" {
		actionName = "file.restored"
	}
	e := Event{EventID: ID(), BindingID: b.ID, Action: actionName, Path: b.Path, Identity: b.Identity, SHA256: b.SHA256, VersionID: b.VersionID, RegistrationID: b.RegistrationID, GapEpoch: b.GapEpoch, Size: b.Size, DeviceID: b.DeviceID, OwnerID: b.OwnerID, Occurred: float64(time.Now().UnixMilli()) / 1000}
	if err = nextSequence(tx, &e); err != nil {
		return err
	}
	er, _ := json.Marshal(e)
	if err = d.checkEventCapacity(tx, len(er)); err != nil {
		// Releasing a lock has already happened. Preserve the complete restored
		// state atomically even if no more audit events fit, then record the gap.
		if _, ok := err.(*QueueCapacityError); ok && j.Action.Kind == "restore" {
			if commitErr := tx.Commit(); commitErr != nil {
				return commitErr
			}
			return err
		}
		return err
	}
	if _, err = tx.Exec("INSERT INTO events VALUES(?,?,?)", e.EventID, string(er), e.Occurred); err != nil {
		return err
	}
	return tx.Commit()
}
func (r *Runtime) restore(a Action) map[string]any {
	var old Journal
	if err := r.DB.LoadAction(a.RestoreID, &old); err != nil || old.Action.Binding != a.Binding {
		return result("failed", "没有对应的本地恢复记录")
	}
	if !old.Active || old.Result == nil || old.Result["status"] != "completed" {
		return result("stale", "只能恢复已成功且仍生效的具体处置")
	}
	if old.Action.Identity != a.Identity || old.Action.DeviceID != a.DeviceID {
		return result("stale", "恢复目标与原处置实例不一致")
	}
	j := Journal{Action: a, Original: old.Original, Destination: old.Destination, Intent: true}
	finish := func(detail string) map[string]any {
		j.Intent = false
		j.Result = result("failed", detail)
		if e := r.DB.SaveAction(a.ID, j); e != nil {
			r.failure(e)
		}
		return j.Result
	}
	if err := r.DB.SaveAction(a.ID, j); err != nil {
		return result("failed", err.Error())
	}
	if old.Action.Kind == "quarantine" {
		if _, err := os.Lstat(old.Original); err == nil {
			return finish("原路径已有文件，未覆盖；请先处理冲突")
		} else if !errors.Is(err, os.ErrNotExist) {
			return finish(err.Error())
		}
	}
	held, ok := r.holds[old.Action.ID]
	if !ok {
		h, err := HoldProtection(old.Destination, old.Action.Kind)
		if err != nil {
			return finish(err.Error())
		}
		held = uintptr(h)
		identity, identityErr := HandleIdentity(h)
		if identityErr != nil || identity != old.Action.Identity {
			_ = CloseProtection(held)
			return finish("受保护路径已替换，未作用于新实例")
		}
		r.holds[old.Action.ID] = held
	}
	identity, identityErr := HandleIdentity(windows.Handle(held))
	heldSHA, hashErr := HashHandle(windows.Handle(held))
	if identityErr != nil || identity != old.Action.Identity || hashErr != nil || heldSHA != a.SHA256 || (old.Action.Kind != "protect_delete" && heldSHA != old.Action.SHA256) {
		return finish("受保护文件身份或当前版本变化，保留原锁，请刷新后恢复")
	}
	if old.Action.Kind == "quarantine" {
		if err := RenameHeld(windows.Handle(held), old.Original); err != nil {
			return finish(err.Error())
		}
	}
	if err := CloseProtection(held); err != nil {
		return finish(err.Error())
	}
	delete(r.holds, old.Action.ID)
	// Report the actual restored object, after the move, rather than the command path.
	restored, err := Snapshot(old.Original)
	if err != nil || restored.Identity != a.Identity || restored.SHA256 != a.SHA256 {
		return finish("恢复后目标身份或内容无法确认")
	}
	bindings, err := r.DB.Bindings()
	if err != nil {
		return result("failed", err.Error())
	}
	j.Intent = false
	j.Result = result("completed", "已恢复，未覆盖其他文件")
	j.Result["effect"] = "restored_pending"
	j.Result["target"] = a
	j.Result["actual"] = actionActual(a, old.Original, restored.Identity, restored.SHA256)
	for _, b := range bindings {
		if b.ID == a.Binding {
			b.State = "restored_pending"
			b.Disposition = ""
			b.Path = old.Original
			b.SHA256 = restored.SHA256
			b.VersionID = restored.SHA256
			b.Size = restored.Size
			b.Mtime = restored.Mtime
			if err = r.DB.commitDisposition(b, j, &old); err != nil {
				if _, ok := err.(*QueueCapacityError); ok {
					j.Result["audit_gap"] = true
					_ = r.MarkGap(err.Error())
					r.healthy.Store(false)
				} else {
					r.recoveryFailed.Store(true)
					r.failure(err)
					return result("failed", "恢复已发生但提交失败，保留intent等待核对："+err.Error())
				}
			}
		}
	}
	r.dirty.Store(true)
	return j.Result
}

// Reapply only recorded per-file actions before reporting healthy after restart.
func (r *Runtime) Recover() error {
	r.operation.Lock()
	defer r.operation.Unlock()
	journals, err := r.DB.Journals()
	if err != nil {
		return err
	}
	// A durable stronger intent must be recovered before the lock it supersedes.
	sort.SliceStable(journals, func(i, k int) bool { return journals[i].Supersedes != "" && journals[k].Supersedes == "" })
	for _, j := range journals {
		if err := r.DB.LoadAction(j.Action.ID, &j); err != nil {
			return err
		}
		if !j.Active && !j.Intent {
			continue
		}
		if j.Action.Kind == "restore" {
			return fmt.Errorf("恢复操作 %s 中断，需要核对本地处置日志", j.Action.ID)
		}
		if _, ok := r.holds[j.Action.ID]; ok {
			continue
		}
		if j.Intent && !j.Active {
			_, destinationErr := os.Lstat(j.Destination)
			alreadyMoved := j.Action.Kind == "quarantine" && destinationErr == nil
			if !alreadyMoved {
				bindings, e := r.DB.Bindings()
				if e != nil {
					return e
				}
				valid := false
				for _, binding := range bindings {
					if r.protectionEnabled.Load() && binding.ID == j.Action.Binding && r.validateFrozen(j.Action, binding) == nil {
						valid = true
					}
				}
				if !valid {
					j.Intent = false
					j.Result = result("stale", "中断的未完成指令已过期或冻结目标代次变化，未补做处置")
					if e = r.DB.SaveAction(j.Action.ID, j); e != nil {
						return e
					}
					continue
				}
			}
		}
		if j.Intent && j.Action.Kind == "quarantine" {
			if _, e := os.Lstat(j.Destination); errors.Is(e, os.ErrNotExist) {
				original, e := Snapshot(j.Original)
				if e != nil {
					return e
				}
				if original.Identity != j.Action.Identity || original.SHA256 != j.Action.SHA256 {
					j.Intent = false
					j.Result = result("stale", "中断前尚未移动文件；原文件已变化，未补做处置")
					if e = r.DB.SaveAction(j.Action.ID, j); e != nil {
						return e
					}
					continue
				}
				if e = os.MkdirAll(filepath.Dir(j.Destination), 0700); e != nil {
					return e
				}
				h, holdErr := HoldProtection(j.Original, "quarantine")
				if holdErr != nil {
					return holdErr
				}
				identity, identityErr := HandleIdentity(h)
				sha, hashErr := HashHandle(h)
				if identityErr != nil || identity != j.Action.Identity || hashErr != nil || sha != j.Action.SHA256 {
					_ = CloseProtection(uintptr(h))
					return fmt.Errorf("恢复隔离时源实例已变化")
				}
				e = RenameHeld(h, j.Destination)
				_ = CloseProtection(uintptr(h))
				if e != nil {
					return e
				}
			}
		}
		snap, e := Snapshot(j.Destination)
		if e != nil {
			return fmt.Errorf("恢复文件保护失败：%w", e)
		}
		if snap.Identity != j.Action.Identity || (j.Action.Kind != "protect_delete" && snap.SHA256 != j.Action.SHA256) {
			return fmt.Errorf("受保护文件在助手离线期间变化，需人工检查")
		}
		kind := j.Action.Kind
		h, e := HoldProtection(j.Destination, kind)
		if e != nil {
			return e
		}
		identity, identityErr := HandleIdentity(h)
		sha, hashErr := HashHandle(h)
		if identityErr != nil || identity != j.Action.Identity || hashErr != nil || (j.Action.Kind != "protect_delete" && sha != j.Action.SHA256) {
			_ = CloseProtection(uintptr(h))
			return fmt.Errorf("重启重新取得句柄时身份或版本已变化")
		}
		r.holds[j.Action.ID] = uintptr(h)
		j.Active = true
		j.Intent = false
		j.Result = result("completed", "重新启动后已恢复文件锁定")
		j.Result["target"] = j.Action
		j.Result["effect"] = actionEffect(j.Action.Kind)
		j.Result["actual"] = actionActual(j.Action, j.Destination, identity, sha)
		bindings, e := r.DB.Bindings()
		if e != nil {
			return e
		}
		for _, b := range bindings {
			if b.ID == j.Action.Binding {
				b.Disposition = actionEffect(j.Action.Kind)
				b.Path = j.Destination
				var old *Journal
				if j.Supersedes != "" {
					var prior Journal
					if e = r.DB.LoadAction(j.Supersedes, &prior); e != nil {
						return e
					}
					old = &prior
				}
				if e = r.DB.commitDisposition(b, j, old); e != nil {
					return e
				}
			}
		}
	}
	return nil
}

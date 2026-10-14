//go:build windows

package endpoint

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/sys/windows"
)

const MaxStagingBytes int64 = 64 * 1024 * 1024
const MaxHashBytes int64 = 10 * 1024 * 1024 * 1024

type LocalEpoch struct {
	RegistrationID string `json:"registration_id"`
	GapEpoch       int64  `json:"gap_epoch"`
	Unresolved     bool   `json:"unresolved"`
	LocalRoot      string `json:"local_root"`
	LastComplete   string `json:"last_complete,omitempty"`
	Rechecking     bool   `json:"rechecking"`
}

func (r *Runtime) Epochs() []LocalEpoch {
	var out []LocalEpoch
	_ = r.DB.LoadState("monitoring_epochs", &out)
	return out
}
func (r *Runtime) HasGap() bool {
	for _, e := range r.Epochs() {
		if e.Unresolved {
			return true
		}
	}
	return false
}
func (r *Runtime) beginRecheck(task ManifestTask) error {
	r.epochMu.Lock()
	defer r.epochMu.Unlock()
	epochs := r.Epochs()
	found := false
	for i := range epochs {
		if epochs[i].RegistrationID == task.RegistrationID {
			if task.GapEpoch < epochs[i].GapEpoch {
				return fmt.Errorf("服务器任务缺口代次旧于本地缺口")
			}
			epochs[i].GapEpoch = task.GapEpoch
			epochs[i].Unresolved = true
			epochs[i].Rechecking = true
			epochs[i].LocalRoot = task.LocalRoot
			found = true
		}
	}
	if !found {
		epochs = append(epochs, LocalEpoch{RegistrationID: task.RegistrationID, GapEpoch: task.GapEpoch, Unresolved: true, Rechecking: true, LocalRoot: task.LocalRoot})
	}
	return r.DB.SaveState("monitoring_epochs", epochs)
}
func (r *Runtime) serverConnected() bool {
	if !r.serverAvailable.Load() {
		return false
	}
	stamp, err := time.Parse(time.RFC3339, r.serverContact())
	return err == nil && time.Since(stamp) < 30*time.Second
}
func (r *Runtime) serverContact() string {
	var s string
	_ = r.DB.LoadState("last_server_contact", &s)
	return s
}

// One durable registration gap is opened until explicit complete revalidation.
// Repeated errors within it do not create a new epoch on every heartbeat.
func (r *Runtime) MarkGap(reason string) error {
	r.epochMu.Lock()
	defer r.epochMu.Unlock()
	bindings, err := r.DB.Bindings()
	if err != nil {
		return err
	}
	epochs := r.Epochs()
	index := map[string]int{}
	for i, e := range epochs {
		index[e.RegistrationID] = i
	}
	for _, b := range bindings {
		if b.RegistrationID == "" {
			continue
		}
		if _, ok := index[b.RegistrationID]; !ok {
			index[b.RegistrationID] = len(epochs)
			epochs = append(epochs, LocalEpoch{RegistrationID: b.RegistrationID, GapEpoch: b.GapEpoch, LocalRoot: b.LocalRoot})
		}
	}
	changed := false
	for i := range epochs {
		if !epochs[i].Unresolved || epochs[i].Rechecking {
			epochs[i].GapEpoch++
			epochs[i].Unresolved = true
			epochs[i].Rechecking = false
			changed = true
		}
	}
	// Persist epochs and invalidated bindings in one transaction. Outbox pressure
	// must not stop durable invalidation; heartbeat also carries these epochs.
	tx, err := r.DB.DB.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	rawEpochs, err := json.Marshal(epochs)
	if err != nil {
		return err
	}
	if _, err = tx.Exec("INSERT INTO state VALUES('monitoring_epochs',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", string(rawEpochs)); err != nil {
		return err
	}
	for _, b := range bindings {
		if i, ok := index[b.RegistrationID]; ok {
			b.GapEpoch = epochs[i].GapEpoch
			b.State = "unverified"
			raw, e := json.Marshal(b)
			if e != nil {
				return e
			}
			if _, err = tx.Exec("UPDATE bindings SET payload=? WHERE id=?", string(raw), b.ID); err != nil {
				return err
			}
		}
	}
	var capacityErr error
	if changed {
		for _, e := range epochs {
			ev := Event{EventID: ID(), Occurred: float64(time.Now().UnixMilli()) / 1000, Action: "monitor.gap", RegistrationID: e.RegistrationID, GapEpoch: e.GapEpoch, Detail: reason}
			if err = nextSequence(tx, &ev); err != nil {
				return err
			}
			raw, e := json.Marshal(ev)
			if e != nil {
				return e
			}
			if err = r.DB.checkEventCapacity(tx, len(raw)); err != nil {
				if _, ok := err.(*QueueCapacityError); !ok {
					return err
				}
				capacityErr = err
				continue
			}
			if _, err = tx.Exec("INSERT INTO events VALUES(?,?,?)", ev.EventID, string(raw), ev.Occurred); err != nil {
				return err
			}
		}
	}
	if err = tx.Commit(); err != nil {
		return err
	}
	r.DB.noteCapacity(capacityErr)
	return capacityErr
}

type ManifestTask struct {
	RegistrationID       string `json:"registration_id"`
	TaskID               string `json:"task_id"`
	ManifestRevision     int64  `json:"manifest_revision"`
	RegistrationRevision int64  `json:"registration_revision"`
	ManifestGeneration   int64  `json:"manifest_generation"`
	GapEpoch             int64  `json:"gap_epoch"`
	DeviceID             string `json:"device_id"`
	LocalRoot            string `json:"local_root"`
}
type ManifestInstance struct {
	Binding
	RelativePath string `json:"relative_path"`
	InstanceID   string `json:"instance_id"`
}
type ManifestIssue struct {
	Path   string `json:"path"`
	Reason string `json:"reason"`
}
type ManifestReport struct {
	ManifestTask
	Complete   bool                `json:"complete"`
	Files      []ManifestInstance  `json:"files"`
	Inventory  []ManifestInstance  `json:"inventory"`
	Unmatched  []string            `json:"unmatched"`
	Errors     []ManifestIssue     `json:"errors"`
	Extra      []string            `json:"extra"`
	Candidates map[string][]string `json:"candidates"`
}

func selectedRoot(root string) (string, error) {
	absolute, err := filepath.Abs(root)
	if err != nil {
		return "", err
	}
	absolute = filepath.Clean(absolute)
	st, err := os.Lstat(absolute)
	if err != nil || st == nil {
		return "", fmt.Errorf("本地根目录不可访问：%v", err)
	}
	if (!st.IsDir() && !st.Mode().IsRegular()) || st.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf("本地根必须是实际目录或普通文件")
	}
	for path := absolute; ; path = filepath.Dir(path) {
		p, _ := windows.UTF16PtrFromString(path)
		attrs, e := windows.GetFileAttributes(p)
		if e != nil {
			return "", e
		}
		if attrs&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0 {
			return "", fmt.Errorf("本地根或父目录为链接/占位，不能核验")
		}
		if filepath.Dir(path) == path {
			break
		}
	}
	return absolute, nil
}
func withinRoot(root, path string) bool {
	relative, err := filepath.Rel(root, path)
	return err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)) && !filepath.IsAbs(relative)
}
func pathKey(s string) string { return strings.ToLower(filepath.ToSlash(filepath.Clean(s))) }

func collectManifest(ctx context.Context, task ManifestTask, files []ManifestFile, mappings map[string]string) (ManifestReport, error) {
	report := ManifestReport{ManifestTask: task, Files: []ManifestInstance{}, Inventory: []ManifestInstance{}, Unmatched: []string{}, Errors: []ManifestIssue{}, Extra: []string{}, Candidates: map[string][]string{}}
	root, err := selectedRoot(task.LocalRoot)
	if err != nil {
		return report, err
	}
	report.LocalRoot = root
	err = filepath.WalkDir(root, func(path string, d fs.DirEntry, walkErr error) error {
		if e := ctx.Err(); e != nil {
			return e
		}
		if walkErr != nil {
			report.Errors = append(report.Errors, ManifestIssue{path, walkErr.Error()})
			if d != nil && d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		p, _ := windows.UTF16PtrFromString(path)
		attrs, e := windows.GetFileAttributes(p)
		if e != nil {
			report.Errors = append(report.Errors, ManifestIssue{path, e.Error()})
			return nil
		}
		if attrs&(windows.FILE_ATTRIBUTE_REPARSE_POINT|windows.FILE_ATTRIBUTE_OFFLINE) != 0 {
			report.Errors = append(report.Errors, ManifestIssue{path, "链接、云占位或离线文件不能核验"})
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if d.IsDir() {
			if path != root && strings.HasPrefix(d.Name(), ".sjfx-quarantine") {
				return filepath.SkipDir
			}
			return nil
		}
		st, e := d.Info()
		if e != nil {
			report.Errors = append(report.Errors, ManifestIssue{path, e.Error()})
			return nil
		}
		if !st.Mode().IsRegular() || st.Size() > MaxHashBytes {
			report.Errors = append(report.Errors, ManifestIssue{path, "非普通文件或超出8GiB核验容量"})
			return nil
		}
		snap, e := SnapshotContext(ctx, path)
		if e != nil {
			report.Errors = append(report.Errors, ManifestIssue{path, e.Error()})
			return nil
		}
		rel, _ := filepath.Rel(root, path)
		report.Inventory = append(report.Inventory, ManifestInstance{Binding: snap, RelativePath: filepath.ToSlash(rel), InstanceID: Hash([]byte(pathKey(root) + "\n" + snap.Identity + "\n" + pathKey(rel)))})
		return nil
	})
	if err != nil {
		return report, err
	}
	byRel := map[string]int{}
	byHash := map[string][]int{}
	counts := map[string]int{}
	for i, local := range report.Inventory {
		byRel[pathKey(local.RelativePath)] = i
		byHash[local.SHA256] = append(byHash[local.SHA256], i)
	}
	for _, server := range files {
		counts[server.SHA256]++
	}
	used := map[int]bool{}
	for _, server := range files {
		indices := []int{}
		if specified, ok := mappings[server.ID]; ok {
			if i, exists := byRel[pathKey(specified)]; exists && report.Inventory[i].SHA256 == server.SHA256 && report.Inventory[i].Size == server.Size {
				indices = append(indices, i)
			} else {
				report.Errors = append(report.Errors, ManifestIssue{specified, "用户映射不存在或字节与服务器版本不同"})
			}
		} else if i, ok := byRel[pathKey(server.RelativePath)]; ok {
			if report.Inventory[i].SHA256 == server.SHA256 && report.Inventory[i].Size == server.Size {
				indices = append(indices, i)
			} else {
				report.Errors = append(report.Errors, ManifestIssue{server.RelativePath, "同相对路径内容不同"})
			}
		} else if counts[server.SHA256] == 1 {
			indices = append(indices, byHash[server.SHA256]...)
		} else {
			for _, i := range byHash[server.SHA256] {
				report.Candidates[server.ID] = append(report.Candidates[server.ID], report.Inventory[i].RelativePath)
			}
		}
		if len(indices) == 0 {
			report.Unmatched = append(report.Unmatched, server.ID)
			continue
		}
		for _, i := range indices {
			if used[i] {
				report.Errors = append(report.Errors, ManifestIssue{report.Inventory[i].RelativePath, "同一本地实例不能冒充两个独立服务器成员"})
				report.Unmatched = append(report.Unmatched, server.ID)
				continue
			}
			used[i] = true
			local := report.Inventory[i]
			local.FileID = server.ID
			local.RegistrationID = task.RegistrationID
			local.RegistrationRevision = task.RegistrationRevision
			if local.RegistrationRevision == 0 {
				local.RegistrationRevision = task.ManifestRevision
			}
			local.ManifestGeneration = task.ManifestGeneration
			local.GapEpoch = task.GapEpoch
			local.VersionID = local.SHA256
			local.DeviceID = task.DeviceID
			local.LocalRoot = root
			report.Files = append(report.Files, local)
		}
	}
	for i, local := range report.Inventory {
		if !used[i] {
			report.Extra = append(report.Extra, local.RelativePath)
		}
	}
	report.Complete = len(report.Unmatched) == 0 && len(report.Errors) == 0 && len(files) > 0
	return report, nil
}

func (r *Runtime) MatchRegistration(ctx context.Context, cid, root string, mappings map[string]string) error {
	r.operation.Lock()
	defer r.operation.Unlock()
	root, err := selectedRoot(root)
	if err != nil {
		return err
	}
	r.setMatch(map[string]any{"state": "running", "root": root})
	if _, err = r.refreshProtection(ctx); err != nil {
		return err
	}
	if !r.protectionEnabled.Load() {
		return r.queueSelectedMatch(ctx, cid, root, mappings)
	}
	_ = r.DB.SaveState("queued_selected_match", queuedMatch{})
	// Flush gaps before starting a current task. A disconnected endpoint cannot
	// submit an apparently current manifest while a durable gap is still queued.
	for {
		pending, e := r.DB.Pending()
		if e != nil {
			return e
		}
		if len(pending) == 0 {
			break
		}
		var ack struct {
			Accepted []string `json:"accepted"`
		}
		if err = r.Client.Post(ctx, "/agent/v2/events", map[string]any{"events": pending}, &ack, false); err != nil {
			return err
		}
		if len(ack.Accepted) == 0 {
			return fmt.Errorf("待报事件未被服务器确认，不能启动新完整核验")
		}
		if err = r.DB.Ack(ack.Accepted); err != nil {
			return err
		}
	}
	var task ManifestTask
	files := []ManifestFile{}
	for offset := 0; ; offset += 500 {
		var page struct {
			ManifestTask
			Task       ManifestTask   `json:"task"`
			Files      []ManifestFile `json:"files"`
			Collection struct {
				State string `json:"state"`
			} `json:"collection"`
		}
		input := map[string]any{"collection_id": cid, "registration_id": cid, "local_root": root, "offset": offset, "limit": 500}
		if offset > 0 {
			input["task_id"] = task.TaskID
		}
		if err = r.Client.Post(ctx, "/agent/v2/registration-manifest", input, &page, false); err != nil {
			return err
		}
		candidate := page.ManifestTask
		if candidate.TaskID == "" {
			candidate = page.Task
		}
		if offset == 0 {
			task = candidate
		} else if candidate != task {
			return fmt.Errorf("分页任务/清单/缺口代次发生变化")
		}
		if task.TaskID == "" || task.RegistrationID == "" || task.DeviceID != r.Client.GetIdentity().DeviceID || !strings.EqualFold(filepath.Clean(task.LocalRoot), root) {
			return fmt.Errorf("服务器核验任务未绑定本设备及实际选择的目录")
		}
		if page.Collection.State != "" && page.Collection.State != "ready" {
			return fmt.Errorf("服务器成员清单未完成")
		}
		files = append(files, page.Files...)
		if len(page.Files) < 500 {
			break
		}
	}
	if err = r.DB.AddRoot(root); err != nil {
		return err
	}
	if err = r.addWatches(root); err != nil {
		return err
	}
	if err = r.beginRecheck(task); err != nil {
		return err
	}
	report, err := collectManifest(ctx, task, files, mappings)
	if err != nil {
		return err
	}
	if _, err = selectedRoot(root); err != nil {
		return err
	}
	// Recheck every mapped instance at the completion boundary. The first file
	// may have changed while a long inventory was still hashing its last file.
	for _, local := range report.Files {
		snap, e := SnapshotContext(ctx, local.Path)
		if e != nil || snap.Identity != local.Identity || snap.SHA256 != local.SHA256 || snap.Size != local.Size {
			report.Errors = append(report.Errors, ManifestIssue{local.Path, "完整核验结束前版本或身份发生变化"})
			report.Complete = false
		}
	}
	for _, e := range r.Epochs() {
		if e.RegistrationID == task.RegistrationID && e.GapEpoch != task.GapEpoch {
			return fmt.Errorf("核验期间出现新缺口，未提交过期结果")
		}
	}
	var response struct {
		Bindings []Binding `json:"bindings"`
		Complete bool      `json:"complete"`
	}
	if err = r.Client.Post(ctx, "/agent/v2/manifest-complete", report, &response, false); err != nil {
		return err
	}
	// Even when server returns a receipt, never clear local gap on incomplete work.
	if !report.Complete || !response.Complete {
		r.setMatch(map[string]any{"state": "incomplete", "root": root, "unmatched": report.Unmatched, "errors": report.Errors, "extra": report.Extra, "candidates": report.Candidates})
		return nil
	}
	r.epochMu.Lock()
	defer r.epochMu.Unlock()
	for _, e := range r.Epochs() {
		if e.RegistrationID == task.RegistrationID && e.GapEpoch != task.GapEpoch {
			return fmt.Errorf("核验期间出现新缺口，结果过期")
		}
	}
	reported := map[string]ManifestInstance{}
	instanceKey := func(b Binding) string { return b.FileID + "\n" + pathKey(b.Path) + "\n" + b.Identity + "\n" + b.SHA256 }
	for _, local := range report.Files {
		reported[instanceKey(local.Binding)] = local
	}
	existing, err := r.DB.Bindings()
	if err != nil {
		return err
	}
	previousByID := map[string]Binding{}
	for _, previous := range existing {
		previousByID[previous.ID] = previous
	}
	seenIDs := map[string]bool{}
	seenInstances := map[string]bool{}
	accepted := []Binding{}
	for _, b := range response.Bindings {
		key := instanceKey(b)
		local, valid := reported[key]
		if !valid || b.ID == "" || seenIDs[b.ID] || seenInstances[key] {
			return fmt.Errorf("服务器返回重复或不在完整清单内的实例")
		}
		seenIDs[b.ID] = true
		seenInstances[key] = true
		local.Binding.ID = b.ID
		if previous, ok := previousByID[b.ID]; ok && previous.Identity == local.Identity && pathKey(previous.Path) == pathKey(local.Path) {
			local.Binding.Disposition = previous.Disposition
		}
		if b.VersionID != "" {
			local.Binding.VersionID = b.VersionID
		}
		local.Binding.State = "watching"
		accepted = append(accepted, local.Binding)
	}
	if len(response.Bindings) != len(report.Files) {
		return fmt.Errorf("服务器对应回执不完整")
	}
	for _, binding := range accepted {
		if err = r.DB.PutBinding(binding); err != nil {
			return err
		}
	}
	epochs := r.Epochs()
	for i := range epochs {
		if epochs[i].RegistrationID == task.RegistrationID {
			epochs[i].Unresolved = false
			epochs[i].Rechecking = false
			epochs[i].LastComplete = time.Now().UTC().Format(time.RFC3339)
		}
	}
	if err = r.DB.SaveState("monitoring_epochs", epochs); err != nil {
		return err
	}
	r.setMatch(map[string]any{"state": "completed", "root": root, "matched_instances": len(report.Files), "unmatched": report.Unmatched, "extra": report.Extra, "task_id": task.TaskID, "gap_epoch": task.GapEpoch})
	r.healthy.Store(true)
	r.forceHash.Store(true)
	r.dirty.Store(true)
	return nil
}

func (r *Runtime) SubmitVersion(ctx context.Context, bindingID string) error {
	r.operation.Lock()
	defer r.operation.Unlock()
	bindings, err := r.DB.Bindings()
	if err != nil {
		return err
	}
	for _, b := range bindings {
		if b.ID != bindingID {
			continue
		}
		if b.RegistrationID == "" || !withinRoot(b.LocalRoot, b.Path) {
			return fmt.Errorf("文件未绑定登记目录")
		}
		snap, e := Snapshot(b.Path)
		if e != nil {
			return e
		}
		if snap.Identity != b.Identity || snap.SHA256 != b.SHA256 {
			return fmt.Errorf("本地版本已变化，请先刷新状态再提交")
		}
		if snap.Size > MaxStagingBytes {
			return fmt.Errorf("选定版本超过64MiB提交容量，请使用服务器受限接收通道")
		}
		f, e := os.Open(b.Path)
		if e != nil {
			return e
		}
		openedIdentity, e := HandleIdentity(windows.Handle(f.Fd()))
		if e != nil || openedIdentity != snap.Identity {
			f.Close()
			return fmt.Errorf("提交快照文件身份变化")
		}
		raw, e := io.ReadAll(io.LimitReader(f, MaxStagingBytes+1))
		f.Close()
		if e != nil {
			return e
		}
		if Hash(raw) != snap.SHA256 || int64(len(raw)) != snap.Size {
			return fmt.Errorf("上传快照字节验证失败")
		}
		after, e := Snapshot(b.Path)
		if e != nil || after.Identity != snap.Identity || after.SHA256 != snap.SHA256 {
			return fmt.Errorf("提交期间文件变化")
		}
		return r.Client.Post(ctx, "/agent/v2/staging-version", map[string]any{"registration_id": b.RegistrationID, "binding_id": b.ID, "expected_sha256": snap.SHA256, "sha256": snap.SHA256, "local_identity": snap.Identity, "size": snap.Size, "gap_epoch": b.GapEpoch, "content_base64": base64.StdEncoding.EncodeToString(raw)}, nil, false)
	}
	return fmt.Errorf("所选本地实例不存在")
}

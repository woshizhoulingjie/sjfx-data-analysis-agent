//go:build windows

package endpoint

import (
	"context"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const LocalAgentRequestLimit = 1024 * 1024
const LocalRegistrationFileLimit = 1000

type LocalRegistrationCapacityError struct{ Detail string }

func (e *LocalRegistrationCapacityError) Error() string {
	return "本机助手登记覆盖不足：" + e.Detail
}

// Local mode has no remote pairing, server manifest, or separate byte upload.
// The bearer belongs to two local processes and is never available to browsers.
func NewLocalRuntime(dir, tokenFile string) (*Runtime, error) {
	if tokenFile == "" {
		tokenFile = os.Getenv("SJFX_LOCAL_AGENT_TOKEN_FILE")
	}
	if tokenFile == "" {
		return nil, fmt.Errorf("本机模式缺少受保护的令牌文件")
	}
	if _, err := FileIdentity(tokenFile); err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(tokenFile)
	if err != nil {
		return nil, err
	}
	token := strings.TrimSpace(string(raw))
	decoded, err := hex.DecodeString(token)
	if err != nil || len(decoded) != 32 {
		return nil, fmt.Errorf("本机令牌必须是32随机字节的64字符hex文本")
	}
	r, err := NewRuntime("http://127.0.0.1", dir, false)
	if err != nil {
		return nil, err
	}
	var mode string
	_ = r.DB.LoadState("runtime_mode", &mode)
	bindings, loadErr := r.DB.Bindings()
	if loadErr != nil || (mode != "local-agent-v1" && len(bindings) != 0) || len(r.Client.GetIdentity().PrivateKey) != 0 {
		r.watcher.Close()
		r.DB.DB.Close()
		return nil, fmt.Errorf("拒绝将既有远端助手状态目录转为本机模式")
	}
	if err = r.DB.SaveState("runtime_mode", "local-agent-v1"); err != nil {
		r.watcher.Close()
		r.DB.DB.Close()
		return nil, err
	}
	r.localToken = []byte(token)
	r.ListenAddr = "127.0.0.1:18766"
	var deviceID string
	_ = r.DB.LoadState("local_device_id", &deviceID)
	r.Client.SetIdentity(Identity{DeviceID: deviceID})
	return r, nil
}

type LocalRegisterFile struct {
	BindingID string `json:"binding_id"`
	FileID    string `json:"file_id"`
	Path      string `json:"local_path"`
	SHA256    string `json:"sha256"`
	VersionID string `json:"version_id"`
	Size      int64  `json:"size"`
	Identity  string `json:"identity"`
}
type LocalRegistration struct {
	DeviceID         string              `json:"device_id"`
	OwnerID          string              `json:"owner_id"`
	RegistrationID   string              `json:"registration_id"`
	ManifestRevision int64               `json:"manifest_revision"`
	GapEpoch         int64               `json:"gap_epoch"`
	LocalRoot        string              `json:"local_root"`
	Files            []LocalRegisterFile `json:"files"`
}

// RegisterLocal validates the actual selected bytes and preserves the caller's
// core instance ID. Python's st_dev/st_ino string is not a Windows action identity.
func (r *Runtime) RegisterLocal(ctx context.Context, in LocalRegistration) ([]Binding, error) {
	return r.registerLocal(ctx, in, LocalRegistrationFileLimit, 8*1024*1024*1024, nil)
}

func (r *Runtime) registerLocal(ctx context.Context, in LocalRegistration, fileLimit int, byteLimit int64, beforeCommit func(*sql.Tx, []Binding) error) ([]Binding, error) {
	r.operation.Lock()
	defer r.operation.Unlock()
	r.epochMu.Lock()
	defer r.epochMu.Unlock()
	if len(r.localToken) == 0 {
		return nil, fmt.Errorf("本机登记仅用于独立本机模式")
	}
	if len(in.Files) > fileLimit {
		return nil, &LocalRegistrationCapacityError{"单个完整登记最多1000个文件；不能按部分成功声称完整覆盖"}
	}
	var expectedBytes int64
	for _, f := range in.Files {
		if f.Size < 0 || f.Size > MaxHashBytes || expectedBytes > byteLimit-f.Size {
			return nil, &LocalRegistrationCapacityError{"单个完整登记的稳定快照总量超过8GiB"}
		}
		expectedBytes += f.Size
	}
	if in.DeviceID == "" || in.OwnerID == "" || in.RegistrationID == "" || in.ManifestRevision < 1 || in.GapEpoch < 0 || len(in.Files) == 0 {
		return nil, fmt.Errorf("本机登记身份、修订或实例数无效")
	}
	deviceID := r.Client.GetIdentity().DeviceID
	if deviceID != "" && deviceID != in.DeviceID {
		return nil, fmt.Errorf("本机设备身份不一致")
	}
	var ownerID string
	_ = r.DB.LoadState("local_owner_id", &ownerID)
	if ownerID != "" && ownerID != in.OwnerID {
		return nil, fmt.Errorf("助手候选state已属于另一账号，禁止跨账号改绑")
	}
	root, err := selectedRoot(in.LocalRoot)
	if err != nil {
		return nil, err
	}
	epochs := r.Epochs()
	index := -1
	for i, e := range epochs {
		if e.RegistrationID == in.RegistrationID {
			index = i
			if in.GapEpoch < e.GapEpoch {
				return nil, fmt.Errorf("登记缺口代次已旧，需重新核验")
			}
		}
	}
	existing, err := r.DB.Bindings()
	if err != nil {
		return nil, err
	}
	byID := map[string]Binding{}
	for _, b := range existing {
		byID[b.ID] = b
	}
	seen := map[string]bool{}
	seenPaths := map[string]bool{}
	out := []Binding{}
	var bytes int64
	for _, f := range in.Files {
		if err = ctx.Err(); err != nil {
			return nil, err
		}
		if f.FileID == "" || f.BindingID != "local:"+f.FileID || seen[f.BindingID] || len(f.SHA256) != 64 || f.VersionID != f.SHA256 {
			return nil, fmt.Errorf("文件实例或内容版本契约无效")
		}
		path, e := filepath.Abs(f.Path)
		if e != nil || !withinRoot(root, path) {
			return nil, fmt.Errorf("本机路径不在授权根目录")
		}
		key := strings.ToLower(filepath.Clean(path))
		if seenPaths[key] {
			return nil, fmt.Errorf("同一路径重复绑定")
		}
		seenPaths[key] = true
		seen[f.BindingID] = true
		snap, e := SnapshotContext(ctx, path)
		if e != nil {
			return nil, e
		}
		bytes += snap.Size
		if bytes > byteLimit {
			return nil, fmt.Errorf("完整登记快照超过8GiB容量")
		}
		if snap.Identity != f.Identity || snap.SHA256 != f.SHA256 || snap.Size != f.Size {
			return nil, fmt.Errorf("核心与助手读取的文件实例或内容版本不一致，拒绝登记")
		}
		snap.ID, snap.FileID, snap.VersionID = f.BindingID, f.FileID, f.VersionID
		snap.OwnerID = in.OwnerID
		snap.DeviceID = in.DeviceID
		snap.RegistrationID = in.RegistrationID
		snap.RegistrationRevision = in.ManifestRevision
		snap.ManifestGeneration = in.ManifestRevision
		snap.GapEpoch = in.GapEpoch
		snap.LocalRoot = root
		if old, ok := byID[snap.ID]; ok {
			if old.OwnerID != "" && old.OwnerID != in.OwnerID {
				return nil, fmt.Errorf("文件实例账号不一致")
			}
			if old.RegistrationID != in.RegistrationID || old.RegistrationRevision > in.ManifestRevision {
				return nil, fmt.Errorf("登记成员或修订发生冲突")
			}
			if old.Disposition != "" && old.Identity != snap.Identity {
				return nil, fmt.Errorf("处置中实例已替换，禁止转移旧处置")
			}
			snap.Disposition = old.Disposition
		}
		out = append(out, snap)
	}
	// Revalidate every member immediately before durable commit. A failed member
	// leaves the complete registration unmodified instead of accepting a partial set.
	for _, b := range out {
		snap, e := SnapshotContext(ctx, b.Path)
		if e != nil || snap.Identity != b.Identity || snap.SHA256 != b.SHA256 || snap.Size != b.Size {
			return nil, fmt.Errorf("登记期间实例或字节变化")
		}
	}
	epoch := LocalEpoch{RegistrationID: in.RegistrationID, GapEpoch: in.GapEpoch, LocalRoot: root, LastComplete: time.Now().UTC().Format(time.RFC3339)}
	if index < 0 {
		epochs = append(epochs, epoch)
	} else {
		epochs[index] = epoch
	}
	tx, err := r.DB.DB.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	for _, b := range out {
		raw, _ := json.Marshal(b)
		if _, err = tx.Exec("INSERT INTO bindings VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", b.ID, string(raw)); err != nil {
			return nil, err
		}
		if _, err = tx.Exec("INSERT OR IGNORE INTO versions VALUES(?,?)", b.ID, b.SHA256); err != nil {
			return nil, err
		}
	}
	if _, err = tx.Exec("INSERT OR IGNORE INTO roots VALUES(?)", root); err != nil {
		return nil, err
	}
	for key, value := range map[string]any{"local_device_id": in.DeviceID, "local_owner_id": in.OwnerID, "monitoring_epochs": epochs} {
		raw, _ := json.Marshal(value)
		if _, err = tx.Exec("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, string(raw)); err != nil {
			return nil, err
		}
	}
	if beforeCommit != nil {
		if err = beforeCommit(tx, out); err != nil {
			return nil, err
		}
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	r.Client.SetIdentity(Identity{DeviceID: in.DeviceID})
	r.forceHash.Store(true)
	r.dirty.Store(true)
	return out, nil
}

type LocalSyncRequest struct {
	DeviceID          string   `json:"device_id"`
	ProtectionEnabled bool     `json:"protection_enabled"`
	AckEventIDs       []string `json:"ack_event_ids"`
	AckResultIDs      []string `json:"ack_result_ids"`
	Actions           []Action `json:"actions"`
}
type LocalActionResult struct {
	ActionID string         `json:"action_id"`
	Action   Action         `json:"action"`
	Result   map[string]any `json:"result"`
}

func (r *Runtime) localSync(ctx context.Context, in LocalSyncRequest) (map[string]any, error) {
	if in.DeviceID == "" || in.DeviceID != r.Client.GetIdentity().DeviceID {
		return nil, fmt.Errorf("本机设备身份不一致或尚未登记")
	}
	if len(in.Actions) > 100 || len(in.AckEventIDs) > 1000 || len(in.AckResultIDs) > 1000 {
		return nil, fmt.Errorf("同步批次超出容量")
	}
	for _, a := range in.Actions {
		if a.ID == "" || len(a.ID) > 128 {
			return nil, fmt.Errorf("本机动作编号无效")
		}
	}
	r.protectionEnabled.Store(in.ProtectionEnabled)
	previously := r.collecting.Swap(in.ProtectionEnabled)
	if err := r.DB.SaveState("collection_enabled", in.ProtectionEnabled); err != nil {
		return nil, err
	}
	r.serverAvailable.Store(true)
	r.DB.SaveState("last_server_contact", time.Now().UTC().Format(time.RFC3339))
	if previously && !in.ProtectionEnabled {
		if err := r.MarkGap("保护总开关关闭：监控覆盖失效"); err != nil {
			return nil, err
		}
		for _, path := range r.watcher.WatchList() {
			_ = r.watcher.Remove(path)
		}
		r.mu.Lock()
		r.watched = map[string]bool{}
		r.mu.Unlock()
		r.healthy.Store(false)
	}
	if err := r.DB.Ack(in.AckEventIDs); err != nil {
		return nil, err
	}
	// Acknowledgements remove only specific durable result rows, never journals.
	for _, id := range append(in.AckResultIDs, in.AckEventIDs...) {
		if _, err := r.DB.DB.Exec("DELETE FROM local_results WHERE id=?", id); err != nil {
			return nil, err
		}
	}
	if in.ProtectionEnabled {
		if err := r.Reconcile(); err != nil {
			r.failure(err)
		}
		if r.AccessAudit {
			if err := r.collectLocalAccess(ctx); err != nil {
				r.auditFailure(err)
			}
		}
	}
	for _, a := range in.Actions {
		var cached LocalActionResult
		var raw string
		err := r.DB.DB.QueryRow("SELECT payload FROM local_results WHERE id=?", a.ID).Scan(&raw)
		if err == nil {
			if err = json.Unmarshal([]byte(raw), &cached); err != nil {
				return nil, err
			}
			if cached.Action != a {
				return nil, fmt.Errorf("动作编号重复且冻结目标不同")
			}
			continue
		} else if err != sql.ErrNoRows {
			return nil, err
		}
		var count int
		if err = r.DB.DB.QueryRow("SELECT COUNT(*) FROM local_results").Scan(&count); err != nil {
			return nil, err
		}
		if count >= DefaultEventLimit {
			return nil, fmt.Errorf("处置结果容量不足；先确认已收到结果")
		}
		cached = LocalActionResult{ActionID: a.ID, Action: a, Result: r.Execute(a)}
		// The full frozen command is retained in storage for idempotency.
		payload, _ := json.Marshal(struct {
			ActionID string         `json:"action_id"`
			Action   Action         `json:"action"`
			Result   map[string]any `json:"result"`
		}{a.ID, a, cached.Result})
		if _, err = r.DB.DB.Exec("INSERT INTO local_results VALUES(?,?)", a.ID, string(payload)); err != nil {
			return nil, err
		}
	}
	events, err := r.DB.Pending()
	if err != nil {
		return nil, err
	}
	rows, err := r.DB.DB.Query("SELECT payload FROM local_results ORDER BY rowid LIMIT 100")
	if err != nil {
		return nil, err
	}
	results := []LocalActionResult{}
	for rows.Next() {
		var raw string
		if err = rows.Scan(&raw); err != nil {
			rows.Close()
			return nil, err
		}
		var v LocalActionResult
		if err = json.Unmarshal([]byte(raw), &v); err != nil {
			rows.Close()
			return nil, err
		}
		results = append(results, v)
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()
	if events == nil {
		events = []Event{}
	}
	publicResults := []map[string]any{}
	for _, v := range results {
		publicResults = append(publicResults, map[string]any{"action_id": v.ActionID, "result": v.Result})
	}
	return map[string]any{"ok": true, "events": events, "results": publicResults, "health": r.Status()}, nil
}

func (r *Runtime) handleLocalAgent(w http.ResponseWriter, q *http.Request) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	reply := func(code int, v any) { w.WriteHeader(code); _ = json.NewEncoder(w).Encode(v) }
	host, _, err := net.SplitHostPort(q.RemoteAddr)
	ip := net.ParseIP(host)
	authorization := q.Header.Get("Authorization")
	bearer := strings.TrimPrefix(authorization, "Bearer ")
	_, hasOrigin := q.Header["Origin"]
	if err != nil || ip == nil || !ip.IsLoopback() || hasOrigin || !strings.HasPrefix(authorization, "Bearer ") || len(r.localToken) == 0 || subtle.ConstantTimeCompare([]byte(bearer), r.localToken) != 1 {
		reply(403, map[string]any{"ok": false, "error": "本机进程认证失败"})
		return
	}
	if q.Method != "POST" || !strings.HasPrefix(q.Header.Get("Content-Type"), "application/json") {
		reply(400, map[string]any{"ok": false, "error": "无效本机请求"})
		return
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, q.Body, LocalAgentRequestLimit))
	decoder.DisallowUnknownFields()
	capacityReply := func(detail string) {
		reply(413, map[string]any{"ok": false, "error": detail, "code": "local_agent_capacity_exceeded", "coverage_complete": false, "limit_bytes": LocalAgentRequestLimit, "limit_files": LocalRegistrationFileLimit})
	}
	switch q.URL.Path {
	case "/agent-local/v1/health":
		status := r.Status()
		delete(status, "bindings")
		reply(200, status)
	case "/agent-local/v1/register/begin", "/agent-local/v1/register/batch", "/agent-local/v1/register/commit", "/agent-local/v1/register/result", "/agent-local/v1/register/abort", "/agent-local/v1/register/finish":
		out, e := r.localRegistrationTransaction(q.Context(), q.URL.Path, decoder)
		if e != nil {
			var capacityErr *LocalRegistrationCapacityError
			var limitErr *http.MaxBytesError
			if errors.As(e, &capacityErr) || errors.As(e, &limitErr) {
				capacityReply(e.Error())
				return
			}
			reply(409, map[string]any{"ok": false, "coverage_complete": false, "error": e.Error()})
			return
		}
		reply(200, out)
	case "/agent-local/v1/register":
		var in LocalRegistration
		if err = decoder.Decode(&in); err != nil {
			var limitErr *http.MaxBytesError
			if errors.As(err, &limitErr) {
				capacityReply("完整本机登记请求超过1MiB；未接受部分登记")
				return
			}
			reply(400, map[string]any{"error": err.Error()})
			return
		}
		bindings, e := r.RegisterLocal(q.Context(), in)
		if e != nil {
			var capacityErr *LocalRegistrationCapacityError
			if errors.As(e, &capacityErr) {
				capacityReply(e.Error())
				return
			}
			reply(409, map[string]any{"ok": false, "error": e.Error()})
			return
		}
		reply(200, map[string]any{"ok": true, "bindings": bindings, "gap_epoch": in.GapEpoch})
	case "/agent-local/v1/sync":
		var in LocalSyncRequest
		if err = decoder.Decode(&in); err != nil {
			reply(400, map[string]any{"error": err.Error()})
			return
		}
		out, e := r.localSync(q.Context(), in)
		if e != nil {
			reply(409, map[string]any{"ok": false, "error": e.Error()})
			return
		}
		reply(200, out)
	default:
		reply(404, map[string]any{"error": "本机接口不存在"})
	}
}

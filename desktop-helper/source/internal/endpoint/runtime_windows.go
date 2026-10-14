//go:build windows

package endpoint

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/fsnotify/fsnotify"
)

type Runtime struct {
	recoveryFailed    atomic.Bool
	dirtyPaths        map[string]bool
	forceHash         atomic.Bool
	protectionEnabled atomic.Bool
	collecting        atomic.Bool
	serverAvailable   atomic.Bool
	auditPending      atomic.Int64
	auditRunner       func(context.Context, string, any, any) error
	auditPolicy       func() (uint32, error)
	localToken        []byte
	localAPIMu        sync.Mutex
	auditConfigured   map[string]bool

	AccessAudit    bool
	auditHealthy   atomic.Bool
	auditLastError string

	Client        *Client
	DB            *Database
	Dir, Origin   string
	ListenAddr    string
	mu            sync.Mutex
	operation     sync.Mutex
	epochMu       sync.Mutex
	watcher       *fsnotify.Watcher
	watched       map[string]bool
	holds         map[string]uintptr
	healthy       atomic.Bool
	lost          atomic.Uint64
	dirty         atomic.Bool
	lastError     string
	matching      map[string]any
	matchCancel   context.CancelFunc
	lastDiscovery time.Time
}

func NewRuntime(server, dir string, allowHTTP bool) (*Runtime, error) {
	u, err := url.Parse(strings.TrimRight(server, "/"))
	if err != nil || u.Host == "" || u.RawQuery != "" || u.User != nil || u.Path != "" {
		return nil, fmt.Errorf("服务器地址必须为网站根地址")
	}
	if u.Scheme != "https" && !(u.Scheme == "http" && (allowHTTP || u.Hostname() == "127.0.0.1" || u.Hostname() == "localhost")) {
		return nil, fmt.Errorf("请使用 HTTPS；可信内网 HTTP 需显式启用 --allow-http")
	}
	db, err := OpenDatabase(dir)
	if err != nil {
		return nil, err
	}
	w, err := fsnotify.NewBufferedWatcher(65536)
	if err != nil {
		db.DB.Close()
		return nil, err
	}
	id, err := LoadIdentity(filepath.Join(dir, "identity.bin"))
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		w.Close()
		db.DB.Close()
		return nil, err
	}
	r := &Runtime{Client: &Client{BaseURL: u.String(), Identity: id, HTTP: &http.Client{Timeout: 30 * time.Second,
		CheckRedirect: func(req *http.Request, via []*http.Request) error { return http.ErrUseLastResponse }}}, DB: db, Dir: dir, Origin: u.String(), watcher: w, watched: map[string]bool{}, holds: map[string]uintptr{}, matching: map[string]any{"state": "idle"}}
	var collecting bool
	_ = db.LoadState("collection_enabled", &collecting)
	r.collecting.Store(collecting)
	return r, nil
}
func (r *Runtime) failure(err error) {
	r.healthy.Store(false)
	r.forceHash.Store(true)
	r.mu.Lock()
	r.lastError = err.Error()
	r.mu.Unlock()
	_ = r.DB.Enqueue(Event{Action: "monitor.error", Detail: err.Error()})
	_ = r.MarkGap(err.Error())
	r.dirty.Store(true)
}
func (r *Runtime) Healthy() bool {
	return r.healthy.Load() && r.protectionEnabled.Load() && r.serverConnected() && !r.HasGap() && !r.recoveryFailed.Load()
}
func (r *Runtime) Status() map[string]any {
	r.mu.Lock()
	defer r.mu.Unlock()
	bindings, _ := r.DB.Bindings()
	status := map[string]any{"ok": true, "device_id": r.Client.GetIdentity().DeviceID, "server": r.Origin, "healthy": r.Healthy(), "protection_enabled": r.protectionEnabled.Load(), "has_unresolved_gaps": r.HasGap(), "access_audit_requested": r.AccessAudit, "access_audit_healthy": r.auditHealthy.Load(), "watch_roots": len(r.watched), "binding_count": len(bindings), "bindings": bindings, "queue_depth": r.DB.Depth(), "oldest_event_age_seconds": r.DB.OldestEventAge(), "lost_events": r.lost.Load(), "last_error": r.lastError, "matching": r.matching, "monitoring_epochs": r.Epochs(), "last_server_contact": r.serverContact(), "server_connected": r.serverConnected(), "capabilities": []string{"selected_directory_file_changes", "file_identity", "signed_complete_manifest", "persistent_queue", "file_read_lock", "file_delete_lock", "quarantine_restore", "explicit_version_staging"}, "coverage_limits": []string{"只采集用户选择目录的变化；并非全操作系统审计", "访问审计仅在请求且Windows权限事件可用时生效", "句柄锁随助手退出释放；删保护允许原位写入，但可能阻止替换式保存", "不保证阻止任意应用外发；未知内容需用户明确提交至服务器检查"}, "agent_version": "0.3.1-reliability-candidate"}
	status["collection_enabled"] = r.collectionEnabled()
	status["access_audit_pending_count"] = r.auditPending.Load()
	status["directory_monitor_healthy"] = r.Healthy()
	status["access_audit_error"] = r.auditLastError
	status["access_audit_backend"] = "Windows Security 4656/4663 and selected-file SACL"
	status["local_mode"] = len(r.localToken) != 0
	var capacityRejections int64
	_ = r.DB.DB.QueryRow("SELECT CAST(value AS INTEGER) FROM state WHERE key='event_capacity_rejections'").Scan(&capacityRejections)
	status["event_capacity_rejections"] = capacityRejections
	if len(r.localToken) != 0 {
		status["registration_limits"] = map[string]any{"max_request_bytes": LocalAgentRequestLimit, "max_files_per_complete_registration": LocalRegistrationFileLimit, "max_snapshot_bytes_per_registration": MaxHashBytes, "event_limit": r.DB.EventLimit, "event_bytes_limit": r.DB.EventBytes, "candidate_limit": r.DB.CandidateLimit, "partial_registration_is_complete": false}
		status["agent_version"] = "0.5.0-desktop-candidate"
		limits := status["registration_limits"].(map[string]any)
		limits["transaction_registration_supported"] = true
		limits["max_files_per_transaction"] = LocalTransactionFileLimit
		limits["max_snapshot_bytes_per_transaction"] = LocalTransactionByteLimit()
		limits["max_file_bytes"] = MaxHashBytes
		limits["max_batch_files"] = LocalTransactionBatchLimit
		limits["max_snapshot_bytes_per_registration"] = int64(8 * 1024 * 1024 * 1024)
		active := 0
		for _, b := range bindings {
			if b.Disposition != "" {
				active++
			}
		}
		status["active_dispositions"] = active
		delete(status, "bindings")
		status["server"] = "local-core-process"
		status["capabilities"] = []string{"local_core_shared_instance_version", "selected_directory_file_changes", "file_identity", "persistent_queue", "file_read_lock", "file_delete_lock", "quarantine_restore", "authenticated_local_process_api"}
		status["coverage_limits"] = []string{"只监控明确登记的本机根目录，监控与Security日志是事后证据，不是全系统事前过滤", "未覆盖第三方应用外发、邮件网关、打印与剪贴板", "内容分析由本机SJFX核心在真实读取前检查；助手不通过副本匹配或独立上传判定安全", "访问审计须权限与策略可用且全部文件已配置，句柄锁随助手退出释放，删锁允许原位编辑"}
	}
	return status
}
func (r *Runtime) Enroll(ctx context.Context, token string) error {
	r.operation.Lock()
	defer r.operation.Unlock()
	if r.Client.GetIdentity().DeviceID != "" {
		return fmt.Errorf("当前助手已绑定设备；请在服务器撤销后通过本机管理操作重新绑定")
	}
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return err
	}
	name, _ := os.Hostname()
	var out struct {
		DeviceID string `json:"device_id"`
	}
	err = r.Client.Post(ctx, "/agent/v2/enroll", map[string]any{"token": token, "public_key": base64.StdEncoding.EncodeToString(key.Public().(ed25519.PublicKey)), "name": name}, &out, true)
	if err != nil {
		return err
	}
	id := Identity{DeviceID: out.DeviceID, PrivateKey: key}
	if err = SaveIdentity(filepath.Join(r.Dir, "identity.bin"), id); err != nil {
		return err
	}
	r.mu.Lock()
	r.Client.SetIdentity(id)
	r.mu.Unlock()
	r.dirty.Store(true)
	return nil
}

type ManifestFile struct {
	ID           string `json:"id"`
	SHA256       string `json:"sha256"`
	Size         int64  `json:"size"`
	RelativePath string `json:"relative_path"`
	Risk         string `json:"risk"`
}

func (r *Runtime) Match(ctx context.Context, cid, root string) error {
	return r.MatchRegistration(ctx, cid, root, nil)
}
func (r *Runtime) setMatch(s map[string]any) { r.mu.Lock(); r.matching = s; r.mu.Unlock() }
func (r *Runtime) addWatches(root string) error {
	if st, err := os.Stat(root); err == nil && st.Mode().IsRegular() {
		parent := filepath.Dir(root)
		if err = r.watcher.Add(parent); err != nil {
			return err
		}
		r.mu.Lock()
		r.watched[parent] = true
		r.mu.Unlock()
		return nil
	}
	r.mu.Lock()
	r.watched = map[string]bool{}
	for _, path := range r.watcher.WatchList() {
		r.watched[path] = true
	}
	r.mu.Unlock()
	return filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if strings.HasPrefix(d.Name(), ".sjfx-quarantine") {
				return filepath.SkipDir
			}
			r.mu.Lock()
			known := r.watched[path]
			r.mu.Unlock()
			if !known {
				if err = r.watcher.Add(path); err != nil {
					return err
				}
				r.mu.Lock()
				r.watched[path] = true
				r.mu.Unlock()
			}
		}
		return nil
	})
}

func (r *Runtime) Reconcile() error {
	if !r.collectionEnabled() {
		return nil
	}
	r.operation.Lock()
	defer r.operation.Unlock()
	r.mu.Lock()
	dirtyPaths := r.dirtyPaths
	r.dirtyPaths = map[string]bool{}
	r.mu.Unlock()
	forceHash := r.forceHash.Swap(false)
	roots, err := r.DB.Roots()
	if err != nil {
		return err
	}
	bindings, err := r.DB.Bindings()
	if err != nil {
		return err
	}
	locations := map[string][]string{}
	paths := map[string]os.FileInfo{}
	for _, root := range roots {
		if err = r.addWatches(root); err != nil {
			return err
		}
		err = filepath.WalkDir(root, func(path string, d fs.DirEntry, e error) error {
			if e != nil {
				return e
			}
			if d.IsDir() {
				if strings.HasPrefix(d.Name(), ".sjfx-quarantine") {
					return filepath.SkipDir
				}
				return nil
			}
			if d.Type()&os.ModeSymlink != 0 {
				return nil
			}
			info, e := d.Info()
			if e != nil {
				return e
			}
			if !info.Mode().IsRegular() {
				return nil
			}
			identity, e := FileIdentity(path)
			if e != nil {
				return e
			}
			paths[path] = info
			locations[identity] = append(locations[identity], path)
			return nil
		})
		if err != nil {
			return err
		}
	}
	for _, b := range bindings {
		if b.Disposition == "quarantine" || b.Disposition == "read_lock" || (b.State == "protected" && b.Disposition == "") {
			continue
		}
		old := b.Path
		action := ""
		contentChanged := false
		info, exists := paths[b.Path]
		if !exists {
			candidates := []string{}
			for _, candidate := range locations[b.Identity] {
				if b.LocalRoot == "" || withinRoot(b.LocalRoot, candidate) {
					candidates = append(candidates, candidate)
				}
			}
			if len(candidates) == 1 {
				b.Path = candidates[0]
				info = paths[b.Path]
				exists = true
				action = "file.renamed"
			} else {
				if b.State != "unavailable" {
					b.State = "unavailable"
					if err = r.DB.Record(b, Event{BindingID: b.ID, Action: "file.unavailable", Path: old, Identity: b.Identity, Detail: "原路径不可访问；可能删除、移出监视范围或存储离线"}); err != nil {
						return err
					}
				}
				continue
			}
		}
		identity, e := FileIdentity(b.Path)
		if e != nil {
			return e
		}
		if b.State == "unavailable" {
			action = "file.reappeared"
		}
		changed := action != "" || forceHash || dirtyPaths[b.Path] || info.Size() != b.Size || info.ModTime().UnixNano() != b.Mtime || identity != b.Identity
		if changed {
			snap, e := Snapshot(b.Path)
			if e != nil {
				return e
			}
			contentChanged = snap.Identity != b.Identity || snap.SHA256 != b.SHA256 || snap.Mtime != b.Mtime || snap.Size != b.Size
			if snap.Identity != b.Identity {
				action = "file.replaced"
			} else if action == "" && (snap.SHA256 != b.SHA256 || snap.Mtime != b.Mtime || snap.Size != b.Size) {
				action = "file.modified"
			}
			b.Identity = snap.Identity
			b.SHA256 = snap.SHA256
			b.Size = snap.Size
			b.Mtime = snap.Mtime
		}
		if contentChanged {
			b.State = "changed"
			b.VersionID = b.SHA256
		} else if b.State != "unverified" && b.State != "changed" && b.State != "restored_pending" {
			b.State = "watching"
		}
		if action != "" {
			if err = r.DB.Record(b, Event{BindingID: b.ID, Action: action, Path: b.Path, OldPath: old, Identity: b.Identity, SHA256: b.SHA256}); err != nil {
				return err
			}
		}
	}
	r.healthy.Store(true)
	r.mu.Lock()
	r.lastError = ""
	r.mu.Unlock()
	return nil
}

func (r *Runtime) Run(ctx context.Context) error {
	if err := r.MarkGap("助手启动或重启：离线期间存在监控缺口，须显式完整重新核验"); err != nil {
		return err
	}
	listenAddr := r.ListenAddr
	if listenAddr == "" {
		listenAddr = "127.0.0.1:18765"
	}
	listener, err := net.Listen("tcp", listenAddr)
	if err != nil {
		return err
	}
	server := &http.Server{Handler: r.Handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 0, MaxHeaderBytes: 8192}
	go func() { _ = server.Serve(listener) }()
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case ev, ok := <-r.watcher.Events:
				if !ok {
					return
				}
				if !r.collectionEnabled() {
					continue
				}
				// Persist observations before coalescing dirty paths; offline edits
				// survive restart even when reconciliation merges several writes.
				selected, e := r.enqueueWatchObservation(ev.Name, ev.Op.String())
				if e != nil {
					r.lost.Add(1)
					r.failure(e)
				}
				if !selected {
					continue
				}
				r.dirty.Store(true)
				r.healthy.Store(false)
				r.mu.Lock()
				if r.dirtyPaths == nil {
					r.dirtyPaths = map[string]bool{}
				}
				r.dirtyPaths[ev.Name] = true
				r.mu.Unlock()
				if ev.Op&fsnotify.Create != 0 {
					if st, e := os.Stat(ev.Name); e == nil && st.IsDir() {
						if e = r.addWatches(ev.Name); e != nil {
							r.failure(e)
						}
					}
				}
			case e, ok := <-r.watcher.Errors:
				if !ok {
					return
				}
				r.lost.Add(1)
				r.failure(e)

			}
		}
	}()
	if err = r.Recover(); err != nil {
		r.recoveryFailed.Store(true)
		r.failure(err)
	}
	r.dirty.Store(true)
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	lastSync := time.Time{}
	lastSweep := time.Time{}
	r.forceHash.Store(true)
	defer func() {
		stop, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = server.Shutdown(stop)
		_ = r.watcher.Close()
		for _, h := range r.holds {
			_ = CloseProtection(h)
		}
		_ = r.DB.DB.Close()
	}()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			if r.collectionEnabled() && (r.dirty.Swap(false) || time.Since(lastSweep) > 60*time.Second) {
				lastSweep = time.Now()
				if err = r.Reconcile(); err != nil {
					r.failure(err)
				}
			}
			if r.collectionEnabled() && r.AccessAudit {
				if e := r.collectLocalAccess(ctx); e != nil {
					r.auditFailure(e)
				}
			}
			if time.Since(lastSync) > 10*time.Second {
				if len(r.localToken) == 0 && r.Client.GetIdentity().DeviceID != "" {
					if err = r.Sync(ctx); err != nil {
						r.mu.Lock()
						r.lastError = err.Error()
						r.mu.Unlock()
					}
				}
				lastSync = time.Now()
			}
		}
	}
}
func (r *Runtime) Sync(ctx context.Context) error {
	response, err := r.refreshProtection(ctx)
	if err != nil {
		return err
	}
	if !r.protectionEnabled.Load() {
		// OFF preserves locks and permits authenticated recovery of their exact journals.
		for _, a := range response.Actions {
			if a.Kind != "restore" {
				continue
			}
			out := r.Execute(a)
			if e := r.Client.Post(ctx, "/agent/v2/action-result", map[string]any{"action_id": a.ID, "result": out}, nil, false); e != nil {
				return e
			}
		}
		return nil
	}
	if r.recoveryFailed.Load() {
		if e := r.Recover(); e == nil {
			r.recoveryFailed.Store(false)
		} else {
			r.failure(e)
		}
	}
	if r.AccessAudit {
		e := r.collectLocalAccess(ctx)
		if e != nil {
			r.failure(e)
		}
	}
	events, err := r.DB.Pending()
	if err != nil {
		return err
	}
	if len(events) > 0 {
		var response struct {
			Accepted []string `json:"accepted"`
		}
		if err = r.Client.Post(ctx, "/agent/v2/events", map[string]any{"events": events}, &response, false); err != nil {
			return err
		}
		if err = r.DB.Ack(response.Accepted); err != nil {
			return err
		}
	}
	for _, a := range response.Actions {
		if _, e := r.refreshProtection(ctx); e != nil {
			return e
		}
		result := r.Execute(a)
		if err = r.Client.Post(ctx, "/agent/v2/action-result", map[string]any{"action_id": a.ID, "result": result}, nil, false); err != nil {
			return err
		}
	}
	r.resumeQueuedMatch(ctx)
	if time.Since(r.lastDiscovery) > 60*time.Second {
		r.lastDiscovery = time.Now()
		if err = r.DiscoverCopies(ctx); err != nil {
			r.failure(err)
			return err
		}
	}
	return nil
}

func (r *Runtime) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) {
		if strings.HasPrefix(q.URL.Path, "/agent-local/") {
			r.localAPIMu.Lock()
			defer r.localAPIMu.Unlock()
			r.handleLocalAgent(w, q)
			return
		}
		if len(r.localToken) != 0 {
			http.Error(w, "本机模式仅允许进程认证接口", http.StatusGone)
			return
		}
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		reply := func(code int, v any) { w.WriteHeader(code); _ = json.NewEncoder(w).Encode(v) }
		if q.Host != "127.0.0.1:18765" {
			reply(403, map[string]any{"ok": false, "error": "无效本机地址"})
			return
		}
		if q.Method == "GET" && q.URL.Path == "/" {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.Header().Set("X-Frame-Options", "DENY")
			w.Header().Set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
			_, _ = w.Write([]byte(localUI))
			return
		}
		origin := q.Header.Get("Origin")
		localManagement := origin == "http://127.0.0.1:18765" && (q.URL.Path == "/status" || q.URL.Path == "/enroll" || q.URL.Path == "/match" || q.URL.Path == "/cancel-match" || q.URL.Path == "/submit-version")
		if origin != r.Origin && !localManagement {
			reply(403, map[string]any{"ok": false, "error": "网站未授权"})
			return
		}
		w.Header().Set("Access-Control-Allow-Origin", origin)
		w.Header().Set("Vary", "Origin")
		w.Header().Set("Access-Control-Allow-Private-Network", "true")
		if q.Method == "OPTIONS" {
			w.Header().Set("Access-Control-Allow-Methods", "POST")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type, X-SJFX-Client")
			reply(204, nil)
			return
		}
		if q.Method != "POST" || q.Header.Get("X-SJFX-Client") != "1" || !strings.HasPrefix(q.Header.Get("Content-Type"), "application/json") {
			reply(403, map[string]any{"ok": false, "error": "无效请求"})
			return
		}
		var data map[string]string
		if err := json.NewDecoder(http.MaxBytesReader(w, q.Body, 16384)).Decode(&data); err != nil {
			reply(400, map[string]any{"ok": false, "error": "请求格式无效"})
			return
		}
		switch q.URL.Path {
		case "/status":
			reply(200, r.Status())
		case "/enroll":
			if err := r.Enroll(q.Context(), data["token"]); err != nil {
				reply(400, map[string]any{"ok": false, "error": err.Error()})
				return
			}
			reply(200, map[string]any{"ok": true})
		case "/sign":
			if !r.Healthy() || r.Client.GetIdentity().DeviceID == "" {
				reply(423, map[string]any{"ok": false, "error": "助手未绑定或监视异常"})
				return
			}
			target := data["target"]
			if !(strings.HasPrefix(target, "/api/") || strings.HasPrefix(target, "/outputs/") || target == "/protection/api/policy") || strings.ContainsAny(target, "\r\n") || len(data["session_tag"]) != 64 {
				reply(400, map[string]any{"ok": false, "error": "签名范围无效"})
				return
			}
			method := data["method"]
			switch method {
			case "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS":
			default:
				reply(400, map[string]any{"error": "无效请求方法"})
				return
			}
			prefix := strings.Join([]string{"sjfx-browser-v1", data["session_tag"], method, target}, "\n")
			reply(200, map[string]any{"ok": true, "headers": r.Client.Proof(prefix)})
		case "/match":
			r.mu.Lock()
			running := r.matching["state"] == "running" || r.matching["state"] == "choosing"
			if !running {
				r.matching = map[string]any{"state": "choosing"}
			}
			r.mu.Unlock()
			if running {
				reply(409, map[string]any{"ok": false, "error": "已有匹配任务"})
				return
			}
			root, err := ChooseDirectory()
			if err != nil {
				r.setMatch(map[string]any{"state": "idle"})
				reply(400, map[string]any{"ok": false, "error": err.Error()})
				return
			}
			r.setMatch(map[string]any{"state": "running"})
			matchCtx, cancel := context.WithCancel(context.Background())
			r.mu.Lock()
			r.matchCancel = cancel
			r.mu.Unlock()
			go func() {
				defer cancel()
				defer func() { r.mu.Lock(); r.matchCancel = nil; r.mu.Unlock() }()
				var mapping map[string]string
				if data["mapping_json"] != "" {
					if err := json.Unmarshal([]byte(data["mapping_json"]), &mapping); err != nil {
						r.setMatch(map[string]any{"state": "failed", "error": "映射JSON无效"})
						return
					}
				}
				if err := r.MatchRegistration(matchCtx, data["collection_id"], root, mapping); err != nil {
					r.setMatch(map[string]any{"state": "failed", "error": err.Error()})
					r.failure(err)
				}
			}()
			reply(202, map[string]any{"ok": true, "state": "running", "root": root})
		case "/cancel-match":
			r.mu.Lock()
			cancel := r.matchCancel
			r.mu.Unlock()
			if cancel != nil {
				cancel()
			}
			reply(200, map[string]any{"ok": true, "state": "cancel_requested"})
		case "/submit-version":
			if data["consent"] != "submit_selected_version" {
				reply(400, map[string]any{"ok": false, "error": "需要明确同意提交所选文件版本"})
				return
			}
			if err := r.SubmitVersion(q.Context(), data["binding_id"]); err != nil {
				reply(400, map[string]any{"ok": false, "error": err.Error()})
				return
			}
			reply(200, map[string]any{"ok": true, "state": "staged", "message": "已提交待检查区，尚未核准使用"})
		default:
			reply(404, map[string]any{"ok": false, "error": "接口不存在"})
		}
	})
}

//go:build windows

package endpoint

import (
	"context"
	"fmt"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestAccessAuditPrivileged(t *testing.T) {
	if os.Getenv("SJFX_ADMIN_SMOKE") != "1" {
		t.Skip("requires an elevated isolated audit smoke test")
	}
	old, err := GetAuditPolicy()
	if os.Getenv("SJFX_TEST_RESTORE_POLICY") == "0" {
		old = 0
	}
	if err != nil {
		t.Fatal(err)
	}
	if backup := os.Getenv("SJFX_TEST_POLICY_BACKUP"); backup != "" {
		if err = os.WriteFile(backup, []byte(fmt.Sprintf("original=%d\n", old)), 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err = SetAuditPolicy(3); err != nil {
		t.Fatal(err)
	}
	defer func() {
		current, e := GetAuditPolicy()
		if e != nil {
			t.Error(e)
			return
		}
		if current != 3 {
			t.Error("audit policy changed concurrently, original setting saved")
			return
		}
		if e = SetAuditPolicy(old); e != nil {
			t.Error(e)
		} else {
			t.Log("original audit policy restored", old)
		}
	}()
	r, b := fixture(t)
	r.AccessAudit = true
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Second)
	defer cancel()
	var original struct {
		Plans []AuditPlan `json:"plans"`
	}
	// Configure returns a read-only plan; capture the exact SACL before apply.
	planInput := map[string]any{"files": []map[string]string{{"path": b.Path, "binding_id": b.ID}}}
	if err := runAudit(ctx, "configure", planInput, &original); err != nil {
		t.Fatal("read original SACL", err)
	}
	if len(original.Plans) != 1 {
		t.Fatal("missing original SACL plan", original)
	}
	originalSDDL := original.Plans[0].Before
	t.Cleanup(func() {
		if err := r.RestoreAccessAudit(context.Background()); err != nil {
			t.Error("audit restoration", err)
		}
		readCtx, readCancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer readCancel()
		var restored struct {
			Plans []AuditPlan `json:"plans"`
		}
		if err := runAudit(readCtx, "configure", planInput, &restored); err != nil {
			t.Error("read restored SACL", err)
			return
		}
		if len(restored.Plans) != 1 || restored.Plans[0].Before != originalSDDL {
			t.Errorf("original SACL not restored exactly: want %q, got %+v", originalSDDL, restored.Plans)
			return
		}
		t.Logf("original SACL restored exactly, including inheritance protection: %q", originalSDDL)
	})
	if err := r.ConfigureAccessAudit(ctx, []Binding{b}); err != nil {
		t.Fatal(err)
	}
	if err := r.CollectAccess(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := os.ReadFile(b.Path); err != nil {
		t.Fatal(err)
	}
	for attempt := 0; attempt < 5; attempt++ {
		if err := r.CollectAccess(ctx); err != nil {
			t.Fatal(err)
		}
		events, err := r.DB.Pending()
		if err != nil {
			t.Fatal(err)
		}
		for _, e := range events {
			if e.BindingID == b.ID && e.Action == "file.read" && e.Process != "" {
				return
			}
		}
		time.Sleep(500 * time.Millisecond)
	}
	t.Fatal("Windows 4663 file read event was not observed")
}

func fixture(t *testing.T) (*Runtime, Binding) {
	t.Helper()
	dir := t.TempDir()
	root := filepath.Join(dir, "source")
	if err := os.Mkdir(root, 0700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "data.txt")
	if err := os.WriteFile(path, []byte("registered content"), 0600); err != nil {
		t.Fatal(err)
	}
	r, err := NewRuntime("http://127.0.0.1:8080", filepath.Join(dir, "state"), false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, h := range r.holds {
			_ = CloseProtection(h)
		}
		r.watcher.Close()
		r.DB.DB.Close()
	})
	b, err := Snapshot(path)
	if err != nil {
		t.Fatal(err)
	}
	b.ID = ID()
	b.FileID = ID()
	b.RegistrationID = "test-registration"
	b.RegistrationRevision = 1
	b.ManifestGeneration = 1
	b.VersionID = b.SHA256
	b.LocalRoot = root
	b.DeviceID = "test-device"
	r.Client.SetIdentity(Identity{DeviceID: b.DeviceID})
	r.protectionEnabled.Store(true)
	if err = r.DB.PutBinding(b); err != nil {
		t.Fatal(err)
	}
	if err = r.DB.AddRoot(root); err != nil {
		t.Fatal(err)
	}
	return r, b
}
func action(b Binding, kind string) Action {
	return Action{ID: ID(), Binding: b.ID, Kind: kind, SHA256: b.SHA256, Identity: b.Identity, Path: b.Path, VersionID: b.VersionID, RegistrationID: b.RegistrationID, RegistrationRevision: b.RegistrationRevision, ManifestGeneration: b.ManifestGeneration, GapEpoch: b.GapEpoch, DeviceID: b.DeviceID, ExpiresAt: float64(time.Now().Unix() + 300)}
}
func currentAction(t *testing.T, r *Runtime, kind string) Action {
	t.Helper()
	bindings, err := r.DB.Bindings()
	if err != nil || len(bindings) != 1 {
		t.Fatal(bindings, err)
	}
	return action(bindings[0], kind)
}
func TestProtectionAndRestore(t *testing.T) {
	for _, kind := range []string{"protect_delete", "deny_read", "quarantine"} {
		t.Run(kind, func(t *testing.T) {
			r, b := fixture(t)
			a := action(b, kind)
			got := r.Execute(a)
			if got["status"] != "completed" {
				t.Fatal(got)
			}
			actual, ok := got["actual"].(map[string]any)
			if !ok || actual["action_id"] != a.ID || actual["local_identity"] != b.Identity || actual["sha256"] != b.SHA256 {
				t.Fatal("missing verified actual target", got)
			}
			bindings, _ := r.DB.Bindings()
			if actual["local_path"] != bindings[0].Path || got["target"] != a {
				t.Fatal("actual destination or frozen target wrong", got)
			}
			if second := r.Execute(a); second["status"] != "completed" || second["actual"] == nil {
				t.Fatal(second)
			}
			if kind == "quarantine" {
				if _, err := os.Stat(b.Path); !os.IsNotExist(err) {
					t.Fatal("original still exists")
				}
			} else {
				if err := os.Remove(b.Path); err == nil {
					t.Fatal("delete not blocked")
				}
			}
			if kind == "deny_read" {
				if _, err := os.ReadFile(b.Path); err == nil {
					t.Fatal("read not blocked")
				}
			}
			restore := currentAction(t, r, "restore")
			restore.RestoreID = a.ID
			got = r.Execute(restore)
			if got["status"] != "completed" {
				t.Fatal(got)
			}
			actual, ok = got["actual"].(map[string]any)
			if !ok || actual["action_id"] != restore.ID || actual["local_path"] != b.Path || actual["local_identity"] != b.Identity || actual["sha256"] != b.SHA256 {
				t.Fatal("restoration receipt does not describe actual restored object", got)
			}
			bytes, err := os.ReadFile(b.Path)
			if err != nil || string(bytes) != "registered content" {
				t.Fatalf("restore %q %v", bytes, err)
			}
			if err = os.Remove(b.Path); err != nil {
				t.Fatalf("lock survived restoration: %v", err)
			}
		})
	}
}
func TestStaleActionAndRestoreConflict(t *testing.T) {
	r, b := fixture(t)
	a := action(b, "deny_read")
	a.SHA256 = strings.Repeat("0", 64)
	if got := r.Execute(a); got["status"] != "stale" {
		t.Fatal(got)
	}
	a = action(b, "quarantine")
	if got := r.Execute(a); got["status"] != "completed" {
		t.Fatal(got)
	}
	if err := os.WriteFile(b.Path, []byte("new unrelated file"), 0600); err != nil {
		t.Fatal(err)
	}
	restore := currentAction(t, r, "restore")
	restore.RestoreID = a.ID
	if got := r.Execute(restore); got["status"] != "failed" || got["actual"] != nil {
		t.Fatal(got)
	}
	bytes, _ := os.ReadFile(b.Path)
	if string(bytes) != "new unrelated file" {
		t.Fatal("restore overwrote unrelated file")
	}
}
func TestRecoverQuarantineBeforeMoveAndAfterRestart(t *testing.T) {
	r, b := fixture(t)
	a := action(b, "quarantine")
	destination := filepath.Join(filepath.Dir(b.Path), ".sjfx-quarantine", "test.blocked")
	j := Journal{Action: a, Original: b.Path, Destination: destination, Intent: true}
	if e := r.DB.SaveAction(a.ID, j); e != nil {
		t.Fatal(e)
	}
	if e := r.Recover(); e != nil {
		t.Fatal(e)
	}
	if _, e := os.Stat(b.Path); !os.IsNotExist(e) {
		t.Fatal("quarantine did not finish")
	}
	if _, e := os.ReadFile(destination); e == nil {
		t.Fatal("recovery did not lock file")
	}
	for id, h := range r.holds {
		_ = CloseProtection(h)
		delete(r.holds, id)
	}
	if e := r.Recover(); e != nil {
		t.Fatal(e)
	}
	a2 := currentAction(t, r, "restore")
	a2.RestoreID = a.ID
	if got := r.Execute(a2); got["status"] != "completed" {
		t.Fatal(got)
	}
	if raw, e := os.ReadFile(b.Path); e != nil || string(raw) != "registered content" {
		t.Fatal("restore after restart failed", e)
	}
}
func TestRenameChangeAndPersistentOutbox(t *testing.T) {
	r, b := fixture(t)
	moved := filepath.Join(filepath.Dir(b.Path), "renamed.txt")
	if err := os.Rename(b.Path, moved); err != nil {
		t.Fatal(err)
	}
	if err := r.Reconcile(); err != nil {
		t.Fatal(err)
	}
	events, err := r.DB.Pending()
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 || events[0].Action != "file.renamed" {
		t.Fatal(events)
	}
	if err = os.WriteFile(moved, []byte("changed content"), 0600); err != nil {
		t.Fatal(err)
	}
	if err = r.Reconcile(); err != nil {
		t.Fatal(err)
	}
	events, _ = r.DB.Pending()
	if len(events) != 2 {
		t.Fatal(events)
	}
	if err = r.DB.Ack([]string{events[0].EventID}); err != nil {
		t.Fatal(err)
	}
	if r.DB.Depth() != 1 {
		t.Fatal("outbox ack")
	}
}
func TestLoopbackOriginAndSigningScope(t *testing.T) {
	r, _ := fixture(t)
	for _, origin := range []string{"", "https://evil.example", "http://127.0.0.1:8081"} {
		req := httptest.NewRequest("POST", "http://127.0.0.1:18765/status", strings.NewReader("{}"))
		req.Header.Set("Origin", origin)
		req.Header.Set("X-SJFX-Client", "1")
		req.Header.Set("Content-Type", "application/json")
		res := httptest.NewRecorder()
		r.Handler().ServeHTTP(res, req)
		if res.Code != 403 {
			t.Fatal(origin, res.Code)
		}
	}
	req := httptest.NewRequest("POST", "http://127.0.0.1:18765/status", strings.NewReader("{}"))
	req.Header.Set("Origin", r.Origin)
	req.Header.Set("X-SJFX-Client", "1")
	req.Header.Set("Content-Type", "application/json")
	res := httptest.NewRecorder()
	r.Handler().ServeHTTP(res, req)
	if res.Code != 200 {
		t.Fatal(res.Code)
	}
}
func TestLocalManagementDoesNotGrantSigningScope(t *testing.T) {
	r, _ := fixture(t)
	for _, entry := range []struct {
		path string
		code int
	}{{"/status", 200}, {"/sign", 403}} {
		q := httptest.NewRequest("POST", "http://127.0.0.1:18765"+entry.path, strings.NewReader("{}"))
		q.Header.Set("Origin", "http://127.0.0.1:18765")
		q.Header.Set("X-SJFX-Client", "1")
		q.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		r.Handler().ServeHTTP(w, q)
		if w.Code != entry.code {
			t.Fatal(entry.path, w.Code)
		}
	}
	q := httptest.NewRequest("GET", "http://127.0.0.1:18765/", nil)
	w := httptest.NewRecorder()
	r.Handler().ServeHTTP(w, q)
	if w.Code != 200 || w.Header().Get("X-Frame-Options") != "DENY" {
		t.Fatal("local management page protections missing")
	}
}

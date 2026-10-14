//go:build windows

package endpoint

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"golang.org/x/sys/windows"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestManifestIndependentInstancesAndExplicitMapping(t *testing.T) {
	root := t.TempDir()
	raw := []byte("same bytes")
	for _, name := range []string{"one.txt", "two.txt", "extra.txt"} {
		if err := os.WriteFile(filepath.Join(root, name), raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	task := ManifestTask{RegistrationID: "r", TaskID: "t", ManifestRevision: 1, ManifestGeneration: 1, DeviceID: "d", LocalRoot: root}
	files := []ManifestFile{{ID: "a", SHA256: Hash(raw), Size: int64(len(raw)), RelativePath: "one.txt"}, {ID: "b", SHA256: Hash(raw), Size: int64(len(raw)), RelativePath: "two.txt"}}
	report, err := collectManifest(context.Background(), task, files, nil)
	if err != nil || !report.Complete || len(report.Files) != 2 || len(report.Extra) != 1 {
		t.Fatal(report, err)
	}
	if report.Files[0].Identity == report.Files[1].Identity || report.Files[0].InstanceID == report.Files[1].InstanceID {
		t.Fatal("identical bytes merged file identity")
	}
	files[0].RelativePath = "server-a.txt"
	files[1].RelativePath = "server-b.txt"
	report, err = collectManifest(context.Background(), task, files, nil)
	if err != nil || report.Complete || len(report.Candidates) != 2 {
		t.Fatal("ambiguous same-hash members silently matched", report, err)
	}
	report, err = collectManifest(context.Background(), task, files, map[string]string{"a": "one.txt", "b": "two.txt"})
	if err != nil || !report.Complete {
		t.Fatal(report, err)
	}
	report, err = collectManifest(context.Background(), task, files, map[string]string{"a": "one.txt", "b": "one.txt"})
	if err != nil || report.Complete || len(report.Errors) == 0 {
		t.Fatal("one instance impersonated two files", report, err)
	}
}

func TestManifestDifferentPathsMismatchAndMissing(t *testing.T) {
	root := t.TempDir()
	raw := []byte("exact bytes")
	os.WriteFile(filepath.Join(root, "different.txt"), raw, 0600)
	task := ManifestTask{LocalRoot: root}
	files := []ManifestFile{{ID: "a", SHA256: Hash(raw), Size: int64(len(raw)), RelativePath: "server-name.txt"}}
	report, err := collectManifest(context.Background(), task, files, nil)
	if err != nil || !report.Complete {
		t.Fatal(report, err)
	}
	files = append(files, ManifestFile{ID: "b", SHA256: Hash([]byte("missing")), Size: 7, RelativePath: "missing.txt"})
	report, err = collectManifest(context.Background(), task, files, nil)
	if err != nil || report.Complete || len(report.Unmatched) != 1 {
		t.Fatal(report, err)
	}
	files[0].RelativePath = "different.txt"
	files[0].SHA256 = Hash([]byte("different bytes"))
	report, err = collectManifest(context.Background(), task, files, nil)
	if err != nil || report.Complete || len(report.Errors) == 0 {
		t.Fatal(report, err)
	}
}

func TestManifestOfflineCapacityRootAndCancellationFailClosed(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "offline.txt")
	os.WriteFile(path, []byte("bytes"), 0600)
	name, _ := windows.UTF16PtrFromString(path)
	if err := windows.SetFileAttributes(name, windows.FILE_ATTRIBUTE_OFFLINE); err != nil {
		t.Fatal(err)
	}
	defer windows.SetFileAttributes(name, windows.FILE_ATTRIBUTE_NORMAL)
	report, err := collectManifest(context.Background(), ManifestTask{LocalRoot: root}, []ManifestFile{{ID: "f", SHA256: Hash([]byte("bytes")), Size: 5, RelativePath: "offline.txt"}}, nil)
	if err != nil || report.Complete || len(report.Errors) != 1 {
		t.Fatal("offline file silently skipped", report, err)
	}
	if _, err = Snapshot(path); err == nil {
		t.Fatal("offline/placeholder Snapshot accepted")
	}
	if _, err = selectedRoot(filepath.Join(root, "missing")); err == nil {
		t.Fatal("missing root accepted")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err = collectManifest(ctx, ManifestTask{LocalRoot: root}, nil, nil); err == nil {
		t.Fatal("cancelled task completed")
	}
	sparse := filepath.Join(root, "large-sparse.dat")
	f, err := os.Create(sparse)
	if err != nil {
		t.Fatal(err)
	}
	var count uint32
	if err = windows.DeviceIoControl(windows.Handle(f.Fd()), 0x000900c4, nil, 0, nil, 0, &count, nil); err != nil {
		f.Close()
		t.Fatal("mark sparse", err)
	}
	if err = f.Truncate(MaxHashBytes + 1); err != nil {
		f.Close()
		t.Fatal(err)
	}
	f.Close()
	report, err = collectManifest(context.Background(), ManifestTask{LocalRoot: root}, nil, nil)
	if err != nil || report.Complete || len(report.Errors) < 2 {
		t.Fatal("hash capacity file silently skipped", report, err)
	}
}

func TestGapPersistsDatabaseReopen(t *testing.T) {
	r, b := fixture(t)
	if err := r.MarkGap("offline restart evidence"); err != nil {
		t.Fatal(err)
	}
	if err := r.DB.DB.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenDatabase(r.Dir)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.DB.Close()
	var epochs []LocalEpoch
	if err = reopened.LoadState("monitoring_epochs", &epochs); err != nil || len(epochs) != 1 || !epochs[0].Unresolved || epochs[0].GapEpoch != b.GapEpoch+1 {
		t.Fatal(epochs, err)
	}
	bindings, err := reopened.Bindings()
	if err != nil || bindings[0].State != "unverified" || bindings[0].GapEpoch != epochs[0].GapEpoch {
		t.Fatal(bindings, err)
	}
}

func TestInterruptedExpiredCommandIsNotAppliedOnRecovery(t *testing.T) {
	r, b := fixture(t)
	a := action(b, "quarantine")
	a.ExpiresAt = 1
	destination := filepath.Join(b.LocalRoot, ".sjfx-quarantine", "not-applied.blocked")
	if err := r.DB.SaveAction(a.ID, Journal{Action: a, Original: b.Path, Destination: destination, Intent: true}); err != nil {
		t.Fatal(err)
	}
	if err := r.Recover(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(b.Path); err != nil {
		t.Fatal("expired interrupted command moved original", err)
	}
	var journal Journal
	if err := r.DB.LoadAction(a.ID, &journal); err != nil || journal.Intent || journal.Result["status"] != "stale" {
		t.Fatal(journal, err)
	}
}

func TestLegacyProtectedStateDoesNotSkipRecordedDeleteLockEdit(t *testing.T) {
	r, b := fixture(t)
	b.State = "protected"
	if err := r.DB.PutBinding(b); err != nil {
		t.Fatal(err)
	}
	if got := r.Execute(action(b, "protect_delete")); got["status"] != "completed" {
		t.Fatal(got)
	}
	if err := os.WriteFile(b.Path, []byte("legacy state allowed edit"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := r.Reconcile(); err != nil {
		t.Fatal(err)
	}
	current, _ := r.DB.Bindings()
	if current[0].SHA256 == b.SHA256 || current[0].State != "changed" || current[0].Disposition != "delete_lock" {
		t.Fatal("legacy generic state suppressed tracking", current)
	}
}

func TestOffHeartbeatStopsEventsNewActionsAndQueuesExplicitSelection(t *testing.T) {
	r, b := fixture(t)
	public, key, _ := ed25519.GenerateKey(rand.Reader)
	_ = public
	r.Client.SetIdentity(Identity{DeviceID: b.DeviceID, PrivateKey: key})
	var eventsCalled, manifestCalled bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch q.URL.Path {
		case "/agent/v2/heartbeat":
			var heartbeat map[string]any
			_ = json.NewDecoder(q.Body).Decode(&heartbeat)
			if _, ok := heartbeat["bindings"]; ok {
				t.Error("heartbeat uploaded all local instance details")
			}
			json.NewEncoder(w).Encode(map[string]any{"protection_enabled": false, "actions": []Action{action(b, "quarantine")}})
		case "/agent/v2/events":
			eventsCalled = true
			t.Error("OFF file events submitted")
			http.Error(w, "off", 409)
		case "/agent/v2/registration-manifest":
			manifestCalled = true
			io.WriteString(w, `{"state":"queued"}`)
		default:
			t.Error("unexpected OFF work", q.URL.Path)
			http.Error(w, "off", 409)
		}
	}))
	defer server.Close()
	r.Client.BaseURL = server.URL
	r.DB.Enqueue(Event{BindingID: b.ID, Action: "file.modified", Path: b.Path, SHA256: b.SHA256})
	depth := r.DB.Depth()
	if err := r.Sync(context.Background()); err != nil || eventsCalled || r.protectionEnabled.Load() {
		t.Fatal(err, eventsCalled)
	}
	if r.DB.Depth() < depth {
		t.Fatal("OFF drained persistent file events")
	}
	if got := r.Execute(action(b, "quarantine")); got["status"] != "stale" {
		t.Fatal(got)
	}
	if _, err := os.Stat(b.Path); err != nil {
		t.Fatal("OFF action moved file", err)
	}
	if err := r.Match(context.Background(), b.RegistrationID, b.LocalRoot); err != nil || !manifestCalled {
		t.Fatal(err, manifestCalled)
	}
	r.mu.Lock()
	state := r.matching["state"]
	r.mu.Unlock()
	if state != "queued" {
		t.Fatal("explicit OFF selection not queued", state)
	}
	if len(r.watcher.WatchList()) != 0 {
		t.Fatal("OFF started selected-root watches")
	}
	before, _ := r.DB.Pending()
	os.WriteFile(b.Path, []byte("edit while switch off"), 0600)
	if err := r.Reconcile(); err != nil {
		t.Fatal(err)
	}
	after, _ := r.DB.Pending()
	if len(before) != len(after) {
		t.Fatal("OFF automatic reconciliation created file events")
	}
}

func TestGapDurableNormalEventsCannotClearAndDeleteLockAllowsEdits(t *testing.T) {
	r, b := fixture(t)
	if err := r.DB.SaveState("monitoring_epochs", []LocalEpoch{{RegistrationID: b.RegistrationID, LocalRoot: b.LocalRoot}}); err != nil {
		t.Fatal(err)
	}
	if err := r.MarkGap("synthetic disconnect"); err != nil {
		t.Fatal(err)
	}
	if err := r.Reconcile(); err != nil {
		t.Fatal(err)
	}
	bindings, _ := r.DB.Bindings()
	if bindings[0].State != "unverified" || !r.HasGap() {
		t.Fatal("reconcile cleared durable gap", bindings)
	}
	stale := action(b, "protect_delete")
	if got := r.Execute(stale); got["status"] != "stale" {
		t.Fatal(got)
	}
	current := bindings[0]
	got := r.Execute(action(current, "protect_delete"))
	if got["status"] != "completed" || got["effect"] != "delete_lock" {
		t.Fatal(got)
	}
	if err := os.WriteFile(current.Path, []byte("allowed in-place new version"), 0600); err != nil {
		t.Fatal("delete lock blocked allowed in-place edit", err)
	}
	if err := r.Reconcile(); err != nil {
		t.Fatal(err)
	}
	bindings, _ = r.DB.Bindings()
	changed := bindings[0]
	if changed.ID != b.ID || changed.FileID != b.FileID || changed.SHA256 == b.SHA256 || changed.Disposition != "delete_lock" || changed.State != "changed" || !r.HasGap() {
		t.Fatal("edit relationship/gap/disposition lost", changed)
	}
	var epochs []LocalEpoch
	if err := r.DB.LoadState("monitoring_epochs", &epochs); err != nil || !epochs[0].Unresolved {
		t.Fatal(epochs, err)
	}
	events, _ := r.DB.Pending()
	var seq int64
	for _, e := range events {
		if e.Sequence <= seq {
			t.Fatal("device sequence not monotonic", events)
		}
		seq = e.Sequence
	}
}

func TestActionFrozenAllDimensionsAndStrongerDisposition(t *testing.T) {
	mutate := []func(*Action){func(a *Action) { a.ExpiresAt = 1 }, func(a *Action) { a.DeviceID = "other" }, func(a *Action) { a.RegistrationID = "other" }, func(a *Action) { a.RegistrationRevision++ }, func(a *Action) { a.ManifestGeneration++ }, func(a *Action) { a.GapEpoch++ }, func(a *Action) { a.VersionID = "other" }, func(a *Action) { a.Identity = "other" }, func(a *Action) { a.Path += ".other" }}
	for i, fn := range mutate {
		t.Run(string(rune('a'+i)), func(t *testing.T) {
			r, b := fixture(t)
			a := action(b, "quarantine")
			fn(&a)
			if got := r.Execute(a); got["status"] != "stale" {
				t.Fatal(got)
			}
			if _, err := os.Stat(b.Path); err != nil {
				t.Fatal("stale command touched file", err)
			}
		})
	}
	r, b := fixture(t)
	if got := r.Execute(action(b, "protect_delete")); got["status"] != "completed" {
		t.Fatal(got)
	}
	if got := r.Execute(action(b, "quarantine")); got["status"] != "completed" || got["effect"] != "quarantine" {
		t.Fatal("delete protection prevented dangerous quarantine", got)
	}
}

func TestSameHashReplacementRejectsOldIdentity(t *testing.T) {
	r, b := fixture(t)
	raw, _ := os.ReadFile(b.Path)
	if err := os.Rename(b.Path, b.Path+".old"); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(b.Path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	if got := r.Execute(action(b, "quarantine")); got["status"] != "stale" {
		t.Fatal(got)
	}
	if err := r.Reconcile(); err != nil {
		t.Fatal(err)
	}
	current, _ := r.DB.Bindings()
	if current[0].ID != b.ID || current[0].Identity == b.Identity || current[0].State != "changed" {
		t.Fatal(current)
	}
}

func TestGapDuringExplicitTaskIncrements(t *testing.T) {
	r, b := fixture(t)
	r.DB.SaveState("monitoring_epochs", []LocalEpoch{{RegistrationID: b.RegistrationID, GapEpoch: 3, Unresolved: true, Rechecking: true, LocalRoot: b.LocalRoot}})
	if err := r.MarkGap("new failure during full recheck"); err != nil {
		t.Fatal(err)
	}
	if got := r.Epochs(); got[0].GapEpoch != 4 || !got[0].Unresolved {
		t.Fatal(got)
	}
	if err := r.MarkGap("repeat same outage"); err != nil {
		t.Fatal(err)
	}
	if got := r.Epochs(); got[0].GapEpoch != 4 {
		t.Fatal(got)
	}
}

func TestSignedCompleteManifestAndExplicitStagingBytes(t *testing.T) {
	dir := t.TempDir()
	root := filepath.Join(dir, "local")
	os.Mkdir(root, 0700)
	raw := []byte("exact bytes for signed matching")
	os.WriteFile(filepath.Join(root, "different.txt"), raw, 0600)
	public, key, _ := ed25519.GenerateKey(rand.Reader)
	var staged bool
	var complete bool
	task := ManifestTask{RegistrationID: "registration", TaskID: "task", ManifestRevision: 1, ManifestGeneration: 1, GapEpoch: 0, DeviceID: "device", LocalRoot: root}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) {
		body, _ := io.ReadAll(q.Body)
		message := strings.Join([]string{"sjfx-agent-v2", q.Method, q.URL.Path, Hash(body), q.Header.Get("X-SJFX-Device"), q.Header.Get("X-SJFX-Time"), q.Header.Get("X-SJFX-Nonce")}, "\n")
		signature, _ := base64.StdEncoding.DecodeString(q.Header.Get("X-SJFX-Proof"))
		if !ed25519.Verify(public, []byte(message), signature) {
			t.Error("unsigned or incorrect complete/body proof")
			http.Error(w, "signature", 403)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch q.URL.Path {
		case "/agent/v2/heartbeat":
			json.NewEncoder(w).Encode(map[string]any{"protection_enabled": true, "actions": []Action{}})
		case "/agent/v2/registration-manifest":
			json.NewEncoder(w).Encode(map[string]any{"task": task, "files": []ManifestFile{{ID: "file", SHA256: Hash(raw), Size: int64(len(raw)), RelativePath: "server-file.txt"}}})
		case "/agent/v2/manifest-complete":
			var report ManifestReport
			json.Unmarshal(body, &report)
			complete = report.Complete
			if report.ManifestTask != task || len(report.Files) != 1 {
				t.Error(report)
			}
			bindings := []Binding{}
			for _, instance := range report.Files {
				b := instance.Binding
				b.ID = "binding"
				bindings = append(bindings, b)
			}
			json.NewEncoder(w).Encode(map[string]any{"complete": report.Complete, "bindings": bindings})
		case "/agent/v2/staging-version":
			var payload map[string]any
			json.Unmarshal(body, &payload)
			data, e := base64.StdEncoding.DecodeString(payload["content_base64"].(string))
			if e != nil || Hash(data) != payload["expected_sha256"] || string(data) != "updated unknown content" {
				t.Error("staging bytes not verified", payload)
			}
			staged = true
			io.WriteString(w, `{"ok":true}`)
		default:
			t.Error("unexpected default upload", q.URL.Path)
			http.Error(w, "unexpected", 404)
		}
	}))
	defer server.Close()
	r, err := NewRuntime(server.URL, filepath.Join(dir, "state"), false)
	if err != nil {
		t.Fatal(err)
	}
	defer r.DB.DB.Close()
	defer r.watcher.Close()
	r.Client.SetIdentity(Identity{DeviceID: "device", PrivateKey: key})
	if err = r.Match(context.Background(), "registration", root); err != nil || !complete || staged {
		t.Fatal(err, complete, staged)
	}
	os.WriteFile(filepath.Join(root, "different.txt"), []byte("updated unknown content"), 0600)
	if err = r.Reconcile(); err != nil {
		t.Fatal(err)
	}
	if staged {
		t.Fatal("edited content automatically uploaded")
	}
	if err = r.SubmitVersion(context.Background(), "binding"); err != nil || !staged {
		t.Fatal(err, staged)
	}
	r.MarkGap("test gap")
	if err = r.Reconcile(); err != nil || !r.HasGap() {
		t.Fatal(err)
	}
	// Server health/contact is independent from unresolved registration gaps.
	if !r.serverConnected() {
		t.Fatal("missing contact after real successful policy heartbeat")
	}
	r.DB.SaveState("last_server_contact", time.Now().Add(-time.Hour).UTC().Format(time.RFC3339))
	if r.serverConnected() {
		t.Fatal("claimed contact with stale heartbeat")
	}
	r.DB.SaveState("last_server_contact", time.Now().UTC().Format(time.RFC3339))
	if !r.serverConnected() {
		t.Fatal("missing actual recent contact")
	}
}

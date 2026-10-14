//go:build windows

package endpoint

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"golang.org/x/sys/windows"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRenameAndEditOneReconcileInvalidatesVersion(t *testing.T) {
	r, b := fixture(t)
	renamed := filepath.Join(b.LocalRoot, "renamed.txt")
	if err := os.Rename(b.Path, renamed); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(renamed, []byte("different concurrent content"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := r.Reconcile(); err != nil {
		t.Fatal(err)
	}
	bs, _ := r.DB.Bindings()
	got := bs[0]
	if got.ID != b.ID || got.Identity != b.Identity || got.Path != renamed || got.State != "changed" || got.VersionID == b.VersionID || got.VersionID != got.SHA256 {
		t.Fatal(got)
	}
	es, _ := r.DB.Pending()
	if len(es) != 1 || es[0].Action != "file.renamed" || es[0].VersionID != got.VersionID || es[0].SHA256 != got.SHA256 {
		t.Fatal(es)
	}
}

func TestReappearanceAndEditInvalidatesVersion(t *testing.T) {
	r, b := fixture(t)
	b.State = "unavailable"
	r.DB.PutBinding(b)
	os.WriteFile(b.Path, []byte("edited while storage unavailable"), 0600)
	if err := r.Reconcile(); err != nil {
		t.Fatal(err)
	}
	bs, _ := r.DB.Bindings()
	if bs[0].State != "changed" || bs[0].VersionID == b.VersionID {
		t.Fatal(bs)
	}
}

func TestOfflineCollectionPersistsAndNeverClaimsHealthy(t *testing.T) {
	r, b := fixture(t)
	_, key, _ := ed25519.GenerateKey(rand.Reader)
	r.Client.SetIdentity(Identity{DeviceID: b.DeviceID, PrivateKey: key})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) { http.Error(w, "offline", 503) }))
	defer srv.Close()
	r.Client.BaseURL = srv.URL
	r.serverAvailable.Store(true)
	r.DB.SaveState("last_server_contact", time.Now().UTC().Format(time.RFC3339))
	r.healthy.Store(true)
	if _, err := r.refreshProtection(context.Background()); err == nil {
		t.Fatal("expected network failure")
	}
	if !r.collectionEnabled() || r.protectionEnabled.Load() || r.serverConnected() || r.Healthy() {
		t.Fatal("offline state claimed enabled/healthy or stopped collecting")
	}
	os.WriteFile(b.Path, []byte("offline edited bytes"), 0600)
	if err := r.Reconcile(); err != nil {
		t.Fatal(err)
	}
	es, _ := r.DB.Pending()
	var edit bool
	for _, e := range es {
		if e.Action == "file.modified" && e.VersionID != b.VersionID {
			edit = true
		}
	}
	if !edit || r.Healthy() || !r.HasGap() {
		t.Fatal(es, r.Status())
	}
	if err := r.DB.DB.Close(); err != nil {
		t.Fatal(err)
	}
	db, err := OpenDatabase(r.Dir)
	if err != nil {
		t.Fatal(err)
	}
	r.DB = db
	es, _ = r.DB.Pending()
	if len(es) < 2 {
		t.Fatal("offline evidence lost at reopen", es)
	}
}

func mockAudit(r *Runtime, readReply map[string]any) {
	r.AccessAudit = true
	r.auditPolicy = func() (uint32, error) { return 3, nil }
	r.auditRunner = func(ctx context.Context, mode string, input, output any) error {
		var reply any
		switch mode {
		case "configure":
			files := input.(map[string]any)["files"].([]map[string]string)
			plans := []AuditPlan{}
			for _, f := range files {
				plans = append(plans, AuditPlan{Path: f["path"], BindingID: f["binding_id"], Before: "before", After: "after"})
			}
			reply = map[string]any{"plans": plans}
		case "restore":
			reply = map[string]any{"ok": true}
		case "read":
			reply = readReply
		}
		raw, _ := json.Marshal(reply)
		return json.Unmarshal(raw, output)
	}
}

func TestAuditPartialBatchContinuesAndNeverReportsCompleteCoverage(t *testing.T) {
	r, b := fixture(t)
	bs := []Binding{b}
	for i := 0; i < 205; i++ {
		path := filepath.Join(b.LocalRoot, fmt.Sprintf("audit-%d.txt", i))
		os.WriteFile(path, []byte("audit"), 0600)
		s, err := Snapshot(path)
		if err != nil {
			t.Fatal(err)
		}
		s.ID = ID()
		s.LocalRoot = b.LocalRoot
		s.RegistrationID = b.RegistrationID
		s.State = "unverified"
		r.DB.PutBinding(s)
		bs = append(bs, s)
	}
	mockAudit(r, map[string]any{"cursor": 1, "gap": false, "events": []any{}})
	if err := r.collectLocalAccess(context.Background()); err != nil {
		t.Fatal(err)
	}
	if r.auditPending.Load() != 6 || r.auditHealthy.Load() || r.HasGap() {
		t.Fatal(r.auditPending.Load(), r.auditHealthy.Load(), r.Epochs())
	}
	if err := r.collectLocalAccess(context.Background()); err != nil {
		t.Fatal(err)
	}
	if r.auditPending.Load() != 0 || !r.auditHealthy.Load() || len(r.auditConfigured) != 206 {
		t.Fatal(r.auditPending.Load(), len(r.auditConfigured))
	}
}

func TestSecurityLogGapDurableEpochAndOldAccessCannotVerify(t *testing.T) {
	r, b := fixture(t)
	mockAudit(r, map[string]any{"cursor": 8, "gap": true, "events": []any{map[string]any{"record_id": 7, "binding_id": b.ID, "path": b.Path, "event_id": 4663, "mask": "0x1", "occurred": time.Now().UTC().Format(time.RFC3339)}}})
	if err := r.collectLocalAccess(context.Background()); err != nil {
		t.Fatal(err)
	}
	bs, _ := r.DB.Bindings()
	if bs[0].GapEpoch != b.GapEpoch+1 || bs[0].State != "unverified" || r.auditHealthy.Load() || !r.HasGap() {
		t.Fatal(bs, r.Epochs())
	}
	es, _ := r.DB.Pending()
	for _, e := range es {
		if e.GapEpoch != bs[0].GapEpoch || e.RegistrationID != b.RegistrationID {
			t.Fatal(e)
		}
	}
	if err := r.Reconcile(); err != nil {
		t.Fatal(err)
	}
	bs, _ = r.DB.Bindings()
	if bs[0].State != "unverified" || !r.HasGap() {
		t.Fatal("ordinary event cleared gap", bs)
	}
	if err := r.DB.DB.Close(); err != nil {
		t.Fatal(err)
	}
	db, err := OpenDatabase(r.Dir)
	if err != nil {
		t.Fatal(err)
	}
	r.DB = db
	if !r.HasGap() {
		t.Fatal("gap not durable")
	}
}

func TestDeleteLockUpgradeStaleContentKeepsOriginalLock(t *testing.T) {
	r, b := fixture(t)
	a := action(b, "protect_delete")
	if got := r.Execute(a); got["status"] != "completed" {
		t.Fatal(got)
	}
	os.WriteFile(b.Path, []byte("edit before reconcile"), 0600)
	if got := r.Execute(action(b, "quarantine")); got["status"] != "stale" {
		t.Fatal(got)
	}
	if _, ok := r.holds[a.ID]; !ok {
		t.Fatal("old lock lost")
	}
	var j Journal
	r.DB.LoadAction(a.ID, &j)
	if !j.Active || j.Result["status"] != "completed" {
		t.Fatal(j)
	}
	if err := os.Remove(b.Path); err == nil {
		t.Fatal("old delete lock released")
	}
}

func TestDeleteLockUpgradeSharingFailureRestoresOriginalLock(t *testing.T) {
	r, b := fixture(t)
	a := action(b, "protect_delete")
	if got := r.Execute(a); got["status"] != "completed" {
		t.Fatal(got)
	}
	p, _ := windows.UTF16PtrFromString(b.Path)
	busy, err := windows.CreateFile(p, windows.GENERIC_READ, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE, nil, windows.OPEN_EXISTING, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer windows.CloseHandle(busy)
	if got := r.Execute(action(b, "deny_read")); got["status"] != "failed" {
		t.Fatal(got)
	}
	if _, ok := r.holds[a.ID]; !ok {
		t.Fatal("sharing failure lost old lock")
	}
	var j Journal
	r.DB.LoadAction(a.ID, &j)
	if !j.Active || j.Result["status"] != "completed" {
		t.Fatal(j)
	}
	if err := os.Remove(b.Path); err == nil {
		t.Fatal("delete lock no longer held")
	}
}

func TestOffRestoresConcreteDispositionAfterGapChange(t *testing.T) {
	for _, kind := range []string{"quarantine", "deny_read", "protect_delete"} {
		t.Run(kind, func(t *testing.T) {
			r, b := fixture(t)
			a := action(b, kind)
			if got := r.Execute(a); got["status"] != "completed" {
				t.Fatal(got)
			}
			r.protectionEnabled.Store(false)
			r.collecting.Store(false)
			if err := r.MarkGap("explicit OFF"); err != nil {
				t.Fatal(err)
			}
			restore := currentAction(t, r, "restore")
			restore.RestoreID = a.ID
			if got := r.Execute(restore); got["status"] != "completed" {
				t.Fatal(got)
			}
			if len(r.holds) != 0 {
				t.Fatal("restore did not release specific lock")
			}
			if _, err := os.Stat(b.Path); err != nil {
				t.Fatal(err)
			}
			js, _ := r.DB.Journals()
			for _, j := range js {
				if j.Active || j.Intent {
					t.Fatal("maintenance cannot finish", j)
				}
			}
		})
	}
}

func TestRenameHeldCannotTouchReplacementPathOrOverwriteDestination(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "held.txt")
	other := filepath.Join(root, "other.txt")
	os.WriteFile(source, []byte("old"), 0600)
	os.WriteFile(other, []byte("replacement"), 0600)
	h, err := HoldProtection(source, "quarantine")
	if err != nil {
		t.Fatal(err)
	}
	defer CloseProtection(uintptr(h))
	if err := os.Rename(other, source); err == nil {
		t.Fatal("replacement overwrote fixed target")
	}
	if err := RenameHeld(h, other); err == nil {
		t.Fatal("held move overwrote destination")
	}
	moved := filepath.Join(root, "moved.txt")
	if err := RenameHeld(h, moved); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(source, []byte("new instance"), 0600)
	if err := RenameHeld(h, source); err == nil {
		t.Fatal("restore overwrote new instance")
	}
	raw, _ := os.ReadFile(source)
	if string(raw) != "new instance" {
		t.Fatal(string(raw))
	}
}

func TestIdenticalCandidatesDeduplicateAcrossAckAndRestart(t *testing.T) {
	r, b := fixture(t)
	copyPath := filepath.Join(b.LocalRoot, "copy.txt")
	raw, _ := os.ReadFile(b.Path)
	os.WriteFile(copyPath, raw, 0600)
	for i := 0; i < 3; i++ {
		if err := r.DiscoverCopies(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	es, _ := r.DB.Pending()
	if len(es) != 1 || es[0].Action != "file.identical_candidate" {
		t.Fatal(es)
	}
	bs, _ := r.DB.Bindings()
	if len(bs) != 1 {
		t.Fatal("candidate auto bound", bs)
	}
	r.DB.Ack([]string{es[0].EventID})
	r.DB.DB.Close()
	db, err := OpenDatabase(r.Dir)
	if err != nil {
		t.Fatal(err)
	}
	r.DB = db
	if err := r.DiscoverCopies(context.Background()); err != nil {
		t.Fatal(err)
	}
	if r.DB.Depth() != 0 {
		t.Fatal("ACK/restart repeated candidate")
	}
	os.Remove(copyPath)
	if err := r.DiscoverCopies(context.Background()); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(copyPath, raw, 0600)
	if err := r.DiscoverCopies(context.Background()); err != nil {
		t.Fatal(err)
	}
	if r.DB.Depth() != 1 {
		t.Fatal("reappearing instance not reported")
	}
}

func TestEventCapacityRejectsAtomicallyAndPersistsLossEvidence(t *testing.T) {
	r, b := fixture(t)
	r.DB.EventLimit = 2
	for i := 0; i < 2; i++ {
		if err := r.DB.Enqueue(Event{EventID: fmt.Sprint(i), Action: "synthetic"}); err != nil {
			t.Fatal(err)
		}
	}
	before := b
	b.State = "changed"
	if err := r.DB.Record(b, Event{Action: "file.modified"}); err == nil {
		t.Fatal("queue overflow silently accepted")
	}
	if r.DB.Depth() != 2 {
		t.Fatal("unbounded queue")
	}
	bs, _ := r.DB.Bindings()
	if bs[0].State != before.State {
		t.Fatal("binding committed without event", bs)
	}
	var rejected int
	err := r.DB.DB.QueryRow("SELECT CAST(value AS INTEGER) FROM state WHERE key='event_capacity_rejections'").Scan(&rejected)
	if err != nil || rejected != 1 {
		t.Fatal(rejected, err)
	}
	if err := r.MarkGap("queue capacity"); err == nil {
		t.Fatal("expected full outbox error")
	}
	if !r.HasGap() {
		t.Fatal("capacity gap not durable")
	}
	r.DB.Ack([]string{"0", "1"})
	if !r.HasGap() {
		t.Fatal("draining queue erased gap")
	}
	if err := r.DB.Enqueue(Event{Action: "large", Detail: strings.Repeat("x", MaxEventPayloadBytes+1)}); err == nil {
		t.Fatal("oversize payload accepted")
	}
}

func TestRepeatedEventIDDoesNotAllocateDeviceSequence(t *testing.T) {
	r, _ := fixture(t)
	e := Event{EventID: "same", Action: "file.read"}
	for i := 0; i < 5; i++ {
		if err := r.DB.Enqueue(e); err != nil {
			t.Fatal(err)
		}
	}
	var seq int
	if err := r.DB.DB.QueryRow("SELECT CAST(value AS INTEGER) FROM state WHERE key='device_sequence'").Scan(&seq); err != nil || seq != 1 || r.DB.Depth() != 1 {
		t.Fatal(seq, err)
	}
}

func TestFullOutboxStillAllowsAtomicOffMaintenanceRestore(t *testing.T) {
	r, b := fixture(t)
	a := action(b, "protect_delete")
	if got := r.Execute(a); got["status"] != "completed" {
		t.Fatal(got)
	}
	r.DB.EventLimit = r.DB.Depth()
	r.protectionEnabled.Store(false)
	restore := currentAction(t, r, "restore")
	restore.RestoreID = a.ID
	got := r.Execute(restore)
	if got["status"] != "completed" || got["audit_gap"] != true {
		t.Fatal(got)
	}
	if len(r.holds) != 0 || !r.HasGap() {
		t.Fatal("restore loss evidence or locks inconsistent")
	}
	bs, _ := r.DB.Bindings()
	if bs[0].Disposition != "" {
		t.Fatal(bs)
	}
	js, _ := r.DB.Journals()
	for _, j := range js {
		if j.Active || j.Intent {
			t.Fatal("restored transaction left maintenance blocked", j)
		}
	}
}

func TestAutomaticQueuedMatchRegistersCancelableContext(t *testing.T) {
	r, b := fixture(t)
	_, key, _ := ed25519.GenerateKey(rand.Reader)
	r.Client.SetIdentity(Identity{DeviceID: b.DeviceID, PrivateKey: key})
	started := make(chan struct{})
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) {
		close(started)
		select {
		case <-q.Context().Done():
		case <-release:
		}
		http.Error(w, "canceled", 503)
	}))
	defer srv.Close()
	defer close(release)
	r.Client.BaseURL = srv.URL
	r.DB.SaveState("queued_selected_match", queuedMatch{CollectionID: b.RegistrationID, Root: b.LocalRoot})
	r.resumeQueuedMatch(context.Background())
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("automatic match did not start")
	}
	r.mu.Lock()
	cancel := r.matchCancel
	r.mu.Unlock()
	if cancel == nil {
		t.Fatal("automatic match missing cancel")
	}
	cancel()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		r.mu.Lock()
		done := r.matchCancel == nil
		r.mu.Unlock()
		if done {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("automatic match cancel did not finish")
}

//go:build windows

package endpoint

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const localTestToken = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

func localFixture(t *testing.T) (*Runtime, LocalRegistration) {
	t.Helper()
	root := t.TempDir()
	token := filepath.Join(root, "token")
	if err := os.WriteFile(token, []byte(localTestToken), 0600); err != nil {
		t.Fatal(err)
	}
	source := filepath.Join(root, "source")
	os.Mkdir(source, 0700)
	path := filepath.Join(source, "bytes.txt")
	os.WriteFile(path, []byte("core direct local input"), 0600)
	r, err := NewLocalRuntime(filepath.Join(root, "candidate-state"), token)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, h := range r.holds {
			CloseProtection(h)
		}
		r.watcher.Close()
		r.DB.DB.Close()
	})
	snap, err := Snapshot(path)
	if err != nil {
		t.Fatal(err)
	}
	in := LocalRegistration{DeviceID: "local-runtime-device", OwnerID: "user-1", RegistrationID: "package-1", ManifestRevision: 1, LocalRoot: source, Files: []LocalRegisterFile{{BindingID: "local:core-file-id", FileID: "core-file-id", Path: path, SHA256: snap.SHA256, VersionID: snap.SHA256, Size: snap.Size, Identity: snap.Identity}}}
	return r, in
}

func localRequest(r *Runtime, path string, input any, token, addr, origin string) *httptest.ResponseRecorder {
	raw, _ := json.Marshal(input)
	q := httptest.NewRequest("POST", "http://127.0.0.1:18766"+path, bytes.NewReader(raw))
	q.RemoteAddr = addr
	q.Header.Set("Content-Type", "application/json")
	if token != "" {
		q.Header.Set("Authorization", "Bearer "+token)
	}
	if origin != "" {
		q.Header.Set("Origin", origin)
	}
	w := httptest.NewRecorder()
	r.Handler().ServeHTTP(w, q)
	return w
}

func TestLocalAgentAuthRejectsRemoteOriginsAndMissingBearer(t *testing.T) {
	r, in := localFixture(t)
	for _, spec := range []struct{ token, addr, origin string }{{"", "127.0.0.1:3", ""}, {"bad", "127.0.0.1:3", ""}, {localTestToken, "203.0.113.1:3", ""}, {localTestToken, "127.0.0.1:3", "http://127.0.0.1:5000"}} {
		w := localRequest(r, "/agent-local/v1/register", in, spec.token, spec.addr, spec.origin)
		if w.Code != 403 {
			t.Fatal(w.Code, w.Body.String())
		}
	}
	w := localRequest(r, "/agent-local/v1/register", in, localTestToken, "127.0.0.1:3", "")
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	for _, path := range []string{"/match", "/submit-version", "/enroll", "/sign"} {
		w := localRequest(r, path, map[string]any{}, localTestToken, "127.0.0.1:3", "")
		if w.Code != 410 {
			t.Fatal("local mode exposed old pairing/upload", path, w.Code)
		}
	}
}

func TestLocalRegistrationSharesCoreIdentityAndRejectsChangedOrCrossOwner(t *testing.T) {
	r, in := localFixture(t)
	bs, err := r.RegisterLocal(context.Background(), in)
	if err != nil {
		t.Fatal(err)
	}
	if bs[0].ID != in.Files[0].BindingID || bs[0].FileID != in.Files[0].FileID || bs[0].VersionID != in.Files[0].SHA256 || bs[0].Identity != in.Files[0].Identity || bs[0].OwnerID != in.OwnerID {
		t.Fatal(bs)
	}
	cross := in
	cross.OwnerID = "user-2"
	if _, err = r.RegisterLocal(context.Background(), cross); err == nil {
		t.Fatal("cross-owner rebound state")
	}
	wrong := in
	wrong.Files = append([]LocalRegisterFile(nil), in.Files...)
	wrong.Files[0].Identity = "wrong"
	if _, err = r.RegisterLocal(context.Background(), wrong); err == nil {
		t.Fatal("identity mismatch accepted")
	}
	wrong.Files[0] = in.Files[0]
	wrong.Files[0].BindingID = "other"
	if _, err = r.RegisterLocal(context.Background(), wrong); err == nil {
		t.Fatal("core instance mismatch accepted")
	}
	os.Rename(in.Files[0].Path, in.Files[0].Path+".old")
	os.WriteFile(in.Files[0].Path, []byte("core direct local input"), 0600)
	if _, err = r.RegisterLocal(context.Background(), in); err == nil {
		t.Fatal("same bytes replaced instance accepted with old identity")
	}
	current, _ := r.DB.Bindings()
	if current[0].Identity != bs[0].Identity {
		t.Fatal("failed registration committed partial binding")
	}
}

func TestLocalSyncPersistsEventsResultsAndOffRestore(t *testing.T) {
	r, in := localFixture(t)
	bs, err := r.RegisterLocal(context.Background(), in)
	if err != nil {
		t.Fatal(err)
	}
	enable := LocalSyncRequest{DeviceID: in.DeviceID, ProtectionEnabled: true}
	out, err := r.localSync(context.Background(), enable)
	if err != nil || !r.Healthy() {
		t.Fatal(err, out)
	}
	a := action(bs[0], "quarantine")
	enable.Actions = []Action{a}
	out, err = r.localSync(context.Background(), enable)
	if err != nil {
		t.Fatal(err)
	}
	results := out["results"].([]map[string]any)
	if len(results) != 1 || results[0]["result"].(map[string]any)["status"] != "completed" {
		t.Fatal(out)
	}
	first := results[0]
	out, err = r.localSync(context.Background(), enable)
	if err != nil || out["results"].([]map[string]any)[0]["action_id"] != first["action_id"] {
		t.Fatal(err, out)
	}
	changed := enable
	changed.Actions = append([]Action(nil), enable.Actions...)
	changed.Actions[0].SHA256 = "different"
	if _, err = r.localSync(context.Background(), changed); err == nil {
		t.Fatal("duplicate changed frozen command accepted")
	}
	off := LocalSyncRequest{DeviceID: in.DeviceID}
	out, err = r.localSync(context.Background(), off)
	if err != nil {
		t.Fatal(err)
	}
	if len(out["results"].([]map[string]any)) != 1 || r.protectionEnabled.Load() || r.Healthy() {
		t.Fatal("OFF dropped results or healthy", out)
	}
	restore := currentAction(t, r, "restore")
	restore.RestoreID = a.ID
	off.Actions = []Action{restore}
	out, err = r.localSync(context.Background(), off)
	if err != nil {
		t.Fatal(err)
	}
	if len(out["results"].([]map[string]any)) != 2 || len(r.holds) != 0 {
		t.Fatal(out)
	}
	r.DB.DB.Close()
	db, err := OpenDatabase(r.Dir)
	if err != nil {
		t.Fatal(err)
	}
	r.DB = db
	off.Actions = nil
	out, err = r.localSync(context.Background(), off)
	if err != nil || len(out["results"].([]map[string]any)) != 2 {
		t.Fatal("unacked results lost", err, out)
	}
	off.AckResultIDs = []string{a.ID, restore.ID}
	for _, e := range out["events"].([]Event) {
		off.AckEventIDs = append(off.AckEventIDs, e.EventID)
	}
	out, err = r.localSync(context.Background(), off)
	if err != nil || len(out["results"].([]map[string]any)) != 0 || len(out["events"].([]Event)) != 0 {
		t.Fatal(err, out)
	}
	if !r.HasGap() {
		t.Fatal("ACK erased OFF gap")
	}
}

func TestLocalAgentRealHTTPUsesOnlyProcessBearer(t *testing.T) {
	r, in := localFixture(t)
	srv := httptest.NewServer(r.Handler())
	defer srv.Close()
	raw, _ := json.Marshal(in)
	q, _ := http.NewRequest("POST", srv.URL+"/agent-local/v1/register", bytes.NewReader(raw))
	q.Header.Set("Content-Type", "application/json")
	q.Header.Set("Authorization", "Bearer "+localTestToken)
	resp, err := http.DefaultClient.Do(q)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatal(resp.StatusCode)
	}
}

func TestLocalModeRejectsLegacyStateAndMalformedToken(t *testing.T) {
	r, in := localFixture(t)
	r.DB.SaveState("runtime_mode", "remote")
	r.DB.PutBinding(Binding{ID: in.Files[0].BindingID})
	dir := r.Dir
	r.DB.DB.Close()
	r.watcher.Close()
	token := filepath.Join(t.TempDir(), "token")
	os.WriteFile(token, []byte(localTestToken), 0600)
	if _, err := NewLocalRuntime(dir, token); err == nil {
		t.Fatal("legacy state silently migrated")
	}
	os.WriteFile(token, []byte(strings.Repeat("g", 64)), 0600)
	if _, err := NewLocalRuntime(t.TempDir(), token); err == nil {
		t.Fatal("malformed token accepted")
	}
}

func TestLocalRegistrationCapacityRejectsCompletePackageWithoutPartialBindings(t *testing.T) {
	r, in := localFixture(t)
	tooMany := in
	tooMany.Files = make([]LocalRegisterFile, LocalRegistrationFileLimit+1)
	w := localRequest(r, "/agent-local/v1/register", tooMany, localTestToken, "127.0.0.1:3", "")
	if w.Code != 413 || !strings.Contains(w.Body.String(), "local_agent_capacity_exceeded") || !strings.Contains(w.Body.String(), "\"coverage_complete\":false") {
		t.Fatal(w.Code, w.Body.String())
	}
	oversize := in
	oversize.Files = append([]LocalRegisterFile(nil), in.Files...)
	oversize.Files[0].Path = strings.Repeat("x", LocalAgentRequestLimit+1)
	w = localRequest(r, "/agent-local/v1/register", oversize, localTestToken, "127.0.0.1:3", "")
	if w.Code != 413 {
		t.Fatal(w.Code, w.Body.String())
	}
	tooLarge := in
	tooLarge.Files = append([]LocalRegisterFile(nil), in.Files...)
	tooLarge.Files[0].Size = MaxHashBytes + 1
	if _, err := r.RegisterLocal(context.Background(), tooLarge); err == nil {
		t.Fatal("aggregate hash quota accepted")
	}
	bs, _ := r.DB.Bindings()
	if len(bs) != 0 {
		t.Fatal("capacity failure accepted partial binding", bs)
	}
}

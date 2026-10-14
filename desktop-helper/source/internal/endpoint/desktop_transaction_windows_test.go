//go:build windows

package endpoint

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func transactionRequest(t *testing.T, r *Runtime, action string, payload any, expected int) map[string]any {
	t.Helper()
	response := localRequest(r, "/agent-local/v1/register/"+action, payload, localTestToken, "127.0.0.1:7", "")
	if response.Code != expected {
		t.Fatalf("%s: status=%d body=%s", action, response.Code, response.Body.String())
	}
	result := map[string]any{}
	if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	return result
}

func TestDesktopTransaction1001CompleteReceiptPagination(t *testing.T) {
	r, registration := localFixture(t)
	registration.Files = nil
	files := []LocalRegisterFile{}
	var bytes int64
	for i := 0; i < 1001; i++ {
		path := filepath.Join(registration.LocalRoot, fmt.Sprintf("large-%04d.txt", i))
		if err := os.WriteFile(path, []byte("current bytes"), 0600); err != nil {
			t.Fatal(err)
		}
		snap, err := Snapshot(path)
		if err != nil {
			t.Fatal(err)
		}
		id := fmt.Sprintf("file-%04d", i)
		files = append(files, LocalRegisterFile{BindingID: "local:" + id, FileID: id, Path: path, SHA256: snap.SHA256, VersionID: snap.SHA256, Identity: snap.Identity, Size: snap.Size})
		bytes += snap.Size
	}
	digest, _ := LocalManifestDigest(files)
	begin := transactionRequest(t, r, "begin", map[string]any{"registration": registration, "expected_count": len(files), "expected_bytes": bytes, "manifest_digest": digest}, 200)
	id := begin["transaction_id"].(string)
	transactionRequest(t, r, "batch", map[string]any{"transaction_id": id, "offset": 0, "files": files[:500]}, 200)
	transactionRequest(t, r, "commit", map[string]any{"transaction_id": id}, 409)
	active, _ := r.DB.Bindings()
	if len(active) != 0 {
		t.Fatal("partial transaction became active")
	}
	for offset := 500; offset < len(files); offset += 500 {
		end := offset + 500
		if end > len(files) {
			end = len(files)
		}
		response := transactionRequest(t, r, "batch", map[string]any{"transaction_id": id, "offset": offset, "files": files[offset:end]}, 200)
		if response["coverage_complete"] != false {
			t.Fatal("batch falsely complete")
		}
	}
	complete := transactionRequest(t, r, "commit", map[string]any{"transaction_id": id}, 200)
	if complete["coverage_complete"] != true || complete["count"] != float64(1001) || complete["manifest_digest"] != digest {
		t.Fatal(complete)
	}
	seen := 0
	for offset := 0; offset < 1001; {
		page := transactionRequest(t, r, "result", map[string]any{"transaction_id": id, "offset": offset, "limit": 500}, 200)
		bindings := page["bindings"].([]any)
		seen += len(bindings)
		offset = int(page["next_offset"].(float64))
		if len(bindings) == 0 {
			t.Fatal("missing receipt members")
		}
	}
	if seen != 1001 {
		t.Fatal(seen)
	}
	transactionRequest(t, r, "finish", map[string]any{"transaction_id": id}, 200)
	active, _ = r.DB.Bindings()
	if len(active) != 1001 {
		t.Fatal("finish altered formal binding")
	}
}

func TestDesktopTransactionChangedMemberRejectsWholeCommit(t *testing.T) {
	r, reg := localFixture(t)
	files := reg.Files
	reg.Files = nil
	digest, _ := LocalManifestDigest(files)
	begin := transactionRequest(t, r, "begin", map[string]any{"registration": reg, "expected_count": 1, "expected_bytes": files[0].Size, "manifest_digest": digest}, 200)
	id := begin["transaction_id"].(string)
	transactionRequest(t, r, "batch", map[string]any{"transaction_id": id, "offset": 0, "files": files}, 200)
	os.WriteFile(files[0].Path, []byte("changed"), 0600)
	transactionRequest(t, r, "commit", map[string]any{"transaction_id": id}, 409)
	active, _ := r.DB.Bindings()
	if len(active) != 0 {
		t.Fatal("changed member committed")
	}
	transactionRequest(t, r, "abort", map[string]any{"transaction_id": id}, 200)
}

func TestDesktopAuditFailureDoesNotInvalidateDirectoryCoverage(t *testing.T) {
	r, _ := localFixture(t)
	r.healthy.Store(true)
	r.protectionEnabled.Store(true)
	r.serverAvailable.Store(true)
	r.auditFailure(fmt.Errorf("Security permission absent"))
	if r.auditHealthy.Load() || r.HasGap() || !r.healthy.Load() {
		t.Fatal("audit permission failure polluted directory coverage")
	}
}

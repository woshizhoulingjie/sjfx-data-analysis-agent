//go:build windows

package endpoint

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
)

// The current protocol is tested without an external fixture or a default SKIP.
// Both local processes use the same core instance/version; no server-path copy.
func TestProtocolLocalEndToEnd(t *testing.T) {
	r, in := localFixture(t)
	server := httptest.NewServer(r.Handler())
	defer server.Close()
	request := func(path string, input any) map[string]json.RawMessage {
		raw, _ := json.Marshal(input)
		q, err := http.NewRequest("POST", server.URL+path, bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		q.Header.Set("Authorization", "Bearer "+localTestToken)
		q.Header.Set("Content-Type", "application/json")
		response, err := http.DefaultClient.Do(q)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		body, _ := io.ReadAll(response.Body)
		if response.StatusCode != 200 {
			t.Fatal(response.StatusCode, string(body))
		}
		out := map[string]json.RawMessage{}
		if err = json.Unmarshal(body, &out); err != nil {
			t.Fatal(err)
		}
		return out
	}
	registration := request("/agent-local/v1/register", in)
	var bindings []Binding
	if err := json.Unmarshal(registration["bindings"], &bindings); err != nil {
		t.Fatal(err)
	}
	b := bindings[0]
	if b.ID != in.Files[0].BindingID || b.VersionID != in.Files[0].VersionID {
		t.Fatal(b)
	}
	sync := LocalSyncRequest{DeviceID: in.DeviceID, ProtectionEnabled: true}
	request("/agent-local/v1/sync", sync)
	protect := action(b, "protect_delete")
	sync.Actions = []Action{protect}
	response := request("/agent-local/v1/sync", sync)
	var results []LocalActionResult
	json.Unmarshal(response["results"], &results)
	if len(results) != 1 || results[0].Result["status"] != "completed" {
		t.Fatal(results)
	}
	if err := os.Remove(b.Path); err == nil {
		t.Fatal("process-issued lock did not deny deletion before effect")
	}
	os.WriteFile(b.Path, []byte("changed local bytes"), 0600)
	sync.Actions = nil
	response = request("/agent-local/v1/sync", sync)
	var events []Event
	json.Unmarshal(response["events"], &events)
	seen := false
	for _, e := range events {
		if e.Action == "file.modified" && e.BindingID == b.ID && e.VersionID != b.VersionID {
			seen = true
		}
	}
	if !seen {
		t.Fatal(events)
	}
	sync.Actions = []Action{action(b, "quarantine")}
	response = request("/agent-local/v1/sync", sync)
	json.Unmarshal(response["results"], &results)
	for _, result := range results {
		if result.ActionID == sync.Actions[0].ID && result.Result["status"] != "stale" {
			t.Fatal("old version command applied", results)
		}
	}
	current := currentAction(t, r, "restore")
	current.RestoreID = protect.ID
	sync.ProtectionEnabled = false
	sync.Actions = []Action{current}
	response = request("/agent-local/v1/sync", sync)
	json.Unmarshal(response["results"], &results)
	restored := false
	for _, result := range results {
		if result.ActionID == current.ID && result.Result["status"] == "completed" {
			restored = true
		}
	}
	if !restored || len(r.holds) != 0 {
		t.Fatal(results)
	}
	if _, err := os.Stat(b.Path); err != nil {
		t.Fatal(err)
	}
	for _, result := range results {
		sync.AckResultIDs = append(sync.AckResultIDs, result.ActionID)
	}
	json.Unmarshal(response["events"], &events)
	for _, e := range events {
		sync.AckEventIDs = append(sync.AckEventIDs, e.EventID)
	}
	sync.Actions = nil
	response = request("/agent-local/v1/sync", sync)
	json.Unmarshal(response["results"], &results)
	if len(results) != 0 {
		t.Fatal("acknowledged results still pending", results)
	}
}

//go:build windows

package endpoint

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestUnboundWordTemporaryObservationUsesRegistrationScope(t *testing.T) {
	r, b := fixture(t)
	b.OwnerID, b.GapEpoch = "test-owner", 2
	if err := r.DB.PutBinding(b); err != nil {
		t.Fatal(err)
	}
	if err := r.DB.SaveState("monitoring_epochs", []LocalEpoch{{RegistrationID: b.RegistrationID, LocalRoot: b.LocalRoot, GapEpoch: 7}}); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(b.LocalRoot, "~$步行路线计划.docx")
	if selected, err := r.enqueueWatchObservation(path, "CREATE"); !selected || err != nil {
		t.Fatal(selected, err)
	}
	events, err := r.DB.Pending()
	if err != nil || len(events) != 1 {
		t.Fatal(events, err)
	}
	e := events[0]
	if e.Action != "monitor.directory_change_observed" || e.RegistrationID != b.RegistrationID || e.LocalRoot != b.LocalRoot || e.GapEpoch != 7 || e.DeviceID != b.DeviceID || e.OwnerID != b.OwnerID || e.Path != path || e.Detail != "CREATE" {
		t.Fatal("temporary observation lost registration scope", e)
	}
	if e.BindingID != "" || e.VersionID != "" || e.SHA256 != "" || e.Identity != "" || e.VersionEvidence != "" || e.Size != 0 {
		t.Fatal("unbound path claimed content identity or version", e)
	}
	if e.EventID == "" || e.Sequence < 1 || e.Occurred <= 0 {
		t.Fatal("observation was not durably sequenced", e)
	}
}

func TestBoundWatchObservationPreservesBindingEvidence(t *testing.T) {
	r, b := fixture(t)
	b.OwnerID, b.GapEpoch = "test-owner", 2
	if err := r.DB.PutBinding(b); err != nil {
		t.Fatal(err)
	}
	if err := r.DB.SaveState("monitoring_epochs", []LocalEpoch{{RegistrationID: b.RegistrationID, LocalRoot: b.LocalRoot, GapEpoch: 7}}); err != nil {
		t.Fatal(err)
	}
	if selected, err := r.enqueueWatchObservation(strings.ToUpper(b.Path), "WRITE"); !selected || err != nil {
		t.Fatal(selected, err)
	}
	events, err := r.DB.Pending()
	if err != nil || len(events) != 1 {
		t.Fatal(events, err)
	}
	e := events[0]
	if e.Action != "file.change_observed" || e.BindingID != b.ID || e.RegistrationID != b.RegistrationID || e.GapEpoch != b.GapEpoch || e.VersionID != b.VersionID || e.Identity != b.Identity || e.DeviceID != b.DeviceID || e.OwnerID != b.OwnerID {
		t.Fatal("bound observation lost original binding evidence", e)
	}
}

func TestWatchObservationRejectsUnselectedPaths(t *testing.T) {
	r, b := fixture(t)
	epoch := LocalEpoch{RegistrationID: b.RegistrationID, LocalRoot: b.LocalRoot}
	for _, tc := range []struct {
		name  string
		path  string
		epoch LocalEpoch
	}{
		{name: "outside", path: filepath.Join(filepath.Dir(b.LocalRoot), "outside.txt"), epoch: epoch},
		{name: "sibling prefix", path: filepath.Join(b.LocalRoot+"-other", "file.txt"), epoch: epoch},
		{name: "parent traversal", path: b.LocalRoot + `\..\outside.txt`, epoch: epoch},
		{name: "relative path", path: "file.txt", epoch: epoch},
		{name: "missing registration", path: b.Path, epoch: LocalEpoch{LocalRoot: b.LocalRoot}},
		{name: "missing root", path: b.Path, epoch: LocalEpoch{RegistrationID: b.RegistrationID}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := r.DB.SaveState("monitoring_epochs", []LocalEpoch{tc.epoch}); err != nil {
				t.Fatal(err)
			}
			if selected, err := r.enqueueWatchObservation(tc.path, "CREATE"); selected || err != nil {
				t.Fatal("unselected observation accepted", selected, err)
			}
		})
	}
	if r.DB.Depth() != 0 {
		t.Fatal("unselected paths were persisted")
	}
}

func TestOverlappingEpochsReceiveTheirOwnDirectoryObservations(t *testing.T) {
	r, b := fixture(t)
	b.OwnerID = "outer-owner"
	if err := r.DB.PutBinding(b); err != nil {
		t.Fatal(err)
	}
	nested := b
	nested.ID, nested.RegistrationID, nested.OwnerID = ID(), "nested-registration", "nested-owner"
	nested.LocalRoot = filepath.Join(b.LocalRoot, "nested")
	nested.Path = filepath.Join(nested.LocalRoot, "data.txt")
	if err := r.DB.PutBinding(nested); err != nil {
		t.Fatal(err)
	}
	epochs := []LocalEpoch{
		{RegistrationID: b.RegistrationID, LocalRoot: b.LocalRoot, GapEpoch: 3},
		{RegistrationID: nested.RegistrationID, LocalRoot: nested.LocalRoot, GapEpoch: 4},
		{RegistrationID: "unrelated", LocalRoot: b.LocalRoot + "-other", GapEpoch: 5},
	}
	if err := r.DB.SaveState("monitoring_epochs", epochs); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(nested.LocalRoot, "~$unbound.docx")
	if selected, err := r.enqueueWatchObservation(path, "REMOVE"); !selected || err != nil {
		t.Fatal(selected, err)
	}
	events, err := r.DB.Pending()
	if err != nil || len(events) != 2 {
		t.Fatal(events, err)
	}
	byRegistration := map[string]Event{}
	for _, e := range events {
		if e.Action != "monitor.directory_change_observed" || e.BindingID != "" || e.VersionID != "" || e.Identity != "" {
			t.Fatal("overlap fabricated file binding", e)
		}
		byRegistration[e.RegistrationID] = e
	}
	outer, inner := byRegistration[b.RegistrationID], byRegistration[nested.RegistrationID]
	if outer.OwnerID != b.OwnerID || outer.LocalRoot != b.LocalRoot || outer.GapEpoch != 3 || inner.OwnerID != nested.OwnerID || inner.LocalRoot != nested.LocalRoot || inner.GapEpoch != 4 {
		t.Fatal("overlapping scopes shared the wrong registration metadata", byRegistration)
	}
}

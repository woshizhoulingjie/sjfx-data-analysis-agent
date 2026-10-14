//go:build windows

package endpoint

import (
	"context"
	"database/sql"
	"errors"
	"testing"
)

func TestAuditRestoreFailureRetainsBackup(t *testing.T) {
	r, b := fixture(t)
	mockAudit(r, map[string]any{})
	if err := r.ConfigureAccessAudit(context.Background(), []Binding{b}); err != nil {
		t.Fatal(err)
	}
	key := "audit:" + b.ID + ":" + b.Identity
	var original string
	if err := r.DB.DB.QueryRow("SELECT value FROM state WHERE key=?", key).Scan(&original); err != nil {
		t.Fatal(err)
	}
	readbackFailure := errors.New("SACL restore readback differs from saved target")
	r.auditRunner = func(ctx context.Context, mode string, input, output any) error {
		if mode != "restore" {
			t.Fatalf("unexpected audit operation %q", mode)
		}
		files := input.(map[string]any)["files"].([]map[string]string)
		if len(files) != 1 || files[0]["path"] != b.Path || files[0]["expected"] != "after" || files[0]["target"] != "before" {
			t.Fatalf("restore did not use saved original target: %+v", files)
		}
		return readbackFailure
	}
	if err := r.RestoreAccessAudit(context.Background()); !errors.Is(err, readbackFailure) {
		t.Fatalf("restore returned %v; want readback failure", err)
	}
	var retained string
	if err := r.DB.DB.QueryRow("SELECT value FROM state WHERE key=?", key).Scan(&retained); err != nil || retained != original {
		t.Fatalf("failed restore lost original backup: value=%q error=%v", retained, err)
	}
	// A later successful verified restore may retire the same durable backup.
	r.auditRunner = func(ctx context.Context, mode string, input, output any) error { return nil }
	if err := r.RestoreAccessAudit(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := r.DB.DB.QueryRow("SELECT value FROM state WHERE key=?", key).Scan(&retained); !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("verified successful restore retained backup: %v", err)
	}
}

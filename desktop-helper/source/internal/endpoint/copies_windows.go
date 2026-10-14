//go:build windows

package endpoint

import (
	"context"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// Content equality discovers another instance only inside explicitly selected roots.
// This is not a claim about which process copied the data or its causal origin.
func (r *Runtime) DiscoverCopies(ctx context.Context) error {
	if !r.collectionEnabled() {
		return nil
	}
	r.operation.Lock()
	defer r.operation.Unlock()
	bindings, err := r.DB.Bindings()
	if err != nil {
		return err
	}
	roots, err := r.DB.Roots()
	if err != nil {
		return err
	}
	known := map[string]bool{}
	present := map[string]bool{}
	bySize := map[int64][]Binding{}
	for _, b := range bindings {
		known[strings.ToLower(filepath.Clean(b.Path))] = true
		if b.State != "unavailable" && b.Disposition != "quarantine" && b.Disposition != "read_lock" {
			bySize[b.Size] = append(bySize[b.Size], b)
		}
	}
	for _, root := range roots {
		err = filepath.WalkDir(root, func(path string, d fs.DirEntry, e error) error {
			if e != nil {
				return e
			}
			if e = ctx.Err(); e != nil {
				return e
			}
			candidateKey := strings.ToLower(filepath.Clean(path))
			if d.IsDir() {
				if strings.HasPrefix(d.Name(), ".sjfx-quarantine") {
					return filepath.SkipDir
				}
				return nil
			}
			if d.Type()&os.ModeSymlink != 0 || known[strings.ToLower(filepath.Clean(path))] {
				return nil
			}
			st, e := d.Info()
			if e != nil {
				return e
			}
			candidates := bySize[st.Size()]
			if len(candidates) == 0 || !st.Mode().IsRegular() {
				return nil
			}
			snap, e := Snapshot(path)
			if e != nil {
				return e
			}
			for _, original := range candidates {
				if snap.SHA256 != original.SHA256 {
					continue
				}
				// Extra local instances remain candidates until an explicit
				// complete manifest maps them. Hash equality is not identity.
				fingerprint := fmt.Sprintf("%s:%s:%s:%d", snap.Identity, snap.SHA256, original.RegistrationID, original.GapEpoch)
				present[candidateKey] = true
				if e = r.DB.EnqueueCandidate(candidateKey, fingerprint, Event{Action: "file.identical_candidate", Path: path, Identity: snap.Identity, SHA256: snap.SHA256, VersionID: snap.SHA256, Size: snap.Size, RegistrationID: original.RegistrationID, GapEpoch: original.GapEpoch, Detail: "本地额外实例内容相同；未自动归入登记成员，不证明复制来源"}); e != nil {
					return e
				}
				break
			}
			return nil
		})
		if err != nil {
			return err
		}
	}
	return r.DB.ForgetAbsentCandidates(present)
}

//go:build windows

package endpoint

import (
	"database/sql"
	"errors"
	"path/filepath"
)

// Directory observations describe a path change inside a registration scope.
// A temporary or otherwise unbound path has no proven content identity/version.
func (r *Runtime) enqueueWatchObservation(path, detail string) (bool, error) {
	var epochs []LocalEpoch
	if err := r.DB.LoadState("monitoring_epochs", &epochs); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return false, nil
		}
		return false, err
	}
	var selected []LocalEpoch
	seen := map[string]bool{}
	for _, epoch := range epochs {
		if epoch.RegistrationID == "" || !filepath.IsAbs(epoch.LocalRoot) || !filepath.IsAbs(path) || !withinRoot(epoch.LocalRoot, path) {
			continue
		}
		key := epoch.RegistrationID + "\x00" + pathKey(epoch.LocalRoot)
		if !seen[key] {
			selected = append(selected, epoch)
			seen[key] = true
		}
	}
	if len(selected) == 0 {
		return false, nil
	}
	bindings, err := r.DB.Bindings()
	if err != nil {
		return true, err
	}
	for _, epoch := range selected {
		deviceID, ownerID := r.Client.GetIdentity().DeviceID, ""
		bound := false
		for _, b := range bindings {
			if b.RegistrationID != epoch.RegistrationID || pathKey(b.LocalRoot) != pathKey(epoch.LocalRoot) {
				continue
			}
			if b.DeviceID != "" {
				deviceID = b.DeviceID
			}
			if b.OwnerID != "" {
				ownerID = b.OwnerID
			}
			if pathKey(b.Path) != pathKey(path) {
				continue
			}
			observation := Event{Action: "file.change_observed", Path: path, Detail: detail,
				BindingID: b.ID, RegistrationID: b.RegistrationID, LocalRoot: epoch.LocalRoot,
				GapEpoch: b.GapEpoch, VersionID: b.VersionID, Identity: b.Identity,
				DeviceID: b.DeviceID, OwnerID: b.OwnerID}
			if err := r.DB.Enqueue(observation); err != nil {
				return true, err
			}
			bound = true
		}
		if !bound {
			observation := Event{Action: "monitor.directory_change_observed", Path: path, Detail: detail,
				RegistrationID: epoch.RegistrationID, LocalRoot: epoch.LocalRoot, GapEpoch: epoch.GapEpoch,
				DeviceID: deviceID, OwnerID: ownerID}
			if err := r.DB.Enqueue(observation); err != nil {
				return true, err
			}
		}
	}
	return true, nil
}

//go:build windows

package endpoint

import (
	"context"
	"fmt"
	"time"
)

type heartbeatReply struct {
	Actions           []Action `json:"actions"`
	ProtectionEnabled bool     `json:"protection_enabled"`
}
type queuedMatch struct {
	CollectionID string            `json:"collection_id"`
	Root         string            `json:"local_root"`
	Mappings     map[string]string `json:"mappings"`
}

func (r *Runtime) refreshProtection(ctx context.Context) (heartbeatReply, error) {
	var response heartbeatReply
	status := r.Status()
	delete(status, "bindings")
	delete(status, "matching")
	if err := r.Client.Post(ctx, "/agent/v2/heartbeat", status, &response, false); err != nil {
		// A network failure revokes new dispositions, not the last consent to collect.
		if r.protectionEnabled.Load() {
			r.collecting.Store(true)
		}
		r.protectionEnabled.Store(false)
		r.serverAvailable.Store(false)
		r.healthy.Store(false)
		if gapErr := r.MarkGap("无法确认服务器保护策略：" + err.Error()); gapErr != nil {
			return response, gapErr
		}
		return response, err
	}
	previous := r.collecting.Swap(response.ProtectionEnabled)
	r.protectionEnabled.Store(response.ProtectionEnabled)
	r.serverAvailable.Store(true)
	if err := r.DB.SaveState("collection_enabled", response.ProtectionEnabled); err != nil {
		return response, err
	}
	if !response.ProtectionEnabled {
		r.healthy.Store(false)
		for _, path := range r.watcher.WatchList() {
			_ = r.watcher.Remove(path)
		}
		r.mu.Lock()
		r.watched = map[string]bool{}
		r.mu.Unlock()
	} else if !previous {
		r.forceHash.Store(true)
		r.dirty.Store(true)
	}
	_ = r.DB.SaveState("last_server_contact", time.Now().UTC().Format(time.RFC3339))
	if previous != response.ProtectionEnabled {
		if err := r.MarkGap("保护总开关状态变化：需显式完整核验后使用"); err != nil {
			return response, err
		}
	}
	return response, nil
}

func (r *Runtime) queueSelectedMatch(ctx context.Context, cid, root string, mappings map[string]string) error {
	var receipt any
	err := r.Client.Post(ctx, "/agent/v2/registration-manifest", map[string]any{"collection_id": cid, "registration_id": cid, "local_root": root, "offset": 0, "limit": 500}, &receipt, false)
	if saveErr := r.DB.SaveState("queued_selected_match", queuedMatch{CollectionID: cid, Root: root, Mappings: mappings}); saveErr != nil {
		return saveErr
	}
	state := map[string]any{"state": "queued", "root": root, "reason": "保护总开关关闭：已保留所选目录，开启后执行完整核验", "server_registered": err == nil}
	if err != nil {
		state["server_error"] = err.Error()
	}
	r.setMatch(state)
	return nil
}

func (r *Runtime) resumeQueuedMatch(ctx context.Context) {
	var queued queuedMatch
	if r.DB.LoadState("queued_selected_match", &queued) != nil || queued.CollectionID == "" {
		return
	}
	r.mu.Lock()
	running := r.matching["state"] == "running" || r.matching["state"] == "choosing"
	if !running {
		r.matching = map[string]any{"state": "running", "root": queued.Root}
		ctx, r.matchCancel = context.WithCancel(ctx)
	}
	r.mu.Unlock()
	if running {
		return
	}
	go func() {
		defer func() {
			r.mu.Lock()
			if r.matchCancel != nil {
				r.matchCancel()
				r.matchCancel = nil
			}
			r.mu.Unlock()
		}()
		if err := r.MatchRegistration(ctx, queued.CollectionID, queued.Root, queued.Mappings); err != nil {
			r.setMatch(map[string]any{"state": "failed", "error": fmt.Sprint(err)})
			r.failure(err)
		}
	}()
}

func (r *Runtime) collectionEnabled() bool {
	return r.collecting.Load() || r.protectionEnabled.Load()
}

func (r *Runtime) collectLocalAccess(ctx context.Context) error {
	bindings, err := r.DB.Bindings()
	if err == nil {
		err = r.ConfigureAccessAudit(ctx, bindings)
	}
	// Remaining batches are normal progress. Read already configured files meanwhile.
	if err == nil {
		err = r.VerifyAccessAudit(ctx, bindings)
	}
	if err == nil {
		err = r.CollectAccess(ctx)
	}
	r.auditHealthy.Store(err == nil && r.auditPending.Load() == 0 && !r.HasGap())
	if err == nil {
		r.mu.Lock()
		r.auditLastError = ""
		r.mu.Unlock()
	}
	return err
}

// Missing Security privileges must not falsely disable directory-change coverage.
func (r *Runtime) auditFailure(err error) {
	r.auditHealthy.Store(false)
	r.mu.Lock()
	r.auditLastError = err.Error()
	r.mu.Unlock()
	_ = r.DB.SaveState("access_audit_error", err.Error())
}

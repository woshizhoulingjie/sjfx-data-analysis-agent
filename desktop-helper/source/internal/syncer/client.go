// Package syncer 负责与服务端通信。
//
// 包名是 syncer 而不是 sync，是为了不和标准库的 sync 混淆。
//
// 只有两组动作（抄 Santa 的 preflight / eventupload 设计）：
//
//	preflight  —— 问服务端「我要盯什么、用什么策略、有没有要执行的处置」
//	eventupload —— 把本地事件报上去，响应里可能带回新的处置指令
//
// 接口少是刻意的：少到不可能做错。
package syncer

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/woshizhoulingjie/sjfx-agent/internal/model"
)

var (
	ErrUnauthorized = errors.New("syncer: node key 失效，需要重新注册")
	ErrNotEnrolled  = errors.New("syncer: 设备尚未注册")
)

// DeviceInfo 是注册时提交的设备信息。
type DeviceInfo struct {
	Hostname     string `json:"hostname"`
	OS           string `json:"os"`
	OSVersion    string `json:"os_version"`
	Arch         string `json:"arch"`
	HardwareUUID string `json:"hardware_uuid"` // 抄 Fleet：用硬件 UUID 唯一标识设备
	AgentVersion string `json:"agent_version"`
}

// EnrollResult 是注册结果。
//
// 服务端生成 node_key 并发下来，而不是让客户端自己生成——
// 凭据必须由服务端签发，客户端自报的凭据不可信。
type EnrollResult struct {
	DeviceID string `json:"device_id"`
	NodeKey  string `json:"node_key"`
}

// Heartbeat 是心跳内容。
//
// 它不只是「我还活着」——同时上报队列深度和监听目录数，
// 这样服务端能发现「应用在跑但监控已经失效」这种更隐蔽的故障。
type Heartbeat struct {
	AgentVersion     string         `json:"agent_version"`
	UptimeSeconds    int64          `json:"uptime_seconds"`
	WatchRoots       int            `json:"watch_roots"`
	QueueDepth       int            `json:"queue_depth"`
	LastEventUTC     *time.Time     `json:"last_event_utc,omitempty"`
	EnforcementState map[string]any `json:"enforcement_state,omitempty"`
}

// HeartbeatResult 里 SessionValid 是「不下应用就没法用系统」的执行点：
// 心跳断了，服务端把该设备上的会话作废。
type HeartbeatResult struct {
	OK                   bool   `json:"ok"`
	PolicyVersion        string `json:"policy_version"`
	NextHeartbeatSeconds int    `json:"next_heartbeat_seconds"`
	SessionValid         bool   `json:"session_valid"`
}

// PreflightResult 是策略与跟踪清单。
type PreflightResult struct {
	PolicyVersion    string              `json:"policy_version"`
	Tracked          []model.TrackedFile `json:"tracked"`
	TrackedTruncated bool                `json:"tracked_truncated"`
	Policies         model.Policies      `json:"policies"`
	Actions          []model.Action      `json:"actions"`
}

// UploadResult 是上报结果。
//
// Actions 让服务端能在收到「副本出现在桌面」的同一刻就回一条「隔离它」，
// 不用等下一轮 preflight。这是强制安装换来的能力。
type UploadResult struct {
	Accepted int            `json:"accepted"`
	Rejected []string       `json:"rejected"`
	Actions  []model.Action `json:"actions"`
}

// Client 是与服务端的全部交互面。
type Client interface {
	Enroll(ctx context.Context, enrollSecret string, info DeviceInfo) (EnrollResult, error)
	Heartbeat(ctx context.Context, hb Heartbeat) (HeartbeatResult, error)
	Preflight(ctx context.Context, knownPolicyVersion, trackedDigest string) (PreflightResult, error)
	UploadEvents(ctx context.Context, events []model.FileEvent) (UploadResult, error)
}

// ---------------------------------------------------------------------------
// HTTP 实现
// ---------------------------------------------------------------------------

// HTTPClient 是 Client 的 HTTP 实现。
type HTTPClient struct {
	BaseURL  string
	DeviceID string
	NodeKey  string
	HTTP     *http.Client
}

// NewHTTPClient 创建客户端。超时给得比较短，因为代理是后台进程，
// 阻塞上报不如尽快失败、进队列重试。
func NewHTTPClient(baseURL, deviceID, nodeKey string) *HTTPClient {
	return &HTTPClient{
		BaseURL:  baseURL,
		DeviceID: deviceID,
		NodeKey:  nodeKey,
		HTTP:     &http.Client{Timeout: 30 * time.Second},
	}
}

func (c *HTTPClient) post(ctx context.Context, path string, in, out any) error {
	body, err := json.Marshal(in)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	// node key 放在头里，不放 URL —— URL 会进日志、进浏览器历史。
	if c.NodeKey != "" {
		req.Header.Set("X-SJFX-NodeKey", c.NodeKey)
	}
	if c.DeviceID != "" {
		req.Header.Set("X-SJFX-Device", c.DeviceID)
	}

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	switch {
	case resp.StatusCode == http.StatusUnauthorized, resp.StatusCode == http.StatusForbidden:
		return ErrUnauthorized
	case resp.StatusCode >= 500:
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("syncer: 服务端错误 %d: %s", resp.StatusCode, string(b))
	case resp.StatusCode >= 400:
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("syncer: 请求被拒 %d: %s", resp.StatusCode, string(b))
	}

	return json.NewDecoder(resp.Body).Decode(out)
}

func (c *HTTPClient) Enroll(ctx context.Context, secret string, info DeviceInfo) (EnrollResult, error) {
	var out EnrollResult
	err := c.post(ctx, "/agent/v1/enroll", map[string]any{
		"enroll_secret": secret,
		"device":        info,
	}, &out)
	return out, err
}

func (c *HTTPClient) Heartbeat(ctx context.Context, hb Heartbeat) (HeartbeatResult, error) {
	var out HeartbeatResult
	err := c.post(ctx, "/agent/v1/heartbeat", hb, &out)
	return out, err
}

func (c *HTTPClient) Preflight(ctx context.Context, knownPolicyVersion, trackedDigest string) (PreflightResult, error) {
	var out PreflightResult
	err := c.post(ctx, "/agent/v1/preflight", map[string]any{
		"known_policy_version": knownPolicyVersion,
		"tracked_digest":       trackedDigest,
	}, &out)
	return out, err
}

func (c *HTTPClient) UploadEvents(ctx context.Context, events []model.FileEvent) (UploadResult, error) {
	var out UploadResult
	if len(events) == 0 {
		return out, nil
	}
	err := c.post(ctx, "/agent/v1/events", map[string]any{"events": events}, &out)
	return out, err
}

package enforce

import (
	"context"
	"log/slog"
	"runtime"
)

// NoopEnforcer 什么都不做，只记日志。
//
// 这是**默认实现**，这样在没写平台实现的机器上也能跑起来，
// 而且不会因为「以为装了防护其实没有」而误判系统的实际能力。
//
// 它故意返回 nil 而不是 ErrNotSupported：
// 策略应用是幂等的、每轮都会重试，返回错误会刷满日志却没有意义。
// 但每次调用都会打一条 Warn，让「防护未生效」这件事在日志里可见。
type NoopEnforcer struct {
	Log *slog.Logger
}

func (n *NoopEnforcer) log(op string, args ...any) {
	l := n.Log
	if l == nil {
		l = slog.Default()
	}
	l.Warn("处置未生效（NoopEnforcer）："+op, args...)
}

func (n *NoopEnforcer) Quarantine(_ context.Context, path, reason string) (Undo, error) {
	n.log("隔离", "path", path, "reason", reason)
	return Undo{Kind: "quarantine", Original: path}, nil
}

func (n *NoopEnforcer) DenyRead(_ context.Context, path, reason string) (Undo, error) {
	n.log("锁读取权限", "path", path, "reason", reason)
	return Undo{Kind: "deny_read", Original: path}, nil
}

func (n *NoopEnforcer) Restore(_ context.Context, u Undo) error {
	n.log("还原", "path", u.Original)
	return nil
}

func (n *NoopEnforcer) BlockRemovableWrite(_ context.Context, enable bool) error {
	n.log("可移动存储写保护", "enable", enable)
	return nil
}

func (n *NoopEnforcer) BlockDomain(_ context.Context, domains []string, enable bool) error {
	n.log("域名封锁", "domains", domains, "enable", enable)
	return nil
}

func (n *NoopEnforcer) ClearClipboard(_ context.Context) error {
	n.log("清空剪贴板")
	return nil
}

func (n *NoopEnforcer) CancelPrint(_ context.Context, printer string) error {
	n.log("取消打印", "printer", printer)
	return nil
}

// NewPlatformEnforcer 返回当前平台的实现。
//
// 各平台实现放在独立文件里，用 build tag 隔离：
//
//	enforcer_windows.go  //go:build windows
//	enforcer_darwin.go   //go:build darwin
//	enforcer_linux.go    //go:build linux
//
// 目前只有 Noop，Windows 实现在 enforcer_windows.go。
func NewPlatformEnforcer() Enforcer {
	switch runtime.GOOS {
	case "windows":
		return newWindowsEnforcer()
	default:
		return &NoopEnforcer{}
	}
}

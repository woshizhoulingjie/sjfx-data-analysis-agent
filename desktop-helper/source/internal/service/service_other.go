//go:build !windows

// Package service 在非 Windows 平台上是空实现。
//
// 服务化的方式各平台差别很大（systemd / launchd），
// 而本代理的目标平台是 Windows，其它平台先保证前台能跑。
package service

import "context"

// Name 是服务名。
const Name = "sjfx-agent"

// DisplayName 是服务显示名。
const DisplayName = "SJFX 文件监视代理"

// Run 在非 Windows 平台永远返回 (false, nil)，调用方回退到前台模式。
func Run(_ func(ctx context.Context) error) (bool, error) {
	return false, nil
}

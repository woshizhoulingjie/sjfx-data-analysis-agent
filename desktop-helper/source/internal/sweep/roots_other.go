//go:build !windows

package sweep

import (
	"os"
	"path/filepath"
)

// DefaultRoots 返回非 Windows 平台上的默认扫描位置。
//
// Linux/macOS 上「可移动设备」通常在 /media/<user>/ 或 /Volumes/ 下，
// 这里做基础覆盖；更精确的做法是读取挂载表，但收益不大——
// 本代理的目标平台是 Windows，其它平台先保证能跑。
func DefaultRoots() []string {
	var roots []string

	if home, err := os.UserHomeDir(); err == nil {
		for _, sub := range []string{"Desktop", "Downloads", "Documents"} {
			p := filepath.Join(home, sub)
			if isDir(p) {
				roots = append(roots, p)
			}
		}
	}

	// 常见可移动设备挂载点
	for _, base := range []string{"/media", "/run/media", "/Volumes", "/mnt"} {
		entries, err := os.ReadDir(base)
		if err != nil {
			continue
		}
		for _, e := range entries {
			if e.IsDir() {
				roots = append(roots, filepath.Join(base, e.Name()))
			}
		}
	}

	return roots
}

func isDir(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.IsDir()
}

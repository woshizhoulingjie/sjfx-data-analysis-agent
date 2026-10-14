//go:build windows

package sweep

import (
	"os"
	"path/filepath"

	"golang.org/x/sys/windows"
)

// 驱动器类型常量。
//
// 这里自己定义而不是引用 windows.DRIVE_*，是为了让代码在
// x/sys 版本变化时不会因为常量改名而编译失败。
const (
	driveRemovable = 2
	driveFixed     = 3
	driveRemote    = 4
)

// DefaultRoots 返回 Windows 上「文件最可能被放过去」的位置。
//
// 两类：
//  1. 桌面 / 下载 / 文档 —— 用户拿到文件后第一时间的落点
//  2. 可移动设备 —— 把文件带出去最直接的通道
//
// 刻意**不包含**整块系统盘：那等于全盘扫描，既慢又越界。
func DefaultRoots() []string {
	var roots []string

	if home, err := os.UserHomeDir(); err == nil {
		for _, sub := range []string{"Desktop", "Downloads", "Documents"} {
			p := filepath.Join(home, sub)
			if isDir(p) {
				roots = append(roots, p)
			}
		}
		// OneDrive 的桌面/文档会被重定向到这里
		oneDrive := filepath.Join(home, "OneDrive")
		for _, sub := range []string{"Desktop", "Documents"} {
			p := filepath.Join(oneDrive, sub)
			if isDir(p) {
				roots = append(roots, p)
			}
		}
	}

	// 可移动设备：从 D: 开始（A:/B: 是软驱，C: 是系统盘）
	for c := 'D'; c <= 'Z'; c++ {
		root := string(c) + `:\`
		if !isDir(root) {
			continue
		}
		if driveType(root) == driveRemovable {
			roots = append(roots, root)
		}
	}

	return roots
}

func isDir(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.IsDir()
}

func driveType(root string) uint32 {
	p, err := windows.UTF16PtrFromString(root)
	if err != nil {
		return 0
	}
	return windows.GetDriveType(p)
}

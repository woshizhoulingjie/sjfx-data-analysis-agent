// sjfx-agent —— 本地文件监视代理
//
// 依赖在实现时用 go get 添加，然后跑 go mod tidy 锁定版本：
//
//	go get github.com/fsnotify/fsnotify      // 跨平台文件系统监听
//	go get modernc.org/sqlite                // 纯 Go SQLite（不需要 cgo）
//	go get golang.org/x/sys/windows/svc      // Windows 服务
//	go get github.com/energye/systray        // 托盘
//
// 这里不预置 require 块，是为了避免写入未经核实的版本号。
module github.com/woshizhoulingjie/sjfx-agent

go 1.26.0

require (
	github.com/fsnotify/fsnotify v1.10.1
	modernc.org/sqlite v1.59.0
)

require (
	github.com/dustin/go-humanize v1.0.1 // indirect
	github.com/google/uuid v1.6.0 // indirect
	github.com/mattn/go-isatty v0.0.24 // indirect
	github.com/ncruces/go-strftime v1.0.0 // indirect
	github.com/remyoudompheng/bigfft v0.0.0-20230129092748-24d4a6f8daec // indirect
	golang.org/x/sys v0.48.0 // indirect
	modernc.org/libc v1.75.7 // indirect
	modernc.org/mathutil v1.7.1 // indirect
	modernc.org/memory v1.12.1 // indirect
)

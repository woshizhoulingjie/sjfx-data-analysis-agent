// sjfx-agent 的入口。
//
// 三种用法：
//
//	# 装机时执行一次（需要管理员权限）
//	sjfx-agent.exe -install -server https://... -enroll-secret C:\...\enroll_secret
//
//	# 运维查看
//	sjfx-agent.exe -status
//	sjfx-agent.exe -uninstall
//
//	# 调试：前台跑
//	sjfx-agent.exe -console -server http://127.0.0.1:18099 -mem-queue -log-level debug
//
// 不加 -console 且不是由服务管理器启动时，会提示并回退到前台模式，
// 这样同一个 exe 既能装成服务也能直接跑，方便排查。
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"github.com/woshizhoulingjie/sjfx-agent/internal/agent"
	"github.com/woshizhoulingjie/sjfx-agent/internal/enforce"
	"github.com/woshizhoulingjie/sjfx-agent/internal/hash"
	"github.com/woshizhoulingjie/sjfx-agent/internal/identity"
	"github.com/woshizhoulingjie/sjfx-agent/internal/service"
	"github.com/woshizhoulingjie/sjfx-agent/internal/store"
	"github.com/woshizhoulingjie/sjfx-agent/internal/syncer"
	"github.com/woshizhoulingjie/sjfx-agent/internal/watch"
)

// version 由构建时注入：
//
//	go build -ldflags "-X main.version=0.1.0" ./cmd/sjfx-agent
var version = "dev"

func main() {
	var (
		serverURL = flag.String("server", os.Getenv("SJFX_SERVER_URL"), "sjfx 服务端地址")
		dataDir   = flag.String("data-dir", defaultDataDir(), "本地数据目录")
		secretPth = flag.String("enroll-secret", defaultEnrollSecretPath(), "一次性注册密钥文件")
		logLevel  = flag.String("log-level", "info", "日志级别 debug/info/warn/error")
		memQueue  = flag.Bool("mem-queue", false, "用内存队列（仅调试，生产禁用）")
		console   = flag.Bool("console", false, "前台运行")

		sweepEvery  = flag.Duration("sweep-every", 6*time.Hour, "定期普查的间隔")
		sweepRoots  = flag.String("sweep-roots", "", "普查要扫的目录（逗号分隔）；留空用平台默认值")
		watchRoots  = flag.String("watch-roots", "", "实时监听要覆盖的目录（逗号分隔）；留空用平台默认落点（下载/桌面/文档）")
		uploadEvery = flag.Duration("upload-every", 15*time.Second, "上报间隔")
		hashEvery   = flag.Duration("hash-worker-every", 30*time.Second, "后台补算哈希的间隔")
		hashUnknown = flag.Bool("hash-unknown", false,
			"对名字+大小匹配不上的新文件也读内容算哈希（覆盖更全，但会读无关文件）")

		doInstall   = flag.Bool("install", false, "安装为 Windows 服务（需管理员）")
		doUninstall = flag.Bool("uninstall", false, "卸载 Windows 服务（需管理员）")
		doStatus    = flag.Bool("status", false, "查看服务状态")
		showVersion = flag.Bool("version", false, "打印版本")
	)
	flag.Parse()

	log := newLogger(*logLevel)
	slog.SetDefault(log)

	if *showVersion {
		fmt.Printf("sjfx-agent %s (%s/%s)\n", version, runtime.GOOS, runtime.GOARCH)
		return
	}

	if *doStatus {
		st, err := service.Status()
		if err != nil {
			fmt.Printf("查询失败: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("服务 %s: %s\n", service.Name, st)
		return
	}

	if *doInstall {
		installService(log, *serverURL, *secretPth)
		return
	}

	if *doUninstall {
		if err := service.Uninstall(); err != nil {
			log.Error("卸载失败", "err", err)
			os.Exit(1)
		}
		log.Info("服务已卸载（本地队列和隔离区保留未删）")
		return
	}

	if *serverURL == "" {
		log.Error("必须指定服务端地址：-server 或环境变量 SJFX_SERVER_URL")
		os.Exit(2)
	}

	// 先试服务模式。不是被 SCM 启动时返回 false，回退到前台。
	opts := options{
		serverURL:   *serverURL,
		dataDir:     *dataDir,
		secretPath:  *secretPth,
		memQueue:    *memQueue,
		sweepEvery:  *sweepEvery,
		sweepRoots:  splitList(*sweepRoots),
		watchRoots:  splitList(*watchRoots),
		uploadEvery: *uploadEvery,
		hashEvery:   *hashEvery,
		hashUnknown: *hashUnknown,
		log:         log,
	}

	if !*console {
		served, err := service.Run(func(ctx context.Context) error {
			return run(ctx, opts)
		})
		if err != nil {
			log.Error("服务模式运行失败", "err", err)
			os.Exit(1)
		}
		if served {
			return
		}
		log.Warn("不是由服务管理器启动，改为前台运行；" +
			"要常驻请用 -install 安装为服务")
	}

	log.Info("sjfx-agent 启动",
		"version", version, "server", *serverURL, "data_dir", *dataDir,
		"os", runtime.GOOS, "sweep_every", opts.sweepEvery)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if err := run(ctx, opts); err != nil {
		log.Error("运行失败", "err", err)
		os.Exit(1)
	}
	log.Info("sjfx-agent 已退出")
}

// options 是运行参数。
//
// 打成一个结构体而不是一长串参数，是因为它要穿过
// service.Run 的回调闭包——参数一多就容易传错位置。
type options struct {
	serverURL   string
	dataDir     string
	secretPath  string
	memQueue    bool
	sweepEvery  time.Duration
	sweepRoots  []string
	watchRoots  []string
	uploadEvery time.Duration
	hashEvery   time.Duration
	hashUnknown bool
	log         *slog.Logger
}

// splitList 把逗号分隔的参数切成切片，顺手去掉空项。
func splitList(s string) []string {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// installService 把当前 exe 注册成服务，并把运行参数写进服务配置。
//
// 参数写进服务配置而不是靠环境变量：
// 服务由 SCM 启动，**不继承**用户的会话环境，
// 靠环境变量传参在服务模式下会全部丢失。
func installService(log *slog.Logger, serverURL, secretPath string) {
	if serverURL == "" {
		log.Error("安装时必须指定服务端地址：-server")
		os.Exit(2)
	}

	args := []string{
		"-server", serverURL,
		"-enroll-secret", secretPath,
		"-log-level", "info",
	}
	if err := service.Install(args); err != nil {
		log.Error("安装服务失败", "err", err)
		os.Exit(1)
	}
	log.Info("服务已安装并启动", "name", service.Name, "args", strings.Join(args, " "))
	log.Info("查看状态: sjfx-agent.exe -status；卸载: sjfx-agent.exe -uninstall")
}

func run(ctx context.Context, opts options) error {
	log := opts.log

	// ---- 1. 身份：已有就复用，没有就注册 ----
	//
	// 这是必须最先完成的事。没有 node key，后面所有同步都会 401。
	//
	// 注册用的客户端是匿名态（此刻还没有 node key）。
	bootstrap := syncer.NewHTTPClient(opts.serverURL, "", "")

	mgr := &identity.Manager{
		Store:  identity.NewFileStore(opts.dataDir),
		Secret: identity.NewFileSecret(opts.secretPath),
		Client: bootstrap,
		Info:   deviceInfo(),
	}
	id, err := mgr.Ensure(ctx)
	if err != nil {
		return fmt.Errorf("设备注册失败: %w", err)
	}
	log.Info("设备身份就绪", "device_id", id.DeviceID)

	// ---- 2. 各模块 ----
	client := syncer.NewHTTPClient(opts.serverURL, id.DeviceID, id.NodeKey)

	watcher, err := watch.New(watch.DefaultOptions())
	if err != nil {
		return fmt.Errorf("初始化文件监听失败: %w", err)
	}

	var st store.Store
	if opts.memQueue {
		log.Warn("使用内存队列——进程退出会丢失未上报事件，仅供调试")
		st = store.NewMemoryStore(0)
	} else {
		sq, err := store.OpenSQLite(opts.dataDir)
		if err != nil {
			return fmt.Errorf("打开本地队列失败: %w", err)
		}
		st = sq
	}

	// ---- 3. 编排并运行 ----
	a := agent.New(agent.Config{
		ServerURL:             opts.serverURL,
		AgentVersion:          version,
		SweepEvery:            opts.sweepEvery,
		SweepRoots:            opts.sweepRoots,
		WatchRoots:            opts.watchRoots,
		UploadEvery:           opts.uploadEvery,
		HashWorkerEvery:       opts.hashEvery,
		HashUnknownCandidates: opts.hashUnknown,
	}, agent.Deps{
		Client:   client,
		Watcher:  watcher,
		Store:    st,
		Index:    store.NewMemoryIndex(),
		Enforcer: enforce.NewPlatformEnforcer(),
		Hasher:   hash.NewCached(4096),
		Logger:   log,
	})

	return a.Run(ctx)
}

func newLogger(level string) *slog.Logger {
	var lv slog.Level
	switch level {
	case "debug":
		lv = slog.LevelDebug
	case "warn":
		lv = slog.LevelWarn
	case "error":
		lv = slog.LevelError
	default:
		lv = slog.LevelInfo
	}
	// 服务模式没有控制台，输出会被丢弃；同时写一份到数据目录下的日志文件，
	// 否则出问题时用户拿不到任何线索。
	var w *os.File
	if pd := os.Getenv("ProgramData"); pd != "" {
		dir := filepath.Join(pd, "sjfx", "agent")
		if err := os.MkdirAll(dir, 0o700); err == nil {
			if f, err := os.OpenFile(filepath.Join(dir, "agent.log"),
				os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600); err == nil {
				w = f
			}
		}
	}

	var h slog.Handler
	opts := &slog.HandlerOptions{Level: lv}
	if w != nil {
		// 控制台和文件同时写：调试时看屏幕，装机后看文件
		h = newMultiHandler(
			slog.NewTextHandler(os.Stderr, opts),
			slog.NewTextHandler(w, opts),
		)
	} else {
		h = slog.NewTextHandler(os.Stderr, opts)
	}
	return slog.New(h)
}

// multiHandler 把日志同时写到多个目的地。
//
// 单独写一个是因为 slog 标准库没有内置的 multi handler（截至 Go 1.27）。
type multiHandler struct {
	handlers []slog.Handler
}

func newMultiHandler(hs ...slog.Handler) slog.Handler {
	return &multiHandler{handlers: hs}
}

func (m *multiHandler) Enabled(ctx context.Context, l slog.Level) bool {
	for _, h := range m.handlers {
		if h.Enabled(ctx, l) {
			return true
		}
	}
	return false
}

func (m *multiHandler) Handle(ctx context.Context, r slog.Record) error {
	var firstErr error
	for _, h := range m.handlers {
		if !h.Enabled(ctx, r.Level) {
			continue
		}
		if err := h.Handle(ctx, r.Clone()); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

func (m *multiHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	out := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		out[i] = h.WithAttrs(attrs)
	}
	return &multiHandler{handlers: out}
}

func (m *multiHandler) WithGroup(name string) slog.Handler {
	out := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		out[i] = h.WithGroup(name)
	}
	return &multiHandler{handlers: out}
}

func defaultDataDir() string {
	if pd := os.Getenv("ProgramData"); pd != "" {
		return filepath.Join(pd, "sjfx", "agent")
	}
	return "/var/lib/sjfx-agent"
}

func defaultEnrollSecretPath() string {
	if pd := os.Getenv("ProgramData"); pd != "" {
		return filepath.Join(pd, "sjfx", "enroll_secret")
	}
	return "/etc/sjfx-agent/enroll_secret"
}

// deviceInfo 收集注册需要的设备信息。
//
// 硬件 UUID 是设备唯一标识——主机名会改、会重名，不能用它做标识
// （这是 Fleet 踩过的坑，它专门提供了 --host-identifier 来兜底）。
func deviceInfo() syncer.DeviceInfo {
	host, _ := os.Hostname()
	return syncer.DeviceInfo{
		Hostname:     host,
		OS:           runtime.GOOS,
		OSVersion:    osVersion(),
		Arch:         runtime.GOARCH,
		HardwareUUID: hardwareUUID(),
		AgentVersion: version,
	}
}

// osVersion 返回系统版本。
// TODO: Windows 用 RtlGetVersion 或注册表 CurrentVersion；
//
//	Linux 读 /etc/os-release；macOS 用 sw_vers。
func osVersion() string { return "" }

// hardwareUUID 返回硬件 UUID。
//
// TODO: Windows 取 Win32_ComputerSystemProduct.UUID（WMI）；
//
//	Linux 读 /sys/class/dmi/id/product_uuid；
//	macOS 用 ioreg 的 IOPlatformUUID。
//
// 取不到时**不能随便编一个**——那样每台机器都会拿到不同的假 ID，
// 重装代理后会被当成新设备。宁可留空让服务端用主机名兜底。
func hardwareUUID() string { return "" }

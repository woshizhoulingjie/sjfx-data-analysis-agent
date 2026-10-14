//go:build windows

// Package service 把代理封装成 Windows 服务。
//
// 为什么要做成服务而不是开机自启的程序：
//
//  1. **权限**。处置动作（改 ACL、写 HKLM 注册表、封锁 U 盘）
//     需要 SYSTEM 权限，普通用户进程做不到。
//  2. **生命周期**。服务进程与用户会话无关——用户注销、切换用户、
//     甚至没登录，代理都还在跑。这正是「不下应用就没法用系统」
//     这个约束能成立的前提。
//  3. **不易被随手关掉**。托盘程序用户可以右键退出，
//     服务要靠服务管理器（需要管理员权限）才能停。
package service

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

// Name 是服务名。sc.exe 命令和事件日志都用它。
const Name = "sjfx-agent"

// DisplayName 是服务管理器里显示的名字。
const DisplayName = "SJFX 文件监视代理"

// Run 以服务方式运行。
//
// 返回 (false, nil) 表示「当前不是被服务管理器启动的」，
// 调用方应该回退到前台模式——这样同一个 exe
// 既能被 SCM 启动，也能在命令行直接跑，方便调试。
func Run(run func(ctx context.Context) error) (bool, error) {
	isService, err := svc.IsWindowsService()
	if err != nil {
		// 判断不了就当作不是服务，走前台。宁可前台跑也不要不跑。
		return false, nil
	}
	if !isService {
		return false, nil
	}

	if err := svc.Run(Name, &handler{run: run}); err != nil {
		return true, err
	}
	return true, nil
}

type handler struct {
	run    func(ctx context.Context) error
	cancel context.CancelFunc
	done   chan struct{}
}

// Execute 是服务管理器调用的入口。
//
// 注意这里必须**及时**响应状态查询和停止请求：
// 服务管理器有超时，太久不回应会被判定为服务无响应并强制杀掉。
func (h *handler) Execute(_ []string, r <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	status <- svc.Status{State: svc.StartPending}

	ctx, cancel := context.WithCancel(context.Background())
	h.cancel = cancel
	h.done = make(chan struct{})

	go func() {
		defer close(h.done)
		if err := h.run(ctx); err != nil {
			// 服务本身不因为运行错误而退出——退出后 SCM 会按恢复策略重启，
			// 但那样会造成重启风暴。记录错误、保持存活，让上层的重试逻辑处理。
			writeEventLog(fmt.Sprintf("代理运行出错: %v", err))
		}
	}()

	status <- svc.Status{
		State:   svc.Running,
		Accepts: svc.AcceptStop | svc.AcceptShutdown,
	}

	for c := range r {
		switch c.Cmd {
		case svc.Interrogate:
			status <- c.CurrentStatus

		case svc.Stop, svc.Shutdown:
			status <- svc.Status{State: svc.StopPending}
			cancel()

			// 给代理一点时间收尾（关监听、刷队列）。
			// 超时就硬退——SCM 不会无限等。
			select {
			case <-h.done:
			case <-time.After(15 * time.Second):
				writeEventLog("停止超时，强制退出；队列中可能有未上报的事件")
			}
			return false, 0
		}
	}
	return false, 0
}

// writeEventLog 把消息写进 Windows 事件日志。
//
// 服务没有控制台，出错时如果只写 stderr，用户永远看不到。
// 事件日志是服务唯一的、用户可见的排错入口。
// TODO: 用 golang.org/x/sys/windows/svc/eventlog 注册事件源后写入；
//
//	未注册事件源时 InstallAsEventCreate 需要管理员权限。
func writeEventLog(msg string) {
	// 暂时退化成写文件——比丢掉强，且不需要注册事件源。
	f, err := os.OpenFile(`C:\ProgramData\sjfx\agent\service.log`,
		os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = fmt.Fprintf(f, "%s %s\n", time.Now().Format(time.RFC3339), msg)
}

// ---------------------------------------------------------------------------
// 安装 / 卸载
//
// 这两步都需要管理员权限——服务是系统级对象。
// 所以安装包（MSI 或安装脚本）必须以管理员身份运行。
// ---------------------------------------------------------------------------

// Install 把当前 exe 注册成自动启动的服务。
//
// args 是传给服务的命令行参数（比如 -server 和 -enroll-secret 的路径）。
// 这些参数会写进服务配置，SCM 启动服务时带上它们。
func Install(args []string) error {
	exePath, err := os.Executable()
	if err != nil {
		return err
	}

	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("连接服务管理器失败（需要管理员权限）: %w", err)
	}
	defer m.Disconnect()

	// 已存在就先删掉，保证 Install 是幂等的
	if s, err := m.OpenService(Name); err == nil {
		_ = s.Delete()
		s.Close()
		time.Sleep(500 * time.Millisecond) // 等 SCM 真正删完，否则创建会失败
	}

	s, err := m.CreateService(Name, exePath, mgr.Config{
		DisplayName: DisplayName,
		Description: "监控已入库文件在本机的操作并上报 sjfx。不监视其它文件。",
		StartType:   mgr.StartAutomatic,
	}, args...)
	if err != nil {
		return fmt.Errorf("创建服务失败: %w", err)
	}
	defer s.Close()

	// 失败自动重启。这是「监控不能悄悄停掉」的兜底：
	// 服务挂了三次之后就不再自动重启，避免无限重启风暴。
	if err := s.SetRecoveryActions([]mgr.RecoveryAction{
		{Type: mgr.ServiceRestart, Delay: 10 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 30 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 60 * time.Second},
	}, 0); err != nil {
		// 恢复策略设不上不该让安装失败，但要让用户知道
		writeEventLog(fmt.Sprintf("设置自动重启失败（服务仍已安装）: %v", err))
	}

	return s.Start()
}

// Uninstall 停止并删除服务。
//
// 注意：这**不会**删除本地队列和隔离区。
// 卸载时把用户还没上报的事件删掉，等于帮人消灭证据——
// 那是绝不允许的。要清理数据必须显式做。
func Uninstall() error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("连接服务管理器失败（需要管理员权限）: %w", err)
	}
	defer m.Disconnect()

	s, err := m.OpenService(Name)
	if err != nil {
		if strings.Contains(err.Error(), "does not exist") {
			return nil // 本来就没装，视为成功
		}
		return err
	}
	defer s.Close()

	// 先尝试优雅停止
	_, _ = s.Control(svc.Stop)
	for i := 0; i < 30; i++ {
		st, err := s.Query()
		if err != nil || st.State == svc.Stopped {
			break
		}
		time.Sleep(time.Second)
	}

	return s.Delete()
}

// Status 返回服务的当前状态，用于排查和托盘展示。
func Status() (string, error) {
	m, err := mgr.Connect()
	if err != nil {
		// 连服务管理器需要管理员权限。这是最常见的失败原因，
		// 直接把原因说出来，别让用户去猜 "Access is denied" 是什么意思。
		return "", fmt.Errorf("查询服务状态需要管理员权限（请在管理员终端里执行）: %w", err)
	}
	defer m.Disconnect()

	s, err := m.OpenService(Name)
	if err != nil {
		return "未安装", nil
	}
	defer s.Close()

	st, err := s.Query()
	if err != nil {
		return "", err
	}
	switch st.State {
	case svc.Stopped:
		return "已停止", nil
	case svc.StartPending:
		return "启动中", nil
	case svc.Running:
		return "运行中", nil
	case svc.StopPending:
		return "停止中", nil
	case svc.Paused:
		return "已暂停", nil
	default:
		return fmt.Sprintf("未知(%d)", st.State), nil
	}
}

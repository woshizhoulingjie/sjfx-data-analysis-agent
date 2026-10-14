// Package enforce 执行服务端下发的处置动作。
//
// 三条硬规则：
//
//  1. **每个动作都必须可撤销。** 误判要能恢复。所以每个执行方法都返回
//     一个 Undo 记录，Restore 靠它还原。不可逆的动作不许加进来。
//
//  2. **只执行，不判断。** 判定在服务端做。这一层拿到的是结论，
//     不该有任何「我觉得这个文件可疑」的逻辑。
//
//  3. **全部用户态实现，不碰内核驱动。** 需要驱动的是「拦截动作」
//     （在 open/write 那一刻拦住），那不在本包的范围内。
//     本包做的是「让目标失效」——不拦动作，让文件打不开、发不出。
package enforce

import (
	"context"
	"errors"
	"time"

	"github.com/woshizhoulingjie/sjfx-agent/internal/model"
)

var (
	ErrNotSupported = errors.New("enforce: 当前平台不支持该动作")
	ErrNotAdmin     = errors.New("enforce: 需要管理员/SYSTEM 权限")
)

// Undo 是一个已执行的处置动作的撤销凭据。
//
// 它必须被持久化（跟事件一起上报、本地也留一份），
// 否则进程重启后就再也无法还原用户的文件。
type Undo struct {
	ActionID string           `json:"action_id"`
	Kind     model.ActionKind `json:"kind"`
	Original string           `json:"original"`           // 处置前的路径
	Applied  string           `json:"applied,omitempty"`  // 处置后的路径（隔离时是新位置）
	Previous string           `json:"previous,omitempty"` // 被覆盖的旧状态（如原 ACL）
	At       time.Time        `json:"at"`
}

// Enforcer 是处置执行器。
//
// 实现分平台：
//
//	enforcer_windows.go   隔离走文件移动，锁权限走 icacls，U盘封锁走 HKLM 注册表
//	enforcer_linux.go     锁权限走 chmod/chattr，U盘封锁走 udev 规则
//	enforcer_darwin.go    锁权限走 chmod，U盘封锁走配置描述文件
type Enforcer interface {
	// Quarantine 把文件移到隔离目录并改扩展名，使它无法被正常程序打开。
	// 隔离目录：%ProgramData%\sjfx\quarantine\<date>\<hash>.<ext>.quarantined
	Quarantine(ctx context.Context, path, reason string) (Undo, error)

	// DenyRead 给文件加一条拒绝读取的 ACL，使它仍留在原地但打不开。
	// 适用于「必须保留在原位置」的场景（比如用户正在引用的文件）。
	DenyRead(ctx context.Context, path, reason string) (Undo, error)

	// Restore 撤销一次处置。这是误判的唯一补救手段，必须可靠。
	Restore(ctx context.Context, undo Undo) error

	// BlockRemovableWrite 开关可移动存储的写入。
	//
	// Windows 实现是一行注册表：
	//	HKLM\SYSTEM\CurrentControlSet\Control\StorageDevicePolicies\WriteProtect = 1
	// 不需要驱动，立即生效，整个通道一起关掉——这是性价比最高的一条阻断。
	BlockRemovableWrite(ctx context.Context, enable bool) error

	// BlockDomain 通过 hosts 或防火墙规则封锁域名（网盘、危险站点）。
	BlockDomain(ctx context.Context, domains []string, enable bool) error

	// ClearClipboard 清空剪贴板。用于命中敏感内容时的即时处置。
	ClearClipboard(ctx context.Context) error

	// CancelPrint 取消打印任务。
	CancelPrint(ctx context.Context, printerName string) error
}

// Dispatcher 把服务端下发的 Action 落到具体执行方法上，并记录 Undo。
type Dispatcher struct {
	Enforcer Enforcer
	OnUndo   func(Undo) // 持久化回调：Undo 必须落盘
}

// Execute 执行一条服务端指令。
//
// 注意 Actions 是幂等的：服务端可能重发同一条 action_id，
// 实现方要能识别「这条已经执行过了」并直接返回成功，不要重复处置。
func (d *Dispatcher) Execute(ctx context.Context, a model.Action) (Undo, error) {
	switch a.Kind {
	case model.ActionQuarantine:
		return d.record(d.Enforcer.Quarantine(ctx, a.Path, a.Reason))
	case model.ActionDenyRead:
		return d.record(d.Enforcer.DenyRead(ctx, a.Path, a.Reason))
	case model.ActionRestore:
		// Restore 需要具体的 Undo 记录，由调用方按 action_id 查出后单独调用
		return Undo{}, errors.New("enforce: restore 需要 Undo 记录，请用 RestoreUndo")
	case model.ActionBlockRemovable:
		return Undo{}, d.Enforcer.BlockRemovableWrite(ctx, true)
	case model.ActionBlockDomain:
		return Undo{}, d.Enforcer.BlockDomain(ctx, a.Domains, true)
	case model.ActionClearClipboard:
		return Undo{}, d.Enforcer.ClearClipboard(ctx)
	case model.ActionCancelPrint:
		return Undo{}, d.Enforcer.CancelPrint(ctx, a.Path)
	default:
		return Undo{}, errors.New("enforce: 未知动作 " + string(a.Kind))
	}
}

func (d *Dispatcher) record(u Undo, err error) (Undo, error) {
	if err != nil {
		return Undo{}, err
	}
	u.At = time.Now().UTC()
	if d.OnUndo != nil {
		d.OnUndo(u)
	}
	return u, nil
}

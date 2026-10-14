//go:build windows

package enforce

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// windowsEnforcer 是 Windows 上的处置实现。
//
// 全部用用户态手段——不碰内核驱动。核心思路是：
//
//	不去拦「打开文件」这个动作，而是让文件本身失效。
//	需要拦动作的是另一码事（minifilter 驱动），不在本包范围。
//
// ⚠️ 这个文件里的命令构造**尚未在真实环境验证**。
//
//	上生产前必须在测试机上逐条验证，尤其 Quarantine 和 DenyRead——
//	写错 ACL 会让用户彻底打不开自己的文件。
type windowsEnforcer struct {
	quarantineRoot string
}

func newWindowsEnforcer() Enforcer {
	root := os.Getenv("ProgramData")
	if root == "" {
		root = `C:\ProgramData`
	}
	return &windowsEnforcer{quarantineRoot: filepath.Join(root, "sjfx", "quarantine")}
}

// Quarantine 把文件移到隔离目录并加 .quarantined 后缀。
//
// 为什么用「移动 + 改后缀」而不是加密或删除：
//   - 移动是原子的，失败也不会留下半个文件
//   - 改后缀让双击打不开（Windows 不再知道用什么程序打开它）
//   - 完全可撤销：移回去、改回后缀就还原了
//
// 用 os.Rename 优先（同盘原子），跨盘时回退到「复制 + 删除」。
func (w *windowsEnforcer) Quarantine(_ context.Context, path, reason string) (Undo, error) {
	info, err := os.Stat(path)
	if err != nil {
		return Undo{}, err
	}
	if info.IsDir() {
		return Undo{}, errors.New("enforce: 暂不支持隔离目录")
	}

	day := time.Now().Format("2006-01-02")
	dstDir := filepath.Join(w.quarantineRoot, day)
	if err := os.MkdirAll(dstDir, 0o700); err != nil {
		return Undo{}, err
	}

	// 目标名带上时间戳，避免同名文件互相覆盖——
	// 覆盖会直接毁掉上一份被隔离的文件，不可接受。
	dst := filepath.Join(dstDir,
		fmt.Sprintf("%d_%s.quarantined", time.Now().UnixNano(), filepath.Base(path)))

	if err := os.Rename(path, dst); err != nil {
		// 跨盘：复制 + 删除
		if err2 := copyFile(path, dst); err2 != nil {
			return Undo{}, fmt.Errorf("隔离失败: %w / %v", err, err2)
		}
		if err2 := os.Remove(path); err2 != nil {
			_ = os.Remove(dst) // 回滚，避免留下两份
			return Undo{}, err2
		}
	}

	// 顺手写一条说明，让用户（和以后的自己）知道为什么被隔离。
	_ = os.WriteFile(dst+".reason.txt",
		[]byte("隔离原因: "+reason+"\n原路径: "+path+"\n时间: "+time.Now().Format(time.RFC3339)+"\n"),
		0o600)

	return Undo{
		Kind:     "quarantine",
		Original: path,
		Applied:  dst,
	}, nil
}

// DenyRead 用 icacls 加一条拒绝读取的 ACE。
//
// 适用场景：文件必须留在原位置（用户正在引用它），但不能再被打开。
// 比隔离更克制，代价是 ACL 的语义比「移动文件」复杂，出错面更大。
//
// ⚠️ 必须记住原有的 ACL 才能还原。这里用 icacls /save 先把当前 ACL 存下来。
func (w *windowsEnforcer) DenyRead(ctx context.Context, path, reason string) (Undo, error) {
	aclBackup := path + ".sjfx-acl.bak"
	if out, err := exec.CommandContext(ctx, "icacls", path, "/save", aclBackup).CombinedOutput(); err != nil {
		return Undo{}, fmt.Errorf("备份 ACL 失败: %v: %s", err, out)
	}

	// 对 Everyone 拒绝读取。用 SID 而不是名字，避免中文系统上的本地化差异。
	out, err := exec.CommandContext(ctx, "icacls", path, "/deny", "*S-1-1-0:(R)").CombinedOutput()
	if err != nil {
		return Undo{}, fmt.Errorf("设置拒绝读取失败: %v: %s", err, out)
	}

	return Undo{
		Kind:     "deny_read",
		Original: path,
		Previous: aclBackup,
	}, nil
}

// Restore 撤销一次处置。
//
// 这是误判的唯一补救手段，必须可靠——所以它对两种动作分开处理，
// 且还原失败时要保留证据（隔离文件不会被删掉）。
func (w *windowsEnforcer) Restore(_ context.Context, u Undo) error {
	switch u.Kind {
	case "quarantine":
		if u.Applied == "" {
			return errors.New("enforce: 撤销凭据缺少隔离后路径")
		}
		if _, err := os.Stat(u.Original); err == nil {
			// 原位置已经有同名文件了——不能覆盖，
			// 那是用户后来自己放的文件。
			return fmt.Errorf("原位置已存在同名文件，未还原: %s", u.Original)
		}
		if err := os.MkdirAll(filepath.Dir(u.Original), 0o755); err != nil {
			return err
		}
		if err := os.Rename(u.Applied, u.Original); err != nil {
			return err
		}
		_ = os.Remove(u.Applied + ".reason.txt")
		return nil

	case "deny_read":
		if u.Previous == "" {
			return errors.New("enforce: 撤销凭据缺少 ACL 备份")
		}
		out, err := exec.Command("icacls", filepath.Dir(u.Original), "/restore", u.Previous).CombinedOutput()
		if err != nil {
			return fmt.Errorf("恢复 ACL 失败: %v: %s", err, out)
		}
		_ = os.Remove(u.Previous)
		return nil

	default:
		return fmt.Errorf("enforce: 不支持撤销该动作 %q", u.Kind)
	}
}

// BlockRemovableWrite 通过注册表开关可移动存储的写入。
//
// 这是性价比最高的一条阻断：一行注册表，整个通道一起关掉，
// 不需要驱动、不需要拦每一次写操作、立即生效。
//
//	HKLM\SYSTEM\CurrentControlSet\Control\StorageDevicePolicies
//	    WriteProtect (DWORD) = 1  →  禁止写入
//
// 注意：需要管理员/SYSTEM 权限。当前用户是本地管理员时，
// 他也能自己删掉这条键——用户态防护防不住有意的对抗，
// 但防得住绝大多数「顺手拷一份」。
func (w *windowsEnforcer) BlockRemovableWrite(ctx context.Context, enable bool) error {
	val := "0"
	if enable {
		val = "1"
	}
	key := `HKLM\SYSTEM\CurrentControlSet\Control\StorageDevicePolicies`
	if out, err := exec.CommandContext(ctx, "reg", "add", key,
		"/v", "WriteProtect", "/t", "REG_DWORD", "/d", val, "/f").CombinedOutput(); err != nil {
		return fmt.Errorf("设置 U 盘写保护失败: %v: %s", err, out)
	}
	return nil
}

// BlockDomain 通过 hosts 文件封锁域名。
//
// 用 hosts 而不是防火墙规则：hosts 一行一条、可读可审计、
// 出问题一眼能看出改了什么，而且还原就是删掉那几行。
// 防火墙规则更"强大"但出错时更难排查。
func (w *windowsEnforcer) BlockDomain(_ context.Context, domains []string, enable bool) error {
	hosts := filepath.Join(os.Getenv("SystemRoot"), "System32", "drivers", "etc", "hosts")
	if hosts == "" || !strings.HasPrefix(hosts, `C:\`) {
		hosts = `C:\Windows\System32\drivers\etc\hosts`
	}

	data, err := os.ReadFile(hosts)
	if err != nil {
		return err
	}

	const begin = "# >>> sjfx-agent block begin >>>"
	const end = "# <<< sjfx-agent block end <<<"

	// 先剥掉旧的段落，保证幂等——重复调用不会把 hosts 越写越长。
	cleaned := stripBlock(string(data), begin, end)

	if enable && len(domains) > 0 {
		var sb strings.Builder
		sb.WriteString(cleaned)
		if !strings.HasSuffix(cleaned, "\n") {
			sb.WriteString("\n")
		}
		sb.WriteString(begin + "\n")
		for _, d := range domains {
			sb.WriteString("0.0.0.0 " + d + "\n")
			sb.WriteString("0.0.0.0 www." + d + "\n")
		}
		sb.WriteString(end + "\n")
		cleaned = sb.String()
	}

	// 备份一次原始内容，便于人工还原
	_ = os.WriteFile(hosts+".sjfx.bak", data, 0o600)
	return os.WriteFile(hosts, []byte(cleaned), 0o644)
}

func (w *windowsEnforcer) ClearClipboard(_ context.Context) error {
	// TODO: 用 golang.org/x/sys/windows 调 OpenClipboard/EmptyClipboard。
	//       注意：OpenClipboard 可能因为别的进程占用而失败，要重试。
	return ErrNotSupported
}

func (w *windowsEnforcer) CancelPrint(_ context.Context, printerName string) error {
	// TODO: 用 WMI（Win32_PrintJob 的 Delete 方法）取消任务，
	//       或调 OpenPrinter/SetJob。需要打印机名或任务 ID。
	_ = printerName
	return ErrNotSupported
}

// ---------------------------------------------------------------------------

func stripBlock(s, begin, end string) string {
	i := strings.Index(s, begin)
	if i < 0 {
		return s
	}
	j := strings.Index(s[i:], end)
	if j < 0 {
		return s[:i]
	}
	return s[:i] + s[i+j+len(end)+1:]
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	if _, err := out.ReadFrom(in); err != nil {
		return err
	}
	return out.Sync()
}

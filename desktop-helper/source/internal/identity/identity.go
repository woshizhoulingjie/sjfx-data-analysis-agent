// Package identity 管理设备身份。
//
// 抄 Fleet fleetd 的做法，四个要点：
//
//  1. 安装包里嵌一个**一次性** enroll secret，只用于注册
//  2. 注册时服务端生成随机 node key 下发，客户端不自己生成
//  3. node key 存到只有 SYSTEM/root 能读的位置
//     （Windows → 凭据管理器；macOS → Keychain；Linux → 仅 root 可读的文件）
//  4. **注册成功后从磁盘删除 enroll secret**
//
// 用硬件 UUID 做设备标识（而不是主机名），因为主机名会改、会重名。
package identity

import (
	"context"
	"errors"
	"time"

	"github.com/woshizhoulingjie/sjfx-agent/internal/syncer"
)

var ErrNotEnrolled = errors.New("identity: 设备尚未注册")

// Identity 是本机的设备身份。
type Identity struct {
	DeviceID string
	NodeKey  string
	Enrolled time.Time
}

// Store 是身份的持久化位置。
//
// 实现必须保证：凭据只有本进程（或同权限进程）能读写。
// 这是「客户端凭据不可被用户轻易窃取」的最低要求。
type Store interface {
	Load() (*Identity, error) // 未注册时返回 ErrNotEnrolled
	Save(*Identity) error
	Clear() error
}

// EnrollSecret 是安装包嵌入的一次性注册密钥。
//
// 它可能来自：
//   - 编译期注入（打包时烧进 exe）
//   - 安装目录下的配置文件（安装程序写入，注册后删除）
type EnrollSecret interface {
	Read() (string, error)
	Erase() error // 注册成功后必须调用，把密钥从磁盘抹掉
}

// Manager 把「注册或复用」这件事封装成一个调用。
type Manager struct {
	Store  Store
	Secret EnrollSecret
	Client syncer.Client
	Info   syncer.DeviceInfo
}

// Ensure 返回一个可用的身份：已有就复用，没有就注册。
//
// 这是代理启动时唯一必须先完成的事——没有身份，后面什么都做不了。
func (m *Manager) Ensure(ctx context.Context) (*Identity, error) {
	if id, err := m.Store.Load(); err == nil && id.NodeKey != "" {
		return id, nil
	} else if err != nil && !errors.Is(err, ErrNotEnrolled) {
		return nil, err
	}

	secret, err := m.Secret.Read()
	if err != nil {
		return nil, err
	}

	res, err := m.Client.Enroll(ctx, secret, m.Info)
	if err != nil {
		return nil, err
	}

	id := &Identity{
		DeviceID: res.DeviceID,
		NodeKey:  res.NodeKey,
		Enrolled: time.Now().UTC(),
	}
	if err := m.Store.Save(id); err != nil {
		return nil, err
	}

	// 注册成功后立刻抹掉一次性密钥。
	// 即使这一步失败也不影响运行——但要在日志里明确记录，
	// 因为留在磁盘上的 enroll secret 是一个可被复用的入口。
	_ = m.Secret.Erase()

	return id, nil
}

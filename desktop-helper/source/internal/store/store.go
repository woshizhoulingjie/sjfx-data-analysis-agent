// Package store 是代理的本地持久化层，有两件事：
//
//  1. 事件队列 —— 断网时不丢事件
//  2. 跟踪索引 —— 本地判断「这个文件要不要上报」
//
// 用 SQLite（modernc.org/sqlite，纯 Go 不用 cgo，交叉编译简单）。
package store

import (
	"errors"
	"time"

	"github.com/woshizhoulingjie/sjfx-agent/internal/model"
)

var (
	ErrQueueFull = errors.New("store: 事件队列已满")
	ErrClosed    = errors.New("store: 已关闭")
)

// Store 是本地事件队列。
//
// 上报成功后必须 MarkSent，否则下次会重复上报。
// 重复上报不是致命的（服务端按 event_id 去重），但会浪费流量和配额。
type Store interface {
	Open() error
	Close() error

	// Enqueue 入队一条事件。队列满时返回 ErrQueueFull。
	Enqueue(ev model.FileEvent) error

	// DequeueBatch 取出最多 n 条尚未上报的事件（不标记为已发）。
	DequeueBatch(n int) ([]model.FileEvent, error)

	// MarkSent 标记已成功上报。
	MarkSent(eventIDs []string) error

	// Depth 返回待上报条数，用于心跳上报和托盘展示。
	Depth() (int, error)

	// PendingHash 返回还没算哈希的未上报事件（Hashed == HashedNo）。
	//
	// 为什么需要"事后补算"：
	//   大文件（现在定在 64 MiB 以上）不能在采集循环里算哈希——
	//   往监听目录里丢一个 10GB 的文件，整个采集就会被它卡死。
	//   但直接跳过又会**永久漏掉这条事件**。
	//   所以先入队（Hashed=0），由后台慢慢补。
	PendingHash(limit int) ([]model.FileEvent, error)

	// SetHash 给一条已入队的事件补上哈希。
	SetHash(eventID, sha256 string, hashed model.HashedState) error

	// Prune 清理已上报且超过保留期的事件，防止本地库无限增长。
	Prune(olderThan time.Duration) (int, error)
}

// Index 是本地跟踪索引：服务端下发的「要盯哪些文件」在本地的副本。
//
// 为什么要两边都能查：
//   - 按路径查（快）：绝大多数事件是「已知路径上的改动」，路径命中就够
//   - 按哈希查（慢但全）：改名、复制产生的是**新路径**，路径查不到，
//     只能靠算出来的哈希反查
//
// 这个双查询是本模块存在的理由，见下面 Filter 的说明。
type Index interface {
	// ReplaceTracked 用服务端最新清单整体替换本地索引。
	ReplaceTracked(files []model.TrackedFile) error

	TrackedByPath(path string) (model.TrackedFile, bool)
	TrackedByHash(sha256 string) (model.TrackedFile, bool)

	// TrackedByName 用文件名做**廉价预筛**，不读文件内容。
	//
	// # 为什么需要它，以及为什么按名字而不是「名字+大小」
	//
	// 判断一个陌生文件是不是被跟踪的那份，最终必须比内容（哈希）。
	// 但比内容意味着**要读整个文件**——在一个装满无关文件的下载目录里，
	// 对每个新文件都读一遍内容，既慢又是对用户隐私的越界
	// （我们承诺的是"只追踪已入库文件"）。
	//
	// 一开始我用「名字+大小」当门槛，实测发现致命缺陷：
	//
	//	被跟踪文件被改过之后**大小变了** → 门槛不命中 → 事件被丢掉
	//	而"改过之后的文件"恰恰最需要追踪。
	//
	// 而且单靠哈希也认不出被改过的文件——哈希已经变了。
	// **名字是唯一的线索。** 所以：
	//
	//	名字对得上 → 去算哈希（可能确认，也可能是"疑似被改过的版本"）
	//	名字对不上 → 不读内容，直接丢弃（除非开了 HashUnknownCandidates）
	//
	// 名字比较大小写不敏感（Windows 上文件名本来就不区分大小写）。
	TrackedByName(name string) (model.TrackedFile, bool)

	// Remember 记住一个「已被确认属于某个跟踪文件的实例」。
	//
	// 为什么必须有这个方法（端到端实测挖出来的两个漏洞）：
	//
	//	漏洞一 · 改过之后版本对不上
	//	  服务端下发的清单里是文件**当时**的哈希 H1。用户改了文件 → 变成 H2。
	//	  「修改」本身能被路径命中，但接着「复制／改名」出现的内容都是 H2，
	//	  拿 H2 去索引里查只有 H1 → 查不到 → 事件全丢。
	//
	//	漏洞二 · 副本的后续事件对不上
	//	  用户在桌面复制出一份副本，路径是新的、不在清单里。
	//	  副本是靠哈希反查认出来的，但**只有那一次**认出来。
	//	  之后用户删掉这个副本，事件走路径查询 → 查不到 → 又丢了。
	//
	// 本地完全知道「这个路径／这个哈希属于那份被跟踪文件」，所以由本地记住，
	// 不必等下一轮 preflight 从服务端补。
	Remember(path, sha256 string, tf model.TrackedFile)

	Count() int
}

// Filter 判断一个刚发生变化的路径「要不要上报」。
//
// 策略是两级，因为单靠任何一级都不够：
//
//	第一级 · 路径命中（快）
//	    路径在跟踪清单里 → 直接上报，然后算哈希确认。
//	    覆盖：原地修改、删除。这是绝大多数事件。
//
//	第二级 · 哈希反查（慢但必要）
//	    路径查不到时，才算哈希，再拿哈希去索引里反查。
//	    覆盖：改名（新路径不在清单里）、复制（副本路径不在清单里）。
//	    这两类正是「文件在本地做了什么」的核心场景，不能漏。
//
// 成本控制：第二级只对 watch 目录里**新出现**的文件做，
// 不对全盘做。并且大文件可以延迟算（先入队 hashed=0，后台补算后再更新）。
type Filter struct {
	Index Index
}

// Verdict 是一次过滤的结论。
type Verdict struct {
	Report bool   // 是否上报
	Hash   string // 需要算哈希时由调用方回填
	Reason string // 命中原因：path / hash / none，排查用
}

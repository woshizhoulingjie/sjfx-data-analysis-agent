// Package model 定义代理与服务端之间的数据契约。
//
// 字段命名对齐 OCSF 1001 (File System Activity)，不要自己发明名字——
// 用标准枚举，以后想对接任何 SIEM / 分析工具都是零成本。
package model

import "time"

// ActivityID 对齐 OCSF File System Activity 的 activity_id。
//
// 代理实际会产生的只有 Create / Update / Delete / Rename / Open 五种，
// 其余列出来是为了完整性，不要删。
type ActivityID int

const (
	ActivityUnknown       ActivityID = 0
	ActivityCreate        ActivityID = 1
	ActivityRead          ActivityID = 2
	ActivityUpdate        ActivityID = 3
	ActivityDelete        ActivityID = 4
	ActivityRename        ActivityID = 5
	ActivitySetAttributes ActivityID = 6
	ActivitySetSecurity   ActivityID = 7
	ActivityGetAttributes ActivityID = 8
	ActivityGetSecurity   ActivityID = 9
	ActivityEncrypt       ActivityID = 10
	ActivityDecrypt       ActivityID = 11
	ActivityMount         ActivityID = 12
	ActivityUnmount       ActivityID = 13
	ActivityOpen          ActivityID = 14
)

var activityNames = map[ActivityID]string{
	ActivityUnknown:       "unknown",
	ActivityCreate:        "created",
	ActivityRead:          "read",
	ActivityUpdate:        "updated",
	ActivityDelete:        "deleted",
	ActivityRename:        "renamed",
	ActivitySetAttributes: "set_attributes",
	ActivitySetSecurity:   "set_security",
	ActivityGetAttributes: "get_attributes",
	ActivityGetSecurity:   "get_security",
	ActivityEncrypt:       "encrypt",
	ActivityDecrypt:       "decrypt",
	ActivityMount:         "mount",
	ActivityUnmount:       "unmount",
	ActivityOpen:          "open",
}

func (a ActivityID) String() string {
	if s, ok := activityNames[a]; ok {
		return s
	}
	return "unknown"
}

// Valid 用于自检：确保不会往服务端发未定义的动作。
func (a ActivityID) Valid() bool {
	_, ok := activityNames[a]
	return ok && a != ActivityUnknown
}

// HashedState 抄 osquery 的 hashed 字段设计。
//
// 为什么要单独一个字段：必须区分「没算哈希」和「算了但是空」。
// 混在一起会让上层无法判断这条事件到底可不可信。
type HashedState int

const (
	HashedFailed HashedState = -1 // 算了但失败（文件被独占、权限不足）
	HashedNo     HashedState = 0  // 还没算
	HashedYes    HashedState = 1  // 已算出
)

// Collector 标识事件是哪个采集器产生的。
// 排查问题时要能一眼看出「这条是实时抓到的还是普查补上的」。
type Collector string

const (
	CollectorWatch   Collector = "watch"   // 实时文件系统监听
	CollectorSweep   Collector = "sweep"   // 定期普查
	CollectorEnforce Collector = "enforce" // 处置动作自身产生的事件
)

// MatchKind 说明这条事件与跟踪清单的匹配强度。
//
// 为什么必须区分：服务端路径和本地路径是两个命名空间，
// 本地只能靠「内容」或「名字」去认，两者的把握程度天差地别：
//
//	exact   —— 内容哈希一致，**可以确认**就是同一份文件。
//	suspect —— 名字对得上但内容不同，**只是疑似**同一份文件的修改版；
//	           也可能是恰好同名的无关文件，需要服务端或人工判断。
//
// 把两者混在一起报，等于把"确证"和"猜测"混为一谈——
// 那会让整个审计结论的可信度打折。
type MatchKind string

const (
	MatchExact    MatchKind = "exact"   // 哈希一致，已确认
	MatchSuspect  MatchKind = "suspect" // 名字一致但哈希不同，疑似
	MatchUnmarked MatchKind = ""        // 未做匹配（如删除事件，文件已不在）
)

// FileEvent 一条本地文件事件。
//
// 这是整个代理的核心数据结构：采集器产出它，store 排队它，
// sync 上报它，服务端拿它和系统内履历按 sha256 对账。
type FileEvent struct {
	EventID    string      `json:"event_id"`            // 客户端生成的唯一 ID，服务端用它去重
	Time       time.Time   `json:"time"`                // 事件发生时间（UTC）
	ActivityID ActivityID  `json:"activity_id"`         // OCSF 枚举
	Sha256     string      `json:"sha256,omitempty"`    // 内容哈希 —— 与服务端对账的唯一键
	Hashed     HashedState `json:"hashed"`              // 哈希计算状态
	Path       string      `json:"path"`                // 当前路径（**本地路径**，不是服务端路径）
	OldPath    string      `json:"old_path,omitempty"`  // rename 时的旧路径
	FileName   string      `json:"file_name,omitempty"` // 文件名（便于展示与预筛）
	Size       int64       `json:"size,omitempty"`      // 字节数
	Collector  Collector   `json:"collector"`           // 来源采集器
	Category   string      `json:"category,omitempty"`  // 路径分类标签（抄 osquery 的 category）
	Match      MatchKind   `json:"match,omitempty"`     // 匹配强度：exact / suspect

	// ProcessName 在 Windows 用户态多半拿不到（Sysmon 从 v6.10 起也不提供文件读取日志）。
	// 留空是正常的，不要为了填它去上驱动。
	ProcessName string `json:"process_name,omitempty"`
}

// TrackedFile 服务端下发的「要盯的文件」。
//
// # 字段语义（非常重要，搞错会导致整个追踪失效）
//
// 服务端路径和用户电脑上的路径是**两个不同的命名空间**：
//
//	服务端                         用户电脑
//	/home/nas511/zhoulingjie/      C:\Users\xxx\Downloads\
//	        report.pdf        ≠           report.pdf
//	                     ↑
//	             只有 Sha256 能连接它们
//
// 所以：
//
//	Sha256 —— **唯一的匹配键**。本地一切都是靠它。
//	Name   —— 原始文件名。本地做「廉价预筛」用：不读内容就能排除掉绝大多数文件。
//	Size   —— 原始大小。同上。
//	Path   —— 服务端路径，**仅作展示**（告诉用户"这是服务器上的哪份文件"）。
//	          本地匹配**绝不能用它**——在 Windows 上
//	          "/home/nas511/x.pdf" 会被解释成 `\home\nas511\x.pdf` 这种不存在的路径。
//
// 本地要监听哪些目录，由**客户端自己决定**（见 agent.computeWatchRoots），
// 不能从 Path 推导。
type TrackedFile struct {
	Sha256 string `json:"sha256"`           // 主键：唯一跨端可用的标识
	Name   string `json:"name,omitempty"`   // 原始文件名（本地预筛用）
	Size   int64  `json:"size,omitempty"`   // 原始大小（本地预筛用）
	Path   string `json:"path,omitempty"`   // 服务端路径（仅展示）
	Origin string `json:"origin,omitempty"` // 来源说明（谁导入的/哪次导入），仅展示
}

// Policies 服务端下发的策略开关。
// 客户端只执行，不判断——规则改了不用更新客户端。
type Policies struct {
	QuarantineEnabled   bool     `json:"quarantine_enabled"`    // 允许隔离危险文件
	RemovableWriteBlock bool     `json:"removable_write_block"` // 封锁 U 盘写入
	BlockedDomains      []string `json:"blocked_domains,omitempty"`
	ClipboardGuard      bool     `json:"clipboard_guard"`
	PrintBlock          bool     `json:"print_block"`
}

// ActionKind 是客户端能执行的全部动作——就这么几个，不再多。
//
// 客户端是「薄执行器」：判定在服务端，这里只有固定的几个动作。
// 动作少是刻意的，少到不可能做错。
type ActionKind string

const (
	ActionQuarantine     ActionKind = "quarantine"      // 移到隔离目录 + 改扩展名
	ActionDenyRead       ActionKind = "deny_read"       // 改 ACL 拒绝读取
	ActionRestore        ActionKind = "restore"         // 撤销上面的处置
	ActionBlockRemovable ActionKind = "block_removable" // U 盘写保护开关
	ActionBlockDomain    ActionKind = "block_domain"    // 写 hosts / 防火墙
	ActionClearClipboard ActionKind = "clear_clipboard"
	ActionCancelPrint    ActionKind = "cancel_print"
)

// Action 服务端下发的处置指令。
//
// 每个动作都必须可撤销——误判要能恢复。ActionRestore 就是干这个的。
type Action struct {
	ActionID string     `json:"action_id"`
	Kind     ActionKind `json:"kind"`
	Path     string     `json:"path,omitempty"`
	Domains  []string   `json:"domains,omitempty"`
	Reason   string     `json:"reason,omitempty"` // 为什么处置它，用于向用户展示
}

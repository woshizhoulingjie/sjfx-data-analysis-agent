package store

import (
	"strings"
	"sync"
	"time"

	"github.com/woshizhoulingjie/sjfx-agent/internal/model"
)

// ---------------------------------------------------------------------------
// MemoryIndex：跟踪索引的内存实现
//
// 索引很小（只放服务端下发的跟踪清单，通常几千到几万条），
// 内存完全放得下。做成内存态的好处是查询是 O(1) 且无 IO。
//
// 代价是每次重启都要重新 preflight 拉全量清单。
// 后续可以落盘缓存，但当下的取舍是：简单优先。
// ---------------------------------------------------------------------------

type MemoryIndex struct {
	mu     sync.RWMutex
	byPath map[string]model.TrackedFile
	byHash map[string]model.TrackedFile
	byName map[string]model.TrackedFile // 键：小写文件名
}

func NewMemoryIndex() *MemoryIndex {
	return &MemoryIndex{
		byPath: make(map[string]model.TrackedFile),
		byHash: make(map[string]model.TrackedFile),
		byName: make(map[string]model.TrackedFile),
	}
}

// nameKey 生成文件名索引键。统一转小写：
// Windows 上文件名不区分大小写，用户把 Report.PDF 和 report.pdf 当同一个文件。
func nameKey(name string) string {
	if name == "" {
		return ""
	}
	return strings.ToLower(name)
}

// ReplaceTracked 整体替换，不做增量合并。
//
// 整体替换是有意的：服务端清单是权威，本地做增量合并的话，
// 一旦某次同步漏了「删除」指令，本地就会永久保留已经不存在的文件，
// 造成「跟踪清单只增不减」。整体替换天然没有这个问题。
//
// 关于 byPath：这里也把服务端路径放进去，但它**只对「代理装在
// 能直接访问服务端同一份存储的机器上」这种情况有用**（比如一台
// 挂载了同一个 NAS 的 Linux 工作站）。对普通用户电脑，服务端路径
// 永远不可能匹配——真正起作用的是 byHash 和 byName。
// 判断"要监听哪些本地目录"时**绝不能**用这些路径。
func (m *MemoryIndex) ReplaceTracked(files []model.TrackedFile) error {
	byPath := make(map[string]model.TrackedFile, len(files))
	byHash := make(map[string]model.TrackedFile, len(files))
	byName := make(map[string]model.TrackedFile, len(files))

	for _, f := range files {
		if f.Path != "" {
			byPath[f.Path] = f
		}
		if f.Sha256 != "" {
			byHash[f.Sha256] = f
		}
		name := f.Name
		if name == "" {
			// 服务端没给 Name 时退回用服务端路径的 basename——
			// 原始文件名通常和它是同一个。注意不能用 filepath.Base：
			// 服务端是 Linux 风格路径，在 Windows 上会被当成一整段。
			name = pathBase(f.Path)
		}
		if name != "" {
			byName[nameKey(name)] = f
		}
	}

	m.mu.Lock()
	m.byPath = byPath
	m.byHash = byHash
	m.byName = byName
	m.mu.Unlock()
	return nil
}

// pathBase 取路径的最后一段，同时兼容 Windows 的 \ 和 Unix 的 /。
//
// 不能用 filepath.Base：服务端路径是 Linux 风格的 /a/b/c，
// 在 Windows 上 filepath.Base 会把它当成一整段（分隔符不匹配）。
func pathBase(p string) string {
	if i := strings.LastIndexAny(p, `/\`); i >= 0 {
		return p[i+1:]
	}
	return p
}

func (m *MemoryIndex) TrackedByPath(path string) (model.TrackedFile, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	f, ok := m.byPath[path]
	return f, ok
}

func (m *MemoryIndex) TrackedByHash(sum string) (model.TrackedFile, bool) {
	if sum == "" {
		return model.TrackedFile{}, false
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	f, ok := m.byHash[sum]
	return f, ok
}

// TrackedByName 用文件名做**廉价预筛**，不读文件内容。
//
// # 为什么按名字而不是「名字+大小」
//
// 一开始我用「名字+大小」当预筛条件，实测发现它有个致命缺陷：
//
//	被跟踪文件被改过之后，**大小变了** → 预筛不命中 → 事件被丢掉
//	而"改过之后的文件"恰恰是最需要追踪的。
//
// 所以大小不能当**门槛**，只能当提示。名字才是门槛：
//
//	名字对得上 → 去算哈希（可能确认，也可能是"疑似被改过的版本"）
//	名字对不上 → 不读内容，直接丢弃（除非开了 HashUnknownCandidates）
//
// 单靠哈希也认不出被改过的文件——哈希已经变了。**名字是唯一的线索。**
func (m *MemoryIndex) TrackedByName(name string) (model.TrackedFile, bool) {
	key := nameKey(name)
	if key == "" {
		return model.TrackedFile{}, false
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	f, ok := m.byName[key]
	return f, ok
}

// Remember 把一个「已确认属于某个跟踪文件的实例」记进索引。
//
// 见 Index 接口上的说明：没有这一步，
//
//	· 文件被改过之后再复制／改名 → 追踪不到
//	· 副本被认出来之后又被删除 → 追踪不到
//
// 注意它不覆盖已有的条目：同一份文件的不同版本、不同副本
// 都应该指向同一个跟踪记录，后来的不该把先前的挤掉。
func (m *MemoryIndex) Remember(path, sum string, tf model.TrackedFile) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if path != "" {
		if _, exists := m.byPath[path]; !exists {
			m.byPath[path] = tf
		}
	}
	if sum != "" {
		if _, exists := m.byHash[sum]; !exists {
			m.byHash[sum] = tf
		}
	}
}

func (m *MemoryIndex) Count() int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return len(m.byPath)
}

// ---------------------------------------------------------------------------
// MemoryStore：事件队列的内存实现
//
// 只用于开发调试和单元测试。**生产不能用**——
// 进程一挂，队列里没上报的事件就全丢了，
// 而「事件不丢」是这个系统的硬要求。
// ---------------------------------------------------------------------------

type MemoryStore struct {
	mu     sync.Mutex
	items  []model.FileEvent
	sent   map[string]struct{}
	limit  int
	closed bool
}

func NewMemoryStore(limit int) *MemoryStore {
	if limit <= 0 {
		limit = 100000
	}
	return &MemoryStore{sent: make(map[string]struct{}), limit: limit}
}

func (s *MemoryStore) Open() error  { return nil }
func (s *MemoryStore) Close() error { s.mu.Lock(); s.closed = true; s.mu.Unlock(); return nil }

func (s *MemoryStore) Enqueue(ev model.FileEvent) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return ErrClosed
	}
	if len(s.items) >= s.limit {
		// 丢掉最旧的一条，而不是拒绝新的。
		// 丢掉最新的会让「刚刚发生的事」不可见，那比丢历史更糟。
		s.items = s.items[1:]
	}
	s.items = append(s.items, ev)
	return nil
}

func (s *MemoryStore) DequeueBatch(n int) ([]model.FileEvent, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]model.FileEvent, 0, n)
	for _, ev := range s.items {
		if _, ok := s.sent[ev.EventID]; ok {
			continue
		}
		out = append(out, ev)
		if len(out) >= n {
			break
		}
	}
	return out, nil
}

func (s *MemoryStore) MarkSent(ids []string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, id := range ids {
		s.sent[id] = struct{}{}
	}
	return nil
}

func (s *MemoryStore) Depth() (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	d := 0
	for _, ev := range s.items {
		if _, ok := s.sent[ev.EventID]; !ok {
			d++
		}
	}
	return d, nil
}

func (s *MemoryStore) Prune(olderThan time.Duration) (int, error) {
	// 内存实现不做保留期清理——进程生命周期内本来就短。
	_ = olderThan
	return 0, nil
}

// PendingHash 返回还没算哈希的未上报事件。
func (s *MemoryStore) PendingHash(limit int) ([]model.FileEvent, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []model.FileEvent
	for _, ev := range s.items {
		if _, sent := s.sent[ev.EventID]; sent {
			continue
		}
		if ev.Hashed != model.HashedNo {
			continue
		}
		out = append(out, ev)
		if len(out) >= limit {
			break
		}
	}
	return out, nil
}

// SetHash 给一条事件补上哈希。
func (s *MemoryStore) SetHash(eventID, sha256 string, hashed model.HashedState) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.items {
		if s.items[i].EventID == eventID {
			s.items[i].Sha256 = sha256
			s.items[i].Hashed = hashed
			return nil
		}
	}
	return ErrNotFound
}

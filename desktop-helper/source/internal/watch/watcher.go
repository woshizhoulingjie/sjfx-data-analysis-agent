// Package watch 采集本地文件系统事件。
//
// 只监听目录、不监听文件——文件系统 API（Windows 的 ReadDirectoryChangesW、
// Linux 的 inotify、macOS 的 FSEvents）都是目录级的。
// 服务端下发的跟踪清单里可能有上千个文件，但它们通常只分布在几十个目录里，
// 所以按目录聚合后 watch 数量是可控的。
package watch

import (
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"
)

// Op 是底层文件系统操作，比 model.ActivityID 更原始。
// 翻译成 OCSF 动作是上层（采集器）的事，这一层不关心语义。
type Op uint8

const (
	OpCreate Op = 1 << iota
	OpWrite
	OpRemove
	OpRename
	OpChmod
)

func (o Op) String() string {
	switch {
	case o&OpCreate != 0:
		return "CREATE"
	case o&OpWrite != 0:
		return "WRITE"
	case o&OpRemove != 0:
		return "REMOVE"
	case o&OpRename != 0:
		return "RENAME"
	case o&OpChmod != 0:
		return "CHMOD"
	}
	return "UNKNOWN"
}

// Event 采集器产出的原始事件：尚未哈希、尚未过滤。
type Event struct {
	Path    string // 事件对应的路径（rename 时是新路径）
	OldPath string // 仅当能确定旧路径时才填，见下方关于改名的说明
	Op      Op
	Time    time.Time
	IsDir   bool
}

// Watcher 跨平台的文件系统监听。
type Watcher interface {
	// SetRoots 用最新目录集合替换现有监听，返回实际监听的目录数。
	// 跟踪清单变化时调用。
	SetRoots(roots []string) (int, error)

	// Events 返回事件通道。调用方必须持续读取，否则内部缓冲会满、事件会丢。
	Events() <-chan Event

	// Close 停止监听并释放资源。
	Close() error
}

// Options 控制监听行为。
type Options struct {
	// Debounce 是事件合并窗口。编辑器保存一次文件会产生一串写事件，
	// 合并成一个 update 才符合人的直觉，也能把事件量压下来。
	Debounce time.Duration

	// BufferSize 是内部事件缓冲。太小会在写入高峰丢事件。
	BufferSize int
}

// DefaultOptions 是保守的默认值。
func DefaultOptions() Options {
	return Options{
		Debounce:   500 * time.Millisecond,
		BufferSize: 4096,
	}
}

// ---------------------------------------------------------------------------
// fsnotify 实现
//
// 用 fsnotify 而不是自己写三套平台代码：它已经把
// Windows 的 ReadDirectoryChangesW、Linux 的 inotify、macOS 的 kqueue/FSEvents
// 统一成了一个接口。这是本模块唯一值得依赖的第三方库。
// ---------------------------------------------------------------------------

type fsWatcher struct {
	fw       *fsnotify.Watcher
	events   chan Event
	debounce time.Duration

	mu      sync.Mutex
	roots   []string
	watched map[string]struct{} // 实际挂了 watch 的目录
	pending map[string]*time.Timer
	closed  bool
	done    chan struct{}
	wg      sync.WaitGroup
}

// New 创建一个基于 fsnotify 的监听器。
func New(opts Options) (Watcher, error) {
	if opts.Debounce <= 0 {
		opts.Debounce = DefaultOptions().Debounce
	}
	if opts.BufferSize <= 0 {
		opts.BufferSize = DefaultOptions().BufferSize
	}

	fw, err := fsnotify.NewWatcher()
	if err != nil {
		return nil, err
	}

	w := &fsWatcher{
		fw:       fw,
		events:   make(chan Event, opts.BufferSize),
		debounce: opts.Debounce,
		watched:  make(map[string]struct{}),
		pending:  make(map[string]*time.Timer),
		done:     make(chan struct{}),
	}
	w.wg.Add(1)
	go w.loop()
	return w, nil
}

func (w *fsWatcher) Events() <-chan Event { return w.events }

// SetRoots 重新计算要监听的目录集合。
//
// 注意：调用方传进来的应该是**目录**，不是文件路径。
// 把文件路径转成它的父目录是调用方（agent）的责任。
func (w *fsWatcher) SetRoots(roots []string) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	if w.closed {
		return 0, os.ErrClosed
	}

	want := make(map[string]struct{}, len(roots))
	for _, r := range roots {
		if r == "" {
			continue
		}
		want[filepath.Clean(r)] = struct{}{}
	}

	// 摘掉不再需要的
	for dir := range w.watched {
		if _, keep := want[dir]; !keep {
			_ = w.fw.Remove(dir)
			delete(w.watched, dir)
		}
	}

	// 挂上新的
	for dir := range want {
		if _, ok := w.watched[dir]; ok {
			continue
		}
		if err := w.addDirLocked(dir); err != nil {
			// 单个目录挂不上（权限、已删除）不应该让整轮失败，
			// 但要继续尝试其它目录。
			continue
		}
	}

	w.roots = roots
	return len(w.watched), nil
}

// addDirLocked 挂一个目录，并递归挂上它已有的子目录。
// 调用方必须持有 w.mu。
func (w *fsWatcher) addDirLocked(dir string) error {
	if _, ok := w.watched[dir]; ok {
		return nil
	}
	if err := w.fw.Add(dir); err != nil {
		return err
	}
	w.watched[dir] = struct{}{}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil // 目录不可读就到此为止，不算致命
	}
	for _, e := range entries {
		if e.IsDir() {
			_ = w.addDirLocked(filepath.Join(dir, e.Name()))
		}
	}
	return nil
}

func (w *fsWatcher) loop() {
	defer w.wg.Done()
	for {
		select {
		case <-w.done:
			return
		case ev, ok := <-w.fw.Events:
			if !ok {
				return
			}
			w.handle(ev)
		case _, ok := <-w.fw.Errors:
			if !ok {
				return
			}
			// TODO: 监听报错必须让用户知道——静默失效的监控比没有监控更危险。
			//       接上日志 + 托盘告警。
		}
	}
}

func (w *fsWatcher) handle(ev fsnotify.Event) {
	var op Op
	switch {
	case ev.Op&fsnotify.Create != 0:
		op = OpCreate
	case ev.Op&fsnotify.Remove != 0:
		op = OpRemove
	case ev.Op&fsnotify.Rename != 0:
		op = OpRename
	case ev.Op&fsnotify.Chmod != 0:
		op = OpChmod
	case ev.Op&fsnotify.Write != 0:
		op = OpWrite
	default:
		return
	}

	isDir := false
	if fi, err := os.Stat(ev.Name); err == nil {
		isDir = fi.IsDir()
	}

	// 新目录出现时要补挂，否则它下面新建的文件不会被监听到。
	// fsnotify 不支持递归监听，这一步必须自己做。
	if op == OpCreate && isDir {
		w.mu.Lock()
		_ = w.addDirLocked(ev.Name)
		w.mu.Unlock()
	}

	switch op {
	case OpWrite:
		w.debounceEmit(ev.Name, OpWrite, isDir)
	case OpCreate:
		w.emit(Event{Path: ev.Name, Op: OpCreate, Time: time.Now(), IsDir: isDir})
	default:
		w.cancelPending(ev.Name)
		w.emit(Event{Path: ev.Name, Op: op, Time: time.Now(), IsDir: isDir})
	}
}

// debounceEmit 把短时间内的连续写事件合并成一次。
func (w *fsWatcher) debounceEmit(path string, op Op, isDir bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return
	}
	if t, ok := w.pending[path]; ok {
		t.Reset(w.debounce)
		return
	}
	w.pending[path] = time.AfterFunc(w.debounce, func() {
		w.mu.Lock()
		delete(w.pending, path)
		closed := w.closed
		w.mu.Unlock()
		if !closed {
			w.emit(Event{Path: path, Op: op, Time: time.Now(), IsDir: isDir})
		}
	})
}

func (w *fsWatcher) cancelPending(path string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if t, ok := w.pending[path]; ok {
		t.Stop()
		delete(w.pending, path)
	}
}

func (w *fsWatcher) emit(ev Event) {
	select {
	case w.events <- ev:
	case <-w.done:
	default:
		// 缓冲满了。丢弃并记一条溢出事件，
		// 因为「丢了事件」本身也是必须让上游知道的事实。
		select {
		case w.events <- Event{Op: Op(0), Time: time.Now(), Path: "__queue_overflow__"}:
		default:
		}
	}
}

func (w *fsWatcher) Close() error {
	w.mu.Lock()
	if w.closed {
		w.mu.Unlock()
		return nil
	}
	w.closed = true
	for _, t := range w.pending {
		t.Stop()
	}
	w.pending = make(map[string]*time.Timer)
	w.mu.Unlock()

	close(w.done)
	err := w.fw.Close()
	w.wg.Wait()
	close(w.events)
	return err
}

// ---------------------------------------------------------------------------
// 关于「改名」的说明（重要）
//
// Windows/macOS 的底层 API 能把一次改名报成「旧路径 → 新路径」的单个事件，
// 但 fsnotify 把旧路径丢掉了，只给一个 RENAME。
//
// 我们**不在客户端做配对**，而是这样处理：
//
//   改名 = 旧路径一次 OpRemove + 新路径一次 OpCreate
//
// 两个事件各自算哈希后，sha256 是同一个值。
// 服务端看到「同一哈希在同一个设备上、几秒内先删后建」，
// 就能判定这是一次改名，并据此更新履历。
//
// 这样客户端不需要维护跨平台不一致的 cookie 配对逻辑，
// 配对规则放在服务端一处实现、一处修改。
// ---------------------------------------------------------------------------

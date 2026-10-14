package watch

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// waitFor 在超时前等一个满足条件的事件。
//
// 文件系统事件是异步的，而且不同平台延迟差很多，
// 所以测试必须等待而不是假设「写完就立刻有事件」。
func waitFor(t *testing.T, ch <-chan Event, timeout time.Duration, match func(Event) bool) (Event, bool) {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case ev, ok := <-ch:
			if !ok {
				return Event{}, false
			}
			if match(ev) {
				return ev, true
			}
		case <-deadline:
			return Event{}, false
		}
	}
}

func newTestWatcher(t *testing.T) Watcher {
	t.Helper()
	w, err := New(Options{Debounce: 100 * time.Millisecond, BufferSize: 256})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = w.Close() })
	return w
}

func TestWatchCreate(t *testing.T) {
	dir := t.TempDir()
	w := newTestWatcher(t)

	if n, err := w.SetRoots([]string{dir}); err != nil || n != 1 {
		t.Fatalf("SetRoots 应监听 1 个目录，实际 %d, err=%v", n, err)
	}

	go func() {
		time.Sleep(200 * time.Millisecond)
		_ = os.WriteFile(filepath.Join(dir, "new.txt"), []byte("hi"), 0o644)
	}()

	ev, ok := waitFor(t, w.Events(), 5*time.Second, func(e Event) bool {
		return filepath.Base(e.Path) == "new.txt"
	})
	if !ok {
		t.Fatal("没等到新建文件的事件")
	}
	if ev.Op != OpCreate {
		t.Errorf("期望 CREATE，实际 %s", ev.Op)
	}
}

func TestWatchRemove(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "doomed.txt")
	if err := os.WriteFile(target, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}

	w := newTestWatcher(t)
	if _, err := w.SetRoots([]string{dir}); err != nil {
		t.Fatal(err)
	}

	go func() {
		time.Sleep(200 * time.Millisecond)
		_ = os.Remove(target)
	}()

	_, ok := waitFor(t, w.Events(), 5*time.Second, func(e Event) bool {
		return filepath.Base(e.Path) == "doomed.txt" && (e.Op == OpRemove || e.Op == OpRename)
	})
	if !ok {
		t.Fatal("没等到删除事件")
	}
}

// TestWatchWriteIsDebounced 编辑器保存一次会产生一串写事件，
// 合并成一个才符合人的直觉，也能把事件量压下来。
func TestWatchWriteIsDebounced(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "doc.txt")
	if err := os.WriteFile(target, []byte("0"), 0o644); err != nil {
		t.Fatal(err)
	}

	w := newTestWatcher(t)
	if _, err := w.SetRoots([]string{dir}); err != nil {
		t.Fatal(err)
	}

	go func() {
		time.Sleep(200 * time.Millisecond)
		for i := 0; i < 20; i++ {
			f, err := os.OpenFile(target, os.O_APPEND|os.O_WRONLY, 0o644)
			if err != nil {
				return
			}
			_, _ = f.WriteString("x")
			_ = f.Close()
			time.Sleep(5 * time.Millisecond)
		}
	}()

	_, ok := waitFor(t, w.Events(), 5*time.Second, func(e Event) bool {
		return filepath.Base(e.Path) == "doc.txt" && e.Op == OpWrite
	})
	if !ok {
		t.Fatal("没等到写事件")
	}

	// debounce 窗口 100ms，20 次写入应该只产生很少的事件。
	// 这里宽松一点：只要不是 20 次就说明合并生效了。
	time.Sleep(500 * time.Millisecond)
	extra := 0
	for {
		select {
		case e := <-w.Events():
			if filepath.Base(e.Path) == "doc.txt" && e.Op == OpWrite {
				extra++
			}
			continue
		default:
		}
		break
	}
	if extra >= 20 {
		t.Fatalf("20 次写入产生了 %d 个事件，debounce 没生效", extra+1)
	}
}

// TestWatchRecursive 新目录出现时必须补挂，否则它下面新建的文件监听到。
func TestWatchRecursive(t *testing.T) {
	dir := t.TempDir()
	w := newTestWatcher(t)
	if _, err := w.SetRoots([]string{dir}); err != nil {
		t.Fatal(err)
	}

	sub := filepath.Join(dir, "sub")
	go func() {
		time.Sleep(200 * time.Millisecond)
		_ = os.MkdirAll(sub, 0o755)
		time.Sleep(400 * time.Millisecond) // 给补挂留时间
		_ = os.WriteFile(filepath.Join(sub, "deep.txt"), []byte("x"), 0o644)
	}()

	_, ok := waitFor(t, w.Events(), 6*time.Second, func(e Event) bool {
		return filepath.Base(e.Path) == "deep.txt"
	})
	if !ok {
		t.Fatal("新目录里的文件没被监听到（递归补挂失效）")
	}
}

// TestSetRootsReplaces 换根目录时必须摘掉旧的，否则 watch 会越积越多。
func TestSetRootsReplaces(t *testing.T) {
	a, b := t.TempDir(), t.TempDir()
	w := newTestWatcher(t)

	if n, _ := w.SetRoots([]string{a}); n != 1 {
		t.Fatalf("第一次应监听 1 个，实际 %d", n)
	}
	n, err := w.SetRoots([]string{b})
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("替换后应仍是 1 个，实际 %d（旧目录没摘掉）", n)
	}

	// 旧目录里产生文件不该再有事件
	_ = os.WriteFile(filepath.Join(a, "old.txt"), []byte("x"), 0o644)
	if _, ok := waitFor(t, w.Events(), 700*time.Millisecond, func(e Event) bool {
		return filepath.Base(e.Path) == "old.txt"
	}); ok {
		t.Fatal("已摘掉的目录不该再产生事件")
	}
}

func TestSetRootsEmptyIsFine(t *testing.T) {
	w := newTestWatcher(t)
	n, err := w.SetRoots(nil)
	if err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("空清单应监听 0 个目录，实际 %d", n)
	}
}

func TestCloseIsIdempotent(t *testing.T) {
	w, err := New(DefaultOptions())
	if err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	// 重复关闭不该 panic
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestSetRootsAfterClose(t *testing.T) {
	w, err := New(DefaultOptions())
	if err != nil {
		t.Fatal(err)
	}
	_ = w.Close()
	if _, err := w.SetRoots([]string{t.TempDir()}); err == nil {
		t.Fatal("关闭后 SetRoots 应返回错误")
	}
}

// TestWatchMissingRootIsSkipped 目录不存在（比如没插 U 盘）不该让整轮失败。
func TestWatchMissingRootIsSkipped(t *testing.T) {
	w := newTestWatcher(t)
	good := t.TempDir()
	n, err := w.SetRoots([]string{filepath.Join(t.TempDir(), "不存在"), good})
	if err != nil {
		t.Fatalf("个别目录挂不上不该报错: %v", err)
	}
	if n != 1 {
		t.Fatalf("应成功挂上 1 个目录，实际 %d", n)
	}
}

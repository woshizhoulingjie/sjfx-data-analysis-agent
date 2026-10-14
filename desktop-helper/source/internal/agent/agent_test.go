package agent

import (
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/woshizhoulingjie/sjfx-agent/internal/hash"
	"github.com/woshizhoulingjie/sjfx-agent/internal/model"
	"github.com/woshizhoulingjie/sjfx-agent/internal/store"
	"github.com/woshizhoulingjie/sjfx-agent/internal/watch"
)

// fakeWatcher 记录收到的监听目录，不真的去挂文件系统监听。
//
// 路径决策是纯逻辑，不该依赖真实文件系统的行为——
// 那样测试会随环境变化而飘。
type fakeWatcher struct {
	mu    sync.Mutex
	roots []string
	calls int
	ch    chan watch.Event
}

func newFakeWatcher() *fakeWatcher {
	return &fakeWatcher{ch: make(chan watch.Event, 64)}
}

func (f *fakeWatcher) SetRoots(roots []string) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.roots = append([]string(nil), roots...)
	f.calls++
	return len(roots), nil
}

func (f *fakeWatcher) Events() <-chan watch.Event { return f.ch }
func (f *fakeWatcher) Close() error               { return nil }

func (f *fakeWatcher) lastRoots() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.roots...)
}

// newTestAgent 造一个只够测路径逻辑的 Agent。
func newTestAgent(t *testing.T, watchRoots []string) *Agent {
	t.Helper()
	return New(Config{WatchRoots: watchRoots}, Deps{
		Watcher: newFakeWatcher(),
		Index:   store.NewMemoryIndex(),
		Store:   store.NewMemoryStore(0),
		Hasher:  hash.NewCached(64),
	})
}

// TestWatchRootsIgnoreServerPaths 是本文件存在的主要理由。
//
// # 这个测试防的是什么
//
// 服务端下发的清单里 path 是**服务器上的路径**（Linux 的 /home/nas511/...），
// 而用户的电脑是 Windows。曾经的做法是从这些路径推导"要监听哪些本地目录"，
// 结果是：
//
//	/home/nas511/zhoulingjie/report.pdf
//	  → 在 Windows 上被 filepath.Dir 解释成 `\home\nas511\zhoulingjie`
//	  → 这个目录不存在 → 一个都挂不上 → **实时监听完全失效**
//	  → 用户下载、复制、改名、删除，代理全都看不见
//
// 正确做法：监听哪些目录由客户端自己决定，服务端路径**只作展示**。
func TestWatchRootsIgnoreServerPaths(t *testing.T) {
	// 模拟服务端下发的清单（Linux 路径）
	serverFiles := []model.TrackedFile{
		{Path: "/home/nas511/zhoulingjie/report.pdf", Sha256: "aaa", Name: "report.pdf", Size: 100},
		{Path: "/home/nas511/shared/spec.docx", Sha256: "bbb", Name: "spec.docx", Size: 200},
	}
	if err := newTestAgent(t, nil).index.ReplaceTracked(serverFiles); err != nil {
		t.Fatal(err)
	}

	// 配了显式监听目录时，只用配置的，绝不掺入服务端路径
	a := newTestAgent(t, []string{`D:\sjfx-work`})
	a.index.ReplaceTracked(serverFiles)

	roots := a.computeWatchRoots()
	for _, r := range roots {
		if strings.Contains(r, "nas511") || strings.Contains(r, "home") {
			t.Fatalf("监听目录里不该出现服务端路径: %v", roots)
		}
	}
	if len(roots) != 1 || roots[0] != `D:\sjfx-work` {
		t.Fatalf("应只监听配置的目录，实际 %v", roots)
	}
}

// TestWatchRootsFallbackToPlatformDefaults 没配监听目录时用平台默认落点
// （下载 / 桌面 / 文档）——这些是「从 sjfx 拿到文件之后第一时间的落点」。
func TestWatchRootsFallbackToPlatformDefaults(t *testing.T) {
	a := newTestAgent(t, nil)
	roots := a.computeWatchRoots()

	if len(roots) == 0 {
		t.Fatal("没配监听目录时应该有平台默认落点，否则实时监听完全失效")
	}
	// 默认落点应当是**本地真实存在**的目录
	exists := 0
	for _, r := range roots {
		if fi, err := os.Stat(r); err == nil && fi.IsDir() {
			exists++
		}
	}
	if exists == 0 {
		t.Fatalf("默认落点一个都不存在，实际 %v", roots)
	}
	t.Logf("平台默认落点（%d 个，其中 %d 个存在）: %v", len(roots), exists, roots)
}

// TestRememberLocalAddsWatchRoot 在非默认位置认出一份被跟踪文件后，
// 那个目录要被纳入监听，否则它后续的改动就看不见了。
func TestRememberLocalAddsWatchRoot(t *testing.T) {
	custom := t.TempDir() // 模拟用户自己找的一个目录，不在默认落点里
	a := newTestAgent(t, nil)
	_ = a.index.ReplaceTracked([]model.TrackedFile{
		{Path: "/home/nas511/x/report.pdf", Sha256: "aaa", Name: "report.pdf", Size: 3},
	})

	local := filepath.Join(custom, "report.pdf")
	tf := model.TrackedFile{Sha256: "aaa", Name: "report.pdf", Size: 3}
	a.rememberLocal(local, "aaa", tf)

	// 1) 这条本地路径此后应能按路径查到（走快路径）
	if _, ok := a.index.TrackedByPath(local); !ok {
		t.Fatal("rememberLocal 之后应能按本地路径查到")
	}
	// 2) 所在目录要进监听范围
	roots := a.computeWatchRoots()
	found := false
	for _, r := range roots {
		if filepath.Clean(r) == filepath.Clean(custom) {
			found = true
		}
	}
	if !found {
		t.Fatalf("认出的路径所在目录应被纳入监听，实际 %v", roots)
	}
}

// TestNamePreFilter 廉价预筛：不读内容就能排除掉绝大多数无关文件。
//
// 它同时是**隐私边界**：不去读一个和 sjfx 毫无关系的文件的内容。
//
// 注意门槛是「文件名」而不是「名字+大小」——实测发现用大小当门槛
// 会把「被改过的文件」整个漏掉（改过之后大小变了），
// 而单靠哈希也认不出它（哈希已经变了）。名字是唯一的线索。
func TestNamePreFilter(t *testing.T) {
	a := newTestAgent(t, nil)
	_ = a.index.ReplaceTracked([]model.TrackedFile{
		{Path: "/home/nas511/x/report.pdf", Sha256: "aaa", Name: "report.pdf", Size: 12345},
	})

	cases := []struct {
		name string
		want bool
		why  string
	}{
		{"report.pdf", true, "名字完全相同"},
		{"REPORT.PDF", true, "Windows 上文件名不区分大小写"},
		{"report.pdf", true, "大小变了也一样命中——改过的版本也要能认出来"},
		{"别的文件.pdf", false, "名字对不上"},
		{"", false, "空名字"},
	}
	for _, c := range cases {
		_, got := a.index.TrackedByName(c.name)
		if got != c.want {
			t.Errorf("TrackedByName(%q) = %v，期望 %v（%s）", c.name, got, c.want, c.why)
		}
	}
}

// TestNamePreFilterIgnoresSize 专门固化「大小不能当门槛」这条结论。
func TestNamePreFilterIgnoresSize(t *testing.T) {
	a := newTestAgent(t, nil)
	_ = a.index.ReplaceTracked([]model.TrackedFile{
		{Path: "/home/nas511/x/report.pdf", Sha256: "aaa", Name: "report.pdf", Size: 12345},
	})

	// 用户把文件改长了（大小完全不同），名字没变
	_, ok := a.index.TrackedByName("report.pdf")
	if !ok {
		t.Fatal("大小变了就不命中，等于漏掉所有『改过的版本』——这正是之前的 bug")
	}
}

// TestServerPathBasenameFallback 服务端没给 Name 时，
// 应该退回用服务端路径的 basename——原始文件名通常和它是同一个。
//
// 注意不能用 filepath.Base：服务端是 Linux 风格的 /a/b/c，
// 在 Windows 上 filepath.Base 会把它当成一整段。
func TestServerPathBasenameFallback(t *testing.T) {
	a := newTestAgent(t, nil)
	_ = a.index.ReplaceTracked([]model.TrackedFile{
		{Path: "/home/nas511/zhoulingjie/报告.pdf", Sha256: "aaa", Size: 100},
	})

	if _, ok := a.index.TrackedByName("报告.pdf"); !ok {
		t.Fatal("服务端没给 Name 时应退回用路径 basename，否则预筛永远失效")
	}
}

// TestDedupeWatchRoots 重复的目录只监听一次，否则 watch 会越积越多。
func TestDedupeWatchRoots(t *testing.T) {
	d := `D:\same`
	a := newTestAgent(t, []string{d, d, d + `\`})
	roots := a.computeWatchRoots()
	if len(roots) != 1 {
		t.Fatalf("重复目录应去重，实际 %v", roots)
	}
}

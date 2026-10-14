package store

import (
	"testing"

	"github.com/woshizhoulingjie/sjfx-agent/internal/model"
)

func tf(path, sum string) model.TrackedFile {
	return model.TrackedFile{Path: path, Sha256: sum}
}

func TestIndexReplaceIsTotal(t *testing.T) {
	idx := NewMemoryIndex()
	if err := idx.ReplaceTracked([]model.TrackedFile{
		tf(`C:\a.txt`, "h1"),
		tf(`C:\b.txt`, "h2"),
	}); err != nil {
		t.Fatal(err)
	}
	if idx.Count() != 2 {
		t.Fatalf("应有 2 条，实际 %d", idx.Count())
	}

	// 整体替换：b.txt 必须消失。
	// 如果做增量合并，某次漏了"删除"指令就会让清单只增不减。
	if err := idx.ReplaceTracked([]model.TrackedFile{tf(`C:\a.txt`, "h1")}); err != nil {
		t.Fatal(err)
	}
	if _, ok := idx.TrackedByPath(`C:\b.txt`); ok {
		t.Fatal("整体替换后 b.txt 不该还在")
	}
	if _, ok := idx.TrackedByHash("h2"); ok {
		t.Fatal("整体替换后 h2 不该还在")
	}
	if _, ok := idx.TrackedByPath(`C:\a.txt`); !ok {
		t.Fatal("a.txt 应该还在")
	}
}

func TestIndexLookup(t *testing.T) {
	idx := NewMemoryIndex()
	_ = idx.ReplaceTracked([]model.TrackedFile{tf(`C:\a.txt`, "h1")})

	if _, ok := idx.TrackedByPath(`C:\a.txt`); !ok {
		t.Fatal("按路径应能查到")
	}
	if _, ok := idx.TrackedByHash("h1"); !ok {
		t.Fatal("按哈希应能查到")
	}
	if _, ok := idx.TrackedByPath(`C:\nope.txt`); ok {
		t.Fatal("不存在的路径不该查到")
	}
	// 空哈希必须查不到：否则任何"哈希算不出来"的文件都会被误认为命中
	if _, ok := idx.TrackedByHash(""); ok {
		t.Fatal("空哈希不该命中")
	}
}

// TestIndexRemember 覆盖实测挖出来的两个漏洞。
func TestIndexRemember(t *testing.T) {
	idx := NewMemoryIndex()
	_ = idx.ReplaceTracked([]model.TrackedFile{tf(`C:\report.txt`, "H1")})

	// 漏洞一：用户改了文件 → H2。改名的后半段会产生 H2 的新路径。
	// 记住这个版本之后，H2 才查得到。
	if _, ok := idx.TrackedByHash("H2"); ok {
		t.Fatal("H2 还没被记住，不该查到")
	}
	idx.Remember(`C:\report.txt`, "H2", tf(`C:\report.txt`, "H1"))
	if _, ok := idx.TrackedByHash("H2"); !ok {
		t.Fatal("Remember 之后 H2 应该查得到")
	}
	// H1 不能被挤掉——同一份文件的不同版本都该指向同一条跟踪记录
	if _, ok := idx.TrackedByHash("H1"); !ok {
		t.Fatal("Remember 不该挤掉已有的哈希")
	}

	// 漏洞二：副本在桌面上被认出来，之后被删除。
	// 只有记住这个**路径**，删除事件才走得到路径查询。
	desktop := `C:\Users\u\Desktop\report-copy.txt`
	if _, ok := idx.TrackedByPath(desktop); ok {
		t.Fatal("副本路径还没被记住")
	}
	idx.Remember(desktop, "H2", tf(`C:\report.txt`, "H1"))
	if _, ok := idx.TrackedByPath(desktop); !ok {
		t.Fatal("Remember 之后副本路径应该查得到，否则副本被删就追踪不到")
	}
}

func TestIndexRememberIgnoresEmpty(t *testing.T) {
	idx := NewMemoryIndex()
	_ = idx.ReplaceTracked([]model.TrackedFile{tf(`C:\a.txt`, "h1")})
	before := idx.Count()

	idx.Remember("", "", tf(`C:\a.txt`, "h1"))

	if idx.Count() != before {
		t.Fatal("空路径空哈希不该改变索引规模")
	}
}

// ---------------------------------------------------------------------------

func TestStoreEnqueueAndDrain(t *testing.T) {
	s := NewMemoryStore(0)

	for i := 0; i < 5; i++ {
		if err := s.Enqueue(model.FileEvent{
			EventID: string(rune('a' + i)),
			Path:    "/x",
			Hashed:  model.HashedYes,
		}); err != nil {
			t.Fatal(err)
		}
	}

	batch, err := s.DequeueBatch(3)
	if err != nil {
		t.Fatal(err)
	}
	if len(batch) != 3 {
		t.Fatalf("应取 3 条，实际 %d", len(batch))
	}

	ids := []string{batch[0].EventID, batch[1].EventID, batch[2].EventID}
	if err := s.MarkSent(ids); err != nil {
		t.Fatal(err)
	}

	depth, _ := s.Depth()
	if depth != 2 {
		t.Fatalf("标记后应剩 2 条，实际 %d", depth)
	}
}

// TestStoreQueueFullDropsOldest 队列满时丢最旧的。
//
// 丢最新的会让"刚刚发生的事"不可见——而那恰恰是最要紧的。
func TestStoreQueueFullDropsOldest(t *testing.T) {
	s := NewMemoryStore(3)
	for i := 0; i < 5; i++ {
		_ = s.Enqueue(model.FileEvent{EventID: string(rune('a' + i)), Hashed: model.HashedYes})
	}
	batch, _ := s.DequeueBatch(10)
	if len(batch) != 3 {
		t.Fatalf("队列容量 3，实际 %d", len(batch))
	}
	// 留下的应该是最新的三条：c d e
	if batch[0].EventID != "c" {
		t.Fatalf("应丢掉最旧的，实际首条是 %s", batch[0].EventID)
	}
}

func TestStorePendingHash(t *testing.T) {
	s := NewMemoryStore(0)
	_ = s.Enqueue(model.FileEvent{EventID: "no-hash-1", Path: "/a", Hashed: model.HashedNo})
	_ = s.Enqueue(model.FileEvent{EventID: "done", Path: "/b", Hashed: model.HashedYes, Sha256: "abc"})
	_ = s.Enqueue(model.FileEvent{EventID: "failed", Path: "/c", Hashed: model.HashedFailed})
	_ = s.Enqueue(model.FileEvent{EventID: "no-hash-2", Path: "/d", Hashed: model.HashedNo})

	pending, err := s.PendingHash(10)
	if err != nil {
		t.Fatal(err)
	}
	// 只取 HashedNo。HashedFailed 不取——那是"算过但失败"，
	// 反复重试不会成功，只会白耗 IO。
	if len(pending) != 2 {
		t.Fatalf("应有 2 条待补算，实际 %d: %+v", len(pending), pending)
	}

	if err := s.SetHash("no-hash-1", "deadbeef", model.HashedYes); err != nil {
		t.Fatal(err)
	}
	pending, _ = s.PendingHash(10)
	if len(pending) != 1 {
		t.Fatalf("回填后应剩 1 条，实际 %d", len(pending))
	}

	if err := s.SetHash("不存在", "x", model.HashedYes); err != ErrNotFound {
		t.Fatalf("对不存在的事件回填应返回 ErrNotFound，实际 %v", err)
	}
}

func TestStoreMarkSentUnknownIDIsHarmless(t *testing.T) {
	s := NewMemoryStore(0)
	if err := s.MarkSent([]string{"从没入队过的 ID"}); err != nil {
		t.Fatalf("标记不存在的 ID 不该报错: %v", err)
	}
	if err := s.MarkSent(nil); err != nil {
		t.Fatalf("空列表不该报错: %v", err)
	}
}

func TestStoreClosedRejectsEnqueue(t *testing.T) {
	s := NewMemoryStore(0)
	_ = s.Close()
	if err := s.Enqueue(model.FileEvent{EventID: "x"}); err != ErrClosed {
		t.Fatalf("关闭后入队应返回 ErrClosed，实际 %v", err)
	}
}

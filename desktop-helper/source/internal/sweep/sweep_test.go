package sweep

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/woshizhoulingjie/sjfx-agent/internal/hash"
	"github.com/woshizhoulingjie/sjfx-agent/internal/model"
	"github.com/woshizhoulingjie/sjfx-agent/internal/store"
)

// setup 造一棵目录树，返回根目录和一个跟踪索引。
//
//	root/
//	  tracked.txt      内容与跟踪清单一致 → 应命中
//	  other.txt        未跟踪 → 不该命中
//	  sub/
//	    copy.txt       跟踪文件的副本（不同路径、同内容）→ 应命中
func setup(t *testing.T) (string, store.Index, string) {
	t.Helper()
	root := t.TempDir()

	trackedPath := filepath.Join(root, "tracked.txt")
	content := []byte("这是被跟踪的内容")
	if err := os.WriteFile(trackedPath, content, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "other.txt"), []byte("无关内容"), 0o644); err != nil {
		t.Fatal(err)
	}
	sub := filepath.Join(root, "sub")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sub, "copy.txt"), content, 0o644); err != nil {
		t.Fatal(err)
	}

	h := hash.NewCached(64)
	sum, err := h.Sum(trackedPath)
	if err != nil {
		t.Fatal(err)
	}

	idx := store.NewMemoryIndex()
	if err := idx.ReplaceTracked([]model.TrackedFile{{Path: trackedPath, Sha256: sum}}); err != nil {
		t.Fatal(err)
	}
	return root, idx, sum
}

func TestScanFindsTrackedAndCopies(t *testing.T) {
	root, idx, sum := setup(t)
	s := New(idx, hash.NewCached(64), nil)
	s.Roots = []string{root}

	var hits []Hit
	st, err := s.Scan(context.Background(), func(h Hit) { hits = append(hits, h) })
	if err != nil {
		t.Fatal(err)
	}

	if len(hits) != 2 {
		t.Fatalf("应命中 2 个（原文件 + sub/ 里的副本），实际 %d: %+v", len(hits), hits)
	}
	for _, h := range hits {
		if h.Sha256 != sum {
			t.Errorf("哈希不符: %s", h.Path)
		}
	}

	// 未跟踪的文件必须被忽略——这是「不做全盘监视」的保证
	for _, h := range hits {
		if filepath.Base(h.Path) == "other.txt" {
			t.Fatal("未跟踪的 other.txt 不该命中")
		}
	}

	if st.FilesSeen != 3 {
		t.Errorf("应看到 3 个文件，实际 %d", st.FilesSeen)
	}
	if st.Matched != 2 {
		t.Errorf("统计里应有 2 个命中，实际 %d", st.Matched)
	}
}

// TestScanSkipsOversize 大文件必须跳过，否则一次普查会把磁盘 IO 占满很久。
func TestScanSkipsOversize(t *testing.T) {
	root := t.TempDir()
	big := filepath.Join(root, "big.bin")
	if err := os.WriteFile(big, make([]byte, 4096), 0o644); err != nil {
		t.Fatal(err)
	}

	idx := store.NewMemoryIndex()
	s := New(idx, hash.NewCached(64), nil)
	s.Roots = []string{root}
	s.MaxSize = 1024 // 比文件小

	st, err := s.Scan(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if st.SkippedBig != 1 {
		t.Errorf("应跳过 1 个超限文件，实际 %d", st.SkippedBig)
	}
	if st.FilesHashed != 0 {
		t.Errorf("超限文件不该被算哈希，实际算了 %d 个", st.FilesHashed)
	}
}

// TestScanSkipDirsSkipDirs 黑名单目录不该被进去。
func TestScanSkipDirs(t *testing.T) {
	root := t.TempDir()
	skip := filepath.Join(root, "node_modules")
	if err := os.MkdirAll(skip, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(skip, "a.js"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}

	idx := store.NewMemoryIndex()
	s := New(idx, hash.NewCached(64), nil)
	s.Roots = []string{root}

	st, err := s.Scan(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if st.FilesSeen != 0 {
		t.Errorf("node_modules 里的文件不该被遍历，实际看到 %d 个", st.FilesSeen)
	}
}

// TestScanMissingRootIsNotFatal 根目录不存在（比如没插 U 盘）不该报错。
func TestScanMissingRootIsNotFatal(t *testing.T) {
	idx := store.NewMemoryIndex()
	s := New(idx, hash.NewCached(64), nil)
	s.Roots = []string{filepath.Join(t.TempDir(), "根本不存在")}

	if _, err := s.Scan(context.Background(), nil); err != nil {
		t.Fatalf("目录不存在不该报错: %v", err)
	}
}

func TestScanHonoursContextCancel(t *testing.T) {
	root, idx, _ := setup(t)
	s := New(idx, hash.NewCached(64), nil)
	s.Roots = []string{root}

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // 立刻取消

	if _, err := s.Scan(ctx, nil); err == nil {
		t.Fatal("已取消的 context 应返回错误")
	}
}

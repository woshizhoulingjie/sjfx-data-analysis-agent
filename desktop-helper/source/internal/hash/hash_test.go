package hash

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func write(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func want(content string) string {
	s := sha256.Sum256([]byte(content))
	return hex.EncodeToString(s[:])
}

func TestSumMatchesStdlib(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "a.txt")
	write(t, p, "hello world")

	got, err := NewCached(64).Sum(p)
	if err != nil {
		t.Fatal(err)
	}
	if got != want("hello world") {
		t.Fatalf("哈希不对: %s", got)
	}
}

func TestSumMissingFile(t *testing.T) {
	h := NewCached(64)
	if _, err := h.Sum(filepath.Join(t.TempDir(), "不存在")); err == nil {
		t.Fatal("文件不存在应返回错误")
	}
}

func TestSumRejectsDirectory(t *testing.T) {
	h := NewCached(64)
	if _, err := h.Sum(t.TempDir()); err == nil {
		t.Fatal("目录不该被算哈希")
	}
}

// TestCacheInvalidatesOnContentChange 是本包最重要的一条测试。
//
// 缓存键是 (path, size, mtime)。如果只用 path，
// 文件被改了但路径没变时会返回**过期的哈希**——
// 那会导致最严重的一类错误：把「文件已变更」报成「文件没变」。
func TestCacheInvalidatesOnContentChange(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "a.txt")
	h := NewCached(64)

	write(t, p, "v1")
	first, err := h.Sum(p)
	if err != nil {
		t.Fatal(err)
	}
	if first != want("v1") {
		t.Fatal("首次哈希不对")
	}

	// 等一毫秒确保 mtime 会变（有些文件系统 mtime 精度不高）
	time.Sleep(10 * time.Millisecond)

	// 内容变长 → size 也变
	write(t, p, "v2-更长的内容")
	second, err := h.Sum(p)
	if err != nil {
		t.Fatal(err)
	}
	if second == first {
		t.Fatal("文件内容变了，哈希必须跟着变（缓存没有失效）")
	}
	if second != want("v2-更长的内容") {
		t.Fatalf("变更后的哈希不对: %s", second)
	}
}

// TestCacheInvalidatesOnSameLengthChange 内容长度相同但内容不同。
//
// 这种改动**只有 mtime 能发现**，光靠 size 是不够的。
func TestCacheInvalidatesOnSameLengthChange(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "a.txt")
	h := NewCached(64)

	write(t, p, "aaaa")
	first, _ := h.Sum(p)

	time.Sleep(10 * time.Millisecond)
	write(t, p, "bbbb") // 同样 4 字节
	second, _ := h.Sum(p)

	if first == second {
		t.Fatal("等长内容变更未被检测到——缓存键少了 mtime")
	}
}

func TestCacheHitsForUnchangedFile(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "a.txt")
	write(t, p, "stable")
	h := NewCached(64)

	first, _ := h.Sum(p)
	second, _ := h.Sum(p)
	if first != second {
		t.Fatal("未变更的文件两次哈希应一致")
	}

	// 缓存内部应只留一条
	h.mu.RLock()
	n := len(h.entries)
	h.mu.RUnlock()
	if n != 1 {
		t.Fatalf("同一文件重复哈希应只缓存一条，实际 %d", n)
	}
}

func TestCacheEvictsWhenFull(t *testing.T) {
	dir := t.TempDir()
	h := NewCached(2)

	for i := 0; i < 5; i++ {
		p := filepath.Join(dir, string(rune('a'+i))+".txt")
		write(t, p, "x")
		if _, err := h.Sum(p); err != nil {
			t.Fatal(err)
		}
	}

	h.mu.RLock()
	n := len(h.entries)
	h.mu.RUnlock()
	if n > 2 {
		t.Fatalf("缓存超过上限: %d", n)
	}
}

// TestShouldHashNow 大文件不能在采集循环里算哈希，
// 否则往监听目录丢一个大文件就会把采集卡住。
func TestShouldHashNow(t *testing.T) {
	cases := []struct {
		size int64
		want bool
	}{
		{0, true},
		{1024, true},
		{MaxImmediateSize, true},
		{MaxImmediateSize + 1, false},
		{10 << 30, false},
		{-1, false}, // stat 失败时传 -1，不该去算
	}
	for _, c := range cases {
		if got := ShouldHashNow(c.size); got != c.want {
			t.Errorf("ShouldHashNow(%d) = %v, 期望 %v", c.size, got, c.want)
		}
	}
}

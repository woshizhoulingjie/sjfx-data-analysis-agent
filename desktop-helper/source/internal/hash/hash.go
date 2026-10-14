// Package hash 计算文件内容哈希。
//
// 哈希是本地事件与服务端履历之间的**唯一关联键**。
// 改名、移动、复制都不影响它——这是整个系统能跨两端对上账的原因。
package hash

import (
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
	"sync"
)

// Hasher 计算文件内容哈希。
type Hasher interface {
	// Sum 返回文件的 SHA-256（小写 hex）。
	// 文件不存在、被独占、权限不足时返回错误——调用方据此把
	// Hashed 标成 HashedFailed，而不是当成空哈希。
	Sum(path string) (string, error)
}

// cacheKey 是缓存的键。
//
// 用 (path, size, mtime) 三元组而不是只用 path：
// 文件被改了但路径没变，只用 path 会返回**过期的哈希**，
// 那会导致最严重的一类错误——把「文件已变更」报成「文件没变」。
//
// size 和 mtime 同时变才算失效，是因为有些工具只改其中一个。
type cacheKey struct {
	Path  string
	Size  int64
	Mtime int64
}

// CachedHasher 是带缓存的实现。
//
// 缓存的价值：一次编辑会触发多条事件，同一条路径可能被哈希多次；
// 大文件重复读取会拖慢整个采集循环。
//
// 缓存是内存态，进程重启即失效——这是有意的，不做持久化，
// 因为磁盘上的缓存一旦与真实文件不一致，风险远大于收益。
type CachedHasher struct {
	mu      sync.RWMutex
	entries map[cacheKey]string
	limit   int
	// TODO: 换成带淘汰的 LRU；当前实现超过 limit 时整体清空，
	//       简单但会造成周期性抖动。先跑通流程再优化。
}

func NewCached(limit int) *CachedHasher {
	if limit <= 0 {
		limit = 4096
	}
	return &CachedHasher{
		entries: make(map[cacheKey]string),
		limit:   limit,
	}
}

func (h *CachedHasher) Sum(path string) (string, error) {
	fi, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	if fi.IsDir() {
		return "", os.ErrInvalid
	}

	key := cacheKey{Path: path, Size: fi.Size(), Mtime: fi.ModTime().UnixNano()}

	h.mu.RLock()
	if v, ok := h.entries[key]; ok {
		h.mu.RUnlock()
		return v, nil
	}
	h.mu.RUnlock()

	sum, err := sumFile(path)
	if err != nil {
		return "", err
	}

	h.mu.Lock()
	if len(h.entries) >= h.limit {
		h.entries = make(map[cacheKey]string, h.limit)
	}
	h.entries[key] = sum
	h.mu.Unlock()

	return sum, nil
}

func sumFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()

	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// MaxImmediateSize 是「立刻算哈希」的大小上限。
//
// 超过这个大小的文件先入队（Hashed=0），由后台慢慢补算。
// 否则往 watch 目录里丢一个 10GB 的文件，采集循环会被它卡住。
const MaxImmediateSize = 64 << 20 // 64 MiB

// ShouldHashNow 判断一个文件是否应该立刻算哈希。
func ShouldHashNow(size int64) bool { return size >= 0 && size <= MaxImmediateSize }

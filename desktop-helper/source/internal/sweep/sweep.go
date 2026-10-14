// Package sweep 定期扫描「用户最可能把文件放过去」的位置。
//
// ## 为什么需要它
//
// watch 只能看到「挂了监听的目录」里发生的事。但用户的实际行为是：
//
//	把文件从 sjfx 下载到桌面 → 复制一份到 U 盘 → 又拖进网盘同步目录
//
// 桌面可能在监听范围内，U 盘和网盘目录不一定在。
// 定期扫这些高概率落点，能把 watch 漏掉的情况补上。
//
// ## 为什么不做全盘扫描
//
// 全盘扫描要遍历整块磁盘、给每个文件算哈希——代价极高，
// 而且**违背了「只追踪已入库文件」这个承诺**：
// 全盘扫描意味着我们读取了用户所有文件的全部内容。
//
// 所以只扫有限的几个位置：桌面、下载、文档、可移动设备。
// 这些是「文件会被带出去」的必经之路。
package sweep

import (
	"context"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"github.com/woshizhoulingjie/sjfx-agent/internal/hash"
	"github.com/woshizhoulingjie/sjfx-agent/internal/store"
)

// Hit 是一次命中：某个位置出现了被跟踪文件的内容。
type Hit struct {
	Path   string
	Sha256 string
	Size   int64
}

// Stats 是一轮普查的统计。上报给服务端，让「覆盖了多少」这件事可见。
type Stats struct {
	Roots       int
	FilesSeen   int
	FilesHashed int
	Matched     int
	SkippedBig  int
	SkippedDir  int
	Errors      int
	Duration    time.Duration
}

// Scanner 扫描器。
type Scanner struct {
	// Roots 要扫的根目录。留空表示用 DefaultRoots()。
	Roots []string

	Index  store.Index
	Hasher hash.Hasher

	// MaxSize 是算哈希的大小上限。超过的直接跳过——
	// 普查是在后台慢慢跑的，读一个 10GB 的文件会占满 IO 很久。
	MaxSize int64

	// SkipDirs 是不进去的目录名（系统目录等）。
	SkipDirs map[string]bool

	Log *slog.Logger
}

// New 创建一个带默认值的扫描器。
func New(index store.Index, hasher hash.Hasher, log *slog.Logger) *Scanner {
	if log == nil {
		log = slog.Default()
	}
	return &Scanner{
		Index:   index,
		Hasher:  hasher,
		MaxSize: MaxIntensiveBytes,
		Log:     log,
		SkipDirs: map[string]bool{
			// 这些目录进去没有意义，还会拖慢普查
			"$RECYCLE.BIN":              true,
			"System Volume Information": true,
			"node_modules":              true,
			".git":                      true,
			"__pycache__":               true,
			"AppData":                   true,
			"Windows":                   true,
			"Program Files":             true,
			"Program Files (x86)":       true,
		},
	}
}

// MaxIntensiveBytes 是普查时算哈希的大小上限（256 MiB）。
//
// 比采集循环的 64 MiB 宽松，因为普查是后台低频任务，
// 但那也不该拿一个 10GB 的文件去占满磁盘 IO。
const MaxIntensiveBytes = 256 << 20

// Scan 遍历 Root 并回调所有命中。
//
// 返回的 Stats 可以用来回答「这轮普查覆盖了多少」——
// 这个数字很重要：它让「监控是否真的在工作」变得可验证，
// 而不是只能相信它是好的。
func (s *Scanner) Scan(ctx context.Context, onHit func(Hit)) (Stats, error) {
	roots := s.Roots
	if len(roots) == 0 {
		roots = DefaultRoots()
	}

	st := Stats{Roots: len(roots)}
	start := time.Now()
	defer func() { st.Duration = time.Since(start) }()

	for _, root := range roots {
		if err := ctx.Err(); err != nil {
			return st, err
		}
		if _, err := os.Stat(root); err != nil {
			continue // 目录不存在（比如没插 U 盘）——正常，跳过
		}
		st.SkippedDir++
		s.scanRoot(ctx, root, &st, onHit)
	}
	return st, nil
}

func (s *Scanner) scanRoot(ctx context.Context, root string, st *Stats, onHit func(Hit)) {
	_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			st.Errors++
			return nil // 单个文件读不到不该中断整轮普查
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}

		if d.IsDir() {
			if path != root && s.SkipDirs[d.Name()] {
				return filepath.SkipDir
			}
			return nil
		}

		// 只处理普通文件
		info, err := d.Info()
		if err != nil {
			st.Errors++
			return nil
		}
		if !info.Mode().IsRegular() {
			return nil
		}

		st.FilesSeen++
		if info.Size() > s.MaxSize {
			st.SkippedBig++
			return nil
		}

		sum, err := s.Hasher.Sum(path)
		if err != nil {
			st.Errors++
			return nil
		}
		st.FilesHashed++

		tf, ok := s.Index.TrackedByHash(sum)
		if !ok {
			return nil // 不是我们盯的文件——这是「不做全盘监视」的保证
		}

		st.Matched++
		if s.Log != nil {
			s.Log.Info("普查命中被跟踪文件",
				"path", path, "sha256", sum[:12], "tracked_path", tf.Path)
		}
		if onHit != nil {
			onHit(Hit{Path: path, Sha256: sum, Size: info.Size()})
		}
		return nil
	})
}

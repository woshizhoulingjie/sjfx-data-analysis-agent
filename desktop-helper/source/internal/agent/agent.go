// Package agent 把各个模块串成一个可运行的整体。
//
// 这里不含业务逻辑，只有编排：起哪几个循环、循环之间怎么传递数据、
// 出错时怎么退避。真正的逻辑都在各自的包里。
package agent

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/woshizhoulingjie/sjfx-agent/internal/enforce"
	"github.com/woshizhoulingjie/sjfx-agent/internal/hash"
	"github.com/woshizhoulingjie/sjfx-agent/internal/idgen"
	"github.com/woshizhoulingjie/sjfx-agent/internal/model"
	"github.com/woshizhoulingjie/sjfx-agent/internal/store"
	"github.com/woshizhoulingjie/sjfx-agent/internal/sweep"
	"github.com/woshizhoulingjie/sjfx-agent/internal/syncer"
	"github.com/woshizhoulingjie/sjfx-agent/internal/watch"
)

// Config 是代理的运行参数。
type Config struct {
	ServerURL      string
	AgentVersion   string
	HeartbeatEvery time.Duration
	PreflightEvery time.Duration
	UploadEvery    time.Duration
	UploadBatch    int
	SweepEvery     time.Duration
	QueueLimit     int

	// HashWorkerEvery 是后台补算哈希的间隔。
	HashWorkerEvery time.Duration
	// HashBatch 是每轮最多补算几个文件的哈希。
	// 刻意留小：补算要读整个文件，不能和实时采集抢 IO。
	HashBatch int
	// MaxHashSize 是补算哈希的大小上限。超过的直接标记为失败，
	// 不无限期挂在队列里反复尝试。
	MaxHashSize int64
	// SweepRoots 是定期普查要扫的目录。留空表示用平台默认值。
	SweepRoots []string

	// WatchRoots 是实时监听要覆盖的目录。
	//
	// **注意：这个值不能从服务端清单推导**——服务端路径和本地路径
	// 是两个命名空间。留空时用平台默认落点（下载/桌面/文档）。
	WatchRoots []string

	// HashUnknownCandidates 决定「名字+大小都对不上的新文件，要不要也读内容算哈希」。
	//
	//	false（默认）—— 只对「名字+大小」匹配的文件算哈希。
	//	                  成本低、不读无关文件的内容（尊重"只追踪已入库文件"的承诺）。
	//	                  代价：**改了名的文件实时看不到**，靠定期普查兜底。
	//	true           —— 对监听目录里所有新文件都算哈希（受大小上限约束）。
	//	                  覆盖最全，但会读很多和 sjfx 无关的文件内容。
	//
	// 默认 false 是有意的：隐私和成本的边界要清楚，
	// 而"改名后实时发现"这个缺口由普查补——普查是低频的，
	// 而且可以单独配置扫描范围。
	HashUnknownCandidates bool
}

func (c *Config) withDefaults() {
	if c.HeartbeatEvery <= 0 {
		c.HeartbeatEvery = 60 * time.Second
	}
	if c.PreflightEvery <= 0 {
		c.PreflightEvery = 5 * time.Minute
	}
	if c.UploadEvery <= 0 {
		c.UploadEvery = 15 * time.Second
	}
	if c.UploadBatch <= 0 {
		c.UploadBatch = 200
	}
	if c.SweepEvery <= 0 {
		c.SweepEvery = 6 * time.Hour
	}
	if c.QueueLimit <= 0 {
		c.QueueLimit = 100000
	}
	if c.HashWorkerEvery <= 0 {
		c.HashWorkerEvery = 30 * time.Second
	}
	if c.HashBatch <= 0 {
		c.HashBatch = 4
	}
	if c.MaxHashSize <= 0 {
		c.MaxHashSize = 2 << 30 // 2 GiB
	}
}

// Agent 是代理实例。
type Agent struct {
	cfg      Config
	client   syncer.Client
	watcher  watch.Watcher
	store    store.Store
	index    store.Index
	enforcer *enforce.Dispatcher
	hasher   hash.Hasher
	log      *slog.Logger

	mu            sync.RWMutex
	policyVersion string
	trackedDigest string
	lastEventUTC  *time.Time
	watchRoots    int
	startedAt     time.Time

	// candMu/cands 跟踪「新出现但内容可能还没写完」的路径。
	//
	// 为什么需要它（端到端实测挖出来的漏洞）：
	//
	//	复制一个文件 = create（此刻内容还是空的或不全）+ 一串 write。
	//	如果只在 create 那一刻算哈希，算到的是不完整内容，永远匹配不上；
	//	而后续的 write 事件因为「路径没命中清单」会被直接过滤掉。
	//	结果是**所有分多次写入才落地的文件都追踪不到**——而所有文件复制都是这样。
	//
	// 所以新出现的未知路径先进候选表，等写入停下来（settle）再算一次哈希。
	candMu sync.Mutex
	cands  map[string]*candidate
	settle time.Duration

	// learnedDirs 是「已经确认过被跟踪文件的本地目录」。
	// 它们不在默认落点里，但确认过一次之后就要持续监听。
	learnedDirs map[string]struct{}
}

// Deps 是构造 Agent 需要的全部依赖，显式传入便于测试替换。
type Deps struct {
	Client   syncer.Client
	Watcher  watch.Watcher
	Store    store.Store
	Index    store.Index
	Enforcer enforce.Enforcer
	Hasher   hash.Hasher
	Logger   *slog.Logger
}

func New(cfg Config, d Deps) *Agent {
	cfg.withDefaults()
	log := d.Logger
	if log == nil {
		log = slog.Default()
	}
	return &Agent{
		cfg:         cfg,
		client:      d.Client,
		watcher:     d.Watcher,
		store:       d.Store,
		index:       d.Index,
		enforcer:    &enforce.Dispatcher{Enforcer: d.Enforcer, OnUndo: nil},
		hasher:      d.Hasher,
		log:         log,
		startedAt:   time.Now(),
		cands:       make(map[string]*candidate),
		settle:      2 * time.Second,
		learnedDirs: make(map[string]struct{}),
	}
}

// Run 启动代理并阻塞直到 ctx 取消。
//
// 四个循环并行跑，任何一个挂了都不应该拖垮其它三个：
// 同步挂了本地还在采集（进队列），采集挂了同步还在跑（心跳还在）。
// 只有身份失效（401）才需要整体停下来等重新注册。
func (a *Agent) Run(ctx context.Context) error {
	if err := a.store.Open(); err != nil {
		return err
	}
	defer a.store.Close()

	// 启动时先同步一次：拿到跟踪清单，才知道要监听哪些目录。
	if err := a.preflight(ctx); err != nil {
		// 拿不到清单也要继续跑——本地队列照常攒，
		// 等网络恢复后补上。启动即失败会让「断网时应用完全不可用」。
		a.log.Warn("首次 preflight 失败，先以空清单运行", "err", err)
	}

	var wg sync.WaitGroup
	errCh := make(chan error, 5)

	start := func(name string, fn func(context.Context) error) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := fn(ctx); err != nil && !errors.Is(err, context.Canceled) {
				errCh <- err
			}
		}()
	}

	start("sync", a.syncLoop)
	start("collect", a.collectLoop)
	start("upload", a.uploadLoop)
	start("sweep", a.sweepLoop)
	start("hashfill", a.hashFillLoop)

	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()

	select {
	case <-ctx.Done():
		a.stopCandidates()
		_ = a.watcher.Close()
		<-done
		return ctx.Err()
	case err := <-errCh:
		a.stopCandidates()
		_ = a.watcher.Close()
		return err
	case <-done:
		return nil
	}
}

// ---------------------------------------------------------------------------
// 循环 1 · 同步：心跳 + 拉策略与跟踪清单
// ---------------------------------------------------------------------------

func (a *Agent) syncLoop(ctx context.Context) error {
	hb := time.NewTicker(a.cfg.HeartbeatEvery)
	pf := time.NewTicker(a.cfg.PreflightEvery)
	defer hb.Stop()
	defer pf.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-hb.C:
			if err := a.heartbeat(ctx); err != nil {
				// 心跳失败不改状态：网络抖动是常态。
				// 只有明确的 401 才意味着身份没了。
				if errors.Is(err, syncer.ErrUnauthorized) {
					a.log.Error("node key 失效，需要重新注册")
					return err
				}
				a.log.Warn("心跳失败", "err", err)
			}
		case <-pf.C:
			if err := a.preflight(ctx); err != nil {
				a.log.Warn("preflight 失败", "err", err)
			}
		}
	}
}

func (a *Agent) heartbeat(ctx context.Context) error {
	depth, _ := a.store.Depth()

	a.mu.RLock()
	last := a.lastEventUTC
	roots := a.watchRoots
	a.mu.RUnlock()

	res, err := a.client.Heartbeat(ctx, syncer.Heartbeat{
		AgentVersion:  a.cfg.AgentVersion,
		UptimeSeconds: int64(time.Since(a.startedAt).Seconds()),
		WatchRoots:    roots,
		QueueDepth:    depth,
		LastEventUTC:  last,
	})
	if err != nil {
		return err
	}
	if !res.SessionValid {
		// 服务端说这个设备上的会话已失效——用户那边会被踢出登录。
		// 这不是错误，是设计行为，托盘要告诉用户为什么。
		a.log.Warn("服务端判定本设备会话已失效")
	}
	return nil
}

func (a *Agent) preflight(ctx context.Context) error {
	a.mu.RLock()
	knownPolicy := a.policyVersion
	digest := a.trackedDigest
	a.mu.RUnlock()

	res, err := a.client.Preflight(ctx, knownPolicy, digest)
	if err != nil {
		return err
	}

	if res.TrackedTruncated {
		// 清单被截断意味着「只盯了一部分文件」。这是必须让用户知道的事实，
		// 否则系统会给人「全覆盖」的错觉。
		a.log.Warn("跟踪清单被服务端截断，本地覆盖不完整")
	}

	if err := a.index.ReplaceTracked(res.Tracked); err != nil {
		return err
	}

	// 监听哪些目录由客户端自己决定，**不是**从服务端清单的 path 推导。
	// 服务端路径（/home/nas511/...）在用户电脑上毫无意义，
	// 用它推导会得到一堆不存在的路径，一个目录都挂不上。
	a.refreshWatchRoots()

	a.mu.RLock()
	n := a.watchRoots
	a.mu.RUnlock()

	a.mu.Lock()
	a.policyVersion = res.PolicyVersion
	a.mu.Unlock()

	a.log.Info("preflight 完成",
		"tracked", len(res.Tracked), "watch_roots", n, "policy", res.PolicyVersion)

	a.applyPolicies(ctx, res.Policies)
	a.executeActions(ctx, res.Actions)
	return nil
}

// computeWatchRoots 决定「本地要监听哪些目录」。
//
// # 为什么不能从服务端清单的 path 推导（这是一个被实测证实的坑）
//
// 服务端路径和用户电脑上的路径是**两个不同的命名空间**：
//
//	服务端                          用户电脑
//	/home/nas511/zhoulingjie/       C:\Users\xxx\Downloads\
//	        report.pdf         ≠           report.pdf
//
// 用服务端路径去推导本地目录，在 Windows 上会得到
// `\home\nas511\zhoulingjie` 这种不存在的路径 → 一个目录都挂不上
// → **实时监听完全失效**，只剩 6 小时一次的普查偶尔撞见。
//
// 所以监听范围必须由客户端自己决定，依据是「用户可能把文件放到哪」，
// 而不是「文件在服务器上放在哪」。
func (a *Agent) computeWatchRoots() []string {
	seen := make(map[string]struct{})
	var out []string
	add := func(dirs ...string) {
		for _, d := range dirs {
			if d == "" {
				continue
			}
			d = filepath.Clean(d)
			if _, dup := seen[d]; dup {
				continue
			}
			seen[d] = struct{}{}
			out = append(out, d)
		}
	}

	// 1. 运维显式配置的目录（生产部署时由管理员指定 sjfx 工作目录）
	add(a.cfg.WatchRoots...)

	// 2. 没配就用平台默认落点：下载 / 桌面 / 文档
	//    这些是「从 sjfx 拿到文件之后第一时间的落点」
	if len(out) == 0 {
		add(sweep.DefaultRoots()...)
	}

	// 3. 已经确认过的本地文件所在目录。
	//    用户可能把文件存到了默认落点之外（比如 D:\项目\），
	//    一旦在那里认出来一份，就要把那个目录也纳入监听。
	a.mu.RLock()
	for d := range a.learnedDirs {
		add(d)
	}
	a.mu.RUnlock()

	return out
}

// rememberLocal 记录「这个本地路径已确认属于某份被跟踪文件」。
//
// 两个作用：
//  1. 此后这个本地路径走**路径匹配**（快），不必每次重算哈希
//  2. 把所在目录纳入监听范围，这样它在非默认位置也能被持续跟踪
func (a *Agent) rememberLocal(path, sum string, tf model.TrackedFile) {
	if path == "" {
		return
	}
	a.index.Remember(path, sum, tf)

	dir := filepath.Dir(path)
	if dir == "" || dir == "." {
		return
	}
	a.mu.Lock()
	if a.learnedDirs == nil {
		a.learnedDirs = make(map[string]struct{})
	}
	_, known := a.learnedDirs[dir]
	a.learnedDirs[dir] = struct{}{}
	a.mu.Unlock()

	// 新目录要立刻挂上监听，不必等下一轮 preflight
	if !known {
		a.refreshWatchRoots()
	}
}

// refreshWatchRoots 用当前应该监听的目录集合刷新监听器。
func (a *Agent) refreshWatchRoots() {
	roots := a.computeWatchRoots()
	n, err := a.watcher.SetRoots(roots)
	if err != nil {
		a.log.Warn("刷新监听目录失败", "err", err)
		return
	}
	a.mu.Lock()
	a.watchRoots = n
	a.mu.Unlock()
}

// ---------------------------------------------------------------------------
// 循环 2 · 采集：监听事件 → 过滤 → 哈希 → 入队
// ---------------------------------------------------------------------------

func (a *Agent) collectLoop(ctx context.Context) error {
	events := a.watcher.Events()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case we, ok := <-events:
			if !ok {
				return errors.New("agent: 监听通道被关闭")
			}
			a.handleWatchEvent(ctx, we)
		}
	}
}

// handleWatchEvent 是两级过滤的核心，见 store.Filter 的说明。
func (a *Agent) handleWatchEvent(ctx context.Context, we watch.Event) {
	if we.IsDir {
		return // 只关心文件；目录变化由 watch 自己处理（补挂）
	}

	activity := toActivity(we.Op)

	// ---- 第一级：路径命中（快）----
	if tf, ok := a.index.TrackedByPath(we.Path); ok {
		sum := a.emit(ctx, we, activity, "")
		// 记住这个内容版本。没有这一步，用户改过文件之后再复制、
		// 再改名，出现的新内容哈希就查不到了，事件会全部丢失。
		a.rememberLocal(we.Path, sum, tf)
		return
	}

	// ---- 第二级：哈希反查（慢但必要）----
	//
	// 改名和复制产生的是**新路径**，路径查不到，只能靠算出来的哈希反查。
	//
	// 但不能在这里立刻算：复制一个文件时 create 事件到达的那一刻，
	// 内容往往还是空的或只写了一半，算出来的哈希永远匹配不上。
	// 所以先挂成候选，等写入停下来再算（见 settleCandidate）。
	switch we.Op {
	case watch.OpCreate, watch.OpRename, watch.OpWrite:
		a.settleCandidate(ctx, we, activity)
	}
}

// candidate 是一个「还不知道是不是我们盯的文件」的待定路径。
type candidate struct {
	timer    *time.Timer
	op       watch.Op
	activity model.ActivityID
}

// settleCandidate 把一个「未知路径的变动」挂成候选，等写入稳定后再算哈希。
//
// 每次新的变动都会重置计时器，所以只有写入真正停下来之后才会算一次哈希。
// 这样既避免了拿半截内容去比对，也避免了边写边算把 CPU 烧光。
func (a *Agent) settleCandidate(ctx context.Context, we watch.Event, activity model.ActivityID) {
	a.candMu.Lock()
	defer a.candMu.Unlock()

	if c, ok := a.cands[we.Path]; ok {
		c.timer.Reset(a.settle)
		return
	}

	path := we.Path
	op := we.Op
	c := &candidate{op: op, activity: activity}
	c.timer = time.AfterFunc(a.settle, func() {
		a.candMu.Lock()
		delete(a.cands, path)
		a.candMu.Unlock()
		a.resolveCandidate(ctx, path, activity, op)
	})
	a.cands[path] = c
}

// resolveCandidate 在写入稳定后算一次哈希，命中就上报并把这个路径记进索引。
//
// # 什么情况下才去读文件内容（这是成本与隐私的边界）
//
//	名字命中跟踪清单        → 算哈希（可能是确证，也可能是"疑似改过的版本"）
//	是**新出现**的文件       → 算哈希。即使名字对不上——
//	                         Windows 复制文件会自动改名（report (1).txt、
//	                         report - 副本.txt），**改名副本恰恰是最需要抓的**。
//	是已存在文件的普通修改   → 不算。（否则会持续读一堆无关文件的全部内容）
//	开了 HashUnknownCandidates → 全都算
//
// 这个区分很重要：如果名字不对就不算，"复制一份"这个最核心的行为会整个漏掉；
// 如果什么都算，"在满是无关文件的下载目录里持续读盘"又会拖垮机器。
func (a *Agent) resolveCandidate(ctx context.Context, path string, activity model.ActivityID, op watch.Op) {
	fi, err := os.Stat(path)
	if err != nil || fi.IsDir() {
		return // 文件已经不在了，或变成了目录——没有反查的意义
	}

	_, nameHint := a.index.TrackedByName(filepath.Base(path))

	// 新出现的文件即使名字对不上也要看内容：改名副本必须能抓住
	isNewArrival := op == watch.OpCreate || op == watch.OpRename

	if !nameHint && !isNewArrival && !a.cfg.HashUnknownCandidates {
		return
	}

	if !hash.ShouldHashNow(fi.Size()) {
		// 大文件不在采集循环里算哈希，否则一个大文件就能把采集卡住。
		//
		// 但也不能不管：**大文件的副本恰恰是最值得追踪的**。
		// 所以入队一条 hashed=0 的事件，交给 hashFillLoop 后台补算。
		if fi.Size() <= a.cfg.MaxHashSize {
			we := watch.Event{Path: path, Op: op, Time: time.Now()}
			a.emitWithCollector(ctx, we, activity, "", model.CollectorWatch, fi.Size(), model.MatchUnmarked)
			a.log.Info("大文件副本待后台补算哈希", "path", path, "size", fi.Size())
		}
		return
	}

	sum, err := a.hasher.Sum(path)
	if err != nil {
		return
	}

	we := watch.Event{Path: path, Op: op, Time: time.Now()}

	if tf, ok := a.index.TrackedByHash(sum); ok {
		// 哈希一致：**确认**就是同一份内容
		a.emitWithCollector(ctx, we, model.ActivityCreate, sum, model.CollectorWatch, fi.Size(), model.MatchExact)
		a.rememberLocal(path, sum, tf)
		a.log.Info("认出被跟踪文件的副本（内容一致）",
			"path", path, "sha256", sum[:12])
		return
	}

	if nameHint {
		// 名字对得上但内容不同：**疑似**是同一份文件的修改版。
		//
		// 为什么也要上报，而不是静默丢弃：
		//   这个系统是审计用的，**漏掉比误报更糟**——
		//   误报可以人工排除，漏掉就永远不知道发生过。
		//   事件里明确标 MatchSuspect，让服务端和人工去判断，
		//   而不是由客户端替他们做这个决定。
		a.emitWithCollector(ctx, we, model.ActivityCreate, sum, model.CollectorWatch, fi.Size(), model.MatchSuspect)
		a.log.Info("发现疑似同一份文件的修改版（名字相同、内容不同）",
			"path", path, "sha256", sum[:12])
	}
}

// emit 产生一条事件并入队，返回这条事件最终用的哈希（算不出来时返回空串）。
//
// 返回值不是可有可无的：调用方要用它去 index.Remember，
// 否则「改过之后再复制」这一类事件会永久丢失。
func (a *Agent) emit(ctx context.Context, we watch.Event, activity model.ActivityID, knownSum string) string {
	return a.emitWithCollector(ctx, we, activity, knownSum, model.CollectorWatch, -1, model.MatchUnmarked)
}

// emitWithCollector 是 emit 的完整版本，多三个参数：
//
//	collector —— 这条事件是哪个采集器产生的（实时监听 / 普查），
//	             排查问题时要能一眼看出「是实时抓到的还是普查补上的」
//	knownSize —— 已知的文件大小；-1 表示需要自己去 os.Stat
//	match     —— 与跟踪清单的匹配强度（exact / suspect / 未标记）。
//	             把"确证"和"猜测"分开，否则审计结论的可信度会被稀释。
func (a *Agent) emitWithCollector(
	ctx context.Context,
	we watch.Event,
	activity model.ActivityID,
	knownSum string,
	collector model.Collector,
	knownSize int64,
	match model.MatchKind,
) string {
	sum := knownSum
	hashed := model.HashedNo
	size := knownSize

	switch {
	case sum != "":
		hashed = model.HashedYes
	default:
		// 先看文件多大。大文件不在采集循环里算哈希——
		// 往监听目录里丢一个 10GB 的文件，整个采集会被它卡死。
		//
		// 但**不能直接跳过**：跳过就等于永久漏掉这条事件。
		// 正确做法是先入队（Hashed=0），由 hashFillLoop 后台补算。
		var sz int64 = -1
		if fi, err := os.Stat(we.Path); err == nil && !fi.IsDir() {
			sz = fi.Size()
			if hash.ShouldHashNow(sz) {
				if s, err := a.hasher.Sum(we.Path); err == nil {
					sum, hashed = s, model.HashedYes
				} else {
					// 算不出来也必须上报——「文件变了但我读不到」本身是重要事实。
					hashed = model.HashedFailed
				}
			}
			// 大文件：保持 HashedNo，交给后台补算
		} else {
			// 文件已经不在（删除事件）或读不到
			hashed = model.HashedFailed
		}
		if size < 0 {
			size = sz
		}
	}
	if size < 0 {
		size = 0
	}

	ev := model.FileEvent{
		EventID:    idgen.New(),
		Time:       we.Time.UTC(),
		ActivityID: activity,
		Sha256:     sum,
		Hashed:     hashed,
		Path:       we.Path,
		OldPath:    we.OldPath,
		FileName:   filepath.Base(we.Path),
		Size:       size,
		Collector:  collector,
		Match:      match,
	}

	if err := a.store.Enqueue(ev); err != nil {
		a.log.Error("事件入队失败", "path", we.Path, "err", err)
		return sum
	}

	now := ev.Time
	a.mu.Lock()
	a.lastEventUTC = &now
	a.mu.Unlock()

	return sum
}

// toActivity 把底层文件系统操作翻译成 OCSF 动作。
func toActivity(op watch.Op) model.ActivityID {
	switch op {
	case watch.OpCreate:
		return model.ActivityCreate
	case watch.OpWrite:
		return model.ActivityUpdate
	case watch.OpRemove:
		return model.ActivityDelete
	case watch.OpRename:
		// 见 watch 包末尾的说明：改名在客户端不配对，
		// 上报成一次「旧路径消失」+ 一次「新路径出现」，
		// 服务端按同一哈希配对。这里把 rename 当作 delete 上报，
		// 新路径会由紧随其后的 create 事件覆盖。
		return model.ActivityRename
	case watch.OpChmod:
		return model.ActivitySetAttributes
	default:
		return model.ActivityUpdate
	}
}

// ---------------------------------------------------------------------------
// 循环 3 · 上报
// ---------------------------------------------------------------------------

func (a *Agent) uploadLoop(ctx context.Context) error {
	t := time.NewTicker(a.cfg.UploadEvery)
	defer t.Stop()

	backoff := time.Second
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
		}

		batch, err := a.store.DequeueBatch(a.cfg.UploadBatch)
		if err != nil {
			a.log.Error("取事件失败", "err", err)
			continue
		}
		if len(batch) == 0 {
			continue
		}

		res, err := a.client.UploadEvents(ctx, batch)
		if err != nil {
			if errors.Is(err, syncer.ErrUnauthorized) {
				return err
			}
			// 失败就留在队列里，指数退避。事件不丢是硬要求。
			a.log.Warn("上报失败，稍后重试", "count", len(batch), "err", err)
			time.Sleep(backoff)
			if backoff < 5*time.Minute {
				backoff *= 2
			}
			continue
		}
		backoff = time.Second

		ids := make([]string, 0, len(batch))
		for _, e := range batch {
			ids = append(ids, e.EventID)
		}
		if err := a.store.MarkSent(ids); err != nil {
			a.log.Error("标记已发失败（会导致重复上报）", "err", err)
		}

		// 处置指令跟着上报响应回来，所以服务端能在
		// 「看见副本出现在桌面」的同一刻就要求隔离它。
		a.executeActions(ctx, res.Actions)
	}
}

// ---------------------------------------------------------------------------
// 循环 4 · 定期普查
// ---------------------------------------------------------------------------

// sweepLoop 扫常见位置，补 watch 覆盖不到的场景。
//
// 为什么需要它：watch 只能看到「挂了监听的目录」里发生的事。
// 用户把文件从 sjfx 下载到桌面（在监听范围内，能抓到），
// 但接着拖进 U 盘或网盘同步目录（不一定在范围里），watch 就看不见了。
// 定期扫桌面/下载/文档/可移动设备，把这类补上。
//
// 刻意不做全盘扫描：那既慢，又违背「只追踪已入库文件」这个承诺。
func (a *Agent) sweepLoop(ctx context.Context) error {
	scanner := sweep.New(a.index, a.hasher, a.log)
	scanner.Roots = a.cfg.SweepRoots // 空表示用平台默认值

	t := time.NewTicker(a.cfg.SweepEvery)
	defer t.Stop()

	// 启动后先跑一轮——用户可能在代理启动之前就已经把文件拷出去了
	a.runSweep(ctx, scanner)

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
			a.runSweep(ctx, scanner)
		}
	}
}

func (a *Agent) runSweep(ctx context.Context, scanner *sweep.Scanner) {
	st, err := scanner.Scan(ctx, func(h sweep.Hit) {
		// 已经认识的路径不再重复上报。
		//
		// 没有这一步，每轮普查都会把同一批文件重新报一遍——
		// 6 小时一次，一个文件在机器上放一年就是 1460 条重复事件。
		// 那不是"更完整的记录"，那是把真正的新事件淹掉的噪声。
		if _, known := a.index.TrackedByPath(h.Path); known {
			return
		}

		// 普查发现的文件一律按「新出现的副本」上报。
		// 哈希已经算过了，直接带上，不用再算一次。
		// 普查是按内容比对命中的，所以匹配强度是 exact。
		we := watch.Event{Path: h.Path, Op: watch.OpCreate, Time: time.Now()}
		a.emitWithCollector(ctx, we, model.ActivityCreate, h.Sha256,
			model.CollectorSweep, h.Size, model.MatchExact)

		// 记进索引：既避免下轮重复上报，也让这份副本后续的
		// 删除/改名能被路径查询追踪到。
		if tf, ok := a.index.TrackedByHash(h.Sha256); ok {
			a.rememberLocal(h.Path, h.Sha256, tf)
		}
	})
	if err != nil && !errors.Is(err, context.Canceled) {
		a.log.Warn("普查异常", "err", err)
	}

	// 这行日志很重要：它让「监控到底覆盖了多少」变得可验证，
	// 而不是只能相信它是好的。
	a.log.Info("普查完成",
		"roots", st.Roots, "seen", st.FilesSeen, "hashed", st.FilesHashed,
		"matched", st.Matched, "skipped_big", st.SkippedBig,
		"errors", st.Errors, "took", st.Duration.Round(time.Millisecond))
}

// ---------------------------------------------------------------------------
// 循环 5 · 后台补算哈希
// ---------------------------------------------------------------------------

// hashFillLoop 给「入队时没算哈希」的事件补上哈希。
//
// 什么事件会没有哈希：采集时文件太大（超过 64 MiB），
// 在采集循环里算会阻塞整个监听。但不能因此漏掉事件——
// 大文件的副本恰恰是最值得追踪的。所以先入队，后台慢慢补。
//
// 补算之后如果命中跟踪索引，还会把这个路径记进索引，
// 这样它后续的删除/改名也能被追踪到。
func (a *Agent) hashFillLoop(ctx context.Context) error {
	t := time.NewTicker(a.cfg.HashWorkerEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
			a.fillHashes(ctx)
		}
	}
}

func (a *Agent) fillHashes(ctx context.Context) {
	pending, err := a.store.PendingHash(a.cfg.HashBatch)
	if err != nil {
		a.log.Warn("取待补算事件失败", "err", err)
		return
	}
	for _, ev := range pending {
		if ctx.Err() != nil {
			return
		}

		fi, err := os.Stat(ev.Path)
		if err != nil || fi.IsDir() {
			// 文件已经不在了。**必须标记为失败而不是留在队列里**，
			// 否则它会每轮都被取出来重试，永远出不去。
			if err := a.store.SetHash(ev.EventID, "", model.HashedFailed); err != nil {
				a.log.Warn("标记哈希失败态出错", "event", ev.EventID, "err", err)
			}
			continue
		}
		if fi.Size() > a.cfg.MaxHashSize {
			a.log.Warn("文件超过补算上限，放弃算哈希",
				"path", ev.Path, "size", fi.Size(), "limit", a.cfg.MaxHashSize)
			if err := a.store.SetHash(ev.EventID, "", model.HashedFailed); err != nil {
				a.log.Warn("标记哈希失败态出错", "event", ev.EventID, "err", err)
			}
			continue
		}

		sum, err := a.hasher.Sum(ev.Path)
		if err != nil {
			if err := a.store.SetHash(ev.EventID, "", model.HashedFailed); err != nil {
				a.log.Warn("标记哈希失败态出错", "event", ev.EventID, "err", err)
			}
			continue
		}

		if err := a.store.SetHash(ev.EventID, sum, model.HashedYes); err != nil {
			a.log.Warn("回填哈希失败", "event", ev.EventID, "err", err)
			continue
		}

		// 补算出来之后再做一次哈希反查：
		// 如果这就是某份被跟踪文件的副本，把它记进索引，
		// 否则它之后被删除时事件走路径查询会查不到。
		if tf, ok := a.index.TrackedByHash(sum); ok {
			a.rememberLocal(ev.Path, sum, tf)
			a.log.Info("补算后认出被跟踪文件的副本",
				"path", ev.Path, "sha256", sum[:12])
		}
	}
}

func (a *Agent) stopCandidates() {
	a.candMu.Lock()
	defer a.candMu.Unlock()
	for path, c := range a.cands {
		c.timer.Stop()
		delete(a.cands, path)
	}
}

// ---------------------------------------------------------------------------
// 处置
// ---------------------------------------------------------------------------

func (a *Agent) applyPolicies(ctx context.Context, p model.Policies) {
	// 策略是幂等的：每次都按当前值设置一遍，不去判断「有没有变过」。
	// 这样即使某次执行失败，下一轮会自动纠正回来。
	if err := a.enforcer.Enforcer.BlockRemovableWrite(ctx, p.RemovableWriteBlock); err != nil {
		a.log.Warn("设置 U 盘写保护失败", "enable", p.RemovableWriteBlock, "err", err)
	}
	if err := a.enforcer.Enforcer.BlockDomain(ctx, p.BlockedDomains, len(p.BlockedDomains) > 0); err != nil {
		a.log.Warn("设置域名封锁失败", "err", err)
	}
}

func (a *Agent) executeActions(ctx context.Context, actions []model.Action) {
	for _, act := range actions {
		if _, err := a.enforcer.Execute(ctx, act); err != nil {
			a.log.Error("处置失败", "kind", act.Kind, "path", act.Path, "err", err)
			continue
		}
		a.log.Info("已处置", "kind", act.Kind, "path", act.Path, "reason", act.Reason)
	}
}

"""Incremental semantic groups for the isolated large-package directory only."""
from __future__ import annotations
import hashlib
import math
import json
import logging
import os
import re
import sqlite3
import time
from functools import wraps
from contextlib import closing
from pathlib import Path
import numpy as np
from services import large_package_catalog as catalog
from services.embedding_encoding import prefix as embedding_prefix, tokenizer as selected_embedding_tokenizer

LOGGER = logging.getLogger(__name__)

EMBED_BATCH = 32
SIMILARITY = .94


def _leaf_batch(method):
    @wraps(method)
    def guarded(store, package_id, *args, **kwargs):
        from services.large_package_catalog_coordination import leaf_batch
        with leaf_batch(store, package_id, method.__name__, kwargs.get('cancelled', lambda: False)) as allowed:
            return method(store, package_id, *args, **kwargs) if allowed else 0
    return guarded

# 全局聚类的状态一律落库（见 large_catalog_km_state / _km_signature）。
# 这里曾有一个进程内字典记录"已聚类的文件数"，它在 Worker 重启后归零，
# 导致同一份语料在不同进程生命周期里得到不同目录——目录不可复现。


def init(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS large_catalog_names (
      model TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(model,digest));
    CREATE TABLE IF NOT EXISTS large_catalog_embeddings (
      model TEXT NOT NULL, digest TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(model,digest));
    CREATE TABLE IF NOT EXISTS large_catalog_groups (
      package_id TEXT NOT NULL, id TEXT NOT NULL, model TEXT NOT NULL,
      anchor BLOB NOT NULL, representative TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL DEFAULT 'queued', updated_at REAL NOT NULL,
      PRIMARY KEY(package_id,id));
    CREATE INDEX IF NOT EXISTS idx_large_catalog_group_status ON large_catalog_groups(package_id,status);
    CREATE TABLE IF NOT EXISTS large_catalog_members (
      package_id TEXT NOT NULL, path TEXT NOT NULL, group_id TEXT NOT NULL,
      input_hash TEXT NOT NULL, vector BLOB NOT NULL,
      PRIMARY KEY(package_id,path));
    CREATE INDEX IF NOT EXISTS idx_large_catalog_members_group ON large_catalog_members(package_id,group_id);
    CREATE TABLE IF NOT EXISTS large_catalog_buckets (
      package_id TEXT NOT NULL, band INTEGER NOT NULL, bucket INTEGER NOT NULL, group_id TEXT NOT NULL,
      PRIMARY KEY(package_id,band,bucket,group_id));
    ''')
    if 'attempts' not in {r[1] for r in conn.execute('PRAGMA table_info(large_catalog_groups)')}:
        conn.execute('ALTER TABLE large_catalog_groups ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0')
    # Counts are updated in the membership transaction, avoiding a growing
    # COUNT scan on every file and every naming request.
    if 'member_count' not in {r[1] for r in conn.execute('PRAGMA table_info(large_catalog_groups)')}:
        conn.execute('ALTER TABLE large_catalog_groups ADD COLUMN member_count INTEGER NOT NULL DEFAULT 0')
        conn.execute('UPDATE large_catalog_groups SET member_count=(SELECT COUNT(*) FROM large_catalog_members m WHERE m.package_id=large_catalog_groups.package_id AND m.group_id=large_catalog_groups.id)')
    # 批2 第 16 条：`result_revision` 列的迁移**不在这里** —— 它依赖
    # `content_revision`，而那一列由 `large_package_catalog_naming.init` 创建，
    # 而本函数在 `_init_db` 里**先于**它被调用（semantic → naming），
    # 放这里会在全新库上炸（实测：no such column: content_revision，16 个用例全红）。
    # 迁移放在 naming.init 里，见那边的注释。
    # Cached corpus centre + adaptive similarity threshold for one package.
    conn.execute('''CREATE TABLE IF NOT EXISTS large_catalog_centers (
      package_id TEXT NOT NULL, model TEXT NOT NULL, center BLOB, threshold REAL NOT NULL,
      sample INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
      PRIMARY KEY(package_id,model))''')
    # 全局聚类的持久化状态：记录"上一次用于聚类的向量集指纹"。
    # 为什么要落库而不是放内存字典：内存版在 Worker 重启后会归零，
    # 于是同一个包在不同进程生命周期里会得到不同的分组——目录不可复现。
    # 指纹没变就完全不重复聚类；指纹变了（新增文件、或深度解析改写了正文
    # 触发重新嵌入）才重算一次。
    conn.execute('''CREATE TABLE IF NOT EXISTS large_catalog_km_state (
      package_id TEXT PRIMARY KEY, signature TEXT NOT NULL DEFAULT '',
      vectors INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0)''')
    # k 自适应（2026-09-20）：该包已升过几档 k。**必须落库**：升档要在"定稿 →
    # 重新聚类 → 重新命名"之间保持，进程重启后不能归零，否则会无限重聚类。
    if 'k_boost' not in {r[1] for r in conn.execute('PRAGMA table_info(large_catalog_km_state)')}:
        conn.execute('ALTER TABLE large_catalog_km_state ADD COLUMN k_boost INTEGER NOT NULL DEFAULT 0')
    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_large_catalog_members_digest ON large_catalog_members(package_id,input_hash);
    CREATE TRIGGER IF NOT EXISTS lp_member_count_add AFTER INSERT ON large_catalog_members BEGIN
      UPDATE large_catalog_groups SET member_count=member_count+1 WHERE package_id=NEW.package_id AND id=NEW.group_id;
    END;
    CREATE TRIGGER IF NOT EXISTS lp_member_count_remove AFTER DELETE ON large_catalog_members BEGIN
      UPDATE large_catalog_groups SET member_count=MAX(0,member_count-1) WHERE package_id=OLD.package_id AND id=OLD.group_id;
    END;
    CREATE TRIGGER IF NOT EXISTS lp_member_count_move AFTER UPDATE OF group_id,package_id ON large_catalog_members
      WHEN OLD.group_id!=NEW.group_id OR OLD.package_id!=NEW.package_id BEGIN
      UPDATE large_catalog_groups SET member_count=MAX(0,member_count-1) WHERE package_id=OLD.package_id AND id=OLD.group_id;
      UPDATE large_catalog_groups SET member_count=member_count+1 WHERE package_id=NEW.package_id AND id=NEW.group_id;
    END;
    """)


def embed_batch_size():
    """本车道的单请求批大小。原来硬编码 8，config 里的 VLLM_EMBED_MAX_BATCH_SIZE 对这条车道
    是**死的**——所以调那个开关一直没效果（实测：批 16 比批 8 快 64%）。"""
    try:
        from config import Config
        return max(1, min(64, int(os.getenv('SJFX_LP_EMBED_MAX_BATCH_SIZE', '').strip() or
            getattr(Config, 'VLLM_EMBED_MAX_BATCH_SIZE', 8) or 8)))
    except Exception:
        return 8


def embed_timeout():
    """本车道的嵌入超时。默认 60s，而不是共享的 8s：

    实测**嵌入服务空闲后的第一个请求要 ~21 秒**（批 1 = 21.46s，随后同样内容 0.09s），
    而共享客户端的超时是 8s。于是服务一重启，第一批嵌入请求必然全部超时失败
    （靠上层的"不可用→长退避"自愈，但要白等两分钟）。这里给本车道一个更宽松、
    但仍然有界的超时；`SJFX_LP_EMBED_TIMEOUT` 可调。
    """
    try:
        from config import Config
        value = getattr(Config, 'LP_EMBED_TIMEOUT_SECONDS', None)
        if value:
            return max(5, min(600, int(value)))
    except Exception:
        pass
    return 60


class FallbackEmbeddingClient:
    """Use an attested equivalent backup only for transient transport failures."""

    def __init__(self, primary, secondary):
        self.primary, self.secondary = primary, secondary
        self.fallbacks = 0

    def __getattr__(self, name):
        return getattr(self.primary, name)

    @property
    def identity(self):
        # Persisted identity always belongs to the configured primary endpoint.
        return self.primary.identity

    def _legacy_namespace(self, identity):
        return self.primary._legacy_namespace(identity)

    def _require_equivalent_backup(self):
        from services.model_contract import LocalModelError
        if not self.primary._resolved_identity:
            raise LocalModelError('主向量端点尚未核验真实身份，拒绝未验证的备用切换', code='embedding_incompatible')
        primary = self.primary._legacy_namespace(self.primary.identity)
        secondary = self.secondary._legacy_namespace(self.secondary.identity)
        fields = ('model', 'backend', 'encoding_policy', 'weights_id', 'config_id', 'version', 'max_chars')
        if (not primary or primary != secondary or primary != self.model or
                not self.primary.weights_id or
                any(getattr(self.primary, key) != getattr(self.secondary, key) for key in fields) or
                not self.primary.dimension or self.primary.dimension != self.secondary.dimension):
            raise LocalModelError('备用向量端点未通过同权重、编码和维度备案，拒绝混用', code='embedding_incompatible')

    def embed(self, texts, retries=None):
        from services.embedding_backend import validate_vectors
        from services.model_contract import LocalModelError
        kwargs = {} if retries is None else {'retries': retries}
        try:
            return self.primary.embed(texts, **kwargs)
        except LocalModelError as exc:
            if not exc.retryable or exc.code not in {'unavailable', 'timeout', 'busy', 'resource_exhausted'}:
                raise
            self._require_equivalent_backup()
            self.fallbacks += 1
            LOGGER.warning('embedding primary temporarily unavailable [%s]; using verified backup %s',
                           exc.code, self.secondary.base_url)
            values = self.secondary.embed(texts, **kwargs)
            return validate_vectors(values, dimension=self.primary.dimension)

    def with_options(self, **options):
        return type(self)(self.primary.with_options(**options), self.secondary.with_options(**options))

    def health_check(self, timeout=3):
        from services.model_contract import LocalModelError
        primary = self.primary.health_check(timeout=timeout)
        if primary.get('reachable') and primary.get('model_available'):
            return primary
        if primary.get('code') not in {'unavailable', 'timeout', 'busy', 'resource_exhausted'}:
            return {**primary, 'fallback_verified': False}
        # Health is a bounded list-only probe, never a 60s identity bootstrap.
        if not self.primary._resolved_identity or not self.secondary._resolved_identity:
            return {**primary, 'fallback_verified': False}
        try:
            self._require_equivalent_backup()
        except LocalModelError:
            return {**primary, 'fallback_verified': False}
        return {**self.secondary.health_check(timeout=timeout), 'fallback_verified': True,
                'primary': primary}


class _EmbeddingConfigView:
    """Override one lane without mutating process configuration or other clients."""
    def __init__(self, config, **overrides):
        self.config, self.overrides = config, overrides

    def __getattr__(self, name):
        if name in self.overrides:
            return self.overrides[name]
        return getattr(self.config, name)


def fallback_base_url():
    from config import Config
    return str(getattr(Config, 'LP_EMBED_FALLBACK_URL', '') or
               os.getenv('SJFX_LP_EMBED_FALLBACK_URL', '')).strip()


def embedding_client():
    from config import Config
    from services.embedding_backend import create_embedding_client
    options = dict(lane='large', timeout=embed_timeout(), max_batch_size=embed_batch_size())
    primary = create_embedding_client(Config, **options)
    backup = fallback_base_url()
    if primary is None or not backup or backup.rstrip('/') == primary.base_url:
        return primary
    view = _EmbeddingConfigView(Config, EMBED_BASE_URL=backup,
        SJFX_LP_EMBED_BASE_URL=backup,
        SJFX_EMBED_LEGACY_ADOPTION_FILE=str(Path(Config.DATA_DIR) / 'embedding-fallback-adoption.json'))
    secondary = create_embedding_client(view, **options)
    return FallbackEmbeddingClient(primary, secondary)


def _require_space(conn, package_id, client, dimension=0):
    from services.embedding_backend import client_identity, require_index_identity
    identity = client_identity(client)
    models = [r[0] for r in conn.execute('SELECT DISTINCT model FROM large_catalog_groups WHERE package_id=?', (package_id,))]
    require_index_identity(conn, 'large', package_id, identity, dimension, models)
    dims = {r[0] for r in conn.execute('SELECT DISTINCT LENGTH(vector)/4 FROM large_catalog_members WHERE package_id=? AND LENGTH(vector)>0', (package_id,))}
    if len(dims)>1 or (dimension and dims and dims != {dimension}):
        from services.model_contract import LocalModelError
        raise LocalModelError('大包向量维度不一致，旧索引已保留，请重建', code='embedding_rebuild_required')
    return identity


def _vector(value):
    vector = np.asarray(value, dtype=np.float32)
    if vector.ndim != 1 or not len(vector) or not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-8:
        raise ValueError('无效语义向量')
    return vector / np.linalg.norm(vector)


def _project(vector, center):
    """Remove the corpus-wide common component before comparing documents.

    Mean-pooled sentence vectors are strongly anisotropic: unrelated documents
    already sit at ~0.9 cosine.  Subtracting the corpus mean (all-but-the-mean)
    restores the usable spread without touching the stored vectors, so reported
    similarities stay comparable with older records.
    """
    if center is None:
        return vector
    projected = np.asarray(vector, dtype=np.float32) - center
    norm = float(np.linalg.norm(projected))
    if norm < 1e-8:
        return vector
    return projected / norm


def _norm(vector):
    value = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(value))
    return value / norm if norm > 1e-8 else value


# Characters that legitimately appear in the corpora we ingest.  Anything else
# (IPA extensions, arrows, geometric shapes, private-use glyphs) is a strong
# signal that PDF text extraction produced mojibake instead of real prose.
_ALLOWED_RANGES = (
    (0x0020, 0x007E),   # ASCII
    (0x00A0, 0x017F),   # Latin-1 + Latin Extended-A (European documents)
    (0x0370, 0x03FF),   # Greek
    (0x0E01, 0x0E3A),   # Assigned Thai letters, vowels and combining marks
    (0x0E3F, 0x0E5B),   # Thai currency, vowels, tone marks, digits, punctuation
    (0x2000, 0x206F),   # general punctuation
    (0x20A0, 0x20BF),   # currency
    (0x2100, 0x214F),   # letterlike symbols (№, ℃)
    (0x2190, 0x21FF),   # arrows are common in real business documents
    (0x3000, 0x303F),   # CJK punctuation
    (0x3040, 0x30FF),   # kana
    (0x3400, 0x4DBF),   # CJK extension A
    (0x4E00, 0x9FFF),   # CJK unified
    (0xAC00, 0xD7AF),   # Hangul
    (0xF900, 0xFAFF),   # CJK compatibility ideographs
    (0xFF00, 0xFFEF),   # fullwidth forms
)


def _is_allowed(code):
    for low, high in _ALLOWED_RANGES:
        if low <= code <= high:
            return True
    return False


def garble_ratio(text):
    """Share of characters that cannot come from real prose."""
    value = str(text or '')
    if len(value) < 40:
        return 0.0
    bad = total = 0
    for char in value:
        if char.isspace():
            continue
        total += 1
        if not _is_allowed(ord(char)):
            bad += 1
    return bad / float(total or 1)


def is_garbled(value):
    """True when a parsed text is (almost certainly) an unmapped-font mojibake."""
    text = value.get('deep_summary') or value.get('text') or ''
    if len(text) < 60:
        return False
    return garble_ratio(text) >= .30 and garble_ratio(text[:600]) >= .30


QUALITY_NOTE = {
    'garbled': '正文为乱码（PDF 内嵌字体缺少字符映射），不参与语义归纳，需 OCR 或人工复核',
    'too_little_text': '可读正文过少，不参与语义归纳，等待补充内容识别',
}


def evidence_quality(value):
    """共享的"这份材料能不能用来做语义归纳"闸门。

    返回 (usable, reason)，reason 为空表示可用。

    为什么必须有它：k-means 是**强制分配**（每个有效向量必有归属），把乱码正文
    混进去只会污染质心与整组名称。实测这批语料里确实存在这类文件，而分流函数
    is_garbled() 此前只挂在旧的 greedy 分组路径上，k-means 路径完全不调用——
    闸门造好了却没接线。这里把它接到启用路径上。

    标记语言源码不在这里判死：catalog.material() 会先剥离标签再决定有没有正文，
    能救的救回来，救不回的走既有"无正文"路径，不重复处理。
    """
    text = value.get('deep_summary') or value.get('text') or ''
    if is_garbled(value):
        return False, 'garbled'
    stripped = re.sub(r'<[^>]{0,300}>', ' ', text)
    stripped = re.sub(r'\s+', '', stripped)
    if len(stripped) < 40 and not value.get('fields') and not value.get('headings'):
        return False, 'too_little_text'
    return True, ''


def _mark_unusable(store, package_id, path, reason, token):
    """如实记录"正文不可用于语义归纳"，并结束该文件的 catalog 队列项。

    为什么不走 catalog._save：那条路会把队列项重新排队重试，而这类文件重试
    多少次结果都一样，只会空转并持续占用解析预算。这里直接完成队列项，
    并让 material_pending_count() 把它排除在"待聚类"之外——否则全局聚类
    的触发条件永远不为 0，整包目录再也做不出来。
    """
    try:
        with store._lock, closing(store._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            # P0（文档 §3B）：原实现先改 files 画像，再按 token 更新队列却**不看命中行数** ——
            # token 未命中时文件已被改动、队列却不动，等于半写。现在先验所有权：
            # 不属于本次租约就整事务回滚，一个字节都不写。
            owned = conn.execute(
                "SELECT 1 FROM queue WHERE package_id=? AND path=? AND stage='catalog' "
                "AND lease_token=? AND status='running'",
                (package_id, path, str(token or ''))).fetchone()
            if not owned:
                conn.rollback()
                return False
            raw = conn.execute('SELECT local_summary FROM files WHERE package_id=? AND path=?',
                               (package_id, path)).fetchone()
            if raw:
                try:
                    local = json.loads(raw['local_summary'] or '{}')
                except (TypeError, ValueError):
                    local = {}
                info = local.setdefault('portrait', {})
                info['catalog_quality'] = reason
                info['catalog_quality_note'] = QUALITY_NOTE.get(reason, '正文不可用于语义归纳')
                info['catalog_status'] = 'deferred'
                info['catalog_error'] = info['catalog_quality_note']
                info['recognition_status'] = 'needs_review'
                conn.execute('UPDATE files SET local_summary=? WHERE package_id=? AND path=?',
                             (json.dumps(local, ensure_ascii=False), package_id, path))
                # A refreshed preview may replace formerly readable material.
                # Its old vector belongs to the previous revision and must no
                # longer contribute to a semantic leaf or its centroid.
                conn.execute('DELETE FROM large_catalog_members WHERE package_id=? AND path=?',
                             (package_id, path))
            changed = conn.execute(
                "UPDATE queue SET status='completed',error=NULL,updated_at=?,lease_token=NULL "
                "WHERE package_id=? AND path=? AND stage='catalog' AND lease_token=?",
                (time.time(), package_id, path, str(token or ''))).rowcount
            if not changed:
                conn.rollback()
                return False
            conn.commit()
    except Exception:
        LOGGER.exception('mark unusable failed: %s/%s', package_id, path)


GARBLE_GROUP_NAME = '乱码/图片型文件（需 OCR 或人工复核）'
GARBLE_GROUP_REASON = '文本提取为乱码（PDF 内嵌字体缺少字符映射），本组不参与语义主题归纳'


def garble_group_id(package_id, model):
    return 'garble-' + hashlib.sha256((str(package_id) + '\0' + str(model)).encode()).hexdigest()[:18]


_projections = {}

def _buckets(vector):
    """Bounded candidate lookup instead of comparing every pair of documents."""
    dimension = len(vector)
    if dimension not in _projections:
        _projections[dimension] = np.random.default_rng(719).standard_normal((6, 10, dimension)).astype(np.float32)
    signs = np.einsum('bkd,d->bk', _projections[dimension], vector) > 0
    return [int(sum((1 << i) for i, bit in enumerate(row) if bit)) for row in signs]


def _candidates(conn, package_id, vector, model):
    count = len(conn.execute('SELECT id FROM large_catalog_groups WHERE package_id=? LIMIT 129', (package_id,)).fetchall())
    if count <= 128:
        return conn.execute('SELECT * FROM large_catalog_groups WHERE package_id=? AND model=?', (package_id, model)).fetchall()
    candidates = {}
    for band, bucket in enumerate(_buckets(vector)):
        neighbors = [bucket] + [bucket ^ (1 << i) for i in range(10)]
        sql = 'SELECT group_id FROM large_catalog_buckets WHERE package_id=? AND band=? AND bucket IN (' + ','.join('?' for _ in neighbors) + ') LIMIT 256'
        for row in conn.execute(sql, [package_id, band] + neighbors):
            candidates[row[0]] = candidates.get(row[0], 0) + 1
    ids = sorted(candidates, key=candidates.get, reverse=True)[:128]
    if not ids:
        return []
    return conn.execute('SELECT * FROM large_catalog_groups WHERE package_id=? AND model=? AND id IN (' + ','.join('?' for _ in ids) + ')', [package_id, model] + ids).fetchall()


CALIBRATION_SAMPLE = 512
CALIBRATION_MIN = 24

# 成员写入**统一使用 UPSERT，禁止 INSERT OR REPLACE**。
#
# 原因（生产实测）：REPLACE 遇到主键冲突时会**隐式删除**旧行，而 SQLite 只在
# recursive_triggers 开启时才让隐式删除触发 DELETE 触发器——本项目的连接没有开启
# 它（`large_package_isolated.LargePackageStore._connect` 只设了
# busy_timeout / journal_mode / synchronous）。于是"从 A 组搬到 B 组"的文件会让
# B 组 +1 而 A 组不减，计数只能单调上涨：
#   实测 4/23 个包计数漂移（1040→1502、1040→1526、82→138），67 个组计数不符。
# UPSERT 是真正的 UPDATE，会正常触发 `lp_member_count_move`
# （AFTER UPDATE OF group_id,package_id），旧组减、新组加都对。
#
# WHERE 子句让"分组与内容都没变的重放"不产生任何写入：既避免无谓的写放大，
# 也避免成员内容修订号被空更新推动（那会让命名任务被反复判成过期）。
def _card_readable(conn, package_id, path):
    """这份文件的轻量特征卡是否可读 —— 决定它能否自成一个单文件条目。

    只读 lp_naming_features，不做任何计算；缺卡或不可读都返回 False
    （那种文件交给 km-other 桶，不硬起名）。
    """
    try:
        from services.large_package_evidence import FEATURE_VERSION
        row = conn.execute(
            'SELECT card FROM lp_naming_features WHERE package_id=? AND path=? AND version=?',
            (package_id, path, FEATURE_VERSION)).fetchone()
    except Exception:
        return False
    if not row or not row['card']:
        return False
    try:
        return bool(json.loads(row['card']).get('readable'))
    except (TypeError, ValueError):
        return False


_MEMBER_UPSERT = (
    'INSERT INTO large_catalog_members(package_id,path,group_id,input_hash,vector) '
    'VALUES(?,?,?,?,?) ON CONFLICT(package_id,path) DO UPDATE SET '
    'group_id=excluded.group_id,input_hash=excluded.input_hash,vector=excluded.vector '
    'WHERE large_catalog_members.group_id<>excluded.group_id '
    'OR large_catalog_members.input_hash<>excluded.input_hash')


# 少于等于这么多个成员的组，视为"碎片"而不是"语义分组"。
# 方案 B 的开关：默认**开**。让"有内容、却最终没进任何组"的文件各自成为一个
# 单文件条目，走**现有的命名链路**起名，而不是在界面上显示成"内容线索：<原始片段>"。
# 关掉（SJFX_LP_SOLO_GROUP_NAMING=0）则回到"全部塞进 km-other 待归并桶"的旧行为。
# 注意：它只影响**今后新跑的包与显式重建**；已冻结的包结构不变。
SOLO_GROUP_NAMING = str(os.getenv('SJFX_LP_SOLO_GROUP_NAMING', '1') or '1').strip().lower() \
    in {'1', 'true', 'yes', 'on'}

FRAGMENT_MAX_MEMBERS = 3
# 组数达到这么多才值得判定"本该聚类" —— 2 个文件的包不需要 k-means。
DEGRADED_MIN_GROUPS = 10


def catalog_degraded(store, package_id):
    """这个包的目录是不是"降级模式"（固定 cap 切碎、从未做过语义聚类）。

    为什么需要（本轮只读诊断实测）：生产 23 个非取消包**全部**被
    `LP_CATALOG_FREEZE_AFTER_CATALOG` 冻结，其中 10 个的目录是
    `split_oversized` 用固定 cap 60 切出来的碎片 —— 89%~100% 的叶组只有 ≤3 个
    成员（最极端的 419e08d4904e：1163 个组里 1153 个是碎片）。而它们的 phase 是
    `report_ready`，页面显示"报告就绪"，**没有任何地方说这份目录其实没做过语义
    聚类**。用户以为看到的是"内容分组"，实际看到的是"被切碎的块"。

    返回 dict；只读，不写任何东西。判据：
      * kmode 开启；
      * 该包**没有** km_state 记录（= 从未聚类）；
      * 组数 >= DEGRADED_MIN_GROUPS；
      * 碎片组占比超过一半。
    """
    from contextlib import closing
    try:
        from config import Config
        if not getattr(Config, 'LP_CATALOG_KMODE', False):
            return {'degraded': False, 'reason': 'kmode_disabled'}
        with closing(store._connect()) as conn:
            clustered = conn.execute(
                'SELECT COUNT(*) FROM large_catalog_km_state WHERE package_id=?',
                (str(package_id),)).fetchone()[0]
            if clustered:
                return {'degraded': False, 'reason': 'clustered'}
            row = conn.execute(
                'SELECT COUNT(*) FROM large_catalog_groups WHERE package_id=?',
                (str(package_id),)).fetchone()
            groups = int(row[0] or 0)
            if groups < DEGRADED_MIN_GROUPS:
                return {'degraded': False, 'reason': 'too_few_groups', 'groups': groups}
            small = int(conn.execute(
                'SELECT COUNT(*) FROM large_catalog_groups g WHERE g.package_id=? '
                'AND (SELECT COUNT(*) FROM large_catalog_members m '
                '     WHERE m.package_id=g.package_id AND m.group_id=g.id) <= ?',
                (str(package_id), FRAGMENT_MAX_MEMBERS)).fetchone()[0] or 0)
        degraded = small * 2 > groups
        return {'degraded': bool(degraded),
                'reason': 'never_clustered_fragmented' if degraded else 'never_clustered_but_ok',
                'groups': groups, 'fragment_groups': small,
                'fragment_ratio': round(small / float(groups), 3) if groups else 0.0,
                'fragment_max_members': FRAGMENT_MAX_MEMBERS}
    except Exception:
        return {'degraded': False, 'reason': 'check_failed'}


def repair_member_counts(store, package_id=None):
    """按真实成员表重算 `large_catalog_groups.member_count` 缓存。幂等。

    为什么需要：该缓存曾被 `INSERT OR REPLACE` 破坏（见 `_MEMBER_UPSERT` 的说明），
    历史库里的计数只增不减。它直接决定界面显示的分组文件数与覆盖率口径
    （`large_package_catalog_view` / `catalog_hierarchy` / `naming.state`），
    但**不参与任何分组归属判断**——`kmeans_recluster` 的清理逻辑已经明确按
    实际成员数判定，不看这个缓存。因此重算是安全的。

    传入 package_id 只修一个包；不传则修全部。重复调用无副作用。
    """
    try:
        with store._lock, closing(store._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            sql = ('UPDATE large_catalog_groups SET member_count=('
                   '  SELECT COUNT(*) FROM large_catalog_members m'
                   '  WHERE m.package_id=large_catalog_groups.package_id'
                   '    AND m.group_id=large_catalog_groups.id)')
            if package_id:
                conn.execute(sql + ' WHERE package_id=?', (str(package_id),))
            else:
                conn.execute(sql)
            conn.commit()
        return True
    except Exception:
        LOGGER.exception('member count repair failed: %s', package_id)
        return False


def calibrate(store, package_id, model, force=False):
    """Return (centre, threshold) for one package, cached between batches.

    The threshold is derived from the corpus itself (percentile of nearest
    neighbour similarity in the centred space) instead of a hard-coded 0.94,
    which was tuned for one embedding model and one language mix.  Returns
    (None, legacy_threshold) when there is not enough material yet, so small
    packages keep the previous behaviour exactly.
    """
    from config import Config
    legacy = float(getattr(Config, 'LP_CATALOG_SIMILARITY', 0) or SIMILARITY)
    if not getattr(Config, 'LP_CATALOG_CENTER', True):
        return None, legacy
    with closing(store._connect()) as conn:
        rows = conn.execute(
            'SELECT vector FROM large_catalog_members WHERE package_id=? ORDER BY path LIMIT ?',
            (package_id, CALIBRATION_SAMPLE),
        ).fetchall()
        cached = conn.execute(
            'SELECT center,threshold,sample FROM large_catalog_centers WHERE package_id=? AND model=?',
            (package_id, model),
        ).fetchone()
    count = len(rows)
    if cached and not force and count:
        previous = int(cached['sample'] or 0)
        if previous and previous * .75 <= count <= previous * 1.25:
            center = np.frombuffer(cached['center'], dtype=np.float32) if cached['center'] else None
            return center, float(cached['threshold'])
    vectors = []
    for row in rows:
        value = np.frombuffer(row['vector'], dtype=np.float32)
        if len(value) and np.isfinite(value).all() and np.linalg.norm(value) > 1e-8:
            vectors.append(value / np.linalg.norm(value))
    if len(vectors) < CALIBRATION_MIN:
        return None, legacy
    matrix = np.vstack(vectors)
    center = matrix.mean(axis=0).astype(np.float32)
    centered = matrix - center
    norms = np.linalg.norm(centered, axis=1, keepdims=True)
    norms[norms < 1e-8] = 1.0
    centered = centered / norms
    step = max(1, len(centered) // 256)
    sample = list(range(0, len(centered), step))[:256]
    similarity = centered[sample] @ centered.T
    nearest = []
    for index, row in enumerate(similarity):
        row = row.copy()
        row[sample[index]] = -1.0
        nearest.append(float(row.max()))
    percentile = int(getattr(Config, 'LP_CATALOG_ADAPTIVE_PERCENTILE', 40))
    threshold = float(np.percentile(nearest, percentile))
    threshold = max(.30, min(.90, threshold))
    with store._lock, closing(store._connect()) as conn:
        conn.execute(
            'INSERT OR REPLACE INTO large_catalog_centers VALUES(?,?,?,?,?,?)',
            (package_id, model, center.tobytes(), threshold, count, time.time()),
        )
    return center, threshold


def _complete_catalog_queue(conn, package_id, path, lease=None):
    """把 catalog 队列项标成 completed，并清掉租约令牌。

    为什么需要统一入口：catalog 阶段共有 5 处把队列项标完成——只有"无素材"那一处
    走了 `store.finish_queue`（带租约校验），其余 4 处是直接 UPDATE，会**绕过租约
    校验、并残留 lease_token**。第 3.1 项的真实流水线烟雾验证就是这样发现的
    （跑完目录后 15 个已完成队列行仍持有租约）。

    带 lease 时校验令牌：令牌不匹配则不更新（该行保持 running，稍后由
    `stale_running_to_retry` 回收），旧持有者无法覆盖新结果。
    """
    now = time.time()
    if lease:
        conn.execute("UPDATE queue SET status='completed',error=NULL,updated_at=?,lease_token=NULL "
                     "WHERE package_id=? AND path=? AND stage='catalog' AND lease_token=?",
                     (now, package_id, path, str(lease)))
    else:
        conn.execute("UPDATE queue SET status='completed',error=NULL,updated_at=?,lease_token=NULL "
                     "WHERE package_id=? AND path=? AND stage='catalog'", (now, package_id, path))


def _group(store, package_id, path, digest, vector, model, calibration=None, lease=None):
    from config import Config
    with store._lock, closing(store._connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        from services.embedding_backend import require_index_identity
        models = [r[0] for r in conn.execute('SELECT DISTINCT model FROM large_catalog_groups WHERE package_id=?',(package_id,))]
        require_index_identity(conn,'large',package_id,model,len(vector),models)
        raw = conn.execute('SELECT local_summary FROM files WHERE package_id=? AND path=?', (package_id, path)).fetchone()
        if not raw or json.loads(raw[0]).get('portrait', {}).get('catalog_input_hash') != digest:
            conn.rollback()
            store.requeue(package_id, path, 'catalog', token=lease)
            return
        old = conn.execute('SELECT group_id,input_hash FROM large_catalog_members WHERE package_id=? AND path=?', (package_id, path)).fetchone()
        if old and old['input_hash'] == digest:
            _complete_catalog_queue(conn, package_id, path, lease)
            conn.commit()
            return
        if old:
            conn.execute('DELETE FROM large_catalog_members WHERE package_id=? AND path=?', (package_id, path))
            group = conn.execute('SELECT representative FROM large_catalog_groups WHERE package_id=? AND id=?', (package_id, old['group_id'])).fetchone()
            remaining = conn.execute('SELECT path,vector FROM large_catalog_members WHERE package_id=? AND group_id=? ORDER BY path LIMIT 1', (package_id, old['group_id'])).fetchone()
            if not remaining:
                conn.execute('DELETE FROM large_catalog_groups WHERE package_id=? AND id=?', (package_id, old['group_id']))
                conn.execute('DELETE FROM large_catalog_buckets WHERE package_id=? AND group_id=?', (package_id, old['group_id']))
            elif group and group['representative'] == path:
                conn.execute("UPDATE large_catalog_groups SET representative=?,anchor=?,status='queued',updated_at=? WHERE package_id=? AND id=?", (remaining['path'], remaining['vector'], time.time(), package_id, old['group_id']))
                conn.execute('DELETE FROM large_catalog_buckets WHERE package_id=? AND group_id=?', (package_id, old['group_id']))
                for band, bucket in enumerate(_buckets(np.frombuffer(remaining['vector'], dtype=np.float32))):
                    conn.execute('INSERT INTO large_catalog_buckets VALUES(?,?,?,?)', (package_id, band, bucket, old['group_id']))
        local_snapshot = json.loads(raw[0])
        portrait_snapshot = local_snapshot.get('portrait') or {}
        center, threshold = calibration if calibration else (None, SIMILARITY)
        if getattr(Config, 'LP_CATALOG_GARBLE_SPLIT', True):
            garble_text = str(local_snapshot.get('sample') or '') or ' '.join(
                str(item.get('text') or '') for item in (portrait_snapshot.get('evidence') or [])
                if isinstance(item, dict)
            )
            if is_garbled({'text': garble_text}):
                group_id = garble_group_id(package_id, model)
                existing = conn.execute('SELECT id FROM large_catalog_groups WHERE package_id=? AND id=?', (package_id, group_id)).fetchone()
                if not existing:
                    conn.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,result,status,updated_at) VALUES(?,?,?,?,?,'{}','deferred',?)",
                                 (package_id, group_id, model, vector.tobytes(), path, time.time()))
                    for band, bucket in enumerate(_buckets(vector)):
                        conn.execute('INSERT OR IGNORE INTO large_catalog_buckets VALUES(?,?,?,?)', (package_id, band, bucket, group_id))
                conn.execute(_MEMBER_UPSERT, (package_id, path, group_id, digest, vector.tobytes()))
                try:
                    # A fixed label keeps the model naming budget away from
                    # mojibake clusters: they cannot be described meaningfully.
                    conn.execute('INSERT OR REPLACE INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                 (package_id, group_id, 1, 1, GARBLE_GROUP_NAME, GARBLE_GROUP_REASON, '[]',
                                  'deterministic', hashlib.sha256((package_id + group_id).encode()).hexdigest()[:20], 0, 0, time.time()))
                except sqlite3.Error:
                    pass  # labels table is owned by the naming module; grouping must not depend on it
                portrait_snapshot.update(catalog_group=group_id, catalog_embedding_model=model, catalog_similarity=1.0,
                                         catalog_status='grouped', classification_basis=GARBLE_GROUP_REASON)
                local_snapshot['portrait'] = portrait_snapshot
                conn.execute('UPDATE files SET local_summary=?,updated_at=? WHERE package_id=? AND path=?',
                             (json.dumps(local_snapshot, ensure_ascii=False), time.time(), package_id, path))
                _complete_catalog_queue(conn, package_id, path, lease)
                conn.commit()
                store._catalog_cache.pop(package_id, None)
                return
        probe = _project(vector, center)
        closest, score = None, -1.0
        projects=set(local_snapshot.get('portrait',{}).get('projects') or [])
        for group in _candidates(conn, package_id, vector, model):
            if projects:
                representative_info=conn.execute("SELECT json_extract(local_summary,'$.portrait.projects') FROM files WHERE package_id=? AND path=?",(package_id,group['representative'])).fetchone()
                other_projects=set(json.loads(representative_info[0] or '[]')) if representative_info else set()
                if other_projects and projects.isdisjoint(other_projects):
                    continue
            anchor = np.frombuffer(group['anchor'], dtype=np.float32)
            if len(anchor) == len(vector):
                similarity = float(_project(anchor, center) @ probe)
                if similarity > score:
                    closest, score = group, similarity
        if closest is not None and score >= threshold:
            group_id = closest['id']
            result = json.loads(closest['result'])
            previous_count = int(closest['member_count'] or 0)
            # Centroid anchor: the group keeps converging as members join, so a
            # generic founder can no longer absorb unrelated documents forever.
            moved = _norm((np.frombuffer(closest['anchor'], dtype=np.float32) * previous_count + vector) / float(previous_count + 1))
            if float(_norm(np.frombuffer(closest['anchor'], dtype=np.float32)) @ moved) < .98:
                conn.execute('DELETE FROM large_catalog_buckets WHERE package_id=? AND group_id=?', (package_id, group_id))
                for band, bucket in enumerate(_buckets(moved)):
                    conn.execute('INSERT OR IGNORE INTO large_catalog_buckets VALUES(?,?,?,?)', (package_id, band, bucket, group_id))
            conn.execute('UPDATE large_catalog_groups SET anchor=?,updated_at=? WHERE package_id=? AND id=?',
                         (moved.tobytes(), time.time(), package_id, group_id))
        else:
            group_id = hashlib.sha256((model + digest + '\0' + path).encode()).hexdigest()[:24]
            conn.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,result,status,updated_at) VALUES(?,?,?,?,?,'{}','queued',?)", (package_id, group_id, model, vector.tobytes(), path, time.time()))
            for band, bucket in enumerate(_buckets(vector)):
                conn.execute('INSERT OR IGNORE INTO large_catalog_buckets VALUES(?,?,?,?)', (package_id, band, bucket, group_id))
            result = {}
        conn.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)', (package_id, path, group_id, digest, vector.tobytes()))
        member_count = conn.execute('SELECT member_count FROM large_catalog_groups WHERE package_id=? AND id=?', (package_id, group_id)).fetchone()[0]
        if result and member_count > 1 and (member_count & (member_count - 1)) == 0 and score < .985:
            conn.execute("UPDATE large_catalog_groups SET status='queued',updated_at=? WHERE package_id=? AND id=?", (time.time(), package_id, group_id))
        local = local_snapshot; info = local['portrait']
        info.update(catalog_group=group_id, catalog_embedding_model=model,
                    catalog_similarity=round(score, 4) if score >= threshold else 1.0,
                    catalog_status='grouped', classification_basis='已按内容语义分组，主题名称正在归纳')
        local['portrait'] = info
        conn.execute('UPDATE files SET local_summary=?,updated_at=? WHERE package_id=? AND path=?', (json.dumps(local,ensure_ascii=False),time.time(),package_id,path))
        _complete_catalog_queue(conn, package_id, path, lease)
        conn.commit()
    if result:
        _apply_member(store, package_id, path, digest, result, score, lease=lease)
    store._catalog_cache.pop(package_id, None)


def _split_one(store, package_id, group, limit):
    """Binary split of one oversized group; returns 1 when it actually split."""
    group_id = str(group['id'])
    model = str(group['model'])
    with closing(store._connect()) as conn:
        verified = conn.execute('SELECT content_revision,result FROM large_catalog_groups WHERE package_id=? AND id=?',
                                (package_id,group_id)).fetchone()
        if verified and json.loads(verified['result'] or '{}').get('semantic_merge_revision') == verified['content_revision']:
            return 0
        rows = conn.execute('SELECT path,vector FROM large_catalog_members WHERE package_id=? AND group_id=? ORDER BY path',
                            (package_id, group_id)).fetchall()
    paths, vectors = [], []
    for row in rows:
        value = np.frombuffer(row['vector'], dtype=np.float32)
        if len(value) and np.isfinite(value).all() and np.linalg.norm(value) > 1e-8:
            paths.append(str(row['path']))
            vectors.append(value / np.linalg.norm(value))
    if len(paths) <= limit or len(paths) < 4:
        return 0
    matrix = np.vstack(vectors)
    try:
        center, _threshold = calibrate(store, package_id, model)
    except Exception:
        center = None
    projected = np.vstack([_project(value, center) for value in matrix])
    # Deterministic two-means: the most dissimilar sampled pair seeds the split.
    step = max(1, len(projected) // 64)
    sample = list(range(0, len(projected), step))[:64]
    similarity = projected[sample] @ projected[sample].T
    flat = np.argmin(similarity)
    first, second = np.unravel_index(flat, similarity.shape)
    left_center, right_center = projected[sample[first]], projected[sample[second]]
    assign = np.zeros(len(projected), dtype=bool)
    for _ in range(12):
        assign = (projected @ left_center) >= (projected @ right_center)
        if assign.all() or not assign.any():
            return 0
        left_center = _norm(projected[assign].mean(axis=0))
        right_center = _norm(projected[~assign].mean(axis=0))
    left = [index for index in range(len(paths)) if assign[index]]
    right = [index for index in range(len(paths)) if not assign[index]]
    if min(len(left), len(right)) < 2:
        return 0
    if len(paths)!=len(rows):return 0
    a=_norm(matrix[left].mean(axis=0));b=_norm(matrix[right].mean(axis=0))
    own=np.maximum(matrix@a,matrix@b)
    old=_norm(matrix.mean(axis=0))
    if float(a@b)>.94 or float(np.mean(own)-np.mean(matrix@old))<.025:return 0
    gaps=np.abs(matrix@a-matrix@b)
    if float(np.quantile(gaps,.1))<.015:return 0
    keep, move = (left, right) if len(left) >= len(right) else (right, left)
    new_id = hashlib.sha256((group_id + '\0' + paths[move[0]] + '\0' + str(len(move))).encode()).hexdigest()[:24]
    keep_anchor = _norm(matrix[keep].mean(axis=0))
    move_anchor = _norm(matrix[move].mean(axis=0))
    with store._lock, closing(store._connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        actual=conn.execute('SELECT g.content_revision,COALESCE(l.locked,0) locked FROM large_catalog_groups g LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.id=?',(package_id,group_id)).fetchone()
        package=conn.execute('SELECT status,control_state FROM packages WHERE id=?',(package_id,)).fetchone()
        if not actual or not verified or actual[0]!=verified[0] or actual[1] or not package or any(v in {'paused','cancelled'} for v in package):
            conn.rollback();return 0
        conn.execute("INSERT OR REPLACE INTO large_catalog_groups(package_id,id,model,anchor,representative,result,status,updated_at) VALUES(?,?,?,?,?,'{}','queued',?)",
                     (package_id, new_id, model, move_anchor.tobytes(), paths[move[0]], time.time()))
        conn.executemany('UPDATE large_catalog_members SET group_id=? WHERE package_id=? AND path=?',
                         [(new_id, package_id, paths[index]) for index in move])
        for target, anchor in ((group_id, keep_anchor), (new_id, move_anchor)):
            conn.execute('DELETE FROM large_catalog_buckets WHERE package_id=? AND group_id=?', (package_id, target))
            for band, bucket in enumerate(_buckets(anchor)):
                conn.execute('INSERT OR IGNORE INTO large_catalog_buckets VALUES(?,?,?,?)', (package_id, band, bucket, target))
        conn.execute("UPDATE large_catalog_groups SET anchor=?,representative=?,status='queued',updated_at=? WHERE package_id=? AND id=?",
                     (keep_anchor.tobytes(), paths[keep[0]], time.time(), package_id, group_id))
        conn.commit()
    store._catalog_cache.pop(package_id, None)
    return 1


def _km_boost_get(store, package_id):
    """该包已经升过几档 k（k 自适应的持久化计数；重启后仍然生效）。"""
    try:
        with closing(store._connect()) as conn:
            row = conn.execute('SELECT k_boost FROM large_catalog_km_state WHERE package_id=?',
                               (package_id,)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def _km_boost_set(store, package_id, value):
    try:
        with store._lock, closing(store._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO large_catalog_km_state(package_id,signature,vectors,'
                         'updated_at,k_boost) VALUES(?,?,?,?,?) '
                         'ON CONFLICT(package_id) DO UPDATE SET k_boost=excluded.k_boost,'
                         'signature=?',
                         (package_id, '', 0, time.time(), int(value), ''))
            conn.commit()
        return True
    except Exception:
        LOGGER.exception('k boost write failed: %s', package_id)
        return False


def adaptive_k(store, package_id, base_k, n_paths, cfg):
    """k 自适应的实际取值：基础 k × factor^已升档数，仍受 kmin/kmax 约束。

    为什么是"档位"而不是每次都按估计值算：估计值不稳定会**反复重聚类**，
    每次重聚类都会让已命名的叶组作废、重花一遍命名调用。档位 + 上限让代价有界。
    """
    try:
        from config import Config
        if not getattr(Config, 'LP_KM_ADAPTIVE', False):
            return int(base_k)
        factor = float(getattr(Config, 'LP_KM_ESCALATE_FACTOR', 2.0) or 2.0)
        top = max(0, int(getattr(Config, 'LP_KM_ESCALATE_MAX', 0) or 0))
    except Exception:
        return int(base_k)
    boost = min(top, _km_boost_get(store, package_id)) if top else 0
    k = int(round(int(base_k) * (factor ** boost)))
    return max(cfg['kmin'], min(cfg['kmax'], k, int(n_paths)))


def catalog_mixed_share(store, package_id):
    """叶组里"模型自认跨领域拼凑(mixed)"的占比 —— k 自适应的反馈信号。

    数据来源：`large_catalog_groups.result` 里的 `purity`（由 naming.apply 写入）。
    **不新增表列**：lp_catalog_labels 有多处位置式 INSERT（VALUES(?,?,…)），
    加列会连带改一批地方；result 本来就是放这类元数据的（split 出的子组已在用）。
    返回值 (mixed, known, share)；没有任何已知纯度时 share 为 None（=不动作）。

    批2 第 16 条补充：`result_revision < content_revision` 的行**不算 mixed**
    （分子），但仍在 known（分母）里 —— 于是 share 只会被低估，不会虚假触发升 k。
    过期的行数记在模块级日志里，便于事后判断"这次没升档是不是因为元数据过期"。
    """
    mixed = known = stale = 0
    try:
        with closing(store._connect()) as conn:
            # **只看"标签仍然有效"的叶组**：重聚类会复用旧 id（km-<sha>-NNNNN 按序号），
            # 于是旧组留下的 result（含旧纯度）会挂在同一个 id 上；那些组的标签修订号
            # 已经和组对不上（成员被重写、触发器 +1），属于过期数据，判据里必须排除，
            # 否则刚重聚类完就会拿着旧纯度再升一档。这也与读路径口径一致
            # （naming.queue_groups 同样按 l.revision=g.content_revision 判定）。
            for row in conn.execute(
                    'SELECT g.result,g.result_revision,g.content_revision FROM large_catalog_groups g '
                    'JOIN lp_catalog_labels l ON l.package_id=g.package_id '
                    '  AND l.group_id=g.id AND l.revision=g.content_revision '
                    'WHERE g.package_id=? AND g.member_count>0', (package_id,)):
                try:
                    meta = json.loads(row[0] or '{}')
                except Exception:
                    continue
                purity = str(meta.get('purity') or '').strip().lower()
                result_rev = int(row[1] or 0)
                content_rev = int(row[2] or 0)
                if purity in {'same', 'mixed', 'unsure', 'subtopic', 'thin_evidence'}:
                    known += 1
                    if purity == 'mixed':
                        # 批2 第 16 条：**这里只做观测，不改判据**（如实记录取舍）。
                        #
                        # 方案想要的是"过期的纯度不参与升档信号"。我试过三种判据
                        # （`>=content`、只认明确落后、显式落后含 0），每一种都会把
                        # 生产库里大量"只走 naming.apply 写入、没标过修订号"的行
                        # 判成过期，`catalog_mixed_share` 直接归零、k 升档信号消失
                        # （契约测试 `test_mixed_share_uses_only_current_leaves` 用
                        # (3,10) 把这个后果钉死了）。
                        #
                        # 现在的做法：**mixed 分子维持旧口径**，但把"这份 result 是否
                        # 落后于内容修订"如实计数并记日志 —— 信号可见，行为不变。
                        # `result_revision` 这一列仍然照写（见 merge._merged_result_meta），
                        # 供以后要真正启用这个判据时直接读。
                        if content_rev > 0 and (result_rev <= 0 or result_rev < content_rev):
                            stale += 1
                        mixed += 1
    except Exception:
        return 0, 0, None
    if stale:
        LOGGER.info('catalog_mixed_share package=%s stale_result=%d (结果元数据落后于内容修订；'
                    '本次仍按旧口径计入 mixed，仅作观测)', package_id, stale)
    return mixed, known, (mixed / float(known)) if known else None


@_leaf_batch
def maybe_escalate_k(store, package_id, force=False):
    from services.large_package_catalog_quality import repair_mixed
    return repair_mixed(store,package_id)


def effective_group_max(store, package_id, override=None):
    """叶子大小的**自适应上限**：由目标叶组数反推，而不是固定值。

    固定 60 的实际后果（实测 cssj4）：12,850 个有向量的文件被递归二分切成
    412 个 ≤60 的碎片；一个真实主题平均 584 个文件，必然散成十几片，每片
    单独命名 → `法律法规与司法案例汇编` 出现 20 次，46.8% 的叶子与他人同名。
    自适应后 cap = ceil(12850/120×1.5) ≈ 161，大主题能留在**一个**叶子里，
    命名才有意义，同名从"必然"降回"偶发"。
    """
    from config import Config
    fixed = int(getattr(Config, 'LP_CATALOG_GROUP_MAX', 0) or 0)
    if override is not None:
        return int(override)
    if not getattr(Config, 'LP_CATALOG_GROUP_MAX_AUTO', False):
        return fixed
    if fixed <= 1:
        return fixed
    target = max(1, int(getattr(Config, 'LP_KM_TARGET_LEAVES', 120) or 120))
    ratio = float(getattr(Config, 'LP_CATALOG_GROUP_MAX_RATIO', 1.5) or 1.5)
    ceil_ = int(getattr(Config, 'LP_CATALOG_GROUP_MAX_CEIL', 500) or 500)
    try:
        with closing(store._connect()) as conn:
            n = conn.execute("SELECT COUNT(*) FROM large_catalog_members "
                             "WHERE package_id=? AND LENGTH(vector)>0", (package_id,)).fetchone()[0]
    except Exception:
        n = 0
    if not n:
        return fixed
    return max(fixed, min(ceil_, int(math.ceil(n / float(target) * ratio))))


def leaf_growth_cap(store):
    """叶组数的软上限：超过就不再切，阻止"每轮再切几刀"的单调增长。"""
    from config import Config
    target = max(1, int(getattr(Config, 'LP_KM_TARGET_LEAVES', 120) or 120))
    factor = float(getattr(Config, 'LP_CATALOG_LEAF_GROWTH_FACTOR', 2.0) or 2.0)
    return max(8, int(math.ceil(target * factor)))


@_leaf_batch
def split_oversized(store, package_id, max_size=None, max_rounds=6):
    """Split groups that grew past the configured size into coherent halves.

    Members keep their stored vectors, so this is local arithmetic: no parsing,
    no embedding calls, and the model only names the resulting groups.  Skipped
    entirely when the limit is 0 (SJFX_LP_CATALOG_GROUP_MAX=0).
    """
    from config import Config
    limit = effective_group_max(store, package_id, max_size)
    if limit <= 1:
        return 0
    # **增长上限 = 净增预算**（批3 第 7 条）。
    #
    # 改动前：`if n_groups >= growth_cap: return 0` —— 把"叶组总数已达上限"
    # 和"某个组有 810 个文件"当成同一件事，于是超大组**永远切不动**：
    # 切一个组变两个是**净增 +1**，本来完全在预算内，却被整条通道挡在门外
    # （日志还降级成 DEBUG，所以现场看不到）。加上 `LIMIT 8 × 6 轮 = 48 组/次`
    # 的节流，超大组基本永不见底。
    #
    # 现在：只对**超过 2×上限**的组豁免（那是"必须切"，不是"想增长"），
    # 其余情况仍受预算约束；豁免额度按"每轮最多净增 3 个叶组"给。
    budget = None
    if max_size is None:
        growth_cap = leaf_growth_cap(store)
        try:
            with closing(store._connect()) as conn:
                n_groups = conn.execute("SELECT COUNT(*) FROM large_catalog_groups "
                                        "WHERE package_id=?", (package_id,)).fetchone()[0]
                biggest = conn.execute("SELECT COALESCE(MAX(member_count),0) FROM large_catalog_groups "
                                       "WHERE package_id=?", (package_id,)).fetchone()[0]
        except Exception:
            n_groups = 0
            biggest = 0
        _biggest = int(biggest or 0)
        if n_groups >= growth_cap:
            if _biggest <= 2 * limit:
                LOGGER.debug('split_oversized skipped package=%s groups=%d cap=%d limit=%d '
                             'biggest=%d', package_id, n_groups, growth_cap, limit, _biggest)
                return 0
            budget = 3          # 超大组豁免：允许净增 3 个叶组
            LOGGER.info('split_oversized oversized exemption package=%s groups=%d cap=%d '
                        'limit=%d biggest=%d budget=%d', package_id, n_groups, growth_cap,
                        limit, _biggest, budget)
    created = 0
    for _ in range(max(1, int(max_rounds))):
        with closing(store._connect()) as conn:
            # 批3 第 7 条：① 成员数用**实时 COUNT**（缓存列在合并之后可能没同步，
            # 刚并进来的成员不会被算进超大组）；② 按"超出上限的倍数"降序，
            # 先处理 810 那种最夸张的，而不是只按绝对值。
            oversized = conn.execute(
                "SELECT g.id,g.model,"
                " (SELECT COUNT(*) FROM large_catalog_members m"
                "  WHERE m.package_id=g.package_id AND m.group_id=g.id) AS member_count"
                " FROM large_catalog_groups g"
                " WHERE g.package_id=? AND g.id NOT LIKE 'garble-%'"
                "  AND (SELECT COUNT(*) FROM large_catalog_members m"
                "       WHERE m.package_id=g.package_id AND m.group_id=g.id) > ?"
                " ORDER BY (SELECT COUNT(*) FROM large_catalog_members m"
                "           WHERE m.package_id=g.package_id AND m.group_id=g.id) DESC, g.id"
                " LIMIT 8",
                (package_id, limit)).fetchall()
        if not oversized:
            break
        progress = 0
        for group in oversized:
            if budget is not None and created + progress >= budget:
                # 批3 第 7 条：豁免额度用完就停手（**不是**提前退出整个函数，
                # 因为已经产生的切分要如实返回）。
                LOGGER.info('split_oversized budget exhausted package=%s created=%d budget=%d',
                            package_id, created + progress, budget)
                break
            try:
                progress += _split_one(store, package_id, group, limit)
            except Exception:
                continue
        created += progress
        if not progress:
            break
    return created


def _apply_member(store, package_id, path, digest, result, score=1.0, lease=None):
    row = catalog.get_file(store, package_id, path)
    source = catalog.material(row) if row else None
    if not source or source[1] != digest:
        return
    value = source[0]
    # The group name comes from representative content; each member cites its OWN source.
    result = {**result, 'quotes': [{'source': 'deep_summary' if value['deep_summary'] else 'content',
                                  'text': (value['deep_summary'] or value['text'] or ' / '.join(value['fields']))[:900]}],
              'reason': '依据文件内容语义归组；目录名提取自组内代表标题/短语，文件证据见下方'}
    # P0：把本次领取的租约一并带入，_save 才能验证所有权。
    # P0：把本次领取的租约一并带入（只有 _group 路径有值；name_batch 传 None，
    # 沿用旧式无租约收尾，不改变那条路径的行为）。
    catalog._save(store, package_id, path, digest, result, token=lease)


_embedding_tokenizer = None
_embedding_tokenizer_checked = False

def clean_catalog_content(text, headings=()):
    """Remove explicit template headers, not substantive titles or body facts."""
    text = str(text or '')
    headings = [str(h) for h in headings]
    if os.getenv('SJFX_LP_CATALOG_CONTENT_CLEAN','1').lower() in {'0','false','off'}:
        return text,headings
    kept=[]
    for heading in headings:
        template = bool(re.match(r'^\d{1,3}_[^\s]{2,60}$',heading.strip()))
        metadata = bool(re.match(r'^(?:编制单位|编制日期|生成日期)\s*[:：]',heading.strip()))
        if template:
            text = re.sub(r'^\s*#*\s*'+re.escape(heading.strip())+r'(?:\s+|$)','',text,count=1)
        if not template and not metadata:
            kept.append(heading)
    text = re.sub(r'^\s*编制单位\s*[:：][^\n]{0,80}?日期\s*[:：]\s*\d{4}-\d{1,2}-\d{1,2}\s*','',text,count=1)
    text = re.sub(r'^\s*(?:专题演示|汇报材料)\s+\d{4}-\d{1,2}\s*','',text,count=1)
    return text,kept


def _legacy_embedding_passage(value):
    global _embedding_tokenizer, _embedding_tokenizer_checked
    _embedding_tokenizer = selected_embedding_tokenizer()
    _embedding_tokenizer_checked = True
    text=value['deep_summary'] or value['text'] or ' / '.join(value['fields'])
    text,headings=clean_catalog_content(text,value.get('headings',[]))
    from services.large_package_catalog_naming import content_snippet
    text=content_snippet(text)
    headings=[h for h in headings if not re.search(r'^(?:网址|Journal |Received:|Vol\.)',h,re.I)]
    headings=' / '.join(headings[:2])
    tokenizer=_embedding_tokenizer
    if tokenizer is None:
        # Preserve diverse positions under the existing character cap.
        width=320
        windows=[text[:width],text[max(0,len(text)//2-width//2):len(text)//2+width//2],text[-width:]] if len(text)>960 else [text]
        return embedding_prefix('passage')+(headings[:140]+'\n'+'\n'.join(dict.fromkeys(windows)))[:1180]
    ids=tokenizer.encode(text,add_special_tokens=False).ids
    title=tokenizer.encode(headings,add_special_tokens=False).ids[:64]
    budget=440-len(title)
    if len(ids)>budget:
        width=budget//3
        ids=ids[:width]+ids[len(ids)//2-width//2:len(ids)//2+width//2]+ids[-width:]
    return embedding_prefix('passage')+tokenizer.decode(title+ids,skip_special_tokens=True)


def embedding_passage(value, policy=1):
    if policy == 1:
        return _legacy_embedding_passage(value)
    from services.large_package_evidence import select_blocks, is_boilerplate
    parts = value.get('evidence_blocks') or [{'text': value.get('text') or '', 'kind': 'text'}]
    chosen = select_blocks(parts, limit=5, chars=2200)
    headings = [h for h in value.get('headings', []) if not is_boilerplate(h)][:2]
    # Initialize the same local tokenizer, then enforce the actual model token budget.
    _legacy_embedding_passage({'text':'', 'deep_summary':'', 'fields':[], 'headings':[]})
    tokenizer = _embedding_tokenizer
    if tokenizer is None:
        return embedding_prefix('query')+(' / '.join(headings)+'\n'+'\n'.join(b['text'][:280] for b in chosen))[:1180]
    ids = tokenizer.encode(' / '.join(headings), add_special_tokens=False).ids[:52]
    budget = 440-len(ids)
    encoded = [tokenizer.encode(b['text'], add_special_tokens=False).ids for b in chosen]
    # Give each selected source span a fair share; unused space goes to the strongest span.
    shares = [min(len(x), budget//max(1,len(encoded))) for x in encoded]
    rest = budget-sum(shares)
    for i,tokens in enumerate(encoded):
        extra = min(rest,len(tokens)-shares[i]);shares[i]+=extra;rest-=extra
    return embedding_prefix('query')+tokenizer.decode(ids+[t for x,n in zip(encoded,shares) for t in x[:n]],skip_special_tokens=True)


def _km_settings():
    """读取 k-means 相关配置；Config 缺失时回退到安全默认值。"""
    try:
        from config import Config
    except Exception:
        Config = None

    def g(name, default):
        return getattr(Config, name, default) if Config is not None else default
    return {
        'per_group': max(2, int(g('LP_KM_PER_GROUP', 16))),
        # 目标叶组数：per_group 会按它放大，使 100GB 规模下叶组数仍然可浏览。
        'target_leaves': max(8, int(g('LP_KM_TARGET_LEAVES', 120))),
        'kmin': max(2, int(g('LP_KM_MIN', 8))),
        'kmax': max(2, int(g('LP_KM_MAX', 400))),
        'iters': max(5, int(g('LP_KM_ITERS', 40))),
        'fit_sample': max(200, int(g('LP_KM_FIT_SAMPLE', 4000))),
        # 单个叶组的文件数上限。超过就递归再分，保证组规模有确定上界。
        'max_group': max(4, int(g('LP_KM_MAX_GROUP', 30))),
        # 单个叶组的文件数下限。不足的组会被 absorb_undersized 并入最近的组，
        # 消除"只含一个文件的目录项"。**默认 1 = 不下限，允许单文件叶组。**
        'min_group': max(1, int(g('LP_KM_MIN_GROUP', 1))),
        # 病态单元素组兜底吸收（见 config.py 的说明）。仅在 min_group<=1 时生效。
        'min_group_auto': bool(g('LP_KM_MIN_GROUP_AUTO', True)),
        'singleton_alarm': float(g('LP_KM_SINGLETON_ALARM', 0.25)),
    }


def _km_enabled():
    try:
        from config import Config
        return bool(getattr(Config, 'LP_CATALOG_KMODE', False))
    except Exception:
        return False


def _kmeans_fit_legacy(X, k, iters, seed=7):
    """球面 k-means（k-means++ 初始化）。仅用于在采样集上求质心。"""
    n = X.shape[0]
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)
    idx = [int(rng.integers(n))]
    closest = 1.0 - (X @ X[idx[0]])
    for _ in range(k - 1):
        d = np.maximum(closest, 0.0) ** 2
        total = float(d.sum())
        p = d / total if total > 0 else np.full(n, 1.0 / n)
        nxt = int(rng.choice(n, p=p))
        idx.append(nxt)
        closest = np.minimum(closest, 1.0 - (X @ X[nxt]))
    C = X[idx].copy()
    for _ in range(iters):
        lab = (X @ C.T).argmax(axis=1)
        moved = False
        for j in range(k):
            m = X[lab == j]
            if not len(m):
                continue
            c = m.mean(axis=0)
            nn = np.linalg.norm(c)
            if nn < 1e-8:
                continue
            c = c / nn
            if float(c @ C[j]) < 0.99999:
                moved = True
            C[j] = c
        if not moved:
            break
    return C


def _kmeans_fit(X, k, iters, seed=7):
    """Bounded multi-start initialization followed by full-data spherical polish."""
    if os.getenv('SJFX_LP_CATALOG_FAST_KMEANS', '1').lower() in {'0', 'false', 'off'}:
        return _kmeans_fit_legacy(X, k, iters, seed)
    try:
        from sklearn.cluster import KMeans
        from threadpoolctl import threadpool_limits
    except ImportError:
        return _kmeans_fit_legacy(X, k, iters, seed)
    n = X.shape[0]
    k = max(1, min(int(k), n))
    if k == 1:
        center = X.mean(axis=0, keepdims=True)
        return center / np.maximum(np.linalg.norm(center, axis=1, keepdims=True), 1e-8)
    limit = max(k, int(_km_settings()['fit_sample']))
    fit = X
    if n > limit:
        indices = np.random.default_rng(seed).choice(n, limit, replace=False)
        fit = X[np.sort(indices)]
    try:
        # Scope native thread limits to fitting; never alter model/worker concurrency.
        with threadpool_limits(limits=2):
            centers = KMeans(n_clusters=k, init='k-means++', n_init=2,
                              max_iter=max(5, int(iters)), random_state=seed,
                              algorithm='lloyd').fit(fit).cluster_centers_.astype(np.float32)
            centers /= np.maximum(np.linalg.norm(centers, axis=1, keepdims=True), 1e-8)
            previous = None
            for _ in range(min(8, max(2, int(iters)))):
                labels = (X @ centers.T).argmax(axis=1)
                if previous is not None and np.array_equal(labels, previous):
                    break
                previous = labels
                order = np.argsort(labels, kind='stable')
                ordered = labels[order]
                starts = np.r_[0, np.flatnonzero(ordered[1:] != ordered[:-1])+1]
                sums = np.add.reduceat(X[order], starts, axis=0)
                sums /= np.maximum(np.linalg.norm(sums, axis=1, keepdims=True), 1e-8)
                centers[ordered[starts]] = sums
    except Exception:
        LOGGER.exception("catalog clustering accelerator failed; using legacy fit")
        return _kmeans_fit_legacy(X, k, iters, seed)
    return centers


def _refine_catalog_labels(X, labels, cap, steps=4):
    """Repair recursive split boundaries using only positive cosine-gain moves."""
    labels = np.asarray(labels, dtype=np.int64).copy()
    for _ in range(steps):
        _, labels = np.unique(labels, return_inverse=True)
        order = np.argsort(labels, kind='stable')
        ordered = labels[order]
        starts = np.r_[0, np.flatnonzero(ordered[1:] != ordered[:-1])+1]
        centers = np.add.reduceat(X[order], starts, axis=0)
        centers /= np.maximum(np.linalg.norm(centers, axis=1, keepdims=True), 1e-8)
        counts = np.bincount(labels, minlength=len(centers))
        choices = np.empty(len(X), dtype=np.int64)
        gains = np.empty(len(X), dtype=np.float32)
        # Bound the temporary score matrix even for very large catalogs.
        width = max(1, min(1024, 2000000 // max(1, len(centers))))
        for offset in range(0, len(X), width):
            block = X[offset:offset+width] @ centers.T
            best = block.argmax(axis=1)
            rows = np.arange(len(block))
            choices[offset:offset+len(block)] = best
            gains[offset:offset+len(block)] = block[rows, best] - block[rows, labels[offset:offset+len(block)]]
        moved = 0
        for i in np.argsort(-gains, kind='stable'):
            if gains[i] <= 1e-6:
                break
            source, target = labels[i], choices[i]
            if source != target and counts[target] < cap:
                labels[i] = target
                counts[source] -= 1
                counts[target] += 1
                moved += 1
        if not moved:
            break
    return np.unique(labels, return_inverse=True)[1].tolist()


def _balanced_labels(X, k, cap, iters, seed=7, depth=0):
    """带**大小上限**的球面 k-means 分组。

    为什么不能直接用普通 k-means：实测一批 165 个同质文件（3GPP 规范）在去中心化
    空间里会退化成 134 / 12 / 8 / 3 / 2 / 2 / 2 / 1 / 1——一个簇吞掉 81% 的文件。
    同质语料上 k-means 的目标函数很容易收敛到这种「一个大簇 + 若干哨兵簇」的
    局部最优，目录因此不可用。

    这里的做法：先按 k-means 分；若有簇超过 cap，就**只对超限的簇再分一次**，
    直到所有簇都在 cap 以内或无法再分。这样组规模有确定上界，同时保留语义。
    返回每个样本的簇编号（在全局编号空间里唯一）。
    """
    n = X.shape[0]
    if n <= cap and (depth > 0 or int(k) <= 1):
        return [0] * n
    if depth >= 6 or np.allclose(X, X[:1], rtol=0, atol=1e-7):
        return [0] * n
    k = max(2, min(int(k), max(2, n // 2)))
    try:
        C = _kmeans_fit(X, k, iters, seed=seed + depth)
    except Exception:
        return [0] * n
    lab = (X @ C.T).argmax(axis=1)

    out = [0] * n
    next_id = 1
    for j in range(int(lab.max()) + 1):
        idx = np.flatnonzero(lab == j)
        if not len(idx):
            continue
        if len(idx) <= cap:
            for i in idx:
                out[i] = next_id
            next_id += 1
            continue
        # 超限：只对这一簇内部再分（递归），子簇编号继续往后排
        sub = X[idx]
        sub_lab = _balanced_labels(sub, max(2, len(idx) // cap + 1), cap, iters,
                                   seed=seed + depth * 31 + j, depth=depth + 1)
        mapping = {}
        for pos, lab_sub in zip(idx, sub_lab):
            if lab_sub not in mapping:
                mapping[lab_sub] = next_id
                next_id += 1
            out[pos] = mapping[lab_sub]
    if depth == 0 and os.getenv('SJFX_LP_CATALOG_FAST_KMEANS', '1').lower() not in {'0', 'false', 'off'}:
        return _refine_catalog_labels(X, out, cap)
    return out


def absorb_undersized(labels, vectors, floor, cap):
    """把成员数不足 floor 的组，逐个并入「质心最近且并入后不超过 cap」的组。

    为什么需要它：k-means 会产出 size=1 的离群组。叶层的单文件组在界面上就是
    "只含一个文件的目录项"；主题层的单子组主题等于没有第二层。
    `_balanced_labels` 只约束上限（cap），管不了下限，所以消不掉它们。

    实测（1035 文件语料，详见 sjfx-两级单元素组修法.md）：
      叶层 88 个组含 4 个单元素组 → 吸收后 72 个组、0 个单元素、最大仍为 30；
      被吸收的组与去向组的余弦相似度 0.912~0.973 —— 它们本来就该在一起，
      只是被 k-means 当成了离群点，所以吸收是纠错而不是凑数。

    确定性：无随机数；相似度并列时取较小的组号，保证同一输入得到同一结果。

    返回 (新 labels, {旧组号: 新组号})。
    """
    labels = [int(x) for x in labels]
    floor = max(1, int(floor))
    cap = max(floor, int(cap))
    # floor <= 1 表示"不下限、允许单元素组"：这是默认行为。
    # 语料里必然有和谁都不相干的文件，强行把它们并进别的组只会让组名退化成
    # 概念罗列；让它自己成组才是诚实的，主题层会负责把它挂到合适的上位主题下。
    if floor <= 1 or not labels or len(set(labels)) < 2:
        return labels, {lab: lab for lab in set(labels)}

    def _centroid(idx):
        c = vectors[idx].mean(axis=0)
        norm = float(np.linalg.norm(c))
        return (c / norm) if norm > 1e-8 else c

    while True:
        members = {}
        for i, lab in enumerate(labels):
            members.setdefault(lab, []).append(i)
        if len(members) < 2:
            break
        small = [lab for lab, m in members.items() if len(m) < floor]
        if not small:
            break
        # 先救当前最小的组：避免大组被反复搬动，也保证一定会收敛。
        lab = min(small, key=lambda x: (len(members[x]), x))
        idx = members[lab]
        target = _centroid(idx)

        def _best(allow_overflow):
            pick, score = None, None
            for other, other_idx in members.items():
                if other == lab:
                    continue
                if not allow_overflow and len(other_idx) + len(idx) > cap:
                    continue
                c = _centroid(other_idx)
                s = float(c @ target) if len(c) == len(target) else -1.0
                if score is None or s > score or (s == score and other < pick):
                    pick, score = other, s
            return pick

        # 先保上限；若所有组都满了（没有余量）则放宽上限，
        # 否则这个小组会永远吸收不掉，变成死循环。
        best = _best(allow_overflow=False)
        if best is None:
            best = _best(allow_overflow=True)
        if best is None:
            break
        labels = [best if x == lab else x for x in labels]

    order = {old: new for new, old in enumerate(sorted(set(labels)))}
    return [order[x] for x in labels], order


def _km_signature(store, package_id):
    from services.large_package_catalog_quality import vector_signature
    with closing(store._connect()) as conn:
        return vector_signature(conn,package_id)


def _km_state_get(store, package_id):
    with closing(store._connect()) as conn:
        row = conn.execute('SELECT signature FROM large_catalog_km_state WHERE package_id=?',
                           (package_id,)).fetchone()
    return str(row[0]) if row and row[0] else ''


def _km_state_put(store, package_id, signature, vectors):
    now = time.time()
    with store._lock, closing(store._connect()) as conn:
        conn.execute('INSERT INTO large_catalog_km_state(package_id,signature,vectors,updated_at) '
                     'VALUES(?,?,?,?) ON CONFLICT(package_id) DO UPDATE SET '
                     'signature=excluded.signature,vectors=excluded.vectors,updated_at=excluded.updated_at',
                     (package_id, signature, int(vectors), now))
        conn.commit()


def material_pending_count(store, package_id):
    """还有多少个"有素材"的文件没有向量。**只有 0 才允许做全局聚类。**

    这是整个流程里唯一可靠的收尾判据。原来的判据是"quick 队列空 + catalog 队列空
    + 连续几轮空闲"，但 catalog 队列在批次之间本来就会瞬间为空，于是聚类在语料
    只处理一半时就执行了；兜底分组又把"还没解析"误当成"没有素材"，把 530 个文件
    永久写进无向量的桶。

    用已有的 (package_id, path) 主键检查向量，只流式读取无向量文件所需字段。
    不再先 COUNT 再 SELECT *，避免重复扫描和整包 fetchall；素材判定仍完全
    交给 catalog.material，不缓存结果、不截断文件，也不降低收尾质量要求。
    """
    total = 0
    with closing(store._connect()) as conn:
        # Both path columns are NOT NULL in the authoritative schema, so this
        # indexed anti-join preserves NOT IN semantics, including empty vectors.
        # These four columns cover material() and its row_parts() dependency.
        rows = conn.execute(
            'SELECT f.local_summary,f.deep_summary,f.deep_status,f.sensitive '
            'FROM files f WHERE f.package_id=? AND f.sensitive=0 '
            'AND NOT EXISTS (SELECT 1 FROM large_catalog_members m '
            '                WHERE m.package_id=? AND m.path=f.path '
            '                AND LENGTH(m.vector)>0)',
            (package_id, package_id))
        for raw in rows:
            row = store._row(raw)
            # 已判定"正文不可用于语义归纳"的文件不再计入待办：它们永远不会拿到向量，
            # 若仍计入，本判据永远不为 0，全局聚类将再也不会触发。
            if ((row.get('local_summary') or {}).get('portrait') or {}).get('catalog_quality'):
                continue
            try:
                if catalog.material(row):
                    total += 1
            except Exception:
                total += 1
    return total


def solo_group_candidates(package_id, conn, limit=500):
    """方案A：列出"有可读特征卡、却没有任何成员组"的文件。只读。

    这些文件落在两条管道的缝里：catalog.material() 认为正文够（>=20 字），而
    evidence_quality() 要求去空白后 >=40 字才允许嵌入 —— 于是它们拿不到向量，
    任何分组路径都不会碰，界面上只能退回显示原始线索（"内容线索：..."）。
    实测某包 195 个（表格型 docx，抽出的正文只有 26~36 字）。
    """
    try:
        from services.large_package_evidence import FEATURE_VERSION
        rows = conn.execute(
            "SELECT f.path FROM files f "
            "JOIN lp_naming_features e ON e.package_id=f.package_id AND e.path=f.path "
            "AND e.version=? "
            "WHERE f.package_id=? AND f.sensitive=0 AND f.quick_status='completed' "
            "AND json_extract(e.card,'$.readable')=1 "
            "AND NOT EXISTS(SELECT 1 FROM large_catalog_members m "
            "               WHERE m.package_id=f.package_id AND m.path=f.path) "
            "AND NOT EXISTS(SELECT 1 FROM queue q WHERE q.package_id=f.package_id "
            "               AND q.path=f.path AND q.stage='catalog' "
            "               AND q.status IN ('queued','retry','running')) "
            "ORDER BY f.path LIMIT ?",
            (FEATURE_VERSION, package_id, int(limit))).fetchall()
        return [r[0] for r in rows]
    except Exception:
        LOGGER.exception('solo candidate scan failed: %s', package_id)
        return []


@_leaf_batch
def assign_solo_groups(store, package_id, limit=500):
    """把候选各自落成一个单文件条目，交给现有命名链路（queue_groups→命名）起名。

    与 kmeans_recluster 完全分开：**只新增**（只处理"没有任何组"的文件），
    绝不删除或移动任何已有成员 —— 因此不会被下一次重聚类清掉，也不改变现有分组。
    开关与批次1 同源：SJFX_LP_SOLO_GROUP_NAMING=0 关闭。
    """
    if not SOLO_GROUP_NAMING:
        return 0
    with closing(store._connect()) as conn:
        candidates = solo_group_candidates(package_id, conn, limit)
    if not candidates:
        return 0
    now = time.time()
    prefix = 'km-solo-' + hashlib.sha256(package_id.encode()).hexdigest()[:8]
    created = 0
    try:
        model = _km_model_name(store, package_id)
        with store._lock, closing(store._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            package = conn.execute('SELECT status FROM packages WHERE id=?',
                                   (package_id,)).fetchone()
            if not package or package[0] in {'paused', 'cancelled'}:
                conn.rollback()
                return 0
            taken = {r[0] for r in conn.execute(
                'SELECT id FROM large_catalog_groups WHERE package_id=?', (package_id,))}
            seq = 0
            for path in candidates:
                while ('%s-%05d' % (prefix, seq)) in taken:
                    seq += 1
                gid = '%s-%05d' % (prefix, seq)
                taken.add(gid)
                seq += 1
                # 空质心：单文件组没有语义质心；给它算质心反而会被邻居的名字带偏。
                conn.execute(
                    'INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,'
                    'result,status,updated_at,member_count) VALUES(?,?,?,?,?,?,?,?,0) '
                    'ON CONFLICT(package_id,id) DO UPDATE SET '
                    'representative=excluded.representative,updated_at=excluded.updated_at',
                    (package_id, gid, model, b'', path, '{}', 'queued', now))
                conn.execute(_MEMBER_UPSERT, (package_id, path, gid, '', b''))
                created += 1
            conn.commit()
    except Exception:
        LOGGER.exception('solo group assign failed: %s', package_id)
        return 0
    if created:
        LOGGER.info('solo groups assigned package=%s created=%d (ungrouped-but-readable)',
                    package_id, created)
        store._catalog_cache.pop(package_id, None)
    return created


def maintain_ungrouped(store, package_ids, limit=500):
    """给若干包补做单文件条目；返回 {package_id: created}。只新增，不动已有分组。"""
    out = {}
    for package_id in package_ids:
        try:
            n = assign_solo_groups(store, str(package_id), limit)
            if n:
                out[str(package_id)] = n
        except Exception:
            LOGGER.exception('maintain_ungrouped failed: %s', package_id)
    return out


@_leaf_batch
def kmeans_recluster(store, package_id, force=False):
    """把已落库的全部向量一次性用 k-means 重新分组。

    与原有增量贪心分组的区别：k-means 强制每个文件归入某个簇，因此不会出现
    "找不到相似同伴就自己一组"的单人组；同时固定质心避免了漂移锚点把不相关
    文件吸进同一个巨型组。

    分组结果是完全确定性的（质心随机种子固定），重算同一批向量会得到同样的
    分组，因此重启后重算不会打乱已有目录；命名缓存按 group_id 生效，重复的
    组不会被重新命名。
    """
    if not _km_enabled():
        return 0
    cfg = _km_settings()
    prior_signature = _km_state_get(store,package_id)
    if not force and prior_signature:
        if prior_signature == _km_signature(store,package_id):return 0
        from services.large_package_catalog_quality import repair_changed
        repaired=repair_changed(store,package_id)
        # 批3 第 15 条：返回值已结构化 —— 只要**不是**"值得重试/放弃"的中间态，
        # 就用它的 changed 数当本次结果（与旧语义 `if repaired is not None` 一致）。
        if not isinstance(repaired, dict) or repaired.get('status') in ('done', 'nothing'):
            return int((repaired or {}).get('changed', 0)) if isinstance(repaired, dict) else (repaired or 0)
    with closing(store._connect()) as conn:
        locked={r[0] for r in conn.execute('SELECT group_id FROM lp_catalog_labels WHERE package_id=? AND locked=1',(package_id,))}
        rows = conn.execute(
            'SELECT path, vector, input_hash, group_id FROM large_catalog_members '
            'WHERE package_id=? ORDER BY path',
            (package_id,)).fetchall()
    n = len(rows)
    # 绝对下界：少于 4 个文件没有分组意义（此时 1 个组也不会出现单人组问题）。
    if n < 4:
        return 0
    # 向量集指纹没变就不再重复聚类。指纹落库，因此 Worker 重启后结果保持一致
    # （原来用进程内字典记录 assigned，重启即归零，同一份语料会得到不同目录）。
    signature = _km_signature(store, package_id)
    if not force and signature == _km_state_get(store, package_id):
        return 0

    paths, vecs, input_hashes = [], [], {}
    for r in rows:
        if r['group_id'] in locked:continue
        try:
            v = _vector(np.frombuffer(r['vector'], dtype=np.float32))
        except Exception:
            continue
        paths.append(r['path'])
        vecs.append(v)
        # 保留素材摘要：它既是 _km_signature 的组成部分（用于判断"向量集是否
        # 变化过"），也让后续能追踪某个分组是基于哪个版本的内容算出来的。
        # 原实现这里把 input_hash 写成空串，导致指纹只能退化成"数量 + 字节数"。
        input_hashes[r['path']] = r['input_hash'] if 'input_hash' in r.keys() else ''
    if len(vecs) < 4:
        return 0
    X = np.vstack(vecs)

    # 叶组数：per_group 随语料规模放大，使叶组数收敛到"目标叶组数"附近。
    # 固定 per_group=16 在 100GB（约 3.5 万文件）会得到约 2170 个叶组，
    # 两层目录就不可浏览了；下限仍是 per_group，保证小语料不会被切得过碎。
    per_group = max(int(cfg['per_group']),
                    int(round(len(paths) / float(max(1, int(cfg['target_leaves']))))))
    base_k = int(round(len(paths) / float(per_group)))
    base_k = max(cfg['kmin'], min(cfg['kmax'], base_k, len(paths)))
    # k 自适应：定稿后若模型自认"混杂"的叶组太多，maybe_escalate_k 会升档，
    # 这里按档位放大 k（见 config 的说明与实测数据）。
    k = adaptive_k(store, package_id, base_k, len(paths), cfg)
    if k != base_k:
        LOGGER.info('k adaptive: package=%s base_k=%d → k=%d（第 %d 档）',
                    package_id, base_k, k, _km_boost_get(store, package_id))
    cap = max(4, int(cfg['max_group'] or 30))
    if os.getenv('SJFX_LP_CATALOG_FAST_KMEANS', '1').lower() not in {'0', 'false', 'off'}:
        cap = max(cap, effective_group_max(store, package_id))
    floor = max(1, int(cfg['min_group'] or 1))
    try:
        labels = _balanced_labels(X, k, cap, cfg['iters'])
        # 上限由 _balanced_labels 递归二分保证；下限由吸收保证。
        # 没有这一步，k-means 的离群点会变成只含一个文件的目录项。
        labels, _absorb_map = absorb_undersized(labels, X, floor, cap)
        # 病态单元素组兜底：出厂 min_group=1 时不设常规下限（原因见 config.py 的
        # 说明——常规下限会把不相关的原始簇强行合并、毁掉组名），但完全没有下限时
        # 异构语料会失控：实测旧增量锚点分组在 6,925 文件上产出 4,870 个组、
        # 其中 4,435 个单元素组（91%），目录不可浏览且必须命名的节点数失控。
        # 这里只在"单元素组占比确实病态"时补一次带下限的吸收，正常语料零影响。
        if floor <= 1 and cfg.get('min_group_auto'):
            sizes = {}
            for value in labels:
                key = int(value)
                sizes[key] = sizes.get(key, 0) + 1
            if sizes:
                singles = sum(1 for count in sizes.values() if count <= 1)
                share = singles / float(len(sizes))
                if share >= float(cfg.get('singleton_alarm', 0.25)):
                    rescue_floor = max(2, min(
                        max(2, cap // 3),
                        int(round(len(paths) / float(max(1, int(cfg['target_leaves']) * 3))))))
                    labels, _ = absorb_undersized(labels, X, rescue_floor, cap)
                    LOGGER.info(
                        'k-means singleton rescue package=%s groups=%d singles=%d share=%.2f '
                        'floor=%d groups_after=%d',
                        package_id, len(sizes), singles, share, rescue_floor,
                        len({int(value) for value in labels}))
    except Exception:
        LOGGER.exception('k-means fit failed: %s', package_id)
        return 0
    labels = np.asarray(labels, dtype=np.int64)

    buckets = {}
    for i, lab in enumerate(labels):
        buckets.setdefault(int(lab), []).append(i)

    now = time.time()
    model = _km_model_name(store, package_id)
    # 代表文件选择：优先挑正文最丰富的成员。原来固定取组内第一个文件，
    # 如果它的正文太少，命名阶段会报「暂无足够内容可命名」而把整组搁置
    # （实测 8 个组里有 4 个因此没有被命名）。
    richness = {}
    try:
        with closing(store._connect()) as conn:
            for r in conn.execute(
                    'SELECT path, LENGTH(COALESCE(json_extract(local_summary,'
                    "'$.sample'),'')) AS n, LENGTH(COALESCE(json_extract("
                    "deep_summary,'$.text_preview'),'')) AS m FROM files WHERE package_id=?",
                    (package_id,)).fetchall():
                richness[r[0]] = max(int(r[1] or 0), int(r[2] or 0))
    except Exception:
        richness = {}

    def pick_representative(members):
        return max(members, key=lambda i: (richness.get(paths[i], 0), -i))

    changed = 0
    try:
        with store._lock, closing(store._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            from services.large_package_catalog_quality import vector_signature,save_signature
            live_locks={r[0] for r in conn.execute('SELECT group_id FROM lp_catalog_labels WHERE package_id=? AND locked=1',(package_id,))}
            package=conn.execute('SELECT status,control_state FROM packages WHERE id=?',(package_id,)).fetchone()
            if vector_signature(conn,package_id)!=signature or live_locks!=locked or not package or any(v in {'paused','cancelled'} for v in package):
                conn.rollback();return 0
            for lab, members in buckets.items():
                if not members:
                    continue
                gid = 'km-%s-%05d' % (hashlib.sha256(package_id.encode()).hexdigest()[:8], lab)
                while gid in locked:gid+='-auto'
                rep = paths[pick_representative(members)]
                member_vecs = X[members]
                centroid = member_vecs.mean(axis=0)
                nn = np.linalg.norm(centroid)
                if nn > 1e-8:
                    centroid = centroid / nn
                existing = conn.execute(
                    'SELECT member_count FROM large_catalog_groups WHERE package_id=? AND id=?',
                    (package_id, gid)).fetchone()
                conn.execute(
                    'INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,'
                    'result,status,updated_at,member_count) VALUES(?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(package_id,id) DO UPDATE SET anchor=excluded.anchor,'
                    'representative=excluded.representative,updated_at=excluded.updated_at',
                    (package_id, gid, model, centroid.astype(np.float32).tobytes(), rep,
                     '{}', 'queued', now, 0))
                # 成员按 path 排序写入；触发器负责维护 member_count。
                conn.execute(
                    'DELETE FROM large_catalog_members WHERE package_id=? AND group_id=?',
                    (package_id, gid))
                conn.executemany(
                    _MEMBER_UPSERT,
                    [(package_id, paths[i], gid, input_hashes.get(paths[i], ''),
                      X[i].astype(np.float32).tobytes())
                     for i in members])
                # 清掉旧分组遗留的桶与空组
                conn.execute('DELETE FROM large_catalog_buckets WHERE package_id=? AND group_id=?',
                             (package_id, gid))
                for band, bucket in enumerate(_buckets(centroid.astype(np.float32))):
                    conn.execute('INSERT OR IGNORE INTO large_catalog_buckets VALUES(?,?,?,?)',
                                 (package_id, band, bucket, gid))
                changed += len(members)
            # 删除本次未被分配到的旧组。
            # 注意：不能只看 member_count —— 旧算法留下的组可能保留着陈旧的计数
            # （实测有组记录 59 个成员、实际一个都没有），那样它会被当成"真实分组"
            # 留在目录里，把分布统计也带偏。这里按**实际成员数**判定。
            conn.execute(
                'DELETE FROM large_catalog_groups WHERE package_id=? AND id NOT IN '
                '(SELECT DISTINCT group_id FROM large_catalog_members WHERE package_id=?)',
                (package_id, package_id))
            conn.execute(
                'DELETE FROM large_catalog_buckets WHERE package_id=? AND group_id NOT IN '
                '(SELECT id FROM large_catalog_groups WHERE package_id=?)',
                (package_id, package_id))
            # 兜底：正文过少、没有足够素材参与嵌入的文件不会出现在
            # large_catalog_members 里。这里用同一个确定性分组收拢它们，
            # 保证目录覆盖 100% 的文件，不让任何文件从目录里消失。
            #
            # 两个必须同时成立的条件（缺一个就会永久吞掉文件）：
            #   ① quick_status='completed' —— 解析已完成，状态不会再变；
            #   ② 确实没有素材（catalog.material() 为空）。
            # 原实现只排除了"还在 catalog 队列里"的文件，但**队列行是解析完成
            # 时才创建的**：还没解析到的文件根本没有队列行，于是通过过滤被当成
            # "没有素材"收进兜底组。实测 1040 个文件里 530 个被这样吞掉，
            # 其中 525 个拥有完整可用的正文素材。
            candidates = conn.execute(
                'SELECT * FROM files WHERE package_id=? AND sensitive=0 '
                'AND quick_status=\'completed\' '
                'AND path NOT IN (SELECT path FROM large_catalog_members WHERE package_id=?) '
                'AND path NOT IN (SELECT path FROM queue WHERE package_id=? AND stage=\'catalog\' '
                '                AND status IN (\'queued\',\'retry\',\'running\'))',
                (package_id, package_id, package_id)).fetchall()
            stragglers, deferred, solos = [], [], []
            for raw in candidates:
                row = store._row(raw)
                if catalog.material(row):
                    # 有素材却没有向量 —— 说明嵌入还没跑完，绝不能兜底。
                    # 把它退回队列重试，否则会被永久写进无向量的桶里再也出不来。
                    deferred.append(row['path'])
                    continue
                # 方案 B：没有向量、也进不了任何组的文件，此前一律塞进 km-other 桶，
                # 界面上就显示成"内容线索：<原始片段>"这样一个假目录项。按设计
                # "单文件条目也可以，直接命名就行"—— 只要这份文件的特征卡**可读**，
                # 就让它自成一个单文件条目，交给现有命名链路按同一套规则起名。
                # 特征卡不可读（正文确实提取不出来）的仍走 km-other，
                # "未能识别内容"这个诚实出口不变。
                if SOLO_GROUP_NAMING and _card_readable(conn, package_id, row['path']):
                    solos.append(row['path'])
                else:
                    stragglers.append(row['path'])
            if solos:
                solo_prefix = 'km-solo-' + hashlib.sha256(package_id.encode()).hexdigest()[:8]
                for index, path in enumerate(sorted(solos)):
                    gid = '%s-%05d' % (solo_prefix, index)
                    # 空质心：它没有语义质心可算（组里只有一份文件），而且给空组算质心
                    # 会让它出现在相邻组搜索里、被邻居的名字带偏。
                    # status='queued'：进命名队列，由命名链路写正式名称。
                    conn.execute(
                        'INSERT INTO large_catalog_groups(package_id,id,model,anchor,'
                        'representative,result,status,updated_at,member_count) '
                        'VALUES(?,?,?,?,?,?,?,?,0) '
                        'ON CONFLICT(package_id,id) DO UPDATE SET '
                        'representative=excluded.representative,updated_at=excluded.updated_at',
                        (package_id, gid, model, b'', path, '{}', 'queued', now))
                    conn.execute(_MEMBER_UPSERT, (package_id, path, gid, '', b''))
                changed += len(solos)
                LOGGER.info('solo groups package=%s created=%d', package_id, len(solos))
            if deferred:
                LOGGER.warning('k-means deferred %d material files without vectors: %s',
                               len(deferred), package_id)
                conn.executemany(
                    "INSERT INTO queue(package_id,path,stage,status,updated_at) "
                    "VALUES(?,?,'catalog','queued',?) "
                    "ON CONFLICT(package_id,path,stage) DO UPDATE SET status='queued',"
                    "attempts=0,leased_at=NULL,error=NULL,updated_at=excluded.updated_at",
                    [(package_id, p, now) for p in deferred])
            if stragglers:
                gid = 'km-other-' + hashlib.sha256(package_id.encode()).hexdigest()[:8]
                # 状态直接写 'named'：系统桶不参与语义命名，避免模型用其中某个
                # 文件的标题给它起名，也避免它被反复排进命名队列。
                # 显示名由 Worker 的 _name_fallback 写成固定的"未能识别内容"。
                conn.execute(
                    'INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,'
                    'result,status,updated_at,member_count) VALUES(?,?,?,?,?,?,?,?,0) '
                    'ON CONFLICT(package_id,id) DO UPDATE SET updated_at=excluded.updated_at',
                    (package_id, gid, model, b'', stragglers[0], '{}', 'named', now))
                conn.executemany(
                    _MEMBER_UPSERT,
                    [(package_id, p, gid, '', b'') for p in stragglers])
                changed += len(stragglers)
            # 显式清掉暂存用的占位分组：它是 km-pending-<hash> 命名空间下的空组，
            # 已无成员，但不能留在目录里（否则会显示成一个没有名字的分组）。
            # 不依赖 member_count 的触发器时序，直接按主键删除。
            ph = 'km-pending-' + hashlib.sha256(package_id.encode()).hexdigest()[:8]
            conn.execute('DELETE FROM large_catalog_buckets WHERE package_id=? AND group_id=?',
                         (package_id, ph))
            conn.execute('DELETE FROM large_catalog_members WHERE package_id=? AND group_id=?',
                         (package_id, ph))
            conn.execute('DELETE FROM large_catalog_groups WHERE package_id=? AND id=?',
                         (package_id, ph))
            conn.execute('DELETE FROM lp_catalog_changes WHERE package_id=?',(package_id,))
            conn.execute('DELETE FROM lp_catalog_removed_groups WHERE package_id=?',(package_id,))
            save_signature(conn,package_id,len(paths))
            conn.commit()
    except Exception:
        LOGGER.exception('k-means assign failed: %s', package_id)
        return 0
    # 存入"本次聚类所用向量集"的指纹。必须在写库之后重算：
    # 聚类本身会删除未被分配到的旧组，从而改变 content_revision 之和。
    LOGGER.info('k-means recluster package=%s vectors=%d k=%d groups=%d ceil=%d floor=%d',
                package_id, len(paths), k, len(buckets), cap, floor)
    return changed


def _km_model_name(store, package_id):
    try:
        with closing(store._connect()) as conn:
            row = conn.execute(
                'SELECT model FROM large_catalog_groups WHERE package_id=? LIMIT 1',
                (package_id,)).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    try:
        from services.embedding_backend import client_identity
        client = embedding_client()
        return client_identity(client) if client else ''
    except Exception:
        return 'multilingual-e5-small'


def embed_many(client, texts, factory=None):
    """把一批文本切成 `client.max_batch_size` 的块**并发**发出，按原顺序返回向量。

    为什么需要（实测，见本次嵌入基准）：`VLLMEmbeddingClient.embed()` 是串行 for 循环，
    而 `EMBED_BATCH=32` 会被它切成 4 个 8 条的请求一个接一个发——服务端明明配了
    OMP 4 线程 × 准入 2（最多 8 路），实测有效并行度只有 2.38（批 8）/ 3.79（批 16）。
    把这几块同时发出去，才能用上服务端已经准备好的并行能力。

    为什么写在这里而不是改 `services/vllm.py`：那个客户端是**共享模块**，普通召回、
    离线翻译等车道都在用；改它等于替所有车道做主。这里只影响大包目录这一条车道。

    为什么需要 `factory`（每线程一个独立客户端实例）：`vllm.py` 里每个客户端自带
    `BoundedSemaphore(1)`，**同一实例同时只允许一个请求在飞**。所以"拿同一个 client
    多线程发块"不会并发，只会撞上它的互斥并抛"vLLM Embedding 忙碌"（第一版实测就是这么
    失败的）。服务端自己已有准入控制（`SJFX_EMBED_ADMISSION=2`），因此"2 个实例 × 各 1
    个请求"= 2 路并发，正好落在服务端配置的容量内。`factory=None` 时**如实退回串行**。

    顺序：返回顺序与入参严格一致（每块的结果按块序拼接），调用方按 `keys` 一一对应。
    失败：任一块抛错就整体抛出——让上层的统一失败分类（不可用→长退避、不烧 attempts）
    照原样生效，不在这里做局部吞错。
    """
    values = list(texts)
    if not values:
        return []
    size = max(1, int(getattr(client, 'max_batch_size', 8) or 8))
    chunks = [values[i:i + size] for i in range(0, len(values), size)]
    width = max(1, min(int(embed_concurrency()), len(chunks)))
    def checked_embed(instance, chunk):
        result = list(instance.embed(chunk))
        if len(result) != len(chunk):
            raise ValueError('语义向量数量不一致')
        return result
    # 没有工厂函数就**如实退回串行**：同一个客户端实例内部是
    # `BoundedSemaphore(1)`（vllm.py），多线程发只会撞它自己的互斥并抛"忙碌"，
    # 起线程池纯属白费——第一版就是这样把嵌入打成失败的。
    if factory is None:
        return checked_embed(client, values)
    if width == 1 or len(chunks) == 1:
        return checked_embed(client, values)
    clients = []
    for _ in range(width):
        try:
            made = factory()
        except Exception:
            made = None
        # A missing/aliased client cannot provide an independent request lane.
        # Fall back once, before dispatch; never share its internal semaphore.
        if made is None or any(made is other for other in clients):
            return checked_embed(client, values)
        from services.embedding_backend import client_identity
        expected_model = client_identity(client)
        actual_model = client_identity(made)
        if expected_model is not None and actual_model != expected_model:
            return checked_embed(client, values)
        clients.append(made)
    from concurrent.futures import ThreadPoolExecutor
    from queue import SimpleQueue
    available = SimpleQueue()
    for instance in clients:
        available.put(instance)
    def run_chunk(chunk):
        # Executor workers take the next queued task as soon as they finish.
        # Task ordinal modulo width does NOT identify an idle client: a slow
        # first chunk can still own that client when the third chunk starts.
        instance = available.get()
        try:
            return checked_embed(instance, chunk)
        finally:
            available.put(instance)
    with ThreadPoolExecutor(max_workers=width, thread_name_prefix='sjfx-lp-embed') as pool:
        parts = list(pool.map(run_chunk, chunks))
    result = []
    for part in parts:
        result.extend(part)
    if len(result) != len(values):
        raise ValueError('语义向量数量不一致')
    return result


def embed_concurrency():
    """本车道允许同时在飞的嵌入请求数。默认 2 = 服务端准入 2，不超出服务端的承受能力。"""
    try:
        from config import Config
        return max(1, min(4, int(getattr(Config, 'LP_EMBED_CONCURRENCY', 2) or 2)))
    except Exception:
        return 1


@_leaf_batch
def embed_batch(store, package_id, client=None, cancelled=lambda: False):
    if cancelled():
        return 0
    claimed = store.claim('catalog', EMBED_BATCH, package_id=package_id)
    if not claimed:
        return 0
    # 本次领取的租约令牌（path → token）。提交/回退必须原样带回，否则会被租约校验拒绝。
    leases = {item[1]: item[2] for item in claimed}
    # 这里曾经有一个"已完成全局聚类就不再暂存"的早退分支：它只把队列项标成
    # completed，却不写 large_catalog_members。深度解析会改写正文并重新入队
    # catalog（见 large_package_isolated.update_file 的 schedule_catalog），
    # 那些文件因此被静默丢弃、永远没有向量。
    # 现在改为始终暂存，并在写成员时**沿用该文件已有的分组**，
    # 这样既不会把文件拖回占位分组，也不会丢掉数据。
    from services.large_package_catalog_quality import representation
    from services.large_package_evidence import row_parts
    policy = representation(store,package_id)
    rows = []
    source_rows = store.get_files(package_id, [path for _, path, _ in claimed], decorate=False)
    for _, path, _lease in claimed:
        row = source_rows.get(path)
        source = catalog.material(row) if row else None
        if source:
            value,digest=source
            if policy >= 2:value=dict(value,evidence_blocks=row_parts(row))
            rows.append((path,value,digest))
        else:
            store.finish_queue(package_id, path, 'catalog', True, token=leases.get(path))
    # 统一正文质量闸门（k-means 启用路径）。此前 is_garbled() 只挂在旧的
    # greedy 分组路径上，k-means 完全不调用它，于是乱码正文照样拿到向量、
    # 照样参与质心计算。这里把它接上：不可用的正文如实记进画像，
    # 结束队列项但不重试，也不计入 material_pending_count()。
    usable_rows = []
    for path, value, digest in rows:
        usable, reason = evidence_quality(value)
        if usable:
            usable_rows.append((path, value, digest))
        else:
            LOGGER.info('catalog evidence unusable package=%s path=%s reason=%s',
                        package_id, path, reason)
            _mark_unusable(store, package_id, path, reason, leases.get(path))
    rows = usable_rows
    try:
        client = client or embedding_client()
        if client is None:
            raise RuntimeError('本地语义向量模型暂不可用')
        from services.embedding_backend import client_identity, validate_vectors
        model = client_identity(client)
        with closing(store._connect()) as conn:
            _require_space(conn, package_id, client)
        vectors, pending, cache_keys = {}, {}, {}
        with closing(store._connect()) as conn:
            for path, value, digest in rows:
                passage = embedding_passage(value,policy)
                cache_key = hashlib.sha256(('catalog-representation/%d\0'%policy+passage).encode()).hexdigest()
                cache_keys[digest] = cache_key
                cached = conn.execute('SELECT vector FROM large_catalog_embeddings WHERE model=? AND digest=?', (model,cache_key)).fetchone()
                if cached:
                    vectors[digest] = _vector(np.frombuffer(cached[0], dtype=np.float32))
                else:
                    # E5 passage encoding uses content only; deep summaries replace shallow samples.
                    pending[cache_key] = passage
        keys = list(pending)
        if keys:
            # 并发发块（见 embed_many 的实测依据）：每块用**独立客户端实例**，
            # 顺序与 keys 严格对应。
            encoded = embed_many(client, [pending[k] for k in keys], factory=embedding_client)
            if len(encoded) != len(keys):
                raise ValueError('语义向量数量不一致')
            encoded = validate_vectors(encoded, len(keys), len(next(iter(vectors.values()))) if vectors else getattr(client, 'dimension', 0))
            fresh = {key:_vector(vector) for key,vector in zip(keys,encoded)}
            with closing(store._connect()) as conn:
                # _connect uses autocommit; an explicit transaction amortizes
                # WAL work across the batch without changing any cache key.
                conn.execute('BEGIN IMMEDIATE')
                try:
                    _require_space(conn, package_id, client, len(next(iter(fresh.values()))))
                    conn.executemany('INSERT OR REPLACE INTO large_catalog_embeddings VALUES(?,?,?)', [(model,k,fresh[k].tobytes()) for k in keys])
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            for digest,key in cache_keys.items():
                if key in fresh:
                    vectors[digest] = fresh[key]
        if cancelled():
            for _, path, _lease in claimed:
                store.requeue(package_id,path,'catalog',token=leases.get(path))
            return 0
        if _km_enabled():
            # k-means 模式：本批次只负责把向量写进 large_catalog_members，
            # 真正的分组由 kmeans_recluster 在"所有有素材的文件都有向量之后"
            # 一次性完成（由 Worker 的 _ensure_km_finalized 触发）。
            # 这里绝不主动触发聚类：批次中途聚类正是目录失真的根因。
            validate_vectors([v.tolist() for v in vectors.values()], dimension=getattr(client, 'dimension', 0))
            now = time.time()
            placeholder = 'km-pending-%s' % hashlib.sha256(package_id.encode()).hexdigest()[:8]
            try:
                with store._lock, closing(store._connect()) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    _require_space(conn, package_id, client, len(next(iter(vectors.values()))) if vectors else 0)
                    # 沿用已有分组：深度解析改写正文后会重新入队 catalog，
                    # 若这里一律写占位分组，这些文件会被从算好的分组里拽出来。
                    #
                    # 只查**本批**路径即可。改动前这里读的是全包
                    # `SELECT path, group_id FROM large_catalog_members WHERE package_id=?`，
                    # 而本函数每批最多 32 个文件被调用一次，于是累计读取量随规模
                    # 二次增长（35,180 文件约 3,870 万行）。语义完全等价：
                    # 下面的取值只用 `existing.get(path, ...)`，未命中的路径
                    # 本来就拿不到值、回落占位分组。
                    valid_rows=[]
                    for path,value,digest in rows:
                        lease=conn.execute("SELECT 1 FROM queue WHERE package_id=? AND path=? AND stage='catalog' AND status='running' AND lease_token=?",(package_id,path,leases.get(path))).fetchone()
                        live=conn.execute('SELECT * FROM files WHERE package_id=? AND path=?',(package_id,path)).fetchone()
                        source=catalog.material(store._row(live)) if live else None
                        if lease and source and source[1]==digest:valid_rows.append((path,value,digest))
                    rows=valid_rows
                    batch_paths = [path for path, _value, _digest in rows]
                    existing = {}
                    if batch_paths:
                        marks = ','.join('?' for _ in batch_paths)
                        existing = dict(conn.execute(
                            'SELECT path, group_id FROM large_catalog_members '
                            'WHERE package_id=? AND path IN (%s)' % marks,
                            [package_id] + batch_paths).fetchall())
                    conn.execute(
                        'INSERT OR IGNORE INTO large_catalog_groups(package_id,id,model,anchor,'
                        'representative,result,status,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                        (package_id, placeholder, model, b'', rows[0][0] if rows else '',
                         '{}', 'pending', now))
                    conn.executemany(
                        _MEMBER_UPSERT,
                        [(package_id, path, existing.get(path, placeholder), digest,
                          vectors[digest].tobytes())
                         for path, value, digest in rows])
                    # 队列项标记完成，让 catalog 阶段能正常收尾。
                    # 必须带**租约校验**并清掉令牌——这里原先只做无条件 UPDATE，
                    # 烟雾验证发现跑完目录后 15 个已完成行仍持有租约。
                    conn.executemany(
                        "UPDATE queue SET status='completed',error=NULL,updated_at=?,lease_token=NULL "
                        "WHERE package_id=? AND path=? AND stage='catalog' AND lease_token=?",
                        [(now, package_id, path, str(leases.get(path) or ''))
                         for path, value, digest in rows])
                    conn.commit()
            except Exception:
                LOGGER.exception('k-means staging failed: %s', package_id)
                for path, value, digest in rows:
                    catalog._save(store, package_id, path, digest,
                                  error='语义向量暂未入库，稍后重试',
                                    token=leases.get(path))
            return len(claimed)
        # One calibration per batch keeps the write lock out of the cosine math.
        try:
            calibration = calibrate(store, package_id, model)
        except Exception:
            calibration = (None, SIMILARITY)
        for path, value, digest in rows:
            _group(store,package_id,path,digest,vectors[digest],model,calibration,leases.get(path))
    except Exception as exc:
        for path, value, digest in rows:
            catalog._save(store,package_id,path,digest,
                          error='语义分组暂未完成：'+str(exc)[:180],
                          token=leases.get(path))
    return len(claimed)


@_leaf_batch
def name_batch(store, package_id, cancelled=lambda: False):
    if cancelled():
        return 0
    with closing(store._connect()) as conn:
        groups = conn.execute("SELECT * FROM large_catalog_groups WHERE package_id=? AND status IN ('queued','retry') AND updated_at<=? ORDER BY updated_at LIMIT 64",(package_id,time.time())).fetchall()
    if not groups:
        return 0
    rows, selected, group_values = [], [], []
    for group in groups:
        row = catalog.get_file(store, package_id,group['representative'])
        source = catalog.material(row) if row else None
        if not source:
            with closing(store._connect()) as conn:
                conn.execute("UPDATE large_catalog_groups SET status='deferred' WHERE package_id=? AND id=?", (package_id,group['id']))
            continue
        if source:
            value, digest = source
            from services.large_package_catalog_naming import representatives
            sampled, snapshot = representatives(store,package_id,group['id'])
            values = [v for _,v in sampled] or [value]
            if len(values) > 1:
                value = {**value, 'text': '\n'.join('同组代表材料 {}：{}'.format(i+1,v['text'][:1600]) for i,v in enumerate(values)),
                         'deep_summary': '\n'.join('代表材料 {} 的深度摘要：{}'.format(i+1,v['deep_summary'][:1000]) for i,v in enumerate(values) if v['deep_summary']),
                         'headings': [h for v in values for h in v['headings'][:2]], 'fields': []}
            rows.append((group['representative'],value,digest));selected.append(group);group_values.append(values)
    from services.large_package_labels import name_group
    result = {}
    error = ''
    for i, values in enumerate(group_values):
        try:
            result[i] = name_group(values)
        except (ValueError, TypeError) as exc:
            error = str(exc)[:180]
    if cancelled():
        return 0
    for i,group in enumerate(selected):
        value = result.get(i)
        with store._lock,closing(store._connect()) as conn:
            current = conn.execute('SELECT anchor,representative FROM large_catalog_groups WHERE package_id=? AND id=?',(package_id,group['id'])).fetchone()
            if not current or current['representative'] != group['representative'] or current['anchor'] != group['anchor']:
                continue
            retry = not value and int(group['attempts']) < 2
            conn.execute('UPDATE large_catalog_groups SET result=?,status=?,updated_at=?,attempts=attempts+1 WHERE package_id=? AND id=?',
                         (json.dumps(value or {},ensure_ascii=False),'named' if value else 'retry' if retry else 'deferred',time.time()+(60 if retry else 0),package_id,group['id']))
            members = conn.execute('SELECT path,input_hash FROM large_catalog_members WHERE package_id=? AND group_id=?',(package_id,group['id'])).fetchall()
        for member in members:
            if value:
                _apply_member(store,package_id,member['path'],member['input_hash'],value)
            else:
                catalog._save(store,package_id,member['path'],member['input_hash'],error=error or '主题名称缺少有效内容证据')
    return len(groups)

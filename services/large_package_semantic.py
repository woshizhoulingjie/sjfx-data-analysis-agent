"""Incremental semantic groups for the isolated large-package directory only."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import time
from contextlib import closing
import numpy as np
from services import large_package_catalog as catalog

EMBED_BATCH = 32
SIMILARITY = .94


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
    # Cached corpus centre + adaptive similarity threshold for one package.
    conn.execute('''CREATE TABLE IF NOT EXISTS large_catalog_centers (
      package_id TEXT NOT NULL, model TEXT NOT NULL, center BLOB, threshold REAL NOT NULL,
      sample INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
      PRIMARY KEY(package_id,model))''')
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


def embedding_client():
    from config import Config
    if not Config.ENABLE_VLLM_EMBEDDINGS:
        return None
    from services.vllm import VLLMEmbeddingClient
    return VLLMEmbeddingClient(Config.VLLM_EMBED_BASE_URL, Config.VLLM_EMBED_MODEL,
                               timeout=Config.VLLM_EMBED_TIMEOUT_SECONDS, max_batch_size=8,
                               max_chars=4096, api_key=Config.VLLM_API_KEY)


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


def _group(store, package_id, path, digest, vector, model, calibration=None):
    from config import Config
    with store._lock, closing(store._connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        raw = conn.execute('SELECT local_summary FROM files WHERE package_id=? AND path=?', (package_id, path)).fetchone()
        if not raw or json.loads(raw[0]).get('portrait', {}).get('catalog_input_hash') != digest:
            conn.rollback()
            store.requeue(package_id, path, 'catalog')
            return
        old = conn.execute('SELECT group_id,input_hash FROM large_catalog_members WHERE package_id=? AND path=?', (package_id, path)).fetchone()
        if old and old['input_hash'] == digest:
            conn.execute("UPDATE queue SET status='completed',updated_at=? WHERE package_id=? AND path=? AND stage='catalog'", (time.time(), package_id, path))
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
                conn.execute('INSERT OR REPLACE INTO large_catalog_members VALUES(?,?,?,?,?)', (package_id, path, group_id, digest, vector.tobytes()))
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
                conn.execute("UPDATE queue SET status='completed',error=NULL,updated_at=? WHERE package_id=? AND path=? AND stage='catalog'",
                             (time.time(), package_id, path))
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
        conn.execute("UPDATE queue SET status='completed',error=NULL,updated_at=? WHERE package_id=? AND path=? AND stage='catalog'", (time.time(),package_id,path))
        conn.commit()
    if result:
        _apply_member(store, package_id, path, digest, result, score)
    store._catalog_cache.pop(package_id, None)


def _split_one(store, package_id, group, limit):
    """Binary split of one oversized group; returns 1 when it actually split."""
    group_id = str(group['id'])
    model = str(group['model'])
    with closing(store._connect()) as conn:
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
    keep, move = (left, right) if len(left) >= len(right) else (right, left)
    new_id = hashlib.sha256((group_id + '\0' + paths[move[0]] + '\0' + str(len(move))).encode()).hexdigest()[:24]
    keep_anchor = _norm(matrix[keep].mean(axis=0))
    move_anchor = _norm(matrix[move].mean(axis=0))
    with store._lock, closing(store._connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
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


def split_oversized(store, package_id, max_size=None, max_rounds=6):
    """Split groups that grew past the configured size into coherent halves.

    Members keep their stored vectors, so this is local arithmetic: no parsing,
    no embedding calls, and the model only names the resulting groups.  Skipped
    entirely when the limit is 0 (SJFX_LP_CATALOG_GROUP_MAX=0).
    """
    from config import Config
    limit = int(max_size if max_size is not None else (getattr(Config, 'LP_CATALOG_GROUP_MAX', 0) or 0))
    if limit <= 1:
        return 0
    created = 0
    for _ in range(max(1, int(max_rounds))):
        with closing(store._connect()) as conn:
            oversized = conn.execute(
                "SELECT id,model,member_count FROM large_catalog_groups WHERE package_id=? AND member_count>? AND id NOT LIKE 'garble-%' ORDER BY member_count DESC LIMIT 8",
                (package_id, limit)).fetchall()
        if not oversized:
            break
        progress = 0
        for group in oversized:
            try:
                progress += _split_one(store, package_id, group, limit)
            except Exception:
                continue
        created += progress
        if not progress:
            break
    return created


def _apply_member(store, package_id, path, digest, result, score=1.0):
    row = catalog.get_file(store, package_id, path)
    source = catalog.material(row) if row else None
    if not source or source[1] != digest:
        return
    value = source[0]
    # The group name comes from representative content; each member cites its OWN source.
    result = {**result, 'quotes': [{'source': 'deep_summary' if value['deep_summary'] else 'content',
                                  'text': (value['deep_summary'] or value['text'] or ' / '.join(value['fields']))[:900]}],
              'reason': '依据文件内容语义归组；目录名提取自组内代表标题/短语，文件证据见下方'}
    catalog._save(store, package_id, path, digest, result)


_embedding_tokenizer = None
_embedding_tokenizer_checked = False

def embedding_passage(value):
    global _embedding_tokenizer, _embedding_tokenizer_checked
    if not _embedding_tokenizer_checked:
        _embedding_tokenizer_checked = True
        try:
            import os
            from pathlib import Path
            from config import Config
            from tokenizers import Tokenizer
            path=Path(os.getenv('SJFX_EMBED_MODEL_PATH',str(Path(Config.VLLM_MODEL_PATH).parent/'multilingual-e5-small')))/'tokenizer.json'
            _embedding_tokenizer=Tokenizer.from_file(str(path))
        except Exception:
            pass
    text=value['deep_summary'] or value['text'] or ' / '.join(value['fields'])
    headings=' / '.join(value.get('headings',[])[:2])
    tokenizer=_embedding_tokenizer
    if tokenizer is None:
        # Preserve diverse positions under the existing character cap.
        width=320
        windows=[text[:width],text[max(0,len(text)//2-width//2):len(text)//2+width//2],text[-width:]] if len(text)>960 else [text]
        return 'passage: '+(headings[:140]+'\n'+'\n'.join(dict.fromkeys(windows)))[:1180]
    ids=tokenizer.encode(text,add_special_tokens=False).ids
    title=tokenizer.encode(headings,add_special_tokens=False).ids[:64]
    budget=440-len(title)
    if len(ids)>budget:
        width=budget//3
        ids=ids[:width]+ids[len(ids)//2-width//2:len(ids)//2+width//2]+ids[-width:]
    return 'passage: '+tokenizer.decode(title+ids,skip_special_tokens=True)


def embed_batch(store, package_id, client=None, cancelled=lambda: False):
    if cancelled():
        return 0
    claimed = store.claim('catalog', EMBED_BATCH, package_id=package_id)
    if not claimed:
        return 0
    rows = []
    for _, path in claimed:
        row = catalog.get_file(store, package_id, path)
        source = catalog.material(row) if row else None
        if source:
            rows.append((path, *source))
        else:
            store.finish_queue(package_id, path, 'catalog', True)
    client = client or embedding_client()
    try:
        if client is None:
            raise RuntimeError('本地语义向量模型暂不可用')
        model = client.model
        vectors, pending = {}, {}
        with closing(store._connect()) as conn:
            for path, value, digest in rows:
                cached = conn.execute('SELECT vector FROM large_catalog_embeddings WHERE model=? AND digest=?', (model,digest)).fetchone()
                if cached:
                    vectors[digest] = _vector(np.frombuffer(cached[0], dtype=np.float32))
                else:
                    # E5 passage encoding uses content only; deep summaries replace shallow samples.
                    pending[digest] = embedding_passage(value)
        keys = list(pending)
        if keys:
            encoded = client.embed([pending[k] for k in keys])
            if len(encoded) != len(keys):
                raise ValueError('语义向量数量不一致')
            for key, vector in zip(keys, encoded):
                vectors[key] = _vector(vector)
            with closing(store._connect()) as conn:
                conn.executemany('INSERT OR REPLACE INTO large_catalog_embeddings VALUES(?,?,?)', [(model,k,vectors[k].tobytes()) for k in keys])
        if cancelled():
            for _, path in claimed:
                store.requeue(package_id,path,'catalog')
            return 0
        # One calibration per batch keeps the write lock out of the cosine math.
        try:
            calibration = calibrate(store, package_id, model)
        except Exception:
            calibration = (None, SIMILARITY)
        for path, value, digest in rows:
            _group(store,package_id,path,digest,vectors[digest],model,calibration)
    except Exception as exc:
        for path, value, digest in rows:
            catalog._save(store,package_id,path,digest,error='语义分组暂未完成：'+str(exc)[:180])
    return len(claimed)


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

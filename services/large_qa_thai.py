"""Offline Thai sidecar; the legacy contentless FTS remains untouched."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from functools import lru_cache
from importlib import metadata, resources
from pathlib import Path
from services.large_qa_resources import resource_code

PACKAGE_VERSION = '5.3.8'
DICTIONARY_SHA256 = '4bf0ab93a570d91b33ae7bbf627eddd5a56f81c6ba33a94c3950f38904235bda'
ALGORITHM = 'thai-v1:newmm-safe:nfc:hex:surface-phrase:' + PACKAGE_VERSION + ':' + DICTIONARY_SHA256
META_KEY = 'thai_lexical'
THAI = re.compile(r'[ก-๛]+')


class ThaiIndexError(RuntimeError):
    """An index failure must not be mistaken for a malformed paragraph."""


def has_thai(text):
    return bool(THAI.search(str(text or '')))


@lru_cache(maxsize=1)
def _provider():
    try:
        if metadata.version('pythainlp') != PACKAGE_VERSION:
            raise ThaiIndexError('Thai tokenizer version mismatch; require ' + PACKAGE_VERSION)
        os.environ['PYTHAINLP_OFFLINE'] = '1'
        # Respect the deprecated flag without setting conflicting flag names.
        flag = 'PYTHAINLP_READ_MODE' if 'PYTHAINLP_READ_MODE' in os.environ else 'PYTHAINLP_READ_ONLY'
        os.environ[flag] = '1'
        # Only the pinned bundled dictionary, never a downloaded user corpus.
        data = resources.files('pythainlp').joinpath('corpus/words_th.txt').read_bytes()
        if hashlib.sha256(data).hexdigest() != DICTIONARY_SHA256:
            raise ThaiIndexError('Thai bundled dictionary checksum mismatch')
        from pythainlp.tokenize import word_tokenize
        from pythainlp.util import Trie
        return word_tokenize, Trie(data.decode('utf-8-sig').splitlines())
    except ThaiIndexError:
        raise
    except Exception as exc:
        if resource_code(exc):
            raise
        raise ThaiIndexError('Offline Thai tokenizer unavailable: ' + type(exc).__name__) from exc


def thai_tokens(text):
    text = str(text or '')
    if not has_thai(text):
        return []
    tokenize, dictionary = _provider()
    try:
        # NFC preserves tones/vowels. Do not discard negations or stopwords.
        terms = []
        for run in THAI.findall(unicodedata.normalize('NFC', text)):
            for word in tokenize(run, custom_dict=dictionary, engine='newmm-safe', keep_whitespace=False):
                if has_thai(word):
                    terms.append('th' + word.encode('utf-8').hex())
        return terms
    except Exception as exc:
        if resource_code(exc):
            raise
        raise ThaiIndexError('Offline Thai tokenization failed') from exc


def _meta(c):
    return {r[0]: json.loads(r[1]) for r in c.execute(
        "SELECT key,value FROM meta WHERE key IN ('index_epoch','generation',?)", (META_KEY,))}


def _stamp(meta):
    return dict(algorithm=ALGORITHM, index_epoch=meta.get('index_epoch'), generation=meta.get('generation'))


def ready(c, meta=None):
    meta = _meta(c) if meta is None else meta
    if meta.get(META_KEY) != _stamp(meta):
        return False
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_thai'").fetchone() is not None


def mark_current(c):
    c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
              (META_KEY, json.dumps(_stamp(_meta(c)), sort_keys=True)))


def add_chunk(c, chunk_id, path, text):
    try:
        title = thai_tokens(Path(path).name + ' ' + path)
        body = thai_tokens(text)
        if title or body:
            # Exact character phrases recover valid compounds whose dictionary
            # segmentation changes with context. Tones remain separate positions,
            # so removing a tone cannot create an accidental exact phrase match.
            surface = surface_tokens(Path(path).name + ' ' + path + ' ' + str(text or ''))
            c.execute('INSERT INTO search_thai(rowid,title,body,surface) VALUES(?,?,?,?)',
                      (chunk_id, ' '.join(title), ' '.join(body), surface))
    except ThaiIndexError:
        raise
    except Exception as exc:
        if resource_code(exc):
            raise
        raise ThaiIndexError('Thai lexical index write failed') from exc


def ensure(c):
    """Bounded atomic upgrade. A rolled-back writer invalidates generation."""
    if not c.in_transaction:
        raise ThaiIndexError('Thai migration requires an existing transaction')
    if ready(c):
        return False
    # Never executescript: implicit COMMIT would break the caller's transaction.
    c.execute('DROP TABLE IF EXISTS search_thai')
    c.execute('CREATE VIRTUAL TABLE search_thai USING fts5(title,body,surface)')
    last_id = -1
    while True:
        rows = c.execute(
            "SELECT id,path,text FROM chunks WHERE id>? AND (path GLOB '*[ก-๛]*' OR text GLOB '*[ก-๛]*') ORDER BY id LIMIT 128",
            (last_id,)).fetchall()
        if not rows:
            break
        for row in rows:
            add_chunk(c, row['id'], row['path'], row['text'])
        last_id = rows[-1]['id']
    mark_current(c)
    return True


def surface_tokens(text):
    runs = THAI.findall(unicodedata.normalize('NFC', str(text or '')))
    return ' br '.join(' '.join('u%04x'%ord(ch) for ch in run) for run in runs)


def candidates(c, query_terms, allowed):
    """Apply authorized path scope BEFORE ranking and LIMIT."""
    if not query_terms or not allowed:
        return []
    parts = []
    for term in dict.fromkeys(query_terms):
        parts.append('"' + term + '"')
        word = bytes.fromhex(term[2:]).decode('utf8')
        parts.append('surface:"' + surface_tokens(word) + '"')
    expression = ' OR '.join(parts)
    paths, found = sorted(allowed), []
    # Bind the full authorized set once: repeated 500-path MATCH scans are
    # disproportionately slow for broad terms in Thai-heavy packages. The
    # membership filter still precedes ORDER/LIMIT, and query_only stays ON.
    sql = '''SELECT c.id,bm25(search_thai,2.0,1.0,0.35) rank
      FROM search_thai JOIN chunks c ON c.id=search_thai.rowid
      WHERE search_thai MATCH ? AND c.path IN (SELECT value FROM json_each(?))
      ORDER BY rank,c.id LIMIT 150'''
    try:
        return [(r['id'], r['rank']) for r in c.execute(sql, [expression, json.dumps(paths, ensure_ascii=False)])]
    except sqlite3.OperationalError as exc:
        if 'no such table: json_each' not in str(exc) and 'no such function: json_each' not in str(exc):
            raise
        # Older/custom SQLite builds retain correct global top-150 semantics.
    for start in range(0, len(paths), 500):
        batch = paths[start:start + 500]
        sql = '''SELECT c.id,bm25(search_thai,2.0,1.0,0.35) rank
          FROM search_thai JOIN chunks c ON c.id=search_thai.rowid
          WHERE search_thai MATCH ? AND c.path IN ({}) ORDER BY rank,c.id LIMIT 150'''.format(','.join('?' for _ in batch))
        found.extend((r['id'], r['rank']) for r in c.execute(sql, [expression] + batch))
    return sorted(found, key=lambda pair: (pair[1], pair[0]))[:150]

"""Synthetic pipeline smoke test. Run from sjfx repo root with .venv/bin/python.
No production DB/mail, no generation calls. All writes are inside TemporaryDirectory.
"""
import os, sys, time, json, sqlite3, tempfile
from pathlib import Path
from contextlib import contextmanager
from email.message import EmailMessage
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import numpy as np

sys.path.insert(0, str(Path.cwd()))
os.environ.setdefault('SJFX_EMBED_MODEL_PATH', '/home/zhoulingjie/models/multilingual-e5-small')
from config import Config
from services.mail.repository import MailRepository
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.semantic import embedding_client, embed_segments_batch, vector_search, model_key, passage_text
from services.mail.retrieval import hybrid_search

class TempStorage:
    def __init__(self, path): self.path = path
    @contextmanager
    def _connect(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        try:
            with c: yield c
        finally: c.close()

def check_vectors(values, count):
    a = np.asarray(values, dtype=np.float32)
    assert a.shape == (count, 384), a.shape
    assert np.isfinite(a).all()
    assert np.allclose(np.linalg.norm(a, axis=1), 1, atol=.01)

settings = dict(MAIL_ENABLE_VECTOR_INDEX=True, ENABLE_VLLM_EMBEDDINGS=True,
                MAIL_EMBED_PAUSED=False, MAIL_ENABLE_AGENT_MINING=False,
                MAIL_EMBED_BASE_URL=os.environ.get('SMOKE_EMBED_URL', 'http://127.0.0.1:18083/v1'),
                MAIL_EMBED_MODEL='multilingual-e5-small', MAIL_EMBED_BATCH=8)
report = {}
with patch.multiple(Config, **settings), tempfile.TemporaryDirectory(prefix='sjfx-embed-smoke-') as td:
    root = Path(td)
    repo = MailRepository(TempStorage(root/'synthetic.sqlite3'))
    repo.migrate()
    package, _ = repo.create_package(owner_id='synthetic-owner', display_name='Synthetic smoke only',
        source_kind='directory', source_path=td, source_revision='synthetic-v1', config={})
    pid = package['package_id']
    corpus = [('Synthetic scholarship', 'Scholarship applications close on 15 October 2026. Submit the application form.'),
              ('ประกาศทุนสมมติ', 'กำหนดส่งใบสมัครทุนการศึกษาวันที่ 15 ตุลาคม 2569'),
              ('虚构午餐通知', '这是一封虚构邮件，周五午餐在餐厅举行，请提前预约。'),
              ('虚构奖学金通知', '虚构奖学金申请截止日期为2026年10月15日，请提交申请表。')]
    for n, (subject, body) in enumerate(corpus):
        m = EmailMessage(); m['From']='sender@example.invalid'; m['To']='reader@example.invalid'
        m['Subject']=subject; m['Message-ID']=f'<synthetic-{n}@example.invalid>'
        m.set_content(body); raw=m.as_bytes(); path=root/f'synthetic-{n}.eml'; path.write_bytes(raw)
        repo.upsert_ingested_message(package_id=pid,
            source={'locator_type':'directory', 'root_path':td, 'source_revision':'synthetic-v1'},
            member=MailMember(path.name,n,len(raw),{'kind':'file','path':str(path)}), raw=raw, parsed=parse_eml(raw))
    start=time.monotonic()
    for _ in range(10):
        status=embed_segments_batch(repo,pid,'synthetic-owner')
        assert not status.get('paused')
        if status['pending']==0: break
        assert status['processed']>0
    assert status['status']=='ready' and status['ready']==status['total']==8, status
    report['index']={'seconds':round(time.monotonic()-start,3), **status}
    again=embed_segments_batch(repo,pid,'synthetic-owner')
    assert again['processed']==again['encoded']==0, again
    with repo._connection() as c:
        rows=c.execute('SELECT dim, vector FROM mail_embeddings WHERE package_id=? AND model=?',
                       (pid,model_key(embedding_client()))).fetchall()
        assert len(rows)==8 and all(r['dim']==384 for r in rows)
        check_vectors([np.frombuffer(r['vector'],dtype=np.float32) for r in rows],8)
        assert c.execute('SELECT COUNT(*) FROM mail_embedding_buckets WHERE package_id=?',(pid,)).fetchone()[0]==48
    query='奖学金申请截止日期'
    items, meta=vector_search(repo,pid,'synthetic-owner',query,limit=8)
    assert meta['status']=='ready' and len(items)==8
    assert all(np.isfinite(x['vector_score']) for x in items)
    fused=hybrid_search(repo,pid,'synthetic-owner',query,limit=8)
    assert fused['retrieval']['vector_index']['status']=='ready', fused['retrieval']
    assert fused['retrieval']['vector']==8 and fused['items']
    assert any('vector' in x.get('retrieval',{}) for x in fused['items'])
    assert all(x.get('evidence_id') and x.get('quote_text') for x in fused['items'])
    report['retrieval']=fused['retrieval']
    # Report observed scores; do not assert a cross-language ranking or accuracy claim.
    report['observed_ranking']=[{'subject':x['subject_raw'],'kind':x['source_kind'],
                              'score':round(x['vector_score'],4)} for x in items]
    def batch_smoke(n):
        start=time.monotonic()
        values=embedding_client().embed([passage_text(f'Synthetic request {n}, sample {i}. Scholarship deadline 15 October.') for i in range(8)])
        elapsed=time.monotonic()-start; check_vectors(values,8)
        assert elapsed<12, f'batch {n} exceeded 12s: {elapsed:.3f}'
        return round(elapsed,3)
    with ThreadPoolExecutor(max_workers=3) as pool:
        report['concurrent_batch8_seconds']=list(pool.map(batch_smoke,range(3)))
report['temporary_files_cleaned']=not root.exists()
assert report['temporary_files_cleaned']
print(json.dumps(report,ensure_ascii=False,indent=2))


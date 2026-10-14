"""Changed-path feature extraction, transactional document frequencies, round snapshots."""
from services.large_package_protection import package_lane
from sjfx_protection.store import ProtectionError
import json
import threading
from collections import Counter
from contextlib import closing
from services.large_package_evidence import FEATURE_VERSION, make_card

_LOCK=threading.RLock()
_BACKGROUND={}

def init(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS lp_naming_features(package_id TEXT NOT NULL,path TEXT NOT NULL,version TEXT NOT NULL,card TEXT NOT NULL,PRIMARY KEY(package_id,path));
    CREATE TABLE IF NOT EXISTS lp_naming_feature_state(package_id TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS lp_feature_dirty(package_id TEXT NOT NULL,path TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(package_id,path));
    CREATE TABLE IF NOT EXISTS lp_feature_meta(package_id TEXT PRIMARY KEY,version TEXT NOT NULL,files INTEGER NOT NULL DEFAULT 0,readable INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS lp_feature_df(package_id TEXT NOT NULL,term TEXT NOT NULL,n INTEGER NOT NULL,PRIMARY KEY(package_id,term));
    CREATE TABLE IF NOT EXISTS lp_feature_snapshot(package_id TEXT PRIMARY KEY,round_id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL);
    DROP TRIGGER IF EXISTS lp_naming_source_insert_v2;
    DROP TRIGGER IF EXISTS lp_naming_source_delete_v2;
    DROP TRIGGER IF EXISTS lp_naming_source_update_v2;
    DROP TRIGGER IF EXISTS lp_naming_package_delete_v2;
    CREATE TRIGGER IF NOT EXISTS lp_naming_source_insert_v3 AFTER INSERT ON files BEGIN
      INSERT INTO lp_feature_dirty VALUES(NEW.package_id,NEW.path,1) ON CONFLICT(package_id,path) DO UPDATE SET generation=generation+1;
      INSERT INTO lp_naming_feature_state VALUES(NEW.package_id,1) ON CONFLICT(package_id) DO UPDATE SET revision=revision+1; END;
    CREATE TRIGGER IF NOT EXISTS lp_naming_source_delete_v3 AFTER DELETE ON files BEGIN
      INSERT INTO lp_feature_dirty VALUES(OLD.package_id,OLD.path,1) ON CONFLICT(package_id,path) DO UPDATE SET generation=generation+1;
      INSERT INTO lp_naming_feature_state VALUES(OLD.package_id,1) ON CONFLICT(package_id) DO UPDATE SET revision=revision+1; END;
    CREATE TRIGGER IF NOT EXISTS lp_naming_source_update_v3
    AFTER UPDATE OF local_summary,deep_summary,deep_status,sensitive,size,modified_at_ns ON files
    WHEN OLD.sensitive IS NOT NEW.sensitive OR OLD.size IS NOT NEW.size OR OLD.modified_at_ns IS NOT NEW.modified_at_ns
      OR OLD.deep_status IS NOT NEW.deep_status OR OLD.deep_summary IS NOT NEW.deep_summary
      OR json_extract(OLD.local_summary,'$.portrait.evidence') IS NOT json_extract(NEW.local_summary,'$.portrait.evidence')
      OR json_extract(OLD.local_summary,'$.portrait.fields') IS NOT json_extract(NEW.local_summary,'$.portrait.fields')
      OR json_extract(OLD.local_summary,'$.portrait.headings') IS NOT json_extract(NEW.local_summary,'$.portrait.headings')
      OR (json_extract(OLD.local_summary,'$.portrait.recognition_status')='restricted') IS NOT (json_extract(NEW.local_summary,'$.portrait.recognition_status')='restricted')
      OR json_extract(OLD.local_summary,'$.sample') IS NOT json_extract(NEW.local_summary,'$.sample')
    BEGIN
      INSERT INTO lp_feature_dirty VALUES(NEW.package_id,NEW.path,1) ON CONFLICT(package_id,path) DO UPDATE SET generation=generation+1;
      INSERT INTO lp_naming_feature_state VALUES(NEW.package_id,1) ON CONFLICT(package_id) DO UPDATE SET revision=revision+1;
      UPDATE large_catalog_groups SET content_revision=content_revision+1 WHERE package_id=NEW.package_id AND id IN
        (SELECT group_id FROM large_catalog_members WHERE package_id=NEW.package_id AND path=NEW.path); END;
    CREATE TRIGGER IF NOT EXISTS lp_naming_package_delete_v3 AFTER DELETE ON packages BEGIN
      DELETE FROM lp_naming_features WHERE package_id=OLD.id;
      DELETE FROM lp_naming_feature_state WHERE package_id=OLD.id;
      DELETE FROM lp_feature_dirty WHERE package_id=OLD.id;
      DELETE FROM lp_feature_meta WHERE package_id=OLD.id;
      DELETE FROM lp_feature_df WHERE package_id=OLD.id;
      DELETE FROM lp_feature_snapshot WHERE package_id=OLD.id; END;
    ''')

@package_lane
def identity(store,package_id):
    return str(store.db_path),str(package_id),FEATURE_VERSION

@package_lane
def revision(store,package_id):
    with closing(store._connect()) as c:
        row=c.execute('SELECT revision FROM lp_naming_feature_state WHERE package_id=?',(package_id,)).fetchone()
    return int(row[0]) if row else 0

@package_lane
def _seed(store,package_id):
    with store._lock,closing(store._connect()) as c:
        row=c.execute('SELECT version FROM lp_feature_meta WHERE package_id=?',(package_id,)).fetchone()
        if row and row[0]==FEATURE_VERSION:return
        c.execute('BEGIN IMMEDIATE')
        try:
            c.execute('DELETE FROM lp_feature_df WHERE package_id=?',(package_id,))
            c.execute('DELETE FROM lp_naming_features WHERE package_id=?',(package_id,))
            c.execute('DELETE FROM lp_feature_snapshot WHERE package_id=?',(package_id,))
            c.execute('INSERT INTO lp_feature_dirty SELECT package_id,path,1 FROM files WHERE package_id=? '
                      'ON CONFLICT(package_id,path) DO UPDATE SET generation=generation+1',(package_id,))
            c.execute('INSERT OR REPLACE INTO lp_feature_meta VALUES(?,?,0,0,0)',(package_id,FEATURE_VERSION))
            c.commit()
        except ProtectionError:
            raise
        except Exception:c.rollback();raise

@package_lane
def ensure_package(store,package_id,max_batches=None):
    with _LOCK:
        _seed(store,package_id)
        cursor='';batch_no=0
        while max_batches is None or batch_no<max_batches:
            with closing(store._connect()) as c:
                rows=c.execute('''SELECT d.path,d.generation,f.local_summary,f.deep_summary,f.deep_status,f.sensitive,
                  f.updated_at,e.card old_card FROM lp_feature_dirty d LEFT JOIN files f ON f.package_id=d.package_id AND f.path=d.path
                  LEFT JOIN lp_naming_features e ON e.package_id=d.package_id AND e.path=d.path
                  WHERE d.package_id=? AND d.path>? ORDER BY d.path LIMIT 128''',(package_id,cursor)).fetchall()
            if not rows:break
            computed=[]
            for raw in rows:
                value=dict(raw);card=None
                if raw['updated_at'] is not None:
                    value['local_summary']=json.loads(raw['local_summary'] or '{}')
                    value['deep_summary']=json.loads(raw['deep_summary'] or '{}')
                    card=make_card(value)
                computed.append((raw,card))
            with store._lock,closing(store._connect()) as c:
                c.execute('BEGIN IMMEDIATE')
                try:
                    if not c.execute('SELECT 1 FROM packages WHERE id=?',(package_id,)).fetchone():
                        c.rollback();return 0
                    delta=Counter();files_delta=read_delta=changed=0
                    for raw,card in computed:
                        live=c.execute('SELECT generation FROM lp_feature_dirty WHERE package_id=? AND path=?',(package_id,raw['path'])).fetchone()
                        if not live or live[0]!=raw['generation']:continue
                        source=c.execute('SELECT local_summary,deep_summary,deep_status,sensitive FROM files WHERE package_id=? AND path=?',
                                         (package_id,raw['path'])).fetchone()
                        expected=tuple(raw[k] for k in ('local_summary','deep_summary','deep_status','sensitive'))
                        if (source is None)!=(card is None) or (source is not None and tuple(source)!=expected):continue
                        old=json.loads(raw['old_card']) if raw['old_card'] else None
                        if old:
                            files_delta-=1
                            if old.get('readable'):read_delta-=1;delta.subtract(set(old.get('terms') or []))
                        if card:
                            files_delta+=1
                            if card.get('readable'):read_delta+=1;delta.update(set(card.get('terms') or []))
                            c.execute('INSERT OR REPLACE INTO lp_naming_features VALUES(?,?,?,?)',
                                      (package_id,raw['path'],FEATURE_VERSION,json.dumps(card,ensure_ascii=False,separators=(',',':'))))
                        else:c.execute('DELETE FROM lp_naming_features WHERE package_id=? AND path=?',(package_id,raw['path']))
                        c.execute('DELETE FROM lp_feature_dirty WHERE package_id=? AND path=? AND generation=?',(package_id,raw['path'],raw['generation']))
                        changed+=1
                    c.executemany('INSERT INTO lp_feature_df VALUES(?,?,?) ON CONFLICT(package_id,term) DO UPDATE SET n=n+excluded.n',
                                  [(package_id,t,n) for t,n in delta.items() if n])
                    c.executemany('DELETE FROM lp_feature_df WHERE package_id=? AND term=? AND n<=0',
                                  [(package_id,t) for t,n in delta.items() if n<0])
                    c.execute('UPDATE lp_feature_meta SET files=files+?,readable=readable+?,revision=revision+? WHERE package_id=?',
                              (files_delta,read_delta,changed,package_id))
                    c.commit()
                except ProtectionError:
                    raise
                except Exception:c.rollback();raise
            cursor=rows[-1]['path'];batch_no+=1
        return revision(store,package_id)

@package_lane
def group_cards(store,package_id,group_id):
    ensure_package(store,package_id)
    with closing(store._connect()) as c:
        rows=c.execute('''SELECT m.path,CASE WHEN d.path IS NULL THEN e.card END card FROM large_catalog_members m
          LEFT JOIN lp_naming_features e ON e.package_id=m.package_id AND e.path=m.path AND e.version=?
          LEFT JOIN lp_feature_dirty d ON d.package_id=m.package_id AND d.path=m.path
          WHERE m.package_id=? AND m.group_id=? ORDER BY m.path''',(FEATURE_VERSION,package_id,group_id)).fetchall()
    return [(r['path'],json.loads(r['card']) if r['card'] else None) for r in rows]

@package_lane
def snapshot_key(store,package_id):
    with closing(store._connect()) as c:
        row=c.execute('SELECT id FROM lp_catalog_rounds WHERE package_id=? ORDER BY started DESC LIMIT 1',(package_id,)).fetchone()
    return row[0] if row else 'initial'

@package_lane
def _meta_df(store,package_id):
    """包级**元数据**文档频次（headings/fields），供 profile.meta_templates 判版式。

    修补 3a：这一项此前恒为空 dict，而 profile.meta_templates 的判据是
    `meta_df[item] >= minimum and df[item] < minimum`。meta_df 为空时它退化成
    `background.get('meta_df') or background.get('df')` —— 两个数是同一个对象，
    **永远不可能同时满足**，于是"模型看到的 headings/fields 是否该被当版式剔除"
    这条判断从来没生效过。这里按卡片的 headings/fields 真实统计一份。
    代价实测约 0.10s / 8182 张卡，且随包级轮次快照一起缓存（每包每轮一次）。
    """
    df={}
    with closing(store._connect()) as c:
        rows=c.execute('SELECT card FROM lp_naming_features WHERE package_id=? AND version=?',
                       (package_id,FEATURE_VERSION)).fetchall()
    for row in rows:
        try:
            card=json.loads(row['card'] or '{}')
        except (TypeError,ValueError):
            continue
        for key in ('headings','fields'):
            for item in set(card.get(key) or []):
                item=str(item or '').strip()
                if 2<=len(item)<=40:
                    df[item]=df.get(item,0)+1
    return df


@package_lane
def background(store,package_id):
    ensure_package(store,package_id)
    key=identity(store,package_id)+(snapshot_key(store,package_id),)
    with _LOCK:
        if key in _BACKGROUND:return _BACKGROUND[key]
        with store._lock,closing(store._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT round_id,data FROM lp_feature_snapshot WHERE package_id=?',(package_id,)).fetchone()
            if row and row['round_id']==key[-1]:value=json.loads(row['data'])
            else:
                meta=c.execute('SELECT * FROM lp_feature_meta WHERE package_id=?',(package_id,)).fetchone()
                value={'files':meta['readable'],'feature_files':meta['files'],
                       'df':dict(c.execute('SELECT term,n FROM lp_feature_df WHERE package_id=?',(package_id,))),
                       'meta_df':_meta_df(store,package_id),
                       'scope':'retained_features_snapshot','revision':meta['revision']}
                c.execute('INSERT OR REPLACE INTO lp_feature_snapshot VALUES(?,?,?,?)',
                          (package_id,key[-1],meta['revision'],json.dumps(value,ensure_ascii=False,separators=(',',':'))))
            c.commit()
        if len(_BACKGROUND)>=8:_BACKGROUND.clear()
        _BACKGROUND[key]=value
        return value

def clear_cache():
    with _LOCK:_BACKGROUND.clear()

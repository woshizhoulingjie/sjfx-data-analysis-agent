"""Transaction-coupled per-package tokens; missing tracking means full scan."""
from __future__ import annotations

EPOCH = 'lp_qa_source_epoch'
REVISIONS = 'lp_qa_source_revisions'
PREFIX = 'lp_qa_change_'
FIELDS = {
    'packages': ('id', 'root_path', 'owner_id'),
    'files': ('package_id', 'path', 'name', 'category', 'size', 'modified_at_ns',
              'sha256', 'sensitive', 'local_summary', 'deep_summary'),
    'large_catalog_members': ('package_id', 'path', 'group_id', 'vector'),
    'large_catalog_groups': ('package_id', 'id', 'model'),
    'large_catalog_centers': ('package_id', 'model'),
    'lp_catalog_labels': ('package_id', 'group_id', 'name'),
}
OPTIONAL_FIELDS = {'files': ('source_kind', 'source_metadata', 'source_changed')}


def _fields(conn=None):
    fields = dict(FIELDS)
    if conn is not None:
        for table, optional in OPTIONAL_FIELDS.items():
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            fields[table] += tuple(k for k in optional if k in columns)
    return fields


def _bump(value, condition='1'):
    return (f'INSERT INTO {REVISIONS}(package_id,token) SELECT '
            f'{value},lower(hex(randomblob(16))) WHERE {condition} ON CONFLICT(package_id) '
            'DO UPDATE SET token=excluded.token;')


def definitions(conn=None):
    result = {}
    for table, fields in _fields(conn).items():
        key = 'id' if table == 'packages' else 'package_id'
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            name = PREFIX + table + '_' + event.lower()
            action, condition = event, ''
            if event == 'UPDATE':
                action += ' OF ' + ','.join(fields)
                condition = ' WHEN ' + ' OR '.join(f'OLD.{k} IS NOT NEW.{k}' for k in fields)
            sides = ('OLD', 'NEW') if event == 'UPDATE' else (('OLD',) if event == 'DELETE' else ('NEW',))
            body = ''.join(_bump(f'{side}.{key}',
                f'OLD.{key} IS NOT NEW.{key}' if event == 'UPDATE' and side == 'NEW' else '1')
                for side in sides)
            result[name] = f'CREATE TRIGGER {name} AFTER {action} ON {table}{condition} BEGIN {body} END'
    return result


def _safe_schema(conn):
    # UPDATE OF silently accepts missing columns. Check them explicitly.
    # REPLACE need not fire DELETE triggers: all unique keys must be package
    # local so that its INSERT trigger also invalidates the deleted package.
    for table, fields in FIELDS.items():
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        if not set(fields).issubset(columns):
            return False
        key = 'id' if table == 'packages' else 'package_id'
        for idx in conn.execute(f'PRAGMA index_list({table})'):
            if idx[2]:
                name = idx[1].replace(chr(34), chr(34)*2)
                keys = list(conn.execute(f'PRAGMA index_xinfo("{name}")'))
                if not any(r[2] == key and r[4] == 'BINARY' and r[5] for r in keys):
                    return False
    return True


def _actual(conn):
    return dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name GLOB ?",
        (PREFIX + '*',)))


def install(conn):
    """Atomic, idempotent setup, only from the writable store initializer."""
    if not _safe_schema(conn):
        return False  # Optional acceleration must not prevent the store opening.
    conn.execute('SAVEPOINT qa_change_install')
    try:
        conn.execute(f'CREATE TABLE IF NOT EXISTS {EPOCH}(id INTEGER PRIMARY KEY CHECK(id=1),epoch TEXT NOT NULL)')
        conn.execute(f'CREATE TABLE IF NOT EXISTS {REVISIONS}(package_id TEXT PRIMARY KEY,token TEXT NOT NULL)')
        expected, actual = definitions(conn), _actual(conn)
        if actual != expected:
            for name in actual:
                conn.execute('DROP TRIGGER "' + name.replace(chr(34), chr(34)*2) + '"')
            for sql in expected.values():
                conn.execute(sql)
            conn.execute(f'DELETE FROM {EPOCH}')
        conn.execute(f'INSERT OR IGNORE INTO {EPOCH} VALUES(1,lower(hex(randomblob(16))))')
        conn.execute('RELEASE qa_change_install')
        return True
    except BaseException:
        conn.execute('ROLLBACK TO qa_change_install')
        conn.execute('RELEASE qa_change_install')
        raise


def read_stamp(conn, package_id):
    """Same source snapshot as rows; resource/corruption errors propagate."""
    if not conn.in_transaction:
        raise ValueError('A source snapshot transaction is required')
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)", (EPOCH, REVISIONS))}
    if len(tables) != 2 or _actual(conn) != definitions(conn) or not _safe_schema(conn):
        return None
    epoch = conn.execute(f'SELECT epoch FROM {EPOCH} WHERE id=1').fetchone()
    if not epoch:
        return None
    row = conn.execute(f'SELECT token FROM {REVISIONS} WHERE package_id=?', (package_id,)).fetchone()
    # schema_version catches a trigger dropped and recreated after missed writes.
    return {'epoch': epoch[0], 'token': row[0] if row else None,
            'schema': conn.execute('PRAGMA schema_version').fetchone()[0]}

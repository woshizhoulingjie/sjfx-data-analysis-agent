"""Connection-local scope membership without JSON1 or variable-count limits."""
from __future__ import annotations


def scope_clause(connection, allowed_keys, column):
    if allowed_keys is None:
        return ''
    if column not in {'s.message_key', 'l.message_key'}:
        raise ValueError('Unsupported internal scope column')
    connection.execute('CREATE TEMP TABLE IF NOT EXISTS mail_research_scope_keys(message_key TEXT PRIMARY KEY)')
    connection.execute('DELETE FROM temp.mail_research_scope_keys')
    connection.executemany('INSERT OR IGNORE INTO temp.mail_research_scope_keys(message_key) VALUES(?)',
                           [(str(key),) for key in allowed_keys])
    return ' AND '+column+' IN (SELECT message_key FROM temp.mail_research_scope_keys)'

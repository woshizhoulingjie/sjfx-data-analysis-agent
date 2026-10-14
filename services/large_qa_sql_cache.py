"""Authenticate QA body rows and rebuild lexical search from authenticated chunks."""
from sjfx_protection import business
from sjfx_protection.sql_cache import GuardedConnection,_quote
from sjfx_protection.store import ProtectionError
SPECS={
 'documents':{'content':('summary','vector','topic_name'),'identity':('path','source_version','size','mtime','source_kind','source_metadata')},
 'chunks':{'content':('text','location'),'identity':('path','version','kind','ordinal')},
 'topic_nodes':{'content':('text','paths','children'),'identity':('id','version','topic','level')},
 'map_cache':{'content':('payload',),'identity':('key',)},
 'search':{'content':('title','body'),'fts':('title','body')},
 'search_thai':{'content':('title','body','surface'),'fts':('title','body','surface')},
 'meta':{'content':('value',),'identity':('key',),'metadata':lambda db,row:row.get('key')!='package'},
}

class QAConnection(GuardedConnection):
    def __init__(self,db,index):
        self.index=index
        super().__init__(db,index.path,SPECS,self._scope)
    def _scope(self,db,table,row):
        ctx=business.context() or {}
        return [{'owner':ctx['owner'],'scope':self.index.package_id}] if ctx.get('owner') and ctx.get('registration_id') else []
    def _row_ok(self,table,rowid):
        try:return super()._row_ok(table,rowid)
        except ProtectionError as exc:
            if table=='map_cache' and exc.code in {'cache_provenance_missing','cache_output_mismatch','context_stale'}:
                self.callback_error=None;return 0
            raise
    def _prepare_fts(self,table):
        # Contentless legacy FTS has no readable body. Its postings are never proof.
        # Reconstruct current postings solely from hash-checked documents/chunks.
        spec=self.specs[table];target=_quote(spec['view'])
        self.raw.execute('CREATE VIRTUAL TABLE IF NOT EXISTS temp.'+target+' USING fts5('+','.join(spec['fts'])+')')
        self.raw.execute('DELETE FROM temp.'+target)
        from services.large_qa_index import tokens
        from services.large_qa_thai import thai_tokens,surface_tokens
        cursor=self.raw.execute('SELECT rowid AS __rowid__,* FROM main.chunks ORDER BY id')
        for raw in cursor:
            row=dict(raw)
            if not self._validate('chunks',row):continue
            doc=self._row('documents',self.raw.execute('SELECT rowid FROM main.documents WHERE path=?',(row['path'],)).fetchone()[0])
            if not self._validate('documents',doc):continue
            title=(doc.get('name') or '')+' '+row['path'];body=row['text']
            if table=='search':
                values=[' '.join(tokens(title)),' '.join(tokens(body))]
            else:
                values=[' '.join(thai_tokens(title)),' '.join(thai_tokens(body)),surface_tokens(title+' '+body)]
            self.raw.execute('INSERT INTO temp.'+target+'(rowid,'+','.join(column.split()[0] for column in spec['fts'])+') VALUES('+','.join('?' for _ in range(len(values)+1))+')',[row['id']]+values)

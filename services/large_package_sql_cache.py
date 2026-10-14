"""Row integrity for large-package summaries, model caches and catalog payloads."""
from pathlib import Path
from sjfx_protection import business
from sjfx_protection.sql_cache import GuardedConnection
from sjfx_protection.store import ProtectionError
CONTENT={
 'files':('local_summary','deep_summary','category','confidence','topic','document_role'),
 'large_catalog_names':('result',),'large_catalog_embeddings':('vector',),
 'large_catalog_groups':('anchor','result','representative'),
 'large_catalog_members':('vector','input_hash'),
 'large_catalog_centers':('center','threshold'),
 'large_catalog_km_state':('signature',),
 'lp_catalog_labels':('name','description','evidence','payload'),
 'lp_catalog_label_history':('payload',),'lp_catalog_cache':('result',),
 'lp_catalog_tree':('payload',),'lp_catalog_branches':('name','description','children','payload'),
 'lp_catalog_versions':('payload',),'lp_evidence_snapshots':('payload',),
 'lp_overview_versions':('payload',),
 'lp_naming_features':('card',),'lp_feature_snapshot':('data',),'lp_feature_df':('term','n'),
 'portrait_groups':('label','terms','basis'), 'portrait_group_terms':('term',),
 'portrait_facets':('value',), 'lp_runs':('result',),
}

class LargeScopes:
    def __init__(self):self.memo={}
    def __call__(self,db,table,row):
        ctx=business.context() or {};contexts=ctx.get('package_contexts') or [ctx]
        if not ctx.get('owner'):return []
        if row.get('owner_id') and row['owner_id']!=ctx['owner']:return []
        pid=row.get('package_id')
        if not pid:
            return [{'owner':ctx['owner'],'scope':value['registration_id']} for value in contexts if value.get('registration_id')]
        if pid not in self.memo:
            item=db.execute('SELECT owner_id,root_path FROM packages WHERE id=?',(pid,)).fetchone()
            if not item or item['owner_id']!=ctx['owner']:self.memo[pid]=False
            else:
                registration=business.protection().package.resolve(ctx['owner'],root=item['root_path'])
                self.memo[pid]=any(value.get('registration_id')==registration['id'] for value in contexts)
        if not self.memo[pid]:return []
        # Physical rows must also stay within this task's deepest registered package.
        if table=='files' and row.get('path'):
            item=db.execute('SELECT root_path FROM packages WHERE id=?',(pid,)).fetchone()
            actual=Path(item[0])/str(row['path']).split('::',1)[0]
            registration=business.protection().package.resolve(ctx['owner'],root=str(actual))
            if not any(value.get('registration_id')==registration['id'] for value in contexts):return []
        return [{'owner':ctx['owner'],'scope':str(pid)}]

class LargeGuardedConnection(GuardedConnection):
    def _row_ok(self,table,rowid):
        try:return super()._row_ok(table,rowid)
        except ProtectionError as exc:
            # An optimization cache may miss and be recomputed from checked inputs.
            # It never returns historical/mutated bytes as current evidence.
            if table in {'large_catalog_names','large_catalog_embeddings','lp_catalog_cache'} and exc.code in {
                    'cache_provenance_missing','cache_output_mismatch','context_stale','cached_version_unverified'}:
                self.callback_error=None
                return 0
            raise

    def execute(self,sql,parameters=()):
        result=super().execute(sql,parameters)
        # Large DB historically uses autocommit outside explicit short transactions.
        # Authenticate completed autocommit writes before another content consumer runs.
        if not self.raw.in_transaction and self.raw.execute('SELECT 1 FROM '+self._journal+' LIMIT 1').fetchone():
            self.commit()
        return result
    def __enter__(self):return self
    def __exit__(self,kind,value,traceback):
        try:
            if kind is None:self.commit()
            else:self.rollback()
        finally:self.close()
        return False
    def close(self):
        try:
            if self.raw.in_transaction:self.rollback()
            self.cleanup()
        finally:self.raw.close()


def protect_connection(db,namespace):
    if not business.enabled():return db
    specs={name:{'content':content,'identity':('package_id','owner_id','path','model','digest','source_kind','source_metadata','protection_context')} for name,content in CONTENT.items()}
    return LargeGuardedConnection(db,namespace,specs,LargeScopes())

"""A reviewed metadata projection for ungated control/list endpoints."""
import re
from urllib.parse import parse_qs

LIST_ROUTES={'/api/jobs','/api/exports','/api/data-sources','/api/large-packages/history',
             '/api/mail/packages','/api/mail/workspace-guide'}
CONTROL_ROUTE=re.compile(r'^/api/(?:jobs|turns|package-processing|large-packages|mail/research-turns)/[^/]+/(?:cancel|pause)$')
STATUS_ROUTE=re.compile(r'^/api/(?:jobs|turns|large-packages|mail/research-turns)/[^/]+(?:/(?:status|progress|events|workflow))?$')

def is_metadata_route(path,method,query=b''):
    if method=='POST':return bool(CONTROL_ROUTE.fullmatch(path))
    if method!='GET':return False
    if path in LIST_ROUTES:return True
    params=parse_qs(query.decode('utf-8','replace') if isinstance(query,bytes) else query)
    return params.get('protection_metadata')==['1'] and bool(STATUS_ROUTE.fullmatch(path))
SCALARS={'ok','code','id','job_id','scan_id','package_id','turn_id','owner_id','status','state','phase',
    'stage','progress','created_at','updated_at','finished_at','server_time','total','offset','limit',
    'next_offset','cancel_requested','cancelled','paused','control_state','queue_position',
    'workflow_version','count','filename','file_name','size','available','configured',
    'source_kind','support_matrix','format','dependency','active','registration_required',
    'protection_required','sequence','next_sequence','after','total_files','processed_files',
    'discovered_files','previewed_files','deep_parsed_files','failed_files','file_count',
    'matched','pending','running','completed','failed','queued','cancelled','waiting',
    'message','error','blocking_code','blocking_reason','can_use','manifest_revision','registration_id',
    'files_complete','files_total','protection_stage','inspection_state'}
CONTAINERS={'job','jobs','items','package','packages','large_package','tasks','package_pause','data_sources',
    'sources','counts','support_matrix','history','pagination','snapshot','detail','turn','events',
    'workflow','queues'}
def sanitize_control(value,depth=0):
    if depth>5:return None
    if isinstance(value,list):return [sanitize_control(v,depth+1) for v in value[:500]]
    if not isinstance(value,dict):return value if isinstance(value,(str,int,float,bool,type(None))) else None
    result={}
    for key,item in value.items():
        if key in SCALARS and isinstance(item,(str,int,float,bool,type(None))):result[key]=item[:200] if isinstance(item,str) else item
        elif key in CONTAINERS and isinstance(item,(list,dict)):result[key]=sanitize_control(item,depth+1)
    return result

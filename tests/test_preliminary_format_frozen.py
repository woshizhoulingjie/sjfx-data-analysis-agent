"""Compare each actual model contract with the frozen 673-file source."""
import ast
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from services import document_analysis as doc
from services import preliminary_summary_fast as fast
from services import preliminary_summary_batch as batch
from services.folder_analysis import _bounded_node_inputs

BASE=Path(__file__).resolve().parent/'fixtures'/'preliminary_format_baseline'

def old_function(file, name, namespace):
    tree=ast.parse((BASE/file).read_text(encoding='utf-8'))
    fn=next(node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name==name)
    local=dict(namespace)
    exec(compile(ast.Module(body=[fn],type_ignores=[]),file,'exec'),local)
    return local[name]

def structure(value):
    if isinstance(value,dict):return {k:structure(v) for k,v in value.items()}
    if isinstance(value,list):return ['list']+[structure(value[0])] if value else ['list']
    return type(value).__name__

def contract(prompt, marker, tail=None):
    raw=prompt.split(marker,1)[1]
    if tail:raw=raw.split(tail,1)[0]
    return json.JSONDecoder().raw_decode(raw.strip())[0]

def test_fast_single_and_batch_field_order_types_lengths_are_frozen():
    original=old_function('preliminary_summary_fast.py','_prompt',vars(fast))
    row={'job':{'id':'x','options':{'path':'a.md'}},'input':'材料'}
    for rows in ([row],[row,dict(row,job={'id':'y','options':{'path':'b.md'}})]):
        before,after=original(rows),fast._prompt(rows)
        a,b=contract(before,'输出JSON：','\n分析材料：'),contract(after,'输出JSON：','\n分析材料：')
        assert a==b  # Ordered keys and literal list/length instructions retained.
        assert list(a['documents'][0])==list(b['documents'][0])
        for bound in ('约150字，最多180字','topics最多3个','key_facts最多3项','arguments和methodology各最多1项','conclusions最多2项','最多3个短章节名'):
            assert bound in before and bound in after

class Capture:
    def __init__(self):self.prompt=''
    def chat_json(self, system, prompt, **kwargs):
        self.prompt=prompt
        return {'json':{'title':'核对记录','core_summary':'材料记录了检查流程。','documents':[{'path':'a.md','title':'核对记录','core_summary':'材料记录了检查流程。','topics':[]}]},'model':'fixture','usage':{}}

def test_ordinary_single_and_candidate_batch_json_contracts_are_frozen():
    document={'text':'材料记录了检查流程。','source':{'path':'a.md'},'structure':{'title':'核对记录'},'evidence':[]}
    for name,args in [('analyze_document_preview',('a.md','a.md')),('analyze_document_previews_batch',([{'path':'a.md','document':document}],))]:
        old=old_function('document_analysis.py',name,vars(doc))
        m1,m2=Capture(),Capture()
        options={'unified_document':document} if name=='analyze_document_preview' else {}
        old(m1,*args,**options);getattr(doc,name)(m2,*args,**options)
        marker='输出 JSON：' if name=='analyze_document_preview' else '只输出 JSON：'
        c1,c2=contract(m1.prompt,marker),contract(m2.prompt,marker)
        assert c1==c2 and structure(c1)==structure(c2)
        if 'documents' not in c1:
            assert list(c1)==list(c2)

def test_real_summary_renderer_and_download_structure_source_unchanged():
    source=Path(__file__).resolve().parents[1]
    assert (source/'static/app.js').read_bytes()==(BASE/'app.js').read_bytes()
    snapshots=json.loads((BASE/'actual_api.json').read_text(encoding='utf-8'))
    assert snapshots and all(r['status']==200 and r['response'].get('ok') for r in snapshots)

def test_node_input_scope_stays_local_and_conditions_are_retained():
    local=[{'path':'local/a.md','title':'本节点资料','summary':'本节点对象与条件。','methodology':['仅在37℃测量'], 'uncertainties':['未验证其他条件']}]
    original=copy.deepcopy(local)
    rows,_,_=_bounded_node_inputs(local,[],[])
    assert rows[0]['methodology']==['仅在37℃测量']
    assert rows[0]['uncertainties']==['未验证其他条件']
    assert '全包首选' not in json.dumps(rows,ensure_ascii=False) and local==original

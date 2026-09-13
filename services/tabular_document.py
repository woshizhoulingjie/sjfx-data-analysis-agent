"""Bounded semantic projection of fully profiled delimited tables.

Statistics cover the profile's declared row range. Record excerpts are samples;
neither samples nor a source hash imply every narrative was model-reviewed.
"""
import csv
import hashlib
import json
import math
import re
from collections import defaultdict


_IDENTIFIER = re.compile(r"(^|_)(id|fips|index|year|yearmonth|day|time|lat|lon|latitude|longitude)($|_)", re.I)


def _numeric(value):
    try:
        result = float(str(value).strip().replace(',', ''))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _clean_number(value):
    return int(value) if float(value).is_integer() else round(value, 6)


def project_table(path, base, profile):
    """Reuse bounded profiling, then compute scoped groups with one CSV pass."""
    columns = profile.get('columns') or {}
    row_count = int(profile.get('row_count') or 0)
    complete = bool((profile.get('coverage') or {}).get('complete'))
    numeric = [n for n, c in columns.items() if c.get('inferred_type') == 'number' and not _IDENTIFIER.search(n)][:12]
    dimensions = [n for n,c in columns.items() if c.get('inferred_type') == 'text' and 1 < int(c.get('unique_count') or 0) <= 100
                  and max((len(str(x)) for x in c.get('sample_values') or []), default=0) <= 100][:8]
    groups = {d:defaultdict(lambda:{'rows':0,'sums':{n:0.0 for n in numeric},'numeric_counts':{n:0 for n in numeric}}) for d in dimensions}
    sample_indices = {1, row_count, max(1,(row_count+1)//2)} | {1 + i*max(0,row_count-1)//7 for i in range(8)}
    samples = {}; extremes = {}; processed = 0
    with open(path, encoding=(profile.get('limits') or {}).get('encoding') or 'utf-8-sig', newline='') as stream:
        reader=csv.DictReader(stream, delimiter='\t' if str(path).lower().endswith('.tsv') else ',')
        for index,row in enumerate(reader,1):
            if index>row_count: break
            processed=index
            values={n:_numeric(row.get(n)) for n in numeric}
            for d in dimensions:
                key=str(row.get(d) or '')
                if not key: continue
                target=groups[d][key];target['rows']+=1
                for n,v in values.items():
                    if v is not None:target['sums'][n]+=v;target['numeric_counts'][n]+=1
            if index in sample_indices:samples[index]=row
            for n,v in values.items():
                if v is not None and (n not in extremes or v>extremes[n][0]):extremes[n]=(v,index,row)
    for _,i,row in extremes.values(): samples[i]=row
    complete=complete and processed==row_count
    profile['grouped_statistics']={d:[{'value':v,'rows':x['rows'],'sums':{k:_clean_number(s) for k,s in x['sums'].items()},'numeric_counts':x['numeric_counts']} for v,x in sorted(items.items())] for d,items in groups.items()}
    profile['grouped_statistics_scope']={'records':processed,'complete':complete,'dimensions':dimensions,'measures':numeric,'missing_group_values_excluded':True,'metric_note':'sum applies to numeric values; identifiers and coordinates are excluded'}
    source=base['source']; evidence=[]; parts=[]
    def add(text,label,**locator):
        text=str(text)
        e={'evidence_id':'E-{}-{:05d}'.format(source['sha256'][:10],len(evidence)+1), 'source_path':source['path'],
           'source_sha256':source['sha256'],'content_sha256':hashlib.sha256(text.encode()).hexdigest(),'text':text,'label':label,
           'parser':'structured-table','table':'主数据表',**locator}
        evidence.append(e);return text
    scope='完整文件' if complete else '已读取的有界范围，不能代表文件全量'
    parts.append(add('结构化表统计：{}。文件 {}，{} 条数据记录，{} 列。以下数值来自程序逐行计算，记录摘录仅用于举例。'.format(scope,source['path'],row_count,len(columns)), 'table_statistics', row_range=[2,row_count+1],derivation={'operation':'count_rows','complete':complete}))
    schema=[{'name':n,'type':c.get('inferred_type'),'missing':c.get('missing_count')} for n,c in columns.items()]
    parts.append('字段定义与缺失数：'+json.dumps(schema,ensure_ascii=False,separators=(',',':')))
    for n in numeric:
        c=columns[n]
        parts.append(add('统计字段 {}：有效数值记录={}，缺失={}，合计={}，最小值={}，最大值={}。计算范围为{}，数据行2至{}。'.format(n,c.get('count'),c.get('missing_count'),c.get('sum'),c.get('min'),c.get('max'),scope,row_count+1), 'table_statistics',column=n,row_range=[2,row_count+1],derivation={'operation':'numeric_profile','field':n,'complete':complete}))
    for d,items in profile['grouped_statistics'].items():
        selected={x['value']:x for x in sorted(items,key=lambda x:-x['rows'])[:8]}
        for n in numeric:
            for x in sorted(items,key=lambda x:-x['sums'].get(n,0))[:3]:selected[x['value']]=x
        for x in selected.values():
            text='分组统计：{}={}；记录数={}；数值合计={}；有效数值数量={}。统计范围={}。'.format(d,x['value'],x['rows'],json.dumps(x['sums'],ensure_ascii=False,separators=(',',':')),json.dumps(x['numeric_counts'],ensure_ascii=False,separators=(',',':')),scope)
            parts.append(add(text,'table_statistics',column=d,row_range=[2,row_count+1],derivation={'operation':'group_count_sum','group':d,'value':x['value'],'complete':complete}))
    for index,row in sorted(samples.items()):
        # Keep identifiers, measures and actual narrative fields together.
        rendered={k:str(v or '')[:700] for k,v in row.items() if v not in (None,'')}
        text='数据记录 {}（抽样示例，不代表全体）：{}'.format(index,json.dumps(rendered,ensure_ascii=False,separators=(',',':')))
        parts.append(add(text,'table_record',record_index=index,row_range=[index+1,index+1],section='CSV 数据记录 {}'.format(index),sampled=True))
    base.update(text='\n\n'.join(parts), evidence=evidence,data_profile=profile)
    base['content_sha256']=hashlib.sha256(base['text'].encode()).hexdigest()
    base['parser']={'name':'structured-table','mode':'fast','requested_mode':(base.get('parser') or {}).get('requested_mode'), 'degraded':not complete,'ocr':False,'remote_services_enabled':False,'record_count':row_count}
    base['structure'].update(table_count=1,title=source.get('name') or source['path'])
    base['coverage'].update(complete=complete,coverage_ratio=1.0 if complete else None,structured_records_complete=complete,
        structured_record_count=row_count,stored_characters=len(base['text']),extracted_characters=len(base['text']),truncated_by_limit=not complete,
        semantic_projection=True,model_full_narrative_review=False,sampled_record_count=len(samples),full_table_statistics=complete,
        coverage_basis='parsed table rows and deterministic aggregates; model reads statistics and identified samples')
    base['warnings'].append('结构化统计覆盖{}；模型阅读字段统计和标明行号的记录示例，不代表逐条阅读全部事件叙述。'.format(scope))
    return base

_ADDITIVE = re.compile(r'(?:^|_)(?:deaths?|injuries|count|amount|quantity|total|revenue|cost|sales)(?:$|_)|数量|金额|人数|销售额', re.I)
_LABELS = {'DEATHS_DIRECT':'直接死亡人数','DEATHS_INDIRECT':'间接死亡人数',
           'INJURIES_DIRECT':'直接受伤人数','INJURIES_INDIRECT':'间接受伤人数'}


def grounded_table_summary(proposal, document):
    """Publish source-computed quantitative claims; retain model prose as unverified.

    Models can confound field labels or invent causes. No generated number or
    causal assertion is promoted to the table's factual summary here.
    """
    profile = document.get('data_profile') or {}
    cols = profile.get('columns') or {}
    count = int(profile.get('row_count') or 0)
    complete = bool((profile.get('coverage') or {}).get('complete'))
    facts = ['数据记录共 {} 条，字段 {} 个。'.format(count, len(cols))]
    measures = [n for n in cols if _ADDITIVE.search(n) and 'sum' in cols[n] and not _IDENTIFIER.search(n)]
    measures.sort(key=lambda n: (0 if 'DEATH' in n.upper() and 'DIRECT' in n.upper() and 'INDIRECT' not in n.upper() else 1 if 'DEATH' in n.upper() else 2,n))
    for name in measures[:6]:
        c = cols[name]
        facts.append('{}（{}）合计 {}；有效数值 {} 条，缺失 {} 条。'.format(
            _LABELS.get(name,name),name,_clean_number(c['sum']),c.get('count',0),c.get('missing_count',0)))
    groups = profile.get('grouped_statistics') or {}
    ranked_dimensions = sorted(groups, key=lambda n: (0 if n.upper()=='EVENT_TYPE' else 1 if re.search(r'type|category|group|类别',n,re.I) else 2,n))
    conclusions = []
    if ranked_dimensions:
        dim = ranked_dimensions[0]; rows = groups[dim]
        if rows:
            top = max(rows,key=lambda x:x['rows'])
            conclusions.append('按 {} 分组，{} 的记录最多，为 {} 条；未提供个体暴露量，不能解释为发生概率。'.format(dim,top['value'],top['rows']))
            for measure in measures[:2]:
                winners = sorted(rows,key=lambda x:-x['sums'].get(measure,0))
                if winners and winners[0]['numeric_counts'].get(measure,0):
                    win=winners[0]
                    conclusions.append('{} 合计最高的 {} 类别是 {}，值为 {}。'.format(_LABELS.get(measure,measure),dim,win['value'],_clean_number(win['sums'][measure])))
    if not measures:
        for dim in ranked_dimensions[:2]:
            for row in sorted(groups[dim],key=lambda x:-x['rows'])[:3]:
                facts.append('{}={} 的数据记录为 {} 条。'.format(dim,row['value'],row['rows']))
    title = str(proposal.get('title') or (document.get('source') or {}).get('name') or '结构化数据表')
    summary = dict(proposal)
    summary['unverified_model_analysis'] = {k:proposal.get(k) for k in ('core_summary','key_facts','arguments','conclusions')}
    scope = '以下统计覆盖全表。' if complete else '以下统计仅覆盖已读取范围，不能视为全表统计。'
    summary.update(title=title,core_summary=scope+' '.join(facts[:5]),summary=scope+' '.join(facts[:5]),
                   key_facts=facts,arguments=[],conclusions=conclusions,
                   methodology=['从原始CSV逐行计算；依据字段名分别统计，标识符不求和。','记录表与关联明细表不得相加或未经聚合直接连接后求和。'],
                   uncertainties=['记录摘录仅供举例，未逐条进行模型语义审阅。','事件记录计数不等于独立灾害次数、人口暴露量或个体风险。'],
                   warnings=list(document.get('warnings') or []),
                   numeric_summary_contract='source-computed-table/1.0',
                   summary_provenance={'factual_text':'deterministic_statistics','model_text':'retained_as_unverified_proposal','source_sha256':(document.get('source') or {}).get('sha256'),'complete_table_statistics':complete})
    # Keep citations to aggregates, never borrow a narrative as proof of a total.
    summary['evidence_chain'] = [e for e in document.get('evidence') or [] if e.get('label')=='table_statistics'][:12]
    return summary

def grounded_group_summary(proposal, documents):
    """Aggregate compatible table schemas separately, without a fan-out join."""
    from copy import deepcopy
    schemas = defaultdict(list)
    for path,document in documents.items():
        profile=document.get('data_profile') or {}
        schemas[tuple(sorted((profile.get('columns') or {}).keys()))].append((path,document))
    facts=[];conclusions=[];scopes=[]
    def importance(group):
        fields, members = group
        return (-sum(bool(_ADDITIVE.search(n)) for n in fields), -sum(int(d['data_profile'].get('row_count') or 0) for _,d in members))
    for number,(_,members) in enumerate(sorted(schemas.items(), key=importance),1):
        prototype=deepcopy(members[0][1]);profile=prototype['data_profile'];profile['row_count']=sum(int(d['data_profile'].get('row_count') or 0) for _,d in members)
        profile['coverage']['complete']=all((d['data_profile'].get('coverage') or {}).get('complete',False) for _,d in members)
        for field,column in profile['columns'].items():
            values=[d['data_profile']['columns'][field] for _,d in members]
            for key in ['sum','count','missing_count']:
                if key in column:column[key]=sum(v.get(key,0) for v in values)
        merged={}
        for _,d in members:
            for dimension,rows in (d['data_profile'].get('grouped_statistics') or {}).items():
                for row in rows:
                    key=(dimension,row['value']);item=merged.setdefault(key,{'value':row['value'],'rows':0,'sums':defaultdict(float),'numeric_counts':defaultdict(int)})
                    item['rows']+=row['rows']
                    for field,value in row['sums'].items():item['sums'][field]+=value
                    for field,value in row['numeric_counts'].items():item['numeric_counts'][field]+=value
        profile['grouped_statistics']={}
        for (dimension,_),row in merged.items():profile['grouped_statistics'].setdefault(dimension,[]).append(row)
        result=grounded_table_summary({},prototype)
        label='同结构表组 {}（{} 个文件）'.format(number,len(members))
        facts.extend(label+'：'+fact for fact in result['key_facts'][:7])
        conclusions.extend(label+'：'+x for x in result['conclusions'][:3])
        scopes.append({'group':number,'paths':[p for p,_ in members],'row_count':profile['row_count'],'columns':list(profile['columns']),'complete':profile['coverage']['complete']})
    summary=dict(proposal)
    summary['unverified_model_analysis']={k:proposal.get(k) for k in ('core_summary','key_facts','arguments','conclusions')}
    summary.update(core_summary='包含 {} 个结构不同的表组，分别统计，未连接后求和。'.format(len(schemas))+' '.join(facts[:8]),
                   summary='分别统计 {} 个同结构表组；各表组的数据记录不能相加为唯一事件或人数。'.format(len(schemas)),
                   key_facts=facts,arguments=[],conclusions=conclusions,
                   methodology=['仅对字段集合一致的表合并统计；按文件已有聚合值求和，没有将关联表连接后放大计数。'],
                   uncertainties=['所有数量指选定文件中的数据记录或指定字段合计；未逐条模型阅读原始叙述。'],
                   table_group_scopes=scopes,numeric_summary_contract='source-computed-table-groups/1.0',
                   summary_provenance={'factual_text':'deterministic_statistics','model_text':'retained_as_unverified_proposal'})
    summary['answer'] = summary['core_summary']
    summary['notable_items'] = facts[:8]
    summary['claims'] = []
    summary['file_conclusions'] = []
    summary['question'] = '不同表结构分别记录什么，哪些统计差异需要核查？'
    return summary


def table_research_direction(documents):
    """Recommend a reproducible comparison grounded in existing table groups."""
    candidates=[]
    for path,d in documents.items():
        profile=d.get('data_profile') or {}
        for dimension,rows in (profile.get('grouped_statistics') or {}).items():
            measures=[n for n in (profile.get('columns') or {}) if _ADDITIVE.search(n) and 'sum' in profile['columns'][n] and not _IDENTIFIER.search(n)]
            if rows and measures and (profile.get('coverage') or {}).get('complete'):
                score=(100 if dimension.upper()=='EVENT_TYPE' else 10 if re.search('type|category|group|类别',dimension,re.I) else 0)+len(rows)
                candidates.append((score,path,d,dimension,measures))
    if not candidates:return None
    _,path,doc,dimension,measures=max(candidates,key=lambda x:x[0])
    measures.sort(key=lambda n:(0 if 'DEATH' in n.upper() and 'INDIRECT' not in n.upper() else 1,n))
    measure=measures[0];label=_LABELS.get(measure,measure)
    members={p:d for p,d in documents.items() if set((d.get('data_profile') or {}).get('columns') or {})==set(doc['data_profile']['columns'])}
    stats=grounded_group_summary({},members)
    evidence=[]
    for d in members.values():
        source_evidence=[e for e in d.get('evidence') or [] if e.get('label')=='table_statistics' and e.get('column')==measure and (e.get('derivation') or {}).get('operation')=='numeric_profile']
        evidence.extend(source_evidence[:1])
    evidence=evidence[:8]
    return {'title':'比较类别记录频次与'+label+'分布','type':'推论','confidence':'中', 'priority':'高',
            'rationale':'所选同结构表已完成逐行统计，可比较 '+dimension+' 的记录频次与 '+measure+' 合计；数量差异用于研究线索，不能直接解释为个体风险或因果。',
            'research_questions':['记录最多的类别是否也是'+label+'最多的类别？','按年份和地区拆分后，差异是否仍然存在？','高值案例的原始记录及关联明细是否一致？'],
            'methods':['按同结构事件主表聚合，关联明细先按主键汇总，避免重复计数。','区分直接与间接指标，核对高值事件和未匹配主键。'],
            'representative_documents':list(members), 'evidence_chain':evidence,
            'basis':'基于完整表格统计与明确字段的可复算研究设计。',
            'limitations':['缺少人口暴露分母，不能计算个人风险。','记录示例不是全部叙述的语义审阅。'],
            'numeric_summary_contract':'table-research-design/1.0'}

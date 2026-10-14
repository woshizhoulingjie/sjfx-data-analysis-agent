"""Controlled acceptance materials, explicitly not quotations from real papers."""
import copy
import hashlib

VISION = "本文研究低质量图像识别，采用多模态融合方法作为基线。在清晰图像数据集上准确率为92%，样本数量为200。实验仅在清晰图像条件完成，尚未验证噪声输入下的可靠性。代码与评价脚本已在附件提供，可复核指标计算口径。"
CALIBRATION = "低质量图像识别任务采用不确定性校准方法。研究目标是评估噪声条件下的预测可靠性，对照采用原始置信度。实验数据和源代码可用，但跨任务迁移尚未验证，校准效果仍需核查。"
COUNTER = "低质量图像识别的复核结果发现，在噪声输入下不确定性校准未改善准确率，样本量为200。作者指出该方法不适用于当前融合输入条件，不能据此宣称迁移有效，仍需核对条件与比较口径。"
NEED = "本研究的需求是解决低质量图像识别在噪声条件下的可靠性问题。当前记录只描述任务与输入条件，没有证明另一任务的方法可以迁移；可用图像样本和对照需要确认。"
EVALUATION = "研究采用检索方法整理档案材料，但缺乏对照评价，未评估不同来源的覆盖差异。现有结果只在单一数据集报告，并没有给出消融与成本评估。档案列表及来源编号可用于复查。"
MIXED = "区域经营简报记录，客户转化率由18%上升至21%，交付周期由7天延长至8天。工作流程采用月度对照表；这些记录尚未解释交付延误的原因，需要核对订单条件和样本口径。"
PLAIN = "调查记录提供了合同条款核对流程，采用逐项比较方法并保留原始记录。结果表明该流程可以定位不同版本的条款变化；此材料描述的是已有流程，未证明普遍适用于所有合同。"

def document(path, text, title, sha=None):
    digest=sha or hashlib.sha256(text.encode()).hexdigest()
    return {"path":path,"source":{"path":path,"sha256":digest,"extension":".md","modified_at":"2020-01-01"},
            "structure":{"title":title,"headings":["研究问题","方法","结果","局限"]},"text":text,
            "evidence":[{"evidence_id":"E-"+digest[:12],"source_path":path,"source_sha256":digest,"text":text,"label":"paragraph","section":"研究结果与局限"}],
            "coverage":{"complete":True}}

def analysis(docs, groups=None):
    docs=copy.deepcopy(docs)
    groups=groups or [("低质量图像识别",[d["path"] for d in docs])]
    return {"statistics":{"parsed_files":len(docs)},"coverage":{"parsed_file_ratio":1},"document_index":docs,
            "research_topic_clusters":[{"topic":name,"members":paths,"representative_documents":paths[:3],
             "evidence_chain":[e for d in docs if d['path'] in paths for e in d['evidence']]} for name,paths in groups]}

def cases():
    a=document('vision/a.md',VISION,'多模态融合与噪声边界')
    b=document('calibration/b.md',CALIBRATION,'不确定性校准')
    n=document('noise/n.md',NEED,'低质量图像识别需求')
    counter=document('vision/counter.md',COUNTER,'噪声条件复核')
    evaluation=document('archive/e.md',EVALUATION,'档案检索评价')
    mixed=document('market/m.md',MIXED,'经营流程记录')
    plain=document('contract/p.md',PLAIN,'合同条款核对')
    copied=copy.deepcopy(a);copied['path']='vision/copy.md';copied['source']['path']=copied['path'];copied['evidence'][0]['source_path']=copied['path']
    changed=copy.deepcopy(a);changed['source']['modified_at']='2099-12-31'
    scoped=analysis([a,evaluation],[('低质量图像识别',['vision/a.md']),('档案检索评价',['archive/e.md'])]);scoped.update(selected_scope_only=True,selected_scope_paths=['vision/a.md'])
    empty=analysis([a]);empty['document_index'][0]['evidence'][0]['preview_only']=True;empty['research_topic_clusters'][0]['evidence_chain'][0]['preview_only']=True
    return [
        ('论文明确局限',analysis([a]),{'boundary'}),
        ('单篇关键判断复核',analysis([plain],[('合同条款核对',['contract/p.md'])]),{'replication'}),
        ('评价缺口',analysis([evaluation],[('档案检索评价',['archive/e.md'])]),{'evaluation'}),
        ('跨主题方法迁移',analysis([b,n],[('不确定性校准',['calibration/b.md']),('低质量图像识别需求',['noise/n.md'])]),{'transfer'}),
        ('结果分歧',analysis([a,counter]),{'disagreement'}),
        ('关键反证',analysis([a,b,counter]),{'boundary'}),
        ('混合经营资料',analysis([mixed],[('客户转化与交付',['market/m.md'])]),{'boundary'}),
        ('多领域机会盘点',analysis([a,evaluation,mixed],[('低质量图像识别',['vision/a.md']),('档案检索评价',['archive/e.md']),('客户转化与交付',['market/m.md'])]),{'boundary','evaluation'}),
        ('重复副本',analysis([a,copied]),{'boundary'}),
        ('修改时间扰动',analysis([changed]),{'boundary'}),
        ('选定范围隔离',scoped,{'boundary'}),
        ('低覆盖预览',empty,set()),
    ]

"""An exact private-source identity survives several cursor checkpoints."""
import json
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from services.mail.person_activity import activity_page,run_activity_slice
from test_person_activity_v3 import make_turn
from test_person_context_upgrade import person_data


class AnchoredModel:
    def __init__(self):self.payloads=[]

    def chat_json(self,system,prompt,**kwargs):
        p=json.loads(prompt)
        if 'draft_findings' in p:
            return {'json':{'scanned_scan_ids':p['scan_manifest'],'findings':p['draft_findings']}}
        self.payloads.append(p)
        by_chunk={c['chunk_id']:c for c in p['chunks']}
        findings=[]
        for span in p['citation_spans']:
            source=by_chunk[span['chunk_id']]
            if source.get('context_overlap') or 'third_party_report' not in span['allowed_kinds']:continue
            for marker in ('START_EVENT','END_EVENT'):
                if marker not in span['quote']:continue
                row={'kind':'third_party_report','statement':marker+'：私下邮件记载扫描件仍未提供。',
                     'completion':'not_established','citations':[{'evidence_id':span['evidence_id']}]}
                if span.get('identity_anchor_ids'):
                    row['identity_anchor']={'evidence_id':span['identity_anchor_ids'][0]}
                findings.append(row)
        return {'json':{'scanned_scan_ids':p['scan_manifest'],'findings':findings}}


def long_private(add,suffix='END_EVENT: The paper original was only shipped; the stamped scan remains missing.'):
    add('Mailbox registration seed.')
    return add('alice@example.com is discussed here. START_EVENT: The stamped scan remains pending.\n'+
               ('Neutral continuation of the same source.\n'*150)+suffix,
               sender='bob@example.com',recipient='carol@example.com')


def complete(repo,tid,model,budget=600,after_slice=None):
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        for _ in range(100):
            result=run_activity_slice(repo,tid,'owner',model,char_budget=budget)
            if after_slice:after_slice(repo.get_turn(tid,'owner')['final'])
            if not result.get('_requeue_slice'):return activity_page(repo,tid,'owner')
    raise AssertionError('cross-batch identity report did not finish')


def test_private_identity_survives_many_batches_with_exact_scan_counts_and_no_context_growth(person_data):
    repo,pid,add=person_data;key=long_private(add)
    tid=make_turn(repo,pid);model=AnchoredModel();checkpoints=[]
    page=complete(repo,tid,model,after_slice=lambda state:checkpoints.append(dict(state)))
    assert page['report']['complete'] and page['progress']['scan_complete']
    assert page['progress']['characters_read']==page['progress']['corpus']['characters']
    assert page['report']['markdown'].count('START_EVENT：')==1
    assert page['report']['markdown'].count('END_EVENT：')==1
    primary_private=[p for p in model.payloads if any(c['message_key']==key and not c.get('context_overlap') for c in p['chunks'])]
    assert len(primary_private)>=3
    anchor_states=[s['_identity_context'] for s in checkpoints if s.get('_identity_context')]
    assert anchor_states and all(len(c['text'])<=900 for c in anchor_states)
    assert len({(c['segment_id'],c['char_start'],c['char_end'],c['text']) for c in anchor_states})==1
    for p in model.payloads:
        contexts=[c for c in p['chunks'] if c.get('context_overlap')]
        assert len(contexts)<=2 and all('scan_id' not in c for c in contexts)
        assert len(p['scan_manifest'])==sum(not c.get('context_overlap') for c in p['chunks'])
        spans=p['citation_spans'];bank={s['evidence_id']:s for s in spans}
        for c in p['chunks']:
            assert set(c['allowed_kinds'])=={k for i in c['evidence_ids'] for k in bank[i]['allowed_kinds']}
        # Original source wording is not duplicated between anchor and tail.
        intervals=[(bank[i]['char_start'],bank[i]['char_end']) for c in contexts for i in c['evidence_ids']]
        assert all(b<=x or y<=a for n,(a,b) in enumerate(intervals) for x,y in intervals[n+1:])
    with repo._connection() as c:
        outputs=[json.loads(r[0]) for r in c.execute('SELECT output_ref FROM mail_research_steps WHERE turn_id=?', (tid,))]
    all_ids=[i for out in outputs for i in out['scanned_chunk_ids']]
    assert len(all_ids)==len(set(all_ids))
    last_payload=primary_private[-1]
    end_span=next(s for s in last_payload['citation_spans'] if 'END_EVENT' in s['quote'])
    assert end_span['identity_anchor_ids']
    assert 'alice@example.com' not in end_span['quote']


@pytest.mark.parametrize('boundary',['forward','next_mail'])
def test_cross_batch_anchor_does_not_lend_identity_past_forward_or_next_message(person_data,boundary):
    repo,pid,add=person_data
    if boundary=='forward':
        key=long_private(add,suffix='\nOriginal message\n'+('Forwarded other-person content.\n'*60)+
                         'END_EVENT: An unrelated person has another shipment.')
    else:
        key=long_private(add,suffix='The first source ends without a final reported event.')
        next_key=add('END_EVENT: An unrelated person has another shipment.',
                     sender='carol@example.com',recipient='dave@example.com')
    tid=make_turn(repo,pid);model=AnchoredModel();page=complete(repo,tid,model)
    assert page['report']['complete'] and 'END_EVENT：' not in page['report']['markdown']
    assert page['progress']['characters_read']==page['progress']['corpus']['characters']
    assert not repo.get_turn(tid,'owner')['final'].get('_identity_context')
    if boundary=='next_mail':
        p=next(p for p in model.payloads if any(c['message_key']==next_key for c in p['chunks']))
        other_sources=[c for c in p['chunks'] if c['message_key']==next_key]
        assert all(c['allowed_kinds']==[] for c in other_sources)
        assert all(not s.get('identity_anchor_ids') for s in p['citation_spans']
                   if s['chunk_id'] in {c['chunk_id'] for c in other_sources})
    end_spans=[s for p in model.payloads for s in p['citation_spans'] if 'END_EVENT' in s['quote']]
    assert end_spans and all(not s.get('identity_anchor_ids') for s in end_spans)


def test_resumed_identity_rechecks_original_segment_hash_before_model_use(person_data):
    repo,pid,add=person_data;long_private(add)
    tid=make_turn(repo,pid);model=AnchoredModel()
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        for _ in range(10):
            run_activity_slice(repo,tid,'owner',model,char_budget=600)
            checkpoint=repo.get_turn(tid,'owner')['final']
            if checkpoint.get('_identity_context'):break
        else:raise AssertionError('identity checkpoint was not retained')
        anchor=checkpoint['_identity_context']
        with repo._connection() as c:
            original=c.execute('SELECT text FROM mail_text_segments WHERE segment_id=?',(anchor['segment_id'],)).fetchone()[0]
            changed=original.replace('alice@example.com','other@example.com')
            c.execute('UPDATE mail_text_segments SET text=? WHERE segment_id=?',(changed,anchor['segment_id']))
        calls=len(model.payloads)
        with pytest.raises(RuntimeError,match='身份锚点'):
            run_activity_slice(repo,tid,'owner',model,char_budget=600)
        failed=repo.get_turn(tid,'owner')['final']
        assert len(model.payloads)==calls and failed['cursor']==checkpoint['cursor']
        assert failed['characters_read']==checkpoint['characters_read']

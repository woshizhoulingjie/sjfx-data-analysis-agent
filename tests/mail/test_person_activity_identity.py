"""Private long-source evidence must carry an independently proved identity."""
from copy import deepcopy

import pytest

from services.mail.person_activity import bind_span_kinds,reading_output_schema,validate_batch
from services.mail.person_research import citation_span_payload


SELECTED={'addresses':['alice@example.com'],'entity_id':None}
ANCHOR='alice@example.com is discussed in this private project update.'
DETAIL='The stamped scan is still missing; the paper original was only shipped.'


def chunk(text,ordinal=1,start=0,**updates):
    value={'chunk_id':'chunk'+str(ordinal),'segment_id':'segment'+str(ordinal),
        'message_key':'message1','mime_path':'1','source_kind':'body','text':text,
        'char_start':start,'char_end':start+len(text),'authored':False,
        'selected_roles':[],'participants':[{'role':'from','address':'bob@example.com'},
            {'role':'to','address':'carol@example.com'}],
        'quotation_boundary':None,'quoted_ranges':[],'attribution_uncertain':False,
        'attribution_version':2,'body_fragments':[{'fragment_kind':'authored',
            'char_start':start,'char_end':start+len(text),'method':'parsed','confidence':1}],
        'sent_at_utc':'2024-10-18T01:00:00+00:00','date_raw':'18 Oct 2024'}
    value.update(updates)
    return value


def two_windows(**second_updates):
    end=len(ANCHOR)+len(DETAIL)
    fragment={'fragment_kind':'authored','char_start':0,'char_end':end,
              'method':'parsed','confidence':1}
    return [chunk(ANCHOR,body_fragments=[fragment]),
            chunk(DETAIL,2,len(ANCHOR),**{'body_fragments':[fragment],**second_updates})]


def batch(chunks,kind='third_party_report',evidence_id='q2',anchor='q1'):
    row={'kind':kind,'statement':'其他人的邮件说明扫描件仍缺失，纸质原件只有寄送记录。',
         'completion':'not_established','citations':[{'evidence_id':evidence_id}]}
    if anchor is not None:row['identity_anchor']={'evidence_id':anchor}
    return {'scanned_scan_ids':['c'+str(i+1) for i,c in enumerate(
        q for q in chunks if not q.get('context_overlap'))],'findings':[row]}


def guided(chunks,purpose='activity_report'):
    spans=bind_span_kinds(citation_span_payload(chunks),chunks,SELECTED,purpose)
    manifest=['c'+str(i+1) for i,c in enumerate(q for q in chunks if not q.get('context_overlap'))]
    return spans,reading_output_schema({'scan_manifest':manifest,'citation_spans':spans},purpose)


def test_long_private_source_adds_anchor_to_appendix_without_losing_scan_spans():
    original=ANCHOR+' '+('x'*1000)+'\n'+DETAIL
    chunks=[chunk(original)]
    spans,schema=guided(chunks)
    assert len(spans)==2 and ''.join(s['quote'] for s in spans)==original
    assert spans[1]['allowed_kinds']==['third_party_report','follow_up']
    assert spans[1]['identity_anchor_ids']==['q1']
    assert spans[0]['identity_group']==spans[1]['identity_group']
    rows=validate_batch(batch(chunks),chunks,SELECTED,'activity_report')
    assert [q['evidence_id'] for q in rows[0]['citations']]==['q2','q1']
    assert rows[0]['identity_anchor']['quote']==spans[0]['quote']
    for variant in schema['properties']['findings']['items']['oneOf']:
        assert 'identity_anchor' in variant['required']
        assert variant['properties']['identity_anchor']['properties']['evidence_id']['enum']==['q1']
        assert set(variant['properties']['citations']['items']['properties']['evidence_id']['enum'])<={'q1','q2'}


def test_same_fragment_across_storage_windows_is_certified_from_original_metadata():
    chunks=two_windows()
    spans,_=guided(chunks)
    assert spans[0]['identity_group']==spans[1]['identity_group']
    assert validate_batch(batch(chunks),chunks,SELECTED,'activity_report')


@pytest.mark.parametrize('anchor',[None,'q2','not-a-span'])
def test_auxiliary_citation_requires_true_registered_anchor(anchor):
    chunks=two_windows()
    with pytest.raises(ValueError):validate_batch(batch(chunks,anchor=anchor),chunks,SELECTED,'activity_report')


@pytest.mark.parametrize('identity',['xalice@example.com','alice@example.com.fake',
                                     'alice@example.com-other','unconfirmed Alice'])
def test_anchor_does_not_guess_identity_or_match_other_mailboxes(identity):
    chunks=two_windows();chunks[0]['text']=chunks[0]['text'].replace('alice@example.com',identity)
    chunks[0]['char_end']=len(chunks[0]['text'])
    with pytest.raises(ValueError):validate_batch(batch(chunks),chunks,SELECTED,'activity_report')


def test_manually_confirmed_name_can_anchor_but_unconfirmed_display_name_cannot():
    chunks=two_windows();chunks[0]['text']=ANCHOR.replace('alice@example.com','李强')
    chunks[0]['char_end']=len(chunks[0]['text'])
    selected={**SELECTED,'entity_id':'confirmed-entity','display_name':'李强'}
    assert validate_batch(batch(chunks),chunks,selected,'activity_report')
    with pytest.raises(ValueError):
        validate_batch(batch(chunks),chunks,{**selected,'entity_id':None},'activity_report')


@pytest.mark.parametrize('updates',[{'message_key':'different-mail'}, {'mime_path':'2'},
                                   {'source_kind':'attachment'},
                                   {'body_fragments':[{'fragment_kind':'authored',
                                    'char_start':len(ANCHOR),'char_end':len(ANCHOR)+len(DETAIL)}]},
                                   {'body_fragments':[{'fragment_kind':'signature',
                                    'char_start':0,'char_end':len(ANCHOR)+len(DETAIL)}]},
                                   {'attribution_uncertain':True},
                                   {'quoted_ranges':[[len(ANCHOR),len(ANCHOR)+len(DETAIL)]]},
                                   {'quotation_boundary':len(ANCHOR)}])
def test_identity_cannot_cross_source_or_attribution_boundaries(updates):
    chunks=two_windows(**updates)
    with pytest.raises(ValueError):validate_batch(batch(chunks),chunks,SELECTED,'activity_report')
    spans,_=guided(chunks)
    assert spans[1]['allowed_kinds']==[]


def test_signature_anchor_and_coarse_nested_forwarding_fragment_are_rejected():
    chunks=two_windows()
    chunks[0]['body_fragments']=[{**chunks[0]['body_fragments'][0],'fragment_kind':'signature'}]
    with pytest.raises(ValueError):validate_batch(batch(chunks),chunks,SELECTED,'activity_report')
    original=ANCHOR+'\nOriginal message\n'+DETAIL
    chunks=[chunk(original)]
    value=batch(chunks,evidence_id='q1')
    with pytest.raises(ValueError):validate_batch(value,chunks,SELECTED,'activity_report')


def test_quoted_region_between_anchor_and_detail_splits_identity_even_in_coarse_fragment():
    middle='> Someone else described another status.\n'
    end=len(ANCHOR)+len(middle)+len(DETAIL)
    fragment={'fragment_kind':'authored','char_start':0,'char_end':end}
    chunks=[chunk(ANCHOR,body_fragments=[fragment]),
            chunk(middle,2,len(ANCHOR),body_fragments=[fragment]),
            chunk(DETAIL,3,len(ANCHOR)+len(middle),body_fragments=[fragment])]
    with pytest.raises(ValueError):validate_batch(batch(chunks,evidence_id='q3'),chunks,SELECTED,'activity_report')


@pytest.mark.parametrize('field,new_value',[('page_number',2),('sheet_name','Sheet2'),('cell_range','C2:D4')])
def test_attachment_anchor_cannot_cross_page_sheet_or_cells(field,new_value):
    chunks=two_windows()
    for c in chunks:c.update(source_kind='attachment',body_fragments=[],page_number=1,
                             sheet_name='Sheet1',cell_range='A1:B4')
    chunks[1][field]=new_value
    with pytest.raises(ValueError):validate_batch(batch(chunks,kind='source_information'),chunks,SELECTED,'activity_report')
    spans,_=guided(chunks)
    assert spans[1]['allowed_kinds']==[]


def test_same_attachment_unit_supports_material_record_without_own_or_received_promotion():
    chunks=two_windows()
    for c in chunks:c.update(source_kind='attachment',body_fragments=[],page_number=1)
    row=validate_batch(batch(chunks,kind='source_information'),chunks,SELECTED,'activity_report')[0]
    assert not row['citations'][0]['authored'] and row['completion']=='not_established'
    spans,_=guided(chunks)
    assert set(spans[1]['allowed_kinds'])=={'source_information','third_party_report'}
    for kind in ('action_statement','request_by_person','response_by_person','received_information','request_to_person'):
        with pytest.raises(ValueError):validate_batch(batch(chunks,kind=kind),chunks,SELECTED,'activity_report')
    for kind in ('expressed_attitude','intent_hypothesis'):
        with pytest.raises(ValueError):validate_batch(batch(chunks,kind=kind),chunks,SELECTED,'deep_analysis')


def test_material_record_requires_material_sources_and_real_relevance():
    chunks=two_windows()
    with pytest.raises(ValueError):validate_batch(batch(chunks,kind='source_information'),chunks,SELECTED,'activity_report')
    incoming=chunk(DETAIL,source_kind='attachment',selected_roles=['to'])
    assert validate_batch(batch([incoming],kind='source_information',evidence_id='q1',anchor=None),
                          [incoming],SELECTED,'activity_report')
    unrelated=deepcopy(incoming);unrelated['selected_roles']=[]
    with pytest.raises(ValueError):
        validate_batch(batch([unrelated],kind='source_information',evidence_id='q1',anchor=None),
                       [unrelated],SELECTED,'activity_report')


def test_self_reported_completion_cannot_be_inferred_from_an_identity_anchor():
    chunks=two_windows();value=batch(chunks);value['findings'][0]['completion']='self_reported'
    with pytest.raises(ValueError):validate_batch(value,chunks,SELECTED,'activity_report')


def test_anchor_must_not_mix_generated_source_fields_or_trust_group_ids():
    chunks=two_windows();value=batch(chunks)
    value['findings'][0]['identity_anchor']['quote']='invented'
    with pytest.raises(ValueError):validate_batch(value,chunks,SELECTED,'activity_report')
    chunks[1]['message_key']='other-mail'
    value=batch(chunks);value['findings'][0]['identity_group']='private_region_1'
    with pytest.raises(ValueError):validate_batch(value,chunks,SELECTED,'activity_report')


def test_scan_manifest_stays_complete_including_unrelated_and_overlap_windows():
    chunks=two_windows()+[chunk('An unrelated private delivery.',3,message_key='other-mail')]
    assert validate_batch(batch(chunks),chunks,SELECTED,'activity_report')
    for ack in (['c1','c2'],['c1','c1','c3'],['c1','c2','unknown']):
        value=batch(chunks);value['scanned_scan_ids']=ack
        with pytest.raises(ValueError):validate_batch(value,chunks,SELECTED,'activity_report')
    chunks=two_windows();chunks[0]['context_overlap']=True
    assert validate_batch(batch(chunks),chunks,SELECTED,'activity_report')
    chunks[1]['context_overlap']=True
    with pytest.raises(ValueError):validate_batch(batch(chunks),chunks,SELECTED,'activity_report')


def test_deep_private_context_keeps_anchor_separate_from_header_role_hypotheses():
    chunks=two_windows()+[chunk('I want to proceed, but only after approval.',3,
        message_key='own-mail',authored=True,selected_roles=['from'])]
    spans,schema=guided(chunks,'deep_analysis')
    assert spans[1]['allowed_kinds']==['context']
    assert validate_batch(batch(chunks,kind='context'),chunks,SELECTED,'deep_analysis')
    variants=schema['properties']['findings']['items']['oneOf']
    hypothesis=next(v for v in variants if v['properties']['kind']['enum']==['intent_hypothesis'])
    assert 'identity_anchor' not in hypothesis['properties']
    assert {'uncertainty','alternative_explanations'}<=set(hypothesis['required'])
    assert hypothesis['properties']['uncertainty']['minLength']==1
    assert hypothesis['properties']['alternative_explanations']['minItems']==1
    assert hypothesis['properties']['citations']['items']['properties']['evidence_id']['enum']==['q3']
    assert 'uniqueItems' not in repr(schema)


def test_private_region_schema_never_mix_anchor_or_citation_enums_across_messages():
    chunks=two_windows()
    other=two_windows()
    for i,c in enumerate(other,3):c.update(chunk_id='chunk'+str(i),message_key='other-private-mail')
    spans,schema=guided(chunks+other)
    variants=[v for v in schema['properties']['findings']['items']['oneOf']
              if v['properties']['kind']['enum']==['third_party_report']]
    assert len(variants)==2
    for variant in variants:
        fields=variant['properties']
        ids=set(fields['citations']['items']['properties']['evidence_id']['enum'])
        anchors=set(fields['identity_anchor']['properties']['evidence_id']['enum'])
        assert (ids,anchors) in [({'q1','q2'},{'q1'}),({'q3','q4'},{'q3'})]

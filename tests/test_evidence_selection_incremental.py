"""Direct greedy oracle for evidence/4; incremental redundancy must remain exact.

The source-ranking policy deliberately changed to preserve scientific body text.
This oracle still recomputes all overlaps instead of using incremental state.
"""
import copy
import hashlib
import random
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from services import large_package_evidence as evidence


def reference_candidates(parts, max_candidates=64):
    pool=[]
    for ordinal,part in enumerate((parts or [])[:64]):
        if not isinstance(part,dict) or part.get('kind') in {'attachment','member_names'}:
            continue
        raw=str(part.get('text') or '')
        if not raw.strip():
            continue
        source_cut=len(raw)>24000
        ranked=[]
        kind=part.get('kind') or 'unknown'
        pieces=[(value,cut) for value,cut in evidence._pieces(raw[:24000])
                if kind in evidence._STRUCTURED_KINDS or not (
                    evidence._LAYOUT_NUMBER.fullmatch(value) or evidence._PREPRINT_NOTICE.search(value))][:128]
        for index,(value,cut) in enumerate(pieces):
            structure=1.0 if kind in {'heading','title','fields','abstract'} else 0.0
            intent=.5 if re.search(r'摘要|目的|结论|结果|提出|用于|abstract|objective|conclusion',value,re.I) else 0
            score=evidence.quality(value)+structure+intent+min(len(evidence._units(value)),20)/40
            if evidence.is_boilerplate(value) or kind in {'footer','navigation'}:
                score-=3
            block=dict(part,text=value,raw_text=value,kind=kind,
                       truncated=bool(part.get('truncated') or cut or source_cut),
                       block_index=ordinal,sentence_index=index,quality=round(evidence.quality(value),3))
            block['id']=part.get('id') or 'B'+hashlib.sha256((str(part.get('location',''))+'\0'+value).encode()).hexdigest()[:12]
            ranked.append((score,index,block))
        selected=sorted(ranked,key=lambda x:(-x[0],x[1]))[:4]
        if ranked:
            for item in (ranked[0],ranked[len(ranked)//2],ranked[-1]):
                if item not in selected:
                    selected.append(item)
        pool.extend(selected)
    pool.sort(key=lambda x:(-x[0],x[2]['block_index'],x[1]))
    return [p for _,_,p in pool[:max_candidates]]


def reference_select(parts, limit=8, chars=4000, terms=()):
    pool = evidence.candidates(parts)
    chosen, seen, used = [], set(), 0
    anchors = {str(t).casefold() for t in terms if str(t)}
    prepared = []
    for block in pool:
        value = block['text']
        key = re.sub(r'\s+', ' ', value).casefold()
        structure = .7 if block['kind'] in {'title', 'heading', 'fields', 'abstract'} else 0
        penalty = 3 if evidence.is_boilerplate(value) or block['kind'] in {'footer', 'navigation'} else .6 if block['kind'] in {'header', 'quoted_history'} else .3 if block['kind'] == 'derived_summary' else 0
        if block['kind'] not in evidence._STRUCTURED_KINDS | {'title','heading'} and re.fullmatch(r'[A-Za-z]{1,24}',value.strip()):
            penalty += .8
        units=evidence._units(value)
        signal=min(12,len(units))/24+min(80,len(value))/320
        score = evidence.quality(value) + signal + structure + min(4, sum(t in value.casefold() for t in anchors))*.12 - penalty
        prepared.append((block, key, units, score))
    pool, selected_units = prepared, []
    while pool and len(chosen) < limit:
        best = None
        for index, (block, key, units, base_score) in enumerate(pool):
            if key in seen:
                continue
            overlap = max((len(units & previous) / max(1, len(units | previous))
                           for previous in selected_units), default=0)
            new_location = .25 if all(p.get('location') != block.get('location') for p in chosen) else 0
            score = base_score + new_location - overlap
            if best is None or score > best[0]:
                best = (score, index, key)
        if best is None:
            break
        _, index, key = best
        block, _, units, _ = pool.pop(index)
        room = chars - used
        if room < 40:
            break
        if len(block['text']) > room:
            if chosen:
                continue
            block = dict(block, text=block['text'][:room], truncated=True)
        chosen.append(block)
        selected_units.append(units)
        seen.add(key)
        used += len(block['text']) + 1
    return chosen


def corpus(seed, count=32):
    rng = random.Random(seed)
    phrases = ['合同尚未批准，须核实付款证据。',
               'Do not approve the payment until evidence is verified.',
               'ห้ามอนุมัติการชำระเงินก่อนตรวจสอบหลักฐาน',
               'Objective: compare experimental measurements.',
               'Copyright all rights reserved', '项目质量检测结果与验收记录',
               'Invoice outstanding 2026-09-26 400 USD', 'ไทยภาษาไทย',
               '\ufffd'*24, '!!!', 'ABC abc ABC', '[共享字符串未采样]']
    kinds = ['text', 'heading', 'title', 'abstract', 'fields', 'footer',
             'navigation', 'header', 'quoted_history', 'derived_summary',
             'attachment', 'member_names', None]
    rows=[]
    for i in range(count):
        text = ' '.join(rng.choices(phrases,k=rng.randint(1,8)))
        if i % 17 == 0:
            text = 'X'*1800 + text
        row = dict(text=text,location=rng.choice([None,'page:1','page:2','sheet:付款','หน้า:3']),
                   kind=rng.choice(kinds),truncated=bool(i%3),source_sha='source-'+str(i),
                   custom={'source':i},id='original-'+str(i))
        if i%5==0:
            row.pop('location')
        rows.append(row)
    return rows


class IncrementalEvidenceTests(unittest.TestCase):
    def test_candidate_generation_exact_equivalence(self):
        for seed in range(45):
            parts=corpus(seed,(seed%9)*8+1)
            with self.subTest(seed=seed):
                self.assertEqual(evidence.candidates(parts),reference_candidates(parts))
        parts=[dict(text='\n'.join('Unique source sentence %d_%d.'%(i,j) for j in range(128)),location=i) for i in range(65)]
        self.assertEqual(evidence.candidates(parts),reference_candidates(parts))

    def test_multilingual_randomized_exact_equivalence(self):
        for seed in range(45):
            parts=corpus(seed,count=(seed%9)*8+1)
            before=copy.deepcopy(parts)
            for limit,chars in [(0,4000),(1,40),(4,300),(8,1800),(16,9600),(64,60000)]:
                with self.subTest(seed=seed,limit=limit,chars=chars):
                    args=dict(limit=limit,chars=chars,terms=['付款','EVIDENCE','ไทย','not'])
                    self.assertEqual(evidence.select_blocks(parts,**args),reference_select(parts,**args))
            self.assertEqual(parts,before)

    def test_empty_and_invalid_parts(self):
        for parts in [None,[],[None,42,'not a block',{},dict(text='')]]:
            self.assertEqual(evidence.select_blocks(parts),reference_select(parts))

    def test_character_boundaries_and_duplicate_locations(self):
        parts=[dict(text='same statement '+str(i)+' '+'evidence '*i,location='page:1') for i in range(20)]
        parts += [dict(text='SAME STATEMENT 1 evidence',location=['page',1]),
                  dict(text='ห้ามอนุมัติการชำระเงิน',location=['page',1])]
        for chars in [-1,0,39,40,41,100,101,700,701,5000]:
            for limit in [1,2,8,16]:
                with self.subTest(chars=chars,limit=limit):
                    self.assertEqual(evidence.select_blocks(parts,limit,chars),reference_select(parts,limit,chars))

    def test_rounds_do_not_share_mutable_state(self):
        inputs=[corpus(seed,12) for seed in range(12)]
        expected=[reference_select(parts) for parts in inputs]
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(evidence.select_blocks,inputs)),expected)

    def test_candidate_quality_evaluated_once_per_sentence(self):
        with patch.object(evidence,'quality',wraps=evidence.quality) as quality:
            blocks=evidence.candidates([dict(text='Alpha objective. Beta conclusion. Gamma result.')])
        self.assertEqual(len(blocks),3)
        self.assertEqual(quality.call_count,3)

    def test_repeated_text_keeps_source_metadata(self):
        parts=[dict(text='Repeated objective statement.',location=str(i),
                    kind='heading' if i%2 else 'text',id='source-'+str(i)) for i in range(20)]
        with patch.object(evidence,'quality',wraps=evidence.quality) as quality:
            blocks=evidence.candidates(parts)
        self.assertEqual(quality.call_count,1)
        self.assertEqual({b['id'] for b in blocks},{p['id'] for p in parts})
        self.assertEqual({b['location'] for b in blocks},{p['location'] for p in parts})
        self.assertEqual([b['kind'] for b in blocks[:10]],['heading']*10)
        with patch.object(evidence,'quality',wraps=evidence.quality) as quality:
            evidence.candidates(parts)
        self.assertEqual(quality.call_count,1,'No cache survives a call')

    def test_overlap_work_is_linear_in_selections(self):
        class CountingSet(set):
            calls=0
            def __and__(self,other):
                type(self).calls+=1
                return super().__and__(other)
        blocks=[dict(text='source record '+str(i),kind='text',location=str(i),id=str(i)) for i in range(64)]
        units=evidence._units
        with patch.object(evidence,'candidates',return_value=blocks),patch.object(evidence,'_units',side_effect=lambda t:CountingSet(units(t))):
            expected=reference_select([],limit=16,chars=60000)
            old=CountingSet.calls
            CountingSet.calls=0
            result=evidence.select_blocks([],limit=16,chars=60000)
            new=CountingSet.calls
        self.assertEqual(result,expected)
        self.assertLessEqual(new,64*15)
        self.assertLess(new,old/4)


if __name__=='__main__':
    unittest.main()

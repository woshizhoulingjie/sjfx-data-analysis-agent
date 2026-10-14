"""Extract source-supported directory labels without a generative model.

Labels describe semantic groups already computed by the embedding pipeline.
Candidate phrases are ranked by repetition, representative coverage and headings;
they never decide membership and never map words to predefined business classes.
"""
from __future__ import annotations
import math
import re
import unicodedata
from collections import Counter, defaultdict

VERSION = 'extractive-semantic-label/1'
STOP = set('''a an the and or of to in on for with from by as at is are was were be been being this that these those we our it its they their you your which who whom when where how what can may will would could should must has have had do does did not no also such than then into over under between through about using used use paper papers study work document documents abstract introduction conclusion conclusions related keywords references author authors university institute department laboratory school received revised accepted doi email copyright rights reserved proceedings published publisher journal volume vol issue pages page figure table shows show shown presents present propose proposed based called called respectively each other more most less many several different common new first second third one two three four five via against however therefore thus while although because during consequently further since even across together research following include includes including within without both either all any some there here very et al e g i e'''.split())
CN_STOP = {'本文','该文','文件','资料','研究','报告','内容','主题','关键事实','后续建议','主要','摘要','通过','进行','基于','以及','相关','可以','分析','介绍','提出','包括','分别','针对','为了','一种','一个','其中','系统','技术','方法','数据','记录','字段'}
CN_GENERIC = re.compile(r'^(?:本文|该文|本研究|文章|主要|通过|针对|提出|介绍|分析|研究了|关于|一种|一篇|一个|的|了|在|与|和|及|是|为|对)+')
BOILER = re.compile(r'disclaimer|all rights|copyright|收稿日期|引用格式|免责声明|版权所有|^目录$|^abstract$|^introduction$|^summary$|^主题$|^关键事实$', re.I)


def clean(text):
    text = unicodedata.normalize('NFKC', str(text or ''))
    text = re.sub(r'https?://\S+|[\w.+-]+@[\w.-]+\.\w+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _body(text):
    text = clean(text)
    abstract = re.search(r'\babstract\b\s*[.:—–-]?', text, re.I)
    if abstract and len(text) - abstract.end() > 100:
        text = text[abstract.end():]
    ending = re.search(r'\b(?:Stream:|Status of This Memo|Copyright Notice|Authors.? Addresses|References|Acknowledgements|Acknowledgments|Correspondence)\b', text, re.I)
    if ending and ending.start() > 120:
        text = text[:ending.start()]
    return text


def _candidates(text):
    """Bounded lexical phrases, including CJK without installing a tokenizer."""
    text = clean(text)[:5000]
    result = {}
    # Keep punctuation boundaries so author lists don't become long topic phrases.
    for part in re.finditer(r'[^\n.!?;。！？；，,:：()（）]+', text):
        segment = part.group()
        words = list(re.finditer(r'[A-Za-z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)*', segment))
        for i, first in enumerate(words):
            word = first.group()
            if word.casefold() in STOP or len(word) < 2:
                continue
            for n in range(1, min(5, len(words)-i) + 1):
                last = words[i+n-1]
                chosen = words[i:i+n]
                if any(x.group().casefold() in STOP for x in chosen):
                    continue
                phrase = segment[first.start():last.end()].strip()
                if re.search(r'\d', phrase) and not re.search(r'[A-Z]{2,}[-/]?\d', phrase):
                    continue
                if len(phrase) > 64 or (n == 1 and not (word.isupper() or '-' in word or len(word) >= 5)):
                    continue
                key = phrase.casefold()
                entry = result.setdefault(key, {'phrase': phrase, 'count': 0, 'position': part.start()+first.start(), 'length': n})
                entry['count'] += 1
    for match in re.finditer(r'[\u4e00-\u9fff]{2,36}', text):
        raw = match.group()
        if BOILER.search(raw) or raw in CN_STOP:
            continue
        phrases = [raw] if len(raw) <= 18 else re.split(r'的|以及|包括|通过|主要|针对|进行了|进行|属于|用于|旨在|是一|以便|并且', raw)
        for phrase in phrases:
            phrase = CN_GENERIC.sub('', phrase).strip('的了在与和及是为对')
            if not 2 <= len(phrase) <= 18 or phrase in CN_STOP:
                continue
            entry = result.setdefault(phrase, {'phrase': phrase, 'count': 0, 'position': match.start(), 'length': min(5,len(phrase)/2)})
            entry['count'] += 1
    return result


def _preferred(value):
    headings = [clean(x) for x in value.get('headings',[]) if 3 <= len(clean(x)) <= 120 and not BOILER.search(clean(x))]
    summary = value.get('deep_summary') or ''
    # Existing deep summaries often contain an explicit subject section.
    subject = re.search(r'(?:\*\*|#+\s*)?主题(?:\*\*)?\s*[:：]?\s*(.+?)(?=\*\*|\n\s*\n|关键事实|$)', summary)
    if subject:
        headings.insert(0, clean(subject.group(1))[:180])
    return headings[:5]


def _source_title(value):
    preferred = _preferred(value)
    for heading in preferred:
        if 4 <= len(heading) <= 100 and not BOILER.search(heading):
            return heading.strip('*#：: ')
    if value.get('deep_summary'):
        return ''
    text = clean(value.get('text') or '')
    abstract = re.search(r'\babstract\b',text,re.I)
    prefix = text[:abstract.start()] if abstract else text[:500]
    rfc = re.match(r'RFC\s+\d+\s+(?:The\s+)?(.+)',prefix,re.I)
    if rfc and len(rfc.group(1)) <= 130:
        return rfc.group(1).strip()
    if len(prefix) > 1500 or re.match(r'(?:Received:|HAL Id:|\d+\s*\|)',prefix,re.I):
        return ''
    if prefix.startswith('This paper is included'):
        marker = re.search(r'sponsored by [^.]+\.\s*',prefix,re.I)
        prefix = prefix[marker.end():] if marker else ''
    prefix = re.split(r'⋆|\*|Invited Paper|https?://|\{',prefix,1,flags=re.I)[0]
    body = _body(text)[:1600].casefold()
    words = list(re.finditer(r'[A-Za-z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)*',prefix[:250]))
    supported_end = 0
    for j in range(len(words)-1):
        for n in (2,3,4):
            if j+n > len(words):
                continue
            selected = words[j:j+n]
            if sum(x.group().casefold() not in STOP for x in selected) < 2:
                continue
            phrase = prefix[selected[0].start():selected[-1].end()].casefold()
            if phrase in body:
                supported_end = max(supported_end,selected[-1].end())
    if supported_end >= 15:
        prefix = prefix[:supported_end]
    elif abstract:
        return ''
    prefix = prefix.strip(' ,.:;—–-')
    while prefix and prefix.split()[-1].casefold() in STOP:
        prefix = prefix.rsplit(' ',1)[0] if ' ' in prefix else ''
    if 8 <= len(prefix) <= 150 and len(prefix.split()) >= 3 and not BOILER.search(prefix):
        return prefix
    return ''


def name_group(values):
    if not values:
        raise ValueError('没有可用于目录命名的内容')
    scores = defaultdict(float)
    coverage = Counter()
    display = {}
    occurrences = {}
    for index, value in enumerate(values[:3]):
        text = value.get('deep_summary') or value.get('text') or ' / '.join(value.get('fields',[]))
        text = _body(text)
        if not text:
            continue
        candidates = _candidates(text)
        preferred = ' '.join(_preferred(value) + value.get('fields',[])[:8]).casefold()
        for key, entry in candidates.items():
            position = entry['position'] / max(1,len(text))
            score = (1+math.log1p(entry['count'])) * (1+.20*entry['length']) * (1.5-position*.5)
            if entry['count'] == 1 and entry['length'] > 2 and key not in preferred:
                score *= .45
            if entry['length'] == 1:
                score *= .3 if len(key) < 5 else .5
            if key in preferred:
                score *= 2.5
            if re.search(r'[\u4e00-\u9fff]', key):
                score *= 1.25
            scores[key] += score
            coverage[key] += 1
            display.setdefault(key,entry['phrase'])
            occurrences.setdefault(key,(index,entry['position']))
    if not scores:
        text = clean(values[0].get('deep_summary') or values[0].get('text') or ' / '.join(values[0].get('fields',[])))
        phrase = text[:60] or '内容待识别'
        return {'category':'内容主题','topic':phrase,'reason':'从代表内容提取目录名称；归属由内容语义分组决定',
                'quotes':[{'source':'content','text':text[:900]}], 'model':VERSION}
    ranked = sorted(scores,key=lambda k:(-scores[k]*(1+.6*(coverage[k]-1)),len(k),k))
    selected=[]
    for key in ranked:
        if selected and re.search(r'[\u4e00-\u9fff]', selected[0]):
            break
        if any(key in old or old in key for old in selected):
            continue
        tokens=set(key.split())
        if any(len(tokens & set(old.split()))/max(1,len(tokens | set(old.split()))) > .34 for old in selected):
            continue
        selected.append(key)
        if len(selected)>=2:
            break
    primary=display[selected[0]]
    source_title = _source_title(values[0]) if len(values) == 1 else ''
    topic=source_title or ' · '.join(display[k] for k in selected)[:140]
    quotes=[]
    for key in selected:
        index, position=occurrences[key]
        value=values[index]
        text=_body(value.get('deep_summary') or value.get('text') or ' / '.join(value.get('fields',[])))
        quotes.append({'source':'deep_summary' if value.get('deep_summary') else 'content','text':text[max(0,position-100):position+700]})
    return {'category':primary,'topic':topic,'reason':'目录名来自组内代表内容的关键短语；文件按内容语义相似性分组',
            'quotes':quotes,'model':VERSION}

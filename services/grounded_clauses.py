"""Bounded conjunction grounding, not an unrestricted entailment engine.

Only fully recognized quantity/status conjunctions are handled. Every atom must
be supported inside one source record window; unrecognized grammar stays with
the existing verifier. Returned text is diagnostic, never replacement prose.
"""
from __future__ import annotations

import re
from services import answer_quality as aq

_ROLES = r'实收|尚缺|实际收到|已收到|已到货|实际到货|仅到货'
_QUANTITY = re.compile(
    r'(?P<prefix>.*?)(?P<role>' + _ROLES + r')\s*(?:为|是|共)?\s*'
    r'(?P<value>[+-]?\d+(?:\.\d+)?)\s*'
    r'(?P<unit>台|件|份|套|辆|箱|个|吨|千克|公斤|毫米|厘米|米)$')
_OPERATION = r'通电联合调试|联合调试|通电调试|书面验收|最终验收|调试|验收|修复'
_STATUS = re.compile(
    r'(?P<polarity>尚未|还未|并未|没有|已经|未|已)?'
    r'(?:完成(?P<operation>' + _OPERATION + r')|(?P<repair>修复|修好))')
_JOIN = re.compile(r'、|并且|而且|且|以及|同时|(?<=台)并(?=尚缺|实收)')
_UNCERTAIN = re.compile(r'(?:尚未|没有|未能|未|不能|无法)(?:得到|获得)?(?:确认|证实|证明)|(?:尚无|没有|无)证据')
_PLANNED = re.compile(r'计划|预计|希望|准备|拟|将于|打算')
_CONDITIONAL = re.compile(r'如果|假设|假如|倘若|若能|若已|若未|若实收|若尚缺|或(?:者)?')
_DATE = re.compile(r'\d{4}年\d{1,2}月\d{1,2}日')
_ORGANIZATION = re.compile(r'[\u4e00-\u9fffA-Za-z0-9_-]{1,20}(?:单位|公司|工厂|车间|部门)')


def _plain(text):
    return re.sub(r'\[\d+\]', '', aq._quantity_label_text(aq.numeric_text(str(text or '')))).replace('**', '').strip(' 。；;，,')


def _context(prefix):
    """Accept explicit identity/date and neutral lead-ins, never drop a claim."""
    projects = aq._projects(prefix)
    identifiers = set(re.findall(aq.RECORD_ID, prefix))
    dates = set(_DATE.findall(prefix))
    organizations = set(_ORGANIZATION.findall(prefix))
    remaining = prefix
    for token in sorted(projects | identifiers | dates | organizations, key=len, reverse=True):
        remaining = remaining.replace(token, '')
    remaining = re.sub(r'合同编号|截至|目前|当前|现已|现在|设备|故障|本次|该|本|的|但', '', remaining)
    if remaining.strip(' （）():：-') or len(projects) > 1 or len(dates) > 1:
        return None
    return {'projects': projects, 'identifiers': identifiers, 'dates': dates,
            'organizations': organizations, 'prefix': prefix}


def _status_matches(text):
    for match in _STATUS.finditer(text):
        start = max([text.rfind(c, 0, match.start()) for c in '，,。；;|'] + [-1]) + 1
        for join in _JOIN.finditer(text, start, match.start()):
            start = join.end()
        prefix = text[start:match.start()]
        operation = match['operation'] or '修复'
        if _UNCERTAIN.search(prefix):
            state = 'unconfirmed'
        elif _PLANNED.search(prefix):
            state = 'planned'
        else:
            if not match['polarity'] or _context(prefix) is None:
                continue
            state = 'done' if match['polarity'] in {'已', '已经'} else 'not_done'
        yield match, operation, state


def _atom(fragment):
    fragment = _plain(fragment)
    if _CONDITIONAL.search(fragment) or _PLANNED.search(fragment) or _UNCERTAIN.search(fragment):
        return None
    quantity = _QUANTITY.fullmatch(fragment)
    if quantity:
        context = _context(quantity['prefix'])
        if context is None:
            return None
        role = '尚缺' if quantity['role'] == '尚缺' else '实收'
        value = quantity['value'] + quantity['unit']
        return {'kind': 'quantity', 'role': role, 'value': value,
                'body': role + value, 'context': context, 'text': fragment}
    matches = list(_status_matches(fragment))
    if len(matches) != 1:
        return None
    match, operation, state = matches[0]
    if fragment[match.end():].strip('故障工作任务 '):
        return None
    context = _context(fragment[:match.start()])
    if context is None:
        return None
    return {'kind': 'status', 'operation': operation, 'state': state,
            'body': match[0], 'context': context, 'text': fragment}


def _record_windows(evidence):
    """Bound matching to a record, not a union of unrelated source clauses."""
    text = _plain(aq.numeric_evidence_text(evidence))
    pieces = re.split(r'([。！？!?；;\n|，]|(?<!\d),|,(?!\d))', text)
    parts, projects, identifiers, date, organization = [], set(), set(), '', set()
    has_observation = False
    for index in range(0, len(pieces), 2):
        clause = pieces[index]
        separator = pieces[index + 1] if index + 1 < len(pieces) else ''
        current_projects = aq._projects(clause)
        current_ids = set(re.findall(aq.RECORD_ID, clause))
        current_dates = set(_DATE.findall(clause))
        current_date = next(iter(current_dates)) if len(current_dates) == 1 else ''
        current_org = set(_ORGANIZATION.findall(clause))
        device_ids = {x for x in identifiers if x.count('-') == 1}
        new_devices = {x for x in current_ids if x.count('-') == 1}
        replaced_id = any(x.split('-')[0] == y.split('-')[0] and x != y
                          for x in identifiers for y in current_ids)
        # Metadata can introduce a record before its facts, not retrospectively
        # assign earlier anonymous facts to a newly named project/device/date.
        late_identity = has_observation and bool(
            (current_projects and not projects) or (current_org and not organization)
            or (current_date and not date) or (current_ids - identifiers))
        changed = bool(
            (current_projects and projects and current_projects != projects)
            or (current_date and date and current_date != date)
            or (current_org and organization and current_org != organization)
            or replaced_id or late_identity
            or (device_ids and new_devices and device_ids != new_devices))
        if parts and (changed or len(parts) >= 16 or sum(map(len, parts)) + len(clause) > 650):
            yield ''.join(parts)
            parts, projects, identifiers, date, organization = [], set(), set(), '', set()
            has_observation = False
        if (len(clause) > 650 or len(current_projects) > 1
                or len(current_org) > 1 or len(current_dates) > 1):
            if parts:
                yield ''.join(parts)
            parts, projects, identifiers, date, organization = [], set(), set(), '', set()
            has_observation = False
            continue
        projects = current_projects or projects
        identifiers.update(current_ids)
        date = current_date or date
        organization = current_org or organization
        parts.extend([clause, separator])
        has_observation = has_observation or bool(_STATUS.search(clause)) or any(
            _QUANTITY.fullmatch(_plain(fragment)) for fragment in _JOIN.split(clause))
        if separator in {';', '；', '|', '\n'}:
            yield ''.join(parts)
            parts, projects, identifiers, date, organization = [], set(), set(), '', set()
            has_observation = False
    if parts:
        yield ''.join(parts)


def _context_matches(context, window):
    return (context['projects'].issubset(aq._projects(window))
            and context['identifiers'].issubset(set(re.findall(aq.RECORD_ID, window)))
            and all(date in window for date in context['dates'])
            and context['organizations'].issubset(set(_ORGANIZATION.findall(window))))


def _record_quantities(window):
    observations = {}
    for clause in re.split(r'[。！？!?；;\n|，]|(?<!\d),|,(?!\d)', window):
        for fragment in _JOIN.split(clause):
            fragment = _plain(fragment)
            match = _QUANTITY.fullmatch(fragment)
            if not match:
                if re.search(r'(?:' + _ROLES + r')\s*\d', fragment):
                    return None
                continue
            if _PLANNED.search(match['prefix']) or _UNCERTAIN.search(match['prefix']):
                continue
            if _context(match['prefix']) is None:
                return None
            role = '尚缺' if match['role'] == '尚缺' else '实收'
            observations.setdefault(role, set()).add(match['value'] + match['unit'])
    return observations


def compound_support(claim, evidence):
    """Return None for unknown grammar, else a full conjunctive proof result."""
    text = _plain(claim)
    fragments = _JOIN.split(text)
    if not 2 <= len(fragments) <= 6:
        return None
    atoms = [_atom(fragment) for fragment in fragments]
    if any(atom is None for atom in atoms) or not any(x['kind'] == 'quantity' for x in atoms):
        return None
    inherited = atoms[0]['context']
    for atom in atoms[1:]:
        if not any(atom['context'][key] for key in ('projects','identifiers','dates','organizations')):
            atom['context'] = inherited
    for window in _record_windows(evidence):
        if _CONDITIONAL.search(window):
            continue
        if not all(_context_matches(atom['context'], window) for atom in atoms):
            continue
        quantities = _record_quantities(window)
        if quantities is None:
            continue
        checks = []
        for atom in atoms:
            if atom['kind'] == 'quantity':
                supported = quantities.get(atom['role']) == {atom['value']}
            else:
                states = [(operation, state) for _, operation, state in _status_matches(window)]
                # Conflicting status observations inside one window do not
                # become support by selecting only the convenient observation.
                observed = {state for operation, state in states if operation == atom['operation']}
                supported = observed == {atom['state']}
            checks.append(supported)
        if all(checks):
            return {'supported': True, 'atom_count': len(atoms),
                    'reason': '每个数量与状态均在同一记录内分别得到支持。'}
    return {'supported': False, 'atom_count': len(atoms),
            'reason': '复合陈述的每一项未能在同一项目、设备与记录范围内共同核实。'}


def status_strength_mismatch(claim, evidence):
    """Detect certainty stronger than all matching status observations."""
    atom = _atom(claim)
    if atom is None or atom['kind'] != 'status':
        return False
    observations = set()
    for window in _record_windows(evidence):
        if not _context_matches(atom['context'], window):
            continue
        observations.update(state for _, operation, state in _status_matches(window)
                            if operation == atom['operation'])
    return bool(observations and atom['state'] not in observations
                and observations.issubset({'unconfirmed', 'planned'}))

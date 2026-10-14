"""Bounded parallel raw-source reading with a single durable checkpoint writer.

Lanes use the existing extraction, fact review, attribution and adaptive retry
pipeline. Only their source cursors are independent. Calls reserve against one
shared ledger, and verified batches are committed atomically in completion order.
A wave's global cursor advances only after every source interval is saved.
"""
from __future__ import annotations

import copy
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import Config
from .person_corpus import next_chunks
from .research.token_budget import init_usage, reserve_call, record_usage, usage_snapshot
from .research.runtime import JobCancelled

UNIT_FIELDS = ('message_key', 'mime_path', 'source_kind', 'page_number', 'sheet_name', 'cell_range')


class ReadingContextCapacityError(ValueError):
    """The fixed reading contract cannot fit even before source text is added."""


def reading_profile(model):
    transport = getattr(model, 'transport', model)
    real = hasattr(transport, 'max_concurrency')
    from services.model_dispatch import _concurrency_limit
    enabled = getattr(Config, 'MAIL_PERSON_PARALLEL_ENABLED', True)
    concurrency = min(getattr(Config, 'MAIL_PERSON_READING_CONCURRENCY', 3),
                      max(1, int(getattr(transport, 'max_concurrency', 1))), _concurrency_limit())
    return {'concurrency': max(1, concurrency) if enabled else 1,
            'char_budget': getattr(Config, 'MAIL_PERSON_READING_CHARS', 12000) if real else 4000,
            'context_tokens': min(32768, Config.LLM_CONTEXT_TOKENS),
            'output_tokens': getattr(Config, 'MAIL_PERSON_READING_OUTPUT_TOKENS', 16384) if real else 5000}


def call_limits(model):
    profile = reading_profile(model)
    return profile['output_tokens'], profile['context_tokens']


def check_context(system, prompt, output, limit):
    # Conservative multilingual estimate, including the adapter policy/template.
    # This is deliberately not advertised as the serving model's tokenizer.
    incoming = math.ceil(len((str(system) + '\n' + prompt).encode('utf-8')) / 2) + 1024
    if incoming + output > limit:
        from services.model_output import ModelOutputError
        raise ModelOutputError('返回不符合结构化契约：原文及完整输出预算超过上下文容量，需缩批，不截断原文')
    return incoming


def fit_output_budget(system, prompt, preferred, context):
    """Use spare context for longer results without shrinking a usable raw batch.

    Keep the previous 8K minimum on 32K services. Smaller services reserve a
    quarter of their context (at least 512 tokens); full response validation
    still rejects truncated output. Fixed-contract failures cannot be repaired
    by splitting the source input.
    """
    minimum = min(preferred, 8000, max(512, context // 4))
    fixed_input = math.ceil(len((str(system) + '\n').encode('utf-8')) / 2) + 1024
    if fixed_input + minimum > context:
        raise ReadingContextCapacityError(
            '人物事实阅读的固定指令与完整输出最低预算超过模型上下文容量'
            '（需要至少%s tokens，配置%s tokens）；请选择容量足够的模型或正确配置上下文，'
            '缩小原文批次无法解决，原游标和已保存事实保留。' % (fixed_input + minimum, context))
    incoming = check_context(system, prompt, minimum, context)
    return min(preferred, context - incoming)


def primary_chunks(repository, pid, owner, selected, cursor, budget):
    chunks, end = next_chunks(repository, pid, owner, selected, cursor, budget, full_package=True)
    # Same boundary rule as the serial reader: don't isolate a new short unit's prefix.
    if chunks and end[1] > 0:
        unit = tuple(chunks[-1][k] for k in ('message_key', 'mime_path', 'source_kind'))
        suffix = len(chunks)
        while suffix > 0 and tuple(chunks[suffix-1][k] for k in ('message_key', 'mime_path', 'source_kind')) == unit:
            suffix -= 1
        if suffix > 0 and chunks[suffix]['char_start'] == 0:
            end = [chunks[suffix]['row_id'], 0]
            chunks = chunks[:suffix]
    return chunks, end


def tail_context(chunks, selected):
    """Derive continuity from exact original spans, without waiting for a model."""
    from .person_activity import bind_span_kinds, _identity_region, _mention
    from .person_research import citation_span_payload, checked_citation
    tail = dict(chunks[-1]); cut = max(0, len(tail['text'])-1200)
    tail.update(text=tail['text'][cut:], char_start=tail['char_start']+cut,
                chunk_id=tail['chunk_id']+':context')
    spans = citation_span_payload(chunks)
    bind_span_kinds(spans, chunks, selected, 'activity_report')
    sources = {c['chunk_id']: c for c in chunks}
    last = checked_citation(spans[-1], sources)
    region = _identity_region(last, sources) if last else None
    identity = None
    if region is not None:
        for span in spans:
            q = checked_citation(span, sources)
            if q and not q.get('selected_roles') and _mention(q, selected) and _identity_region(q, sources) == region:
                identity = {**sources[q['chunk_id']], 'text': q['quote'], 'char_start': q['char_start'],
                            'char_end': q['char_end'],
                            'chunk_id': q['segment_id']+':identity:'+str(q['char_start']), 'context_overlap': True}
                break
    return tail, identity


class LaneRepository:
    """Expose an isolated reading cursor; all evidence writes go to the coordinator."""
    def __init__(self, lane):
        self._activity_lane = lane

    def __getattr__(self, name):
        return getattr(self._activity_lane.controller.repository, name)

    def get_turn(self, tid, owner, **kwargs):
        lane = self._activity_lane
        turn = lane.controller.repository.get_turn(tid, owner, **kwargs)
        if turn and tid == lane.controller.tid:
            turn = copy.deepcopy(turn)
            turn['final'] = copy.deepcopy(lane.state)
            turn['error'] = lane.error
            if turn['status'] not in {'cancelled', 'cancelling'}:
                turn['status'] = 'failed' if lane.error else 'running'
        return turn

    def update_turn(self, tid, owner, **kwargs):
        if tid != self._activity_lane.controller.tid:
            raise ValueError('并行阅读不能修改其他任务')
        self._activity_lane.error = kwargs.get('error')


class Lane:
    def __init__(self, controller, index):
        self.controller = controller
        self.index = index
        self.state = copy.deepcopy(controller.checkpoint['_parallel_wave']['lanes'][index]['state'])
        self.error = None
        self.repository = LaneRepository(self)

    def next_chunks(self, repository, pid, owner, selected, cursor, budget, **kwargs):
        chunks, end = next_chunks(repository, pid, owner, selected, cursor, budget, **kwargs)
        bound = self.controller.checkpoint['_parallel_wave']['lanes'][self.index]['end']
        limited = []
        for chunk in chunks:
            if chunk['row_id'] > bound[0] or chunk['row_id'] == bound[0] and bound[1] == 0:
                break
            if chunk['row_id'] == bound[0]:
                start = int(chunk['chunk_id'].rsplit(':', 1)[1])
                take = bound[1] - start
                if take <= 0:
                    break
                if take < len(chunk['text']):
                    chunk = {**chunk, 'text': chunk['text'][:take], 'char_end': chunk['char_start']+take}
            limited.append(chunk)
        if tuple(end) > tuple(bound):
            end = list(bound)
        return limited, end

    def commit(self, before, after, *, step=None, terminal=False, operational=False):
        return self.controller.commit_lane(self, before, after, step, terminal, operational)

    def call(self, model, system, payload, *, field='findings', verification=False):
        return self.controller.call(self, model, system, payload, field, verification)

    def run(self, model):
        from .person_activity import run_activity_slice
        controller = self.controller
        while True:
            controller.active()
            with controller.lock:
                row = controller.checkpoint['_parallel_wave']['lanes'][self.index]
                if row['status'] == 'done':
                    return
                budget = row['budget']
            self.error = None
            result = run_activity_slice(self.repository, controller.tid, controller.owner, model,
                                        cancelled=controller.cancelled, char_budget=budget)
            if not result.get('_requeue_slice'):
                return


class Wave:
    def __init__(self, repository, tid, owner, spec, selected, checkpoint, cancelled, profile):
        self.repository, self.tid, self.owner = repository, tid, owner
        self.spec, self.selected = spec, selected
        self.checkpoint = copy.deepcopy(checkpoint)
        self.cancelled, self.profile = cancelled, profile
        self.lock = threading.RLock()

    def active(self):
        turn = self.repository.get_turn(self.tid, self.owner, include_events=False)
        if not turn or self.cancelled() or turn['status'] in {'cancelled', 'cancelling'}:
            if turn and turn['status'] not in {'cancelled', 'cancelling'}:
                self.repository.update_turn(self.tid, self.owner, status='cancelled', sufficiency='partial')
            raise JobCancelled('并行人物阅读已停止，已核验批次保留')

    def save(self, updated, step=None, terminal=False, accounting_only=False):
        from .person_activity import _commit
        if accounting_only:
            # Cancellation stops evidence commits, but spent tokens must survive.
            with self.repository._connection() as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT final_json FROM mail_research_turns WHERE turn_id=? AND owner_id=?',
                                (self.tid, self.owner)).fetchone()
                if not row or json.loads(row[0]) != self.checkpoint:
                    raise RuntimeError('并行阅读预算检查点已被其他执行器修改')
                c.execute('UPDATE mail_research_turns SET final_json=? WHERE turn_id=? AND owner_id=?',
                          (json.dumps(updated, ensure_ascii=False), self.tid, self.owner))
            result = {'turn_id': self.tid}
        else:
            result = _commit(self.repository, self.tid, self.owner, self.spec, self.selected,
                             self.checkpoint, updated, step=step, terminal=terminal)
        self.checkpoint = updated
        return result

    def decorate(self, updated):
        from .person_activity import now
        lanes = updated['_parallel_wave']['lanes']
        states = [dict(index=i+1, state=row.get('activity', {}).get('state', row['status']),
                       characters=row['characters'], saved_characters=row.get('saved_characters', 0),
                       message_count=row['message_count'], summary=row.get('activity', {}).get('summary', ''),
                       retry_attempt=row['state'].get('adaptive_batch', {}).get('retry_count', 0))
                  for i, row in enumerate(lanes)]
        running = sum(row['status'] == 'running' for row in lanes)
        updated['parallel_reading'] = {'concurrency': self.profile['concurrency'],
            'context_tokens': self.profile['context_tokens'], 'batch_char_budget': self.profile['char_budget'],
            'active_batches': running, 'lanes': states}
        updated['activity'] = {'stage': 'reading', 'state': 'running' if running else 'checkpointed',
            'summary': '原文并行阅读：%s 路处理，%s/%s 组已核查保存' %
                       (self.profile['concurrency'], sum(r['status'] == 'done' for r in lanes), len(lanes)),
            'updated_at': now()}
        active = [row.get('activity', {}) for row in lanes if row['status']=='running']
        for key in ('started_at', 'generation_started_at'):
            times = [a[key] for a in active if a.get(key)]
            if times:updated['activity'][key] = min(times)
        if active:updated['activity']['model'] = active[0].get('model', '')
        return updated

    def commit_lane(self, lane, before, after, step, terminal, operational):
        with self.lock:
            if before != lane.state:
                raise RuntimeError('并行子批次检查点变化')
            updated = copy.deepcopy(self.checkpoint)
            wave = updated['_parallel_wave']; row = wave['lanes'][lane.index]
            if row['status'] == 'done':
                raise RuntimeError('并行子批次重复提交')
            lane.state = copy.deepcopy(after)
            row['state'] = copy.deepcopy(after); row['activity'] = after.get('activity', {})
            if step:
                if not tuple(row['start']) < tuple(after['cursor']) <= tuple(row['end']):
                    raise RuntimeError('并行阅读游标越界')
                delta = after['characters_read']-before['characters_read']
                if delta <= 0 or row.get('saved_characters', 0)+delta > row['characters']:
                    raise RuntimeError('并行阅读覆盖计数越界')
                updated['characters_read'] += delta
                updated['findings_count'] += after['findings_count']-before['findings_count']
                updated['batches'] += 1
                row['saved_characters'] = row.get('saved_characters', 0)+delta
                if after['cursor'] == row['end']:
                    if row['saved_characters'] != row['characters']:
                        raise RuntimeError('并行子批次扫描计数不完整')
                    row['status'] = 'done'
                else:
                    row['status'] = 'pending'
                    lane.state['batches'] = wave['next_batch_index']
                    wave['next_batch_index'] += 1
                    row['state'] = copy.deepcopy(lane.state)
            elif terminal:
                # A denied reservation isn't a completed source interval.
                row['status'] = 'budget_exhausted'
            elif after.get('adaptive_batch'):
                row['status'] = 'pending'
            result = self.save(self.decorate(updated), step=step)
            if step and row['status'] == 'done' or terminal:
                return {'turn_id': self.tid, 'sufficiency': 'partial'}
            return result

    def call(self, lane, model, system, payload, field, verification):
        from .person_activity import reading_output_schema, synthesis_output_schema, now
        from services.model_dispatch import background_generation
        prompt = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        output, context = call_limits(model)
        output = fit_output_budget(system, prompt, output, context)
        with self.lock:
            self.active()
            updated = copy.deepcopy(self.checkpoint)
            usage = init_usage({'max_tokens': self.spec['max_tokens']}, updated['token_usage'])
            reservation = None if updated['model_calls'] >= self.spec['model_call_budget'] else reserve_call(usage, system, prompt, output)
            if reservation is None:
                lane.state.update(budget_exhausted=True, token_budget_exhausted=bool(usage.get('exhausted')))
                updated['budget_exhausted'] = True
                updated['token_budget_exhausted'] = bool(usage.get('exhausted'))
                updated['_parallel_wave']['lanes'][lane.index]['state'] = copy.deepcopy(lane.state)
                self.save(self.decorate(updated))
                return None, copy.deepcopy(lane.state)
            activity = {'stage': 'reading', 'state': 'waiting_for_model', 'started_at': now(),
                        'summary': '核对本组事实与原文' if verification else '读取本组完整原文',
                        'output_token_limit': output,
                        'model': str(getattr(model, 'model', type(model).__name__))}
            updated['model_calls'] += 1; updated['token_usage'] = usage_snapshot(usage)
            row = updated['_parallel_wave']['lanes'][lane.index]
            row.update(status='running', reservation=reservation, activity=activity)
            lane.state.update(model_calls=updated['model_calls'], activity=activity)
            row['state'] = copy.deepcopy(lane.state)
            self.save(self.decorate(updated))

        def status(state):
            with self.lock:
                self.active()
                updated = copy.deepcopy(self.checkpoint)
                row = updated['_parallel_wave']['lanes'][lane.index]
                row['activity']['state'] = state
                if state == 'running':row['activity']['generation_started_at'] = now()
                lane.state['activity'] = copy.deepcopy(row['activity'])
                row['state'] = copy.deepcopy(lane.state)
                self.save(self.decorate(updated))

        response = None
        try:
            with background_generation(self.cancelled, 100, status):
                self.active()
                response = model.chat_json(system, prompt, required_fields=('scanned_scan_ids', 'findings') if field == 'findings' else (field,),
                    output_context='人物事实校核' if verification else '人物事实报告', max_tokens=output,
                    long_output=True, timeout=getattr(Config, 'MAIL_PERSON_READING_TIMEOUT_SECONDS', 600),
                    retries=0, structured_retry_limit=0,
                    output_schema=reading_output_schema(payload, 'activity_report') if field == 'findings' else synthesis_output_schema(payload))
        finally:
            with self.lock:
                updated = copy.deepcopy(self.checkpoint)
                usage = init_usage({'max_tokens': self.spec['max_tokens']}, updated['token_usage'])
                row = updated['_parallel_wave']['lanes'][lane.index]
                record_usage(usage, response, system, prompt, row['reservation'])
                if isinstance(response, dict) and str(response.get('finish_reason', '')).lower() in {'length', 'max_tokens', 'max_length'}:
                    usage['failed_calls'] += 1
                row['reservation'] = None
                row['activity']['state'] = 'validating'
                updated['token_usage'] = usage_snapshot(usage)
                lane.state.update(token_usage=usage_snapshot(usage), activity=copy.deepcopy(row['activity']))
                row['state'] = copy.deepcopy(lane.state)
                turn = self.repository.get_turn(self.tid, self.owner, include_events=False)
                self.save(self.decorate(updated), accounting_only=bool(self.cancelled() or not turn or turn['status'] in {'cancelled', 'cancelling'}))
        self.active()
        if isinstance(response, dict) and str(response.get('finish_reason', '')).lower() in {'length', 'max_tokens', 'max_length'}:
            raise ValueError('模型输出被截断，本批未跳过，可继续重试')
        return response.get('json', response) if isinstance(response, dict) else response, copy.deepcopy(lane.state)

    def plan(self):
        from .person_activity import _commit
        checkpoint = self.checkpoint
        if checkpoint.get('_parallel_wave'):
            # A process may have died after reserving tokens. Charge the bounded
            # unknown attempt once; never erase its cost or a verified interval.
            updated = copy.deepcopy(checkpoint)
            usage = init_usage({'max_tokens': self.spec['max_tokens']}, updated['token_usage'])
            for row in updated['_parallel_wave']['lanes']:
                if row.get('reservation'):
                    record_usage(usage, None, '', '', row['reservation'])
                    row['reservation'] = None
                if row['status'] != 'done':
                    row['status'] = 'pending'
                    row['state'].update(budget_exhausted=False, token_budget_exhausted=False)
            updated['token_usage'] = usage_snapshot(usage)
            self.save(self.decorate(updated))
            return
        cursor = list(checkpoint['cursor']); lanes = []
        tail, identity = checkpoint.get('_context_tail'), checkpoint.get('_identity_context')
        batch_index = checkpoint.get('_next_batch_index', checkpoint['batches'])
        for index in range(self.profile['concurrency']):
            budget = min(self.profile['char_budget'], checkpoint.get('adaptive_batch', {}).get('char_budget', self.profile['char_budget']))
            chunks, end = primary_chunks(self.repository, self.spec['package_id'], self.owner, self.selected, cursor, budget)
            if not chunks:
                break
            if end == cursor:
                raise RuntimeError('并行原文计划游标未推进')
            state = {k: copy.deepcopy(v) for k, v in checkpoint.items() if k not in {'_parallel_wave', 'parallel_reading', 'report'}}
            state.update(cursor=list(cursor), batches=batch_index+index, pending_token_reservation=None,
                         _context_tail=tail, _identity_context=identity)
            lanes.append({'start': list(cursor), 'end': list(end), 'budget': budget, 'state': state,
                          'characters': sum(len(c['text']) for c in chunks),
                          'message_count': len({c['message_key'] for c in chunks}), 'status': 'pending',
                          'segment_versions': {c['segment_id']: c['text_sha256'] for c in chunks}})
            # Preserve an identity anchor across consecutive splits of the same
            # source region, just as the serial reader does.
            context_chunks = list(chunks)
            if identity and all(identity.get(k) == chunks[0].get(k) for k in UNIT_FIELDS):
                context_chunks.insert(0, identity)
            tail, identity = tail_context(context_chunks, self.selected)
            cursor = list(end)
        if not lanes:
            if checkpoint['characters_read'] != self.spec['corpus']['characters']:
                raise RuntimeError('全包扫描计数不一致，禁止生成完整报告')
            self.save({**checkpoint, 'phase': 'assembly', 'scan_complete': True})
            return
        updated = copy.deepcopy(checkpoint)
        updated['_parallel_wave'] = {'start': checkpoint['cursor'], 'end': cursor, 'lanes': lanes,
                                     'next_batch_index': batch_index+len(lanes)}
        self.save(self.decorate(updated))

    def finish(self):
        updated = copy.deepcopy(self.checkpoint)
        wave = updated['_parallel_wave']; rows = wave['lanes']
        if all(row['status'] == 'done' for row in rows):
            last = rows[-1]['state']
            updated.update(cursor=wave['end'], _context_tail=last.get('_context_tail'),
                           _identity_context=last.get('_identity_context'), _next_batch_index=wave['next_batch_index'])
            updated.pop('_parallel_wave')
            updated['parallel_reading']['active_batches'] = 0
            return self.save(updated)
        if updated.get('budget_exhausted'):
            updated.update(complete=False, activity={'stage': 'reading', 'state': 'budget_exhausted',
                           'summary': '预算用完，已核查的并行批次保留，可继续未完成原文'})
            return self.save(updated, terminal=True)
        raise RuntimeError('并行阅读存在未完成区间，禁止推进完整覆盖')


def run_parallel_slice(repository, tid, owner, spec, selected, checkpoint, model, cancelled, profile):
    wave = Wave(repository, tid, owner, spec, selected, checkpoint, cancelled, profile)
    wave.plan()
    if not wave.checkpoint.get('_parallel_wave'):
        return {'turn_id': tid, '_requeue_slice': True}
    lanes = [Lane(wave, i) for i, row in enumerate(wave.checkpoint['_parallel_wave']['lanes']) if row['status'] != 'done']
    errors = []
    with ThreadPoolExecutor(max_workers=profile['concurrency'], thread_name_prefix='mail-person-raw') as pool:
        tasks = [pool.submit(lane.run, model) for lane in lanes]
        for task in as_completed(tasks):
            try:
                task.result()
            except BaseException as exc:
                errors.append(exc)
    if errors:
        raise errors[0]
    return wave.finish()

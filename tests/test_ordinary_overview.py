import copy
import re

from services.ordinary_overview import build_overview_presentation, translate_overview, translation_records
from test_ordinary_research_docx import report_fixture


class Translator:
    def __init__(self, transform):
        self.transform = transform
        self.calls = 0

    def chat_json(self, _system, prompt, **kwargs):
        import json
        self.calls += 1
        rows = json.loads(prompt.split('\n', 1)[1])
        return {'json': {'translations': self.transform(rows)}, 'model': 'local'}


def english_report():
    report = report_fixture()
    report['recommended_research_direction']['title'] = 'Verify noisy input boundaries'
    report['recommended_research_direction']['known_work'] = 'The method cannot improve accuracy above 0.4.'
    return report


def test_pending_body_is_chinese_and_appendix_keeps_full_original():
    report = english_report()
    original = copy.deepcopy(report)
    view = build_overview_presentation(report)
    body = ' '.join([view['direction']['title']] + [r['text'] for r in view['reasons']])
    assert not re.search('[A-Za-z]', body)
    assert '中文主题待核验' in view['direction']['title']
    assert report == original
    counter = view['evidence_appendix'][-1]
    assert counter['number'] == 14
    assert '不适用于噪声标准差>0.4' in counter['text']
    assert '反证线索' in counter['roles'] and '背景支撑' not in counter['roles']
    assert any(row['text'] == report['recommended_research_direction']['known_work'] for row in view['source_notes'])


def test_translation_preserves_negation_numbers_and_cache():
    report = english_report()
    model = Translator(lambda rows: [{'key': r['key'], 'text': '方法不能改善准确率超过0.4的情况。' if '0.4' in r['source'] else '验证噪声输入的适用边界'} for r in rows])
    translations, status = translate_overview(report, model)
    assert status['status'] == 'completed'
    assert model.calls == 1
    cached, status = translate_overview(report, model, translations)
    assert status['status'] == 'cached' and cached == translations and model.calls == 1
    view = build_overview_presentation(report, cached)
    assert view['direction']['title'] == '验证噪声输入的适用边界'
    assert '不能' in view['reasons'][0]['text']
    altered = copy.deepcopy(report)
    altered['recommended_research_direction']['known_work'] = 'The method cannot improve accuracy above 0.5.'
    view = build_overview_presentation(altered, cached)
    assert '尚待核验' in view['reasons'][0]['text']


def test_translation_rejects_changed_numbers_dropped_negation_duplicate_and_unknown_keys():
    report = english_report()
    rows = translation_records(report)
    model = Translator(lambda records: [{'key': r['key'], 'text': '方法改善准确率超过0.5。'} for r in records] + [{'key': rows[0]['key'], 'text': '另一个标题'}, {'key': 'unknown', 'text': '虚构内容'}])
    accepted, status = translate_overview(report, model)
    assert accepted == {} and status['status'] == 'partial'


def test_selected_and_alternative_evidence_share_stable_numbers():
    report = report_fixture()
    direction = report['recommended_research_direction']
    direction['candidate_id'] = 'chosen'
    alternative = copy.deepcopy(direction)
    alternative.update(candidate_id='other', title='备选方向')
    report['direction_candidates'] = [direction, alternative]
    view = build_overview_presentation(report)
    assert len(view['evidence_appendix']) == 14
    assert view['alternatives'][0]['evidence_numbers'] == view['direction']['evidence_numbers']
    assert len(view['source_notes']) == len({r['text'] for r in view['source_notes']})


def test_scientific_notation_is_chinese_without_changing_quantity():
    from services.ordinary_overview import _source, _valid_translation
    source = _source('Fig.1 e shows 6C, 62 Gt of CO2; SoC and X-CT.')
    assert '图1子图五' in source and '6倍倍率' in source and '62 十亿吨' in source
    assert '二氧化碳' in source and '荷电状态' in source
    assert not _valid_translation(source, '图1子图五显示5倍倍率和62十亿吨二氧化碳，荷电状态与断层成像。')
    assert _valid_translation(source, '图1子图五显示6倍倍率和62十亿吨二氧化碳，荷电状态与断层成像。')


def test_tied_scores_explain_temporary_selection_without_claiming_superiority():
    report = report_fixture()
    direction = report['recommended_research_direction']
    direction.update(candidate_id='first', score=70.9, score_breakdown={'goal_known': False})
    alternative = copy.deepcopy(direction)
    alternative.update(candidate_id='second', title='资源条件复核', score=70.9)
    report['direction_candidates']=[direction, alternative]
    view=build_overview_presentation(report)
    reason=view['reasons'][2]['text']
    assert '同为70.9' in reason and '不能证明' in reason and '稳定顺序' in reason
    assert '尚未明确研究目标' in reason
    assert '资源条件复核' in reason

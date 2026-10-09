"""Date typography is equivalent; calendar identities cannot be reassembled."""
import pytest
from services.mail.facts import normalize_date, date_mentions
from services.mail.research.verifier import verify_temporal, verify_translated_finding


@pytest.mark.parametrize('day',[('10','30'),('9','17')])
def test_real_chinese_date_layout(day):
    month,day=day
    claim=f'截止2026年{month}月{day}日。'
    source=f'截止2026 年 {month} 月 {day} 日。'
    assert normalize_date(source)==f'2026-{int(month):02}-{int(day):02}'
    assert verify_temporal(claim,source)
    assert verify_translated_finding(claim,source)


@pytest.mark.parametrize('space',[' ','\t','\u00a0','\u3000'])
def test_horizontal_spaces_only_change_date_layout(space):
    assert verify_temporal('2026-09-17',f'2026{space}年{space}9{space}月{space}17{space}日')


def test_every_complete_date_must_have_a_source_tuple():
    source='2026 年 9 月 17 日审批，2026 年 10 月 30 日到货。'
    assert verify_temporal('2026年10月30日到货，2026年9月17日审批。',source)
    assert not verify_temporal('2026年9月17日审批，2026年10月31日到货。',source)
    # All individual numeric components appear, but these tuples do not.
    assert not verify_temporal('2026年9月30日到货。',source)
    assert not verify_translated_finding('2026年10月17日。',source)


@pytest.mark.parametrize('claim,source',[
    ('2026年2月30日','2026年2月30日'),
    ('2026年2月30日，2026年9月17日','2026年9月17日'),
    ('2026年9月17日','2026年2月30日，2026年10月30日'),
    ('2026年9月17日','2026年9月18日'),
    ('2026年9月17日','2025年9月17日'),
    ('2026年9月17日','2026年10月17日'),
    ('2026年9月17日','2026 年。9 月。17 日。'),
    ('2026年9月17日','2026 年\n9 月\n17 日'),
])
def test_invalid_different_or_disjoint_date_sources_rejected(claim,source):
    assert not verify_temporal(claim,source)


def test_invalid_first_date_cannot_hide_valid_second_or_claimed_invalid_date():
    source='2026年2月30日；2026 年 9 月 17 日。'
    assert date_mentions(source)==[None,'2026-09-17']
    assert verify_temporal('2026年9月17日',source)
    assert not verify_temporal('2026年2月30日；2026年9月17日',source)


def test_multiple_multilingual_dates_preserve_complete_calendar_identity():
    source='17 September 2026; 30 ตุลาคม 2569'
    assert verify_temporal('2026年9月17日；2026年10月30日',source)
    assert not verify_temporal('2026年9月30日',source)

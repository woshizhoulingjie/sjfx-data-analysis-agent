"""Equivalent decimal formatting must not change evidence requirements."""
import pytest

from services.mail.research.verifier import verify_numeric, verify_translated_finding


@pytest.mark.parametrize('claim,source', [
    ('90000元/台', '90,000.00 元/台'),
    ('90,000.00元/台', '90000元/台'),
    ('90000.0000 CNY', '90000.00 CNY'),
    ('人民币９００００元', '人民币90,000.00元'),
    ('0.1000000000000000001 USD', '0.1000000000000000001000 USD'),
])
def test_equivalent_decimal_representation_is_supported(claim, source):
    assert verify_numeric(claim, source)


@pytest.mark.parametrize('claim,source', [
    ('90000.01元/台', '90,000.00元/台'),
    ('9000元/台', '90,000.00元/台'),
    ('900000元/台', '90,000.00元/台'),
    ('-90000元', '90000.00元'),
    ('0.1000000000000000002 USD', '0.1000000000000000001 USD'),
    ('90000 USD', '90000.00 CNY'),
    ('90000 THB', '90000.00 CNY'),
    ('90000万元', '90000.00元'),
    ('9万元', '90,000.00元'),
    ('审批预计2026年9月17日通过', '审批预计2026年9月16日通过'),
])
def test_value_currency_magnitude_and_date_changes_remain_rejected(claim, source):
    assert not verify_numeric(claim, source)


def test_finding_still_requires_a_date_to_be_present_in_its_own_quote():
    quote = '原则同意90,000.00元/台，须经财务审批通过后方可生效。'
    assert verify_translated_finding('原则同意90000元/台，须经财务审批通过后生效。', quote)
    assert not verify_translated_finding('原则同意90000元/台，须经财务审批通过后生效。\n2026年9月17日能否完成审批？', quote)
    assert verify_translated_finding('原则同意90000元/台，须经财务审批通过后生效。\n2026年9月17日能否完成审批？',
                                     quote + '\n审批流程预计于2026年9月17日前完成。')


def test_decimal_equivalence_does_not_remove_a_condition_or_negation_gate():
    quote = '尚未最终确认90,000.00元/台，须经财务审批通过后方可生效。'
    assert not verify_translated_finding('已最终确认90000元/台。', quote)
def test_repair_feedback_identifies_missing_numbers_without_relaxing_gate():
    from services.mail.research.verifier import numeric_mismatches,translated_numeric_source
    quote='价格90,000.00元/台，2026年10月30日前交付。'
    assert numeric_mismatches('财务是否2026年9月17日前审批？',translated_numeric_source(quote))==['9','17']
    assert numeric_mismatches('90000元/台',quote)==[]

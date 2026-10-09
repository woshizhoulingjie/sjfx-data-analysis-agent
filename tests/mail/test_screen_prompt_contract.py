"""Guard source-limited screening instructions without invoking a model."""
from services.mail.analysis import SCREEN_SYSTEM


def test_screen_rationale_preserves_modality_and_requires_a_quantity_baseline():
    assert 'Rationale may state only what this supplied mail slice proves' in SCREEN_SYSTEM
    assert 'expected future demand is not a purchase commitment' in SCREEN_SYSTEM
    assert 'priority inquiry is not a promise to purchase from that supplier' in SCREEN_SYSTEM
    assert 'earlier quantity and the change' in SCREEN_SYSTEM
    assert 'Do not infer a missing baseline' in SCREEN_SYSTEM


def test_screen_direct_background_labels_follow_user_criteria():
    assert 'directly relevant, background only, or unrelated' in SCREEN_SYSTEM
    assert 'Do not blur direct relevance and background' in SCREEN_SYSTEM
    assert 'direct matches only, background is not_matched' in SCREEN_SYSTEM
    assert 'not_matched applies ONLY to this supplied slice' in SCREEN_SYSTEM
    assert 'Preserve the actor' in SCREEN_SYSTEM

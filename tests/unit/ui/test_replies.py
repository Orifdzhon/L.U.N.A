from luna.core.models import Reply
from luna.core.prompts import fallback_text
from luna.ui.replies import is_silent


def test_apologies_are_silent_in_both_languages():
    for kind in ("cancelled", "timeout"):
        for lang in ("ru", "en"):
            assert is_silent(Reply(fallback_text(kind, lang), lang))


def test_normal_and_gave_up_replies_are_spoken():
    assert not is_silent(Reply("Привет", "ru"))
    assert not is_silent(Reply(fallback_text("gave_up", "en"), "en"))

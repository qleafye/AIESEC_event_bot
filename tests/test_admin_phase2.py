"""Phase 2 admin pure-helper tests: callback parse, card renderer, settings guide."""
from handlers.admin import (
    _parse_appr,
    _render_application_card,
    _render_settings_guide,
    APPROVAL_SETTINGS_DOC,
)


def test_parse_appr_with_id():
    assert _parse_appr("appr_approve:123") == ("appr_approve", 123)


def test_parse_appr_no_id():
    assert _parse_appr("appr_all") == ("appr_all", None)


def test_parse_appr_bad_id():
    assert _parse_appr("appr_x:abc") == ("appr_x", None)


def test_card_shows_position_and_escapes():
    user = {"full_name": "<b>hack</b>", "city": "Москва", "resume_file_id": "f1"}
    out = _render_application_card(user, 1, 3)
    assert "1/3" in out
    assert "<b>hack</b>" not in out  # escaped
    assert "&lt;b&gt;hack" in out
    assert "файлом" in out  # резюме файлом → кнопка ниже


def test_card_no_resume_marker():
    out = _render_application_card({"full_name": "Иван"}, 2, 2)
    assert "📎 Резюме: нет" in out


def test_card_shows_resume_text():
    # Таня п.4: текст-резюме виден прямо в карточке (обрезка длинного).
    out = _render_application_card({"full_name": "Иван", "resume_text": "мой богатый опыт"}, 1, 1)
    assert "Резюме (текст)" in out
    assert "мой богатый опыт" in out


def test_settings_guide_lists_keys_and_defaults():
    current = {key: None for key, _, _ in APPROVAL_SETTINGS_DOC}
    current["full_approval"] = "manual"
    out = _render_settings_guide(APPROVAL_SETTINGS_DOC, current)
    assert "pending_notify_mode" in out
    assert "full_approval" in out
    assert "(по умолчанию)" in out  # at least one unset key shows its default

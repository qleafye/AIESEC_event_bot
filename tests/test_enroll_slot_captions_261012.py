"""Подписи сессий на экране выбора слота: compact_template и экран слота."""
from __future__ import annotations

from database import db, session_enroll_db as edb
from domain.settings.schema import SETTINGS_SCHEMA
from services.forum.session_enroll import ENROLL_TEXT_KEYS, compact_template
from services.i18n.i18n_form_manual import FORM_DEFAULT_EN
from tests._enroll38 import CITY, ready, run
from tests._enroll38_chat import buttons, setup_world
from tests.test_session_enroll_flow_38 import press

T = "• <b>{title}</b> — {speaker}\nТрек: {track} · {competencies}"
FULL = {"title": "A", "speaker": "Иван", "track": "Карьера", "competencies": "🍀 Мышление"}


def test_compact_full():
    assert compact_template(T, FULL) == "• <b>A</b> — Иван\nТрек: Карьера · 🍀 Мышление"


def test_compact_no_speaker():
    assert compact_template(T, {**FULL, "speaker": ""}) == "• <b>A</b>\nТрек: Карьера · 🍀 Мышление"


def test_compact_no_competencies():
    assert compact_template(T, {**FULL, "competencies": ""}) == "• <b>A</b> — Иван\nТрек: Карьера"


def test_compact_no_speaker_no_competencies():
    assert compact_template(T, {**FULL, "speaker": " ", "competencies": ""}) == "• <b>A</b>\nТрек: Карьера"


def test_compact_no_track_no_competencies_one_line():
    assert compact_template(T, {**FULL, "track": "", "competencies": ""}) == "• <b>A</b> — Иван"


def test_compact_unknown_placeholder_and_plain_segments_stay():
    assert compact_template("{title} — {x} · просто", {"title": "A"}) == "A — {x} · просто"
    assert compact_template("", FULL) == ""


def test_registry_and_en():
    spec = SETTINGS_SCHEMA["session_enroll_session_line"]
    assert spec["group"] == "event" and spec["per_city"] is True
    assert set(spec["placeholders"]) == {"title", "speaker", "track", "competencies"}
    assert "session_enroll_session_line" in ENROLL_TEXT_KEYS
    assert spec["default"] == T and spec["default"] in FORM_DEFAULT_EN


async def _world():
    ids = await setup_world()
    c1 = await edb.create_competency(CITY, "🍀 Мышление")
    c2 = await edb.create_competency(CITY, "🤝 Команда")
    await edb.toggle_session_competency(ids["A"], c1)
    await edb.toggle_session_competency(ids["A"], c2)
    await db.update_program_session(ids["A"], speaker="Иван")
    await db.update_program_session(ids["B"], speaker="Мария")
    return ids


def test_slot_screen_blocks_in_button_order(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _world()
        tid = ids["business"]
        cb = await press(f"se:t:{tid}")
        text, kb = cb.message.last
        lines = text.split("\n")
        assert lines[0] == "30.10, 10:00–12:30. Выбери одну сессию:" and lines[1] == ""
        # трек делегата (Бизнес) первым: B, D, затем A, C — как кнопки
        order = [t for t in lines if t.startswith("• ")]
        assert order[0] == "• <b>Сессия B</b> — Мария" and order[1] == "• <b>Сессия D</b>"
        assert "• <b>Сессия A</b> — Иван\nТрек: Карьера · 🍀 Мышление, 🤝 Команда" in text
        assert "• <b>Сессия C</b>\nТрек: Карьера" in text  # ни спикера, ни компетенций
        assert "Сессия C</b> —" not in text and "Карьера ·" not in text.split("Сессия C")[1]
        titles = [t for t, _ in buttons(kb)]
        assert titles[0] == "⭐ Сессия B" and titles[1] == "⭐ Сессия D"
    run(go())


def test_slot_screen_escapes_html(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _world()
        await db.update_program_session(ids["A"], title="<b>x</b>", speaker="<i>y</i>")
        cb = await press(f"se:t:{ids['career']}")
        text = cb.message.last[0]
        assert "&lt;b&gt;x&lt;/b&gt;" in text and "&lt;i&gt;y&lt;/i&gt;" in text
        assert "<i>" not in text
    run(go())


def test_slot_screen_empty_template_only_header(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _world()
        await db.set_setting("session_enroll_session_line", "")  # очищен общий текст
        cb = await press(f"se:t:{ids['career']}")
        assert cb.message.last[0] == "30.10, 10:00–12:30. Выбери одну сессию:"
    run(go())

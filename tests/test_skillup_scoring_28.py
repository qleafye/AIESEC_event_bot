"""Phase 28 Plan 07 (SU-08, A-04, СкиллАп 5): автоскоринг заявки.

Три пласта сторожей:
- Задача 1: `reg_engine.compute_score`/`scoring_rules`/`course_number` — чистая формула ТЗ
  §3.6 без БД (таблица кейсов по образцу FINALIZE_GOLDEN).
- Задача 2: запись балла на финале (`services/reg_finalize.py`) и столбцы листа
  (`handlers/reg_schema.py`).
- Задача 3: балл в карточке модерации — бот (`handlers/admin_moderation.py`) и приложение
  (`services/applications.py`) одинаково; делегатские поверхности его не видят никогда.

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, фикстура временной
БД — тот же приём, что `tests/test_skillup_core_28.py::_ready(tmp_path)`.
"""
import asyncio
import inspect

from config import config
from database import db
import reg_engine
from services import reg_finalize as rf
import services.applications as applications
from handlers import admin_moderation as am
from handlers import reg_schema as rs

UID = 900800500


def _ready(tmp_path, name="test_skillup_scoring_28.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: compute_score / course_number / scoring_rules — чистая формула ТЗ §3.6
# ══════════════════════════════════════════════════════════════════════════════════════════

_RULES = {
    "it_fields": ["IT и технологии"],
    "senior_statuses": ["Магистратура/Аспирантура"],
    "readiness_counts": ["Готов(а) выйти сейчас", "Через 3–6 месяцев"],
    "experience_counts": ["Стажировка", "Коммерческий опыт до года", "Коммерческий опыт больше года"],
    "course_from": 3,
    "stack_from": 2,
}

_EMPTY_RULES = {
    "it_fields": [], "senior_statuses": [], "readiness_counts": [], "experience_counts": [],
    "course_from": None, "stack_from": None,
}


def test_course_number_leading_digit():
    assert reg_engine.course_number("3") == 3
    assert reg_engine.course_number("5+") == 5
    assert reg_engine.course_number("1") == 1


def test_course_number_none_for_non_numeric():
    assert reg_engine.course_number("Магистратура/Аспирантура") is None
    assert reg_engine.course_number("") is None
    assert reg_engine.course_number(None) is None


def test_compute_score_empty_answers_is_zero():
    score, is_it = reg_engine.compute_score({}, _RULES)
    assert score == 0
    assert is_it is False


def test_compute_score_empty_rules_is_zero_even_with_full_answers():
    """Пустые множества/пороги (дефолт четырёх списков + двух порогов) — оба «настраиваемых»
    слагаемых (направление/стек, курс/статус) нулевые, даже если делегат ответил на всё
    («выключенный по умолчанию скоринг не должен раздавать баллы этими пунктами»). Резюме —
    ЕДИНСТВЕННОЕ слагаемое без реестрового множества (`resume_type` проверяется напрямую),
    поэтому оно всё равно засчитывается — это не баг, а прямое чтение формулы ТЗ §3.6."""
    answers = {
        "study_field": "IT и технологии", "stack": "Python, Go, SQL и базы данных",
        "course": "5+", "education_status": "Магистратура/Аспирантура",
        "resume_type": "file", "readiness": "Готов(а) выйти сейчас",
        "experience": "Стажировка",
    }
    score, is_it = reg_engine.compute_score(answers, _EMPTY_RULES)
    assert score == 1  # только +1 за резюме
    assert is_it is False


def test_direction_addend_by_it_field():
    answers = {"study_field": "IT и технологии"}
    score, is_it = reg_engine.compute_score(answers, _RULES)
    assert score == 2
    assert is_it is False  # только первая половина условия


def test_direction_addend_by_stack_threshold():
    """«стек от N» — по числу ВЫБРАННЫХ подписей, а не по конкретным пунктам."""
    answers = {"stack": "Python, Go"}  # 2 пункта >= stack_from=2
    score, _is_it = reg_engine.compute_score(answers, _RULES)
    assert score == 2


def test_direction_addend_stack_below_threshold_does_not_fire():
    answers = {"stack": "Python"}  # 1 пункт < stack_from=2
    score, _is_it = reg_engine.compute_score(answers, _RULES)
    assert score == 0


def test_course_addend_by_threshold():
    answers = {"course": "3"}  # >= course_from=3
    score, _is_it = reg_engine.compute_score(answers, _RULES)
    assert score == 2


def test_course_addend_five_plus_equals_five():
    answers = {"course": "5+"}
    score, _is_it = reg_engine.compute_score(answers, _RULES)
    assert score == 2


def test_course_below_threshold_does_not_fire():
    answers = {"course": "2"}
    score, _is_it = reg_engine.compute_score(answers, _RULES)
    assert score == 0


def test_course_addend_by_senior_status_not_by_course_number():
    """«Магистратура/Аспирантура» не имеет ведущего числа (R-A3b) — засчитывается ТОЛЬКО через
    множество «старшие статусы», а не через порог курса."""
    answers = {"education_status": "Магистратура/Аспирантура"}
    score, _is_it = reg_engine.compute_score(answers, _RULES)
    assert score == 2


def test_resume_addend_file_or_link():
    assert reg_engine.compute_score({"resume_type": "file"}, _RULES)[0] == 1
    assert reg_engine.compute_score({"resume_type": "link"}, _RULES)[0] == 1


def test_resume_addend_fallback_file_id_or_url_without_fork():
    """События без развилки резюме никогда не кладут `resume_type` — запасной путь через
    `resume_file_id`/`resume_url`."""
    assert reg_engine.compute_score({"resume_file_id": "AgAD1"}, _RULES)[0] == 1
    assert reg_engine.compute_score({"resume_url": "https://cloud/x.pdf"}, _RULES)[0] == 1


def test_resume_addend_plain_text_does_not_fire():
    """Резюме голым текстом (без файла/ссылки) баллов не даёт — формула ТЗ считает только
    file/link."""
    assert reg_engine.compute_score({"resume_text": "2 года опыта"}, _RULES)[0] == 0


def test_readiness_addend():
    assert reg_engine.compute_score({"readiness": "Готов(а) выйти сейчас"}, _RULES)[0] == 1
    assert reg_engine.compute_score({"readiness": "После выпуска"}, _RULES)[0] == 0


def test_experience_addend():
    assert reg_engine.compute_score({"experience": "Стажировка"}, _RULES)[0] == 1
    assert reg_engine.compute_score({"experience": "Нет опыта"}, _RULES)[0] == 0


def test_max_score_is_seven():
    answers = {
        "study_field": "IT и технологии", "course": "5+",
        "resume_type": "file", "readiness": "Готов(а) выйти сейчас",
        "experience": "Стажировка",
    }
    score, is_it = reg_engine.compute_score(answers, _RULES)
    assert score == 7
    assert is_it is True


def test_is_it_3plus_requires_both_halves():
    """`is_it_3plus` — направление/стек И курс/статус ОДНОВРЕМЕННО, а не сумма баллов."""
    only_direction = {"study_field": "IT и технологии", "readiness": "Готов(а) выйти сейчас"}
    score, is_it = reg_engine.compute_score(only_direction, _RULES)
    assert score >= 3  # набрал баллы (направление + готовность)
    assert is_it is False  # но НЕ обе половины


def test_compute_score_is_pure_function_signature():
    """Сигнатура ровно `compute_score(answers, rules) -> (score, is_it_3plus)` — тот же класс
    функции, что `decide_status` (без БД/aiogram)."""
    sig = inspect.signature(reg_engine.compute_score)
    assert list(sig.parameters) == ["answers", "rules"]
    assert not inspect.iscoroutinefunction(reg_engine.compute_score)


def test_scoring_rules_reads_registry(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("score_it_fields", "IT и технологии")
        await db.set_setting("score_course_from", "4")
        return await reg_engine.scoring_rules()

    rules = _run(go())
    assert rules["it_fields"] == ["IT и технологии"]
    assert rules["course_from"] == 4
    assert rules["senior_statuses"] == []
    assert rules["stack_from"] == 2  # реестровый дефолт ключа (не «пусто»)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: запись балла на финале + столбцы листа
# ══════════════════════════════════════════════════════════════════════════════════════════

async def _seed_scoring_rules():
    await db.set_setting("reg_scoring_enabled", "on")
    await db.set_setting("score_it_fields", "IT и технологии")
    await db.set_setting("score_senior_statuses", "Магистратура/Аспирантура")
    await db.set_setting("score_readiness_counts", "Готов(а) выйти сейчас")
    await db.set_setting("score_experience_counts", "Стажировка")
    await db.set_setting("score_course_from", "3")
    await db.set_setting("score_stack_from", "2")


def test_score_written_on_new(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_scoring_rules()
        draft = {
            "telegram_id": UID, "kind": "new",
            "answers": {
                "full_name": "Иван Иванов", "study_field": "IT и технологии",
                "course": "5+", "resume_type": "file", "readiness": "Готов(а) выйти сейчас",
                "experience": "Стажировка",
            },
        }
        await rf.finalize_data(UID, "@ivan", draft)
        return await db.get_user(UID)

    user = _run(go())
    assert user["score"] == 7
    assert user["is_it_3plus"] == 1


def test_score_recomputed_on_edit(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_scoring_rules()
        draft_new = {
            "telegram_id": UID, "kind": "new",
            "answers": {"full_name": "Иван Иванов", "study_field": "Бизнес и управление"},
        }
        await rf.finalize_data(UID, "@ivan", draft_new)
        before = await db.get_user(UID)

        # Правка меняет направление на IT — балл ОБЯЗАН пересчитаться, хотя diff не про балл.
        draft_edit = {
            "telegram_id": UID, "kind": "edit",
            "answers": {"study_field": "IT и технологии"}, "updated_by": "bot",
        }
        await rf.finalize_data(UID, "@ivan", draft_edit)
        after = await db.get_user(UID)
        return before, after

    before, after = _run(go())
    assert before["score"] == 0
    assert after["score"] == 2


def test_edit_without_changes_does_not_rewrite_unchanged_score(tmp_path):
    """D-14: правка без фактических изменений — не событие; безусловный пересчёт всё равно
    не пишет лишний UPDATE, если значение не изменилось (тот же дух, что diff())."""
    _ready(tmp_path)

    async def go():
        await _seed_scoring_rules()
        draft_new = {
            "telegram_id": UID, "kind": "new",
            "answers": {"full_name": "Иван Иванов", "study_field": "IT и технологии"},
        }
        await rf.finalize_data(UID, "@ivan", draft_new)
        # Правка с тем же самым значением поля — diff пустой.
        draft_edit = {
            "telegram_id": UID, "kind": "edit",
            "answers": {"study_field": "IT и технологии"}, "updated_by": "bot",
        }
        result = await rf.finalize_data(UID, "@ivan", draft_edit)
        user = await db.get_user(UID)
        return result, user

    result, user = _run(go())
    assert result["changed_columns"] == []
    assert user["score"] == 2


def test_no_score_when_disabled(tmp_path):
    """Выключенный `reg_scoring_enabled` (дефолт off) — колонки в БД пустые, столбцов листа
    нет вовсе (D-06)."""
    _ready(tmp_path)

    async def go():
        draft = {
            "telegram_id": UID, "kind": "new",
            "answers": {"full_name": "Иван Иванов", "study_field": "IT и технологии", "course": "5+"},
        }
        await rf.finalize_data(UID, "@ivan", draft)
        user = await db.get_user(UID)
        headers = await rs.active_sheet_headers()
        return user, headers

    user, headers = _run(go())
    assert user.get("score") is None
    assert user.get("is_it_3plus") in (None, 0)
    assert "Балл" not in headers
    assert "IT 3+" not in headers


def test_sheet_has_score_columns_when_enabled(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_scoring_enabled", "on")
        return await rs.active_sheet_headers()

    headers = _run(go())
    assert "Балл" in headers
    assert "IT 3+" in headers
    # Место — сразу после системной колонки «Детали».
    assert headers.index("Балл") == headers.index("Детали") + 1
    assert headers.index("IT 3+") == headers.index("Балл") + 1


def test_scoring_failure_does_not_lose_application(tmp_path, monkeypatch):
    """Падение `scoring_rules` — заявка всё равно сохранена (T-28-07-03, тот же принцип, что у
    season-резолва в finalize_data)."""
    _ready(tmp_path)

    async def boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(reg_engine, "scoring_rules", boom)

    async def go():
        await db.set_setting("reg_scoring_enabled", "on")
        draft = {
            "telegram_id": UID, "kind": "new",
            "answers": {"full_name": "Иван Иванов"},
        }
        result = await rf.finalize_data(UID, "@ivan", draft)
        user = await db.get_user(UID)
        return result, user

    result, user = _run(go())
    assert result["mode"] == "new"
    assert user is not None and user["full_name"] == "Иван Иванов"
    assert user.get("score") is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: балл в карточке модерации — бот и приложение одинаково; делегату — нигде
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_bot_card_shows_score_line_after_track():
    user = {
        "full_name": "Иван Иванов", "participant_type": "party_overnight",
        "score": 5, "is_it_3plus": 1,
    }
    out = am._render_application_card(user, 1, 1, scoring_enabled=True)
    lines = out.splitlines()
    track_idx = next(i for i, l in enumerate(lines) if l.startswith("🎉 Трек"))
    score_idx = next(i for i, l in enumerate(lines) if l.startswith("🧮 Балл"))
    assert score_idx == track_idx + 1
    assert "🧮 Балл: 5/7" in lines[score_idx]
    assert "IT 3+" in lines[score_idx]


def test_bot_card_hides_score_when_scoring_disabled():
    user = {"full_name": "Иван Иванов", "score": 5, "is_it_3plus": 1}
    out = am._render_application_card(user, 1, 1)  # scoring_enabled default False
    assert "Балл" not in out


def test_bot_card_hides_score_when_none_even_if_enabled():
    user = {"full_name": "Иван Иванов", "score": None}
    out = am._render_application_card(user, 1, 1, scoring_enabled=True)
    assert "Балл" not in out


def test_it_badge_absent_when_false():
    user = {"full_name": "Иван Иванов", "score": 3, "is_it_3plus": 0}
    out = am._render_application_card(user, 1, 1, scoring_enabled=True)
    assert "Балл: 3/7" in out
    assert "IT 3+" not in out


def test_web_card_badges_neutral_and_ordered(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_scoring_enabled", "on")
        await db.add_user({"telegram_id": UID, "full_name": "Иван Иванов", "registration_date": "2026-09-07 10:00:00"})
        await db.update_user_answers(
            UID, {"score": 6, "is_it_3plus": 1}, allowed_columns=["score", "is_it_3plus"]
        )
        user = await db.get_user(UID)
        return await applications.card_payload(user)

    payload = _run(go())
    kinds = [b["kind"] for b in payload["badges"]]
    assert "score" in kinds and "it_3plus" in kinds
    assert kinds.index("score") < kinds.index("it_3plus")
    score_badge = next(b for b in payload["badges"] if b["kind"] == "score")
    assert score_badge["text"] == "Балл: 6/7"
    it_badge = next(b for b in payload["badges"] if b["kind"] == "it_3plus")
    assert it_badge["text"] == "IT 3+"
    # Нейтральные чипы — не входят в EDITED_BADGE_KINDS фронта (applications.js).
    assert "score" not in applications.__dict__.get("EDITED_BADGE_KINDS", ())


def test_web_card_it_badge_absent_when_false(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_scoring_enabled", "on")
        await db.add_user({"telegram_id": UID, "full_name": "Иван Иванов", "registration_date": "2026-09-07 10:00:00"})
        await db.update_user_answers(
            UID, {"score": 2, "is_it_3plus": 0}, allowed_columns=["score", "is_it_3plus"]
        )
        user = await db.get_user(UID)
        return await applications.card_payload(user)

    payload = _run(go())
    kinds = [b["kind"] for b in payload["badges"]]
    assert "score" in kinds
    assert "it_3plus" not in kinds


def test_web_card_no_score_badge_when_disabled(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.add_user({"telegram_id": UID, "full_name": "Иван Иванов", "registration_date": "2026-09-07 10:00:00"})
        await db.update_user_answers(
            UID, {"score": 6, "is_it_3plus": 1}, allowed_columns=["score", "is_it_3plus"]
        )
        user = await db.get_user(UID)
        return await applications.card_payload(user)

    payload = _run(go())
    kinds = [b["kind"] for b in payload["badges"]]
    assert "score" not in kinds
    assert "it_3plus" not in kinds


def test_cards_texts_identical_between_surfaces():
    """Байт-в-байт паритет текста балла между ботом и приложением (D-09) — общая
    константа-шаблон `services.applications.score_badge_text`, не две копии."""
    score = 4
    web_text = applications.score_badge_text(score)
    assert web_text == "Балл: 4/7"
    user = {"full_name": "Иван Иванов", "score": score, "is_it_3plus": 0}
    bot_line = am._render_application_card(user, 1, 1, scoring_enabled=True)
    assert web_text in bot_line
    assert applications.IT_3PLUS_BADGE_TEXT == "IT 3+"


def test_delegate_surfaces_never_expose_score():
    """Балл не попадает ни в сводку анкеты (`reg_engine.summary_fields`), ни в профиль
    Mini App делегата — сторож-тест по всему плану (T-28-07-01)."""
    import pathlib

    answers = {
        "full_name": "Иван Иванов", "score": 6, "is_it_3plus": 1,
        "study_field": "IT и технологии",
    }
    fields = reg_engine.summary_fields(answers)
    labels = [label for label, _value in fields]
    assert "Балл" not in labels
    assert not any("score" in str(v).lower() for _l, v in fields)

    profile_path = pathlib.Path(__file__).resolve().parent.parent / "miniapp" / "routers" / "profile.py"
    if profile_path.exists():
        src = profile_path.read_text(encoding="utf-8")
        assert "score" not in src.lower()
        assert "is_it_3plus" not in src.lower()

"""Phase 27 (27-04, LANG-06) — главный сторож фазы: английская подпись ЛЮБОГО select/multi
шага анкеты обязана вернуться русским каноном (`reg_engine.canonical_option`) и пройти
`reg_engine.validate_answer` без ошибки. Табличный прогон по ВСЕМ шагам с вариантами
(`_CHOICE_STEPS`, `_BESPOKE_CHOICE`, `_MEMBERSHIP_STEPS`, `SELECT_CONFIG`, `MULTI_CONFIG`), а не
по образцам — расползание Google-таблицы на два языка ловится здесь, не глазами (27-CONTEXT.md).

pytest-asyncio в этом окружении нет — каждый async-вызов через asyncio.run() (см.
tests/test_db_phase5.py).
"""
import asyncio

from config import config
from database import db
import reg_engine as re
from services.i18n import src_hash


def _db_ready(tmp_path, name="test_i18n_options_roundtrip_27.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _fake_tr_map(all_canons: set[str]) -> dict[str, str]:
    """Перевод = 'EN:' + канон — подписи гарантированно отличаются от канона (кроме тех, что
    уже покрыты ярусом A — UI_EN побеждает всегда, см. services/i18n.py::tr, это ожидаемо)."""
    return {src_hash(canon): f"EN:{canon}" for canon in all_canons}


# ── Все шаги с вариантами (Task 2 бакеты, буквально по тексту плана) ────────────────────────

def _steps_with_options() -> list[str]:
    steps = set(re._CHOICE_STEPS) | set(re._BESPOKE_CHOICE) | set(re._MEMBERSHIP_STEPS)
    steps |= set(re.SELECT_CONFIG) | set(re.MULTI_CONFIG)
    return sorted(steps)


def _is_multi(step_key: str) -> bool:
    return re.REG_STEP_TYPES.get(step_key) == "multi" or step_key in re.MULTI_CONFIG


def test_english_label_roundtrips_to_russian_canon_for_every_option(tmp_path):
    _db_ready(tmp_path)

    async def go():
        steps = _steps_with_options()
        assert steps  # sanity: buckets are non-empty in the current schema

        all_canons: set[str] = set()
        per_step_options: dict[str, list[str]] = {}
        for step in steps:
            opts = await re.options(step)
            per_step_options[step] = opts
            all_canons.update(opts)

        tr_map = _fake_tr_map(all_canons)

        for step in steps:
            opts = per_step_options[step]
            if not opts:
                continue  # реестровый override может дать пустой список -- нечего катать
            pairs = await re.option_pairs(step, "en", tr_map)
            assert [c for c, _l in pairs] == opts, step
            for canon, label in pairs:
                got = re.canonical_option(pairs, label)
                assert got == canon, (step, canon, label, got)
                if canon == "Другое":
                    # "Другое" -- НЕ обычный ответ, а литерал-триггер «напиши свой вариант»
                    # (`_BESPOKE_CHOICE`/`_CHOICE_STEPS` в reg_engine.validate_answer): round-
                    # trip до канона обязан сработать (проверено строкой выше), а ошибка от
                    # validate_answer здесь ОЖИДАЕМА и не является провалом сторожа.
                    continue
                raw = [got] if _is_multi(step) else got
                value, error = re.validate_answer(step, raw)
                assert error is None, (step, canon, label, error)

    asyncio.run(go())


def test_membership_steps_english_literals_roundtrip(tmp_path):
    """_MEMBERSHIP_STEPS: «Yes»/«No»/«Offline»/«Online»/«Online only» -> канон, без ошибки
    «Выбери «Да» или «Нет»» и подобных."""
    _db_ready(tmp_path)

    async def go():
        cases = {
            "work_status": [("Yes", "Да"), ("No", "Нет")],
            "informal_day": [("Yes", "Да"), ("No", "Нет"), ("Online only", "Буду только в онлайне")],
            "attendance_format": [("Offline", "Offline"), ("Online", "Online")],
        }
        for step, pairs_in in cases.items():
            opts = await re.options(step)
            pairs = await re.option_pairs(step, "en", {})
            assert [c for c, _l in pairs] == opts
            for en_label, expected_canon in pairs_in:
                got = re.canonical_option(pairs, en_label)
                assert got == expected_canon, (step, en_label, got)
                value, error = re.validate_answer(step, got)
                assert error is None, (step, en_label, error)

    asyncio.run(go())


def test_service_words_other_and_skip():
    """Служебные слова яруса A -- канонизация работает даже БЕЗ пар конкретного шага (пустой
    список pairs), т.к. это financial fallback (ступень 3 canonical_option)."""
    assert re.canonical_option([], "Other") == "Другое"
    assert re.canonical_option([], "Skip") == "Пропустить"
    assert re.canonical_option([], "Yes") == "Да"
    assert re.canonical_option([], "No") == "Нет"


def test_free_text_returns_none_for_choice_step(tmp_path):
    """Шаг с variants (city, other_allowed) -- текст, которого нет ни среди вариантов, ни в
    ярусе A, -- свободный ввод: canonical_option отдаёт None, вызывающий сохраняет как есть."""
    _db_ready(tmp_path)

    async def go():
        pairs = await re.option_pairs("city", "en", {})
        assert re.canonical_option(pairs, "Мой особенный город") is None

    asyncio.run(go())


def test_russian_identity_pairs_are_the_same_object(tmp_path):
    """lang == 'ru' -- подпись это ТОТ ЖЕ объект, что канон (`is`), не копия (tr() identity,
    обязательное условие для test_miniapp_labels_drift/test_reg_engine_parity)."""
    _db_ready(tmp_path)

    async def go():
        for step in _steps_with_options():
            opts = await re.options(step)
            if not opts:
                continue
            pairs = await re.option_pairs(step, "ru", {})
            for canon, label in pairs:
                assert label is canon, (step, canon)
            # round-trip тождественный -- лестница резолюции сразу выигрывает на шаге 1.
            for canon, label in pairs:
                assert re.canonical_option(pairs, label) == canon

    asyncio.run(go())


def test_empty_translation_map_falls_back_to_russian_and_roundtrips(tmp_path):
    """Модуль включён, но перевод конкретного шага ещё не готов (пустая карта) -- fail-soft:
    подписи русские, round-trip всё равно сходится (canonical_option матчит по каноническому
    значению -- ступень 2 лестницы)."""
    _db_ready(tmp_path)

    async def go():
        for step in _steps_with_options():
            opts = await re.options(step)
            if not opts:
                continue
            pairs = await re.option_pairs(step, "en", {})
            for canon in opts:
                assert re.canonical_option(pairs, canon) == canon

    asyncio.run(go())

"""Phase 30 (30-04, задача 1, A2-05) — сторож формата `repeatable` и двуформатного чтения
`mini_portfolio`: `parse_repeatable`/`dump_repeatable`/`repeatable_display`/`repeatable_max` —
единственные правила формата (30-04-PLAN.md `<interfaces>`), и лист/карточка заявки
(`handlers/reg_schema.py::SHEET_COLUMNS`) читают их же, а не собственную копию.

pytest-asyncio недоступен — async через `asyncio.run()`, фикстура временной БД — тот же приём,
что `tests/test_reg_drafts.py::_ready(tmp_path)`.
"""
import asyncio
import json

from config import config
from database import db as db_mod

import reg_engine
from handlers import reg_schema


def _ready(tmp_path, name="reg_repeatable.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db_mod.init_db())


# ── parse_repeatable: дуальное чтение (JSON-список / legacy-текст / пусто) ─────────────────

def test_parse_repeatable_reads_legacy_free_text_as_single_block_without_title():
    items = reg_engine.parse_repeatable("Делал сайт для клиента, полгода фултайм")
    assert items == [{"title": "", "description": "Делал сайт для клиента, полгода фултайм"}]


def test_parse_repeatable_reads_json_list_as_is():
    raw = json.dumps([
        {"title": "Стажировка", "description": "Маркетинг, три месяца"},
        {"title": "Пет-проект", "description": "Бот для друзей"},
    ], ensure_ascii=False)
    items = reg_engine.parse_repeatable(raw)
    assert items == [
        {"title": "Стажировка", "description": "Маркетинг, три месяца"},
        {"title": "Пет-проект", "description": "Бот для друзей"},
    ]


def test_parse_repeatable_broken_json_falls_back_to_legacy_block():
    items = reg_engine.parse_repeatable('[{"title": "оборвано"')
    assert items == [{"title": "", "description": '[{"title": "оборвано"'}]


def test_parse_repeatable_empty_or_dash_gives_empty_list():
    assert reg_engine.parse_repeatable(None) == []
    assert reg_engine.parse_repeatable("") == []
    assert reg_engine.parse_repeatable("-") == []


def test_parse_repeatable_accepts_already_parsed_list_from_mini_app():
    items = reg_engine.parse_repeatable([{"title": "A", "description": "B"}, "голый текст"])
    assert items == [{"title": "A", "description": "B"}, {"title": "", "description": "голый текст"}]


# ── dump_repeatable: сериализация (единственная точка записи) ──────────────────────────────

def test_dump_repeatable_roundtrips_through_parse_repeatable():
    items = [{"title": "Стажировка", "description": "Три месяца"}, {"title": "", "description": "Просто текст"}]
    dumped = reg_engine.dump_repeatable(items)
    assert reg_engine.parse_repeatable(dumped) == items


def test_dump_repeatable_does_not_escape_cyrillic():
    dumped = reg_engine.dump_repeatable([{"title": "Стажировка", "description": ""}])
    assert "\\u" not in dumped
    assert "Стажировка" in dumped


# ── repeatable_display: человекочитаемая строка для листа/карточки ─────────────────────────

def test_repeatable_display_joins_title_and_description_with_dash_and_semicolon():
    items = [
        {"title": "Стажировка", "description": "Маркетинг, три месяца"},
        {"title": "Пет-проект", "description": "Бот для друзей"},
    ]
    assert reg_engine.repeatable_display(items) == (
        "Стажировка — Маркетинг, три месяца; Пет-проект — Бот для друзей"
    )


def test_repeatable_display_does_not_drop_block_without_description():
    items = [{"title": "Стажировка", "description": ""}, {"title": "Пет-проект", "description": "Бот"}]
    assert reg_engine.repeatable_display(items) == "Стажировка; Пет-проект — Бот"


def test_repeatable_display_of_legacy_block_shows_bare_text():
    items = reg_engine.parse_repeatable("Делал сайт для клиента")
    assert reg_engine.repeatable_display(items) == "Делал сайт для клиента"


def test_repeatable_display_of_empty_list_is_empty_string():
    assert reg_engine.repeatable_display([]) == ""


# ── repeatable_max: число из реестра, не тумблер (T-30-10) ─────────────────────────────────

def test_repeatable_max_returns_configured_default_when_unset(tmp_path):
    _ready(tmp_path)
    limit = asyncio.run(reg_engine.repeatable_max("mini_portfolio"))
    assert isinstance(limit, int) and limit > 0


# ── SHEET_COLUMNS «Портфолио»: единая точка форматирования для листа И карточки заявки ─────

def _portfolio_value(data: dict) -> str:
    for header, _gate, fn in reg_schema.SHEET_COLUMNS:
        if header == "Портфолио":
            return fn(data)
    raise AssertionError("колонка «Портфолио» не найдена в SHEET_COLUMNS")


def test_sheet_column_renders_legacy_text_answer_as_human_text():
    value = _portfolio_value({"mini_portfolio": "Делал сайт для клиента"})
    assert value == "Делал сайт для клиента"
    assert "['" not in value
    assert "[{" not in value


def test_sheet_column_renders_json_blocks_as_human_text_without_json_syntax():
    raw = json.dumps([{"title": "Стажировка", "description": "Маркетинг"}], ensure_ascii=False)
    карточка_заявки = _portfolio_value({"mini_portfolio": raw})
    строка_листа = _portfolio_value({"mini_portfolio": raw})
    assert карточка_заявки == "Стажировка — Маркетинг"
    assert "['" not in карточка_заявки
    assert "[{" not in строка_листа


def test_sheet_column_renders_dash_for_empty_answer():
    assert _portfolio_value({}) == "-"
    assert _portfolio_value({"mini_portfolio": None}) == "-"
    assert _portfolio_value({"mini_portfolio": "-"}) == "-"


# ── validate_answer: repeatable-ветка — только когда raw уже список (Mini App) ─────────────

def test_validate_answer_repeatable_accepts_list_and_serializes_to_json():
    value, error = reg_engine.validate_answer(
        "mini_portfolio", [{"title": "Стажировка", "description": "Три месяца"}],
    )
    assert error is None
    assert json.loads(value) == [{"title": "Стажировка", "description": "Три месяца"}]


def test_validate_answer_repeatable_string_input_stays_plain_text_legacy_path():
    """Инвариант «выключенный v2 = поведение прежнее»: голая строка (сегодняшний бот) НЕ
    оборачивается в JSON — второй копии формата в записи нет, JSON пишется, только когда
    клиент явно прислал список."""
    value, error = reg_engine.validate_answer("mini_portfolio", "Просто текст без структуры")
    assert error is None
    assert value == "Просто текст без структуры"


def test_validate_answer_repeatable_enforces_max_items():
    too_many = [{"title": f"Проект {i}", "description": "x"} for i in range(3)]
    value, error = reg_engine.validate_answer(
        "mini_portfolio", too_many, repeatable_max_items=2,
    )
    assert value is None
    assert error is not None and "2" in error


def test_validate_answer_repeatable_enforces_per_item_length():
    value, error = reg_engine.validate_answer(
        "mini_portfolio", [{"title": "x" * 500, "description": "ok"}],
    )
    assert value is None
    assert error is not None

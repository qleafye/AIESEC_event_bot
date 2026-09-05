"""Phase 25 (CITYQ-01) — городская ось вопросов анкеты: сторож паритета выключенного модуля
и обратимости композита трек×город.

Назначение файла (контракт, а не просто набор проверок), по образцу
`tests/test_content_percity_offparity.py`/`tests/test_settings_percity_resolver.py`:

    «Композит `{base}{TRACK}__city__{CODE}` собирается и разбирается обратно без потерь;
    выключенный модуль городов — нулевая разница с сегодняшним поведением; включённый модуль
    резолвит вопросы/тексты/режим резюме по трёхтрековому порядку, зафиксированному в
    CONTEXT.md.»

pytest-asyncio недоступен в этом окружении — каждый async-хелпер гоняется через
asyncio.run(), config.DB_PATH указывает на файл в tmp_path (та же конвенция, что у соседей).
"""
import asyncio

from config import config
from database import db
import cities
import reg_engine as e
import settings_ops
from handlers import admin_reg_config
from handlers import reg_schema


def _db_ready(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


async def _module_on():
    await db.set_setting("event_city_enabled", "on")


# ── 1) Обратимость композита трек×город ────────────────────────────────────────────────

def test_roundtrip_composite_key_for_every_track_suffix():
    suffixes = list(settings_ops.REG_QUESTION_TRACK_SUFFIXES) + [""]
    for suffix in suffixes:
        base = f"reg_q_goal{suffix}"
        composed = cities.per_city_key(base, "spb")
        assert composed == f"{base}__city__spb"
        assert cities.split_per_city_key(composed) == (base, "spb")
        assert settings_ops.base_setting_key(composed) == base


def test_roundtrip_rejects_reverse_order_track_after_city():
    # Обратный порядок (город ДО трека) — код после __city__ содержит "__party" и не входит
    # в city_codes(), поэтому split_per_city_key обязан отвергнуть его целиком. Это и есть
    # причина, по которой формат зафиксирован как {base}{TRACK}__city__{CODE}, а не наоборот.
    reverse = "reg_q_goal__city__spb__party"
    assert cities.split_per_city_key(reverse) is None


# ── 2) Паритет при выключенном модуле городов ──────────────────────────────────────────

def test_module_off_parity_across_all_four_new_surfaces(tmp_path):
    """Свежая база: `event_city_enabled` НЕ выставляется вовсе. Городские переопределения на
    все четыре новых кандидата посеяны, но ни один не должен повлиять ни на один резолвер —
    иначе городской слой протёк бы мимо гейта `cities_module_on()`."""
    _db_ready(tmp_path, "test_percity25_module_off.db")

    async def scenario():
        await db.set_setting("reg_q_formats__city__spb", "off")
        await db.set_setting("reg_q_formats__party__city__spb", "off")
        await db.set_setting("reg_prompt_expectations__city__spb", "Городской текст ожиданий")
        await db.set_setting("reg_resume_mode__city__spb", "text_only")

        for city_code in ("spb", None):
            enabled_full = await e.is_step_enabled_for_track("reg_q_formats", None, city_code)
            enabled_full_global = await e.is_step_enabled_for_track("reg_q_formats", None, None)
            assert enabled_full == enabled_full_global

        steps_spb = await e.enabled_steps({"event_city": "spb"})
        steps_global = await e.enabled_steps({})
        assert steps_spb == steps_global

        prompt_spb = await e.prompt("expectations", None, "spb")
        prompt_global = await e.prompt("expectations", None, None)
        assert prompt_spb == prompt_global
        assert "Городской" not in prompt_spb

        help_spb = await e.help_text("resume", None, "spb")
        help_global = await e.help_text("resume", None, None)
        assert help_spb == help_global

        mode_spb = await e.resume_mode("spb")
        mode_global = await e.resume_mode(None)
        assert mode_spb == mode_global == "file_or_text"

    asyncio.run(scenario())


# ── 3) Позитив: модуль включён, три трека, три разных ветки резолюции ─────────────────

def test_full_track_reads_city_composite_without_track_suffix(tmp_path):
    _db_ready(tmp_path, "test_percity25_full_track.db")

    async def scenario():
        await _module_on()
        await db.set_setting("reg_q_formats", "on")
        await db.set_setting("reg_q_formats__city__spb", "off")
        result = await e.is_step_enabled_for_track("reg_q_formats", None, "spb")
        assert result is False
        # Другой город без своего переопределения -- как везде (on).
        result_msk = await e.is_step_enabled_for_track("reg_q_formats", None, "msk")
        assert result_msk is True

    asyncio.run(scenario())


def test_party_track_reads_city_composite_then_falls_back_to_bare_party(tmp_path):
    _db_ready(tmp_path, "test_percity25_party_track.db")

    async def scenario():
        await _module_on()
        # Городской__party композит побеждает.
        await db.set_setting("reg_q_formats__party", "on")
        await db.set_setting("reg_q_formats__party__city__spb", "off")
        result = await e.is_step_enabled_for_track("reg_q_formats", "party_overnight", "spb")
        assert result is False

        # Городского __party нет -- падаем на голый __party.
        await db.delete_setting("reg_q_formats__party__city__spb")
        result2 = await e.is_step_enabled_for_track("reg_q_formats", "party_overnight", "spb")
        assert result2 is True

    asyncio.run(scenario())


def test_short_track_reads_city_composite_else_false(tmp_path):
    _db_ready(tmp_path, "test_percity25_short_track.db")

    async def scenario():
        await _module_on()
        await db.set_setting("reg_q_formats__short__city__spb", "on")
        result = await e.is_step_enabled_for_track("reg_q_formats", "short", "spb")
        assert result is True

        await db.delete_setting("reg_q_formats__short__city__spb")
        # Ни городского __short, ни голого __short -- short ничего не наследует.
        result2 = await e.is_step_enabled_for_track("reg_q_formats", "short", "spb")
        assert result2 is False

    asyncio.run(scenario())


def test_party_without_track_override_ignores_global_city_composite(tmp_path):
    """Закрепление решения CONTEXT: party БЕЗ своего трекового переопределения читает
    ГЛОБАЛЬНЫЙ базовый ключ, а не городской слой над ним -- `reg_q_X__city__C` доступен
    только full-треку. Будущая «оптимизация» не должна тихо это поменять."""
    _db_ready(tmp_path, "test_percity25_party_no_override.db")

    async def scenario():
        await _module_on()
        await db.set_setting("reg_q_formats", "on")
        await db.set_setting("reg_q_formats__city__spb", "off")
        # Нет ни reg_q_formats__party, ни reg_q_formats__party__city__spb.
        result = await e.is_step_enabled_for_track("reg_q_formats", "party_overnight", "spb")
        assert result is True, "party обязан унаследовать глобальный reg_q_formats, не городской"

    asyncio.run(scenario())


# ── 4) Тексты вопроса и режим резюме ────────────────────────────────────────────────────

def test_city_prompt_override_wins_over_global(tmp_path):
    _db_ready(tmp_path, "test_percity25_prompt_city.db")

    async def scenario():
        await _module_on()
        await db.set_setting("reg_prompt_expectations", "Глобальный текст ожиданий")
        await db.set_setting("reg_prompt_expectations__city__spb", "Городской текст ожиданий")
        result = await e.prompt("expectations", None, "spb")
        assert result == "Городской текст ожиданий"
        result_msk = await e.prompt("expectations", None, "msk")
        assert result_msk == "Глобальный текст ожиданий"

    asyncio.run(scenario())


def test_resume_mode_text_only_by_city_else_file_or_text(tmp_path):
    _db_ready(tmp_path, "test_percity25_resume_mode.db")

    async def scenario():
        await _module_on()
        await db.set_setting("reg_resume_mode__city__spb", "text_only")
        assert await e.resume_mode("spb") == "text_only"
        assert await e.resume_mode("msk") == "file_or_text"

    asyncio.run(scenario())


def test_resume_text_only_default_prompt_and_help_text(tmp_path):
    _db_ready(tmp_path, "test_percity25_resume_text_only.db")

    async def scenario():
        await _module_on()
        await db.set_setting("reg_resume_mode__city__spb", "text_only")
        prompt_text = await e.prompt("resume", None, "spb")
        assert prompt_text == (
            "Опиши вкратце свой опыт участия в проектах / активностях и, если есть, опыт "
            "работы. Например: был организатором школьных мероприятий, был куратором в "
            "университете и т. п."
        )
        help_text = await e.help_text("resume", None, "spb")
        assert help_text == "Коротко, текстом в чате."

    asyncio.run(scenario())


# ── 5) Пресеты не задевают городские ключи ─────────────────────────────────────────────

def test_presets_do_not_touch_city_override_keys(tmp_path):
    _db_ready(tmp_path, "test_percity25_presets.db")

    async def scenario():
        await _module_on()
        await db.set_setting("reg_q_goal__city__spb", "off")

        await admin_reg_config._apply_event_preset("forum")
        assert await db.get_setting("reg_q_goal__city__spb") == "off"

        await reg_schema._apply_party_preset()
        assert await db.get_setting("reg_q_goal__city__spb") == "off"

        await reg_schema._apply_short_preset()
        assert await db.get_setting("reg_q_goal__city__spb") == "off"

    asyncio.run(scenario())


# ── 6) Выключенный по городу "goal" не ломает сохранение ──────────────────────────────

def test_city_disabled_goal_does_not_break_validate_and_apply():
    """`validate_answer`/`apply_answer` не знают про enabled_steps/city вовсе -- шаг просто
    не попадёт в список опрашиваемых, а сама механика ответа не меняется."""
    value, error = e.validate_answer("goal", ["Найти возможность трудоустройства"])
    assert error is None
    assert value == "Найти возможность трудоустройства"

    result = e.apply_answer({}, "goal", value)
    assert result["goal"] == value

"""Приёмка 15.09 (Анкета 2.0 на стенде, девять тумблеров `reg_form_*` включены): карточка
«Образование» поглощает свои под-вопросы, «шаг N из M» считает то, что делегат реально увидит,
ответы «да/нет» показываются словами, а плитка «Другое» знает, что обязана раскрыть поле.

pytest-asyncio в окружении нет (см. шапку `tests/test_reg_composite_edu_260915.py`) — только
`asyncio.run()`.
"""
import asyncio

import pytest

from config import config
from database import db as bot_db
from database.db import init_db, set_setting

import reg_engine
from reg_engine import (
    advance_anchor,
    composite_absorbed_steps,
    form_spec,
    step_spec,
)
from tests.test_miniapp_frontend import MINIAPP_STATIC, SCREENS_DIR, _js_without_comments
from tests.test_miniapp_routes import (
    DELEGATE_ID,
    UNREGISTERED_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)

FORM_SCREEN_JS = SCREENS_DIR / "form.js"


@pytest.fixture
def client(tmp_path):
    """Тот же харнесс HTTP-контракта анкеты, что `tests/test_miniapp_form.py` — второй копии
    подписи initData/временной БД в проекте не заводим."""
    cfg_path = _use_tmp_db(tmp_path, "reg_form_v2_uat_260915_routes.db")
    _standard_seed()
    return _client(_cfg(cfg_path))

_EDU_PARTS = ("university", "course", "study_field")

_ALL_ON_FLAGS = {
    "v2_enabled": True, "chips": True, "lookup_search": True, "edu_card": True,
    "repeatable": True, "limit_counter": True, "status_screen": True,
    "header_settings": True, "haptics": True,
}
_ALL_OFF_FLAGS = {name: False for name in reg_engine.FORM_V2_TOGGLE_KEYS}


def _ready(tmp_path, name="reg_form_v2_uat_260915.db", *, v2=True):
    """Стенд приёмки: девять тумблеров `reg_form_*` включены, `edu_conditional` выключен
    (иначе ВУЗ/курс/программа не попадают в набор шагов, пока делегат не ответил «учусь», и
    поглощать нечего), вопрос «Амбассадор» включён (дефолт `off`)."""
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(init_db())
    for key in reg_engine.FORM_V2_TOGGLE_KEYS:
        asyncio.run(set_setting(f"reg_form_{key}", "on" if v2 else "off"))
    asyncio.run(set_setting("edu_conditional", "off"))
    asyncio.run(set_setting("reg_q_ambassador", "on"))


def _steps(answers=None):
    spec = asyncio.run(form_spec(answers or {}, "full", None))
    return spec, [row["key"] for row in spec["steps"]]


# ── п.3б «дальше почему-то пошёл вопрос про вуз» ───────────────────────────────────────────

def test_composite_parts_are_not_separate_steps_when_card_is_on(tmp_path):
    _ready(tmp_path)
    _spec, keys = _steps()
    assert "education_status" in keys, "шаг-карточка обязан остаться"
    for part in _EDU_PARTS:
        assert part not in keys, f"{part} рисуется ВНУТРИ карточки, второго экрана быть не должно"


def test_composite_parts_stay_separate_steps_when_card_is_off(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_off.db", v2=False)
    _spec, keys = _steps()
    assert "education_status" in keys
    for part in _EDU_PARTS:
        assert part in keys, "выключенная карточка = четыре обычных шага, как сегодня"


def test_absorbed_steps_empty_without_card_flag():
    assert composite_absorbed_steps(_ALL_OFF_FLAGS) == {}
    assert composite_absorbed_steps({**_ALL_ON_FLAGS, "edu_card": False}) == {}


def test_absorbed_steps_map_parts_to_toggle_step():
    absorbed = composite_absorbed_steps(_ALL_ON_FLAGS)
    assert absorbed == {part: "education_status" for part in _EDU_PARTS}


# ── п.6 «сбита нумерация» ──────────────────────────────────────────────────────────────────

def test_progress_total_counts_only_steps_delegate_will_see(tmp_path):
    _ready(tmp_path)
    spec, keys = _steps()
    assert spec["progress"]["total"] == len(keys)
    assert len(keys) == len(set(keys)), "дублей шагов в мастере быть не должно"


def test_card_shrinks_step_count_by_number_of_absorbed_parts(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_count_on.db")
    _spec_on, keys_on = _steps()
    _ready(tmp_path, "reg_form_v2_uat_count_off.db", v2=False)
    _spec_off, keys_off = _steps()
    absorbed_present = [part for part in _EDU_PARTS if part in keys_off]
    assert absorbed_present, "фикстура обязана включать хотя бы одну часть карточки"
    assert len(keys_off) - len(keys_on) == len(absorbed_present)


# ── п.3б/п.3в: следующий шаг после карточки — тот, что ЗА группой ─────────────────────────

def test_advance_anchor_jumps_over_the_whole_education_group(tmp_path):
    _ready(tmp_path)
    enabled = asyncio.run(reg_engine.enabled_steps({"participant_type": "full"}))
    anchor = advance_anchor("education_status", enabled, _ALL_ON_FLAGS)
    group = [key for key in enabled if key in ("education_status",) + _EDU_PARTS]
    assert anchor == group[-1]
    assert anchor != "education_status", "иначе мастер переспрашивает ВУЗ отдельным экраном"


def test_advance_anchor_is_identity_when_card_is_off(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_anchor_off.db", v2=False)
    enabled = asyncio.run(reg_engine.enabled_steps({"participant_type": "full"}))
    assert advance_anchor("education_status", enabled, _ALL_OFF_FLAGS) == "education_status"


def test_advance_anchor_is_identity_for_step_outside_any_group():
    assert advance_anchor("age", ["age", "phone"], _ALL_ON_FLAGS) == "age"


# ── п.8 «на итоговом просмотре анкеты амбассадор false» ───────────────────────────────────

def test_boolean_answer_gets_human_display(tmp_path):
    _ready(tmp_path)
    # `answers` — словарь КОЛОНОК (так его собирает черновик/`answers_from_user_row`), у шага
    # «Амбассадор» колонка называется иначе, чем шаг.
    column = reg_engine.STEP_TO_COLUMN["ambassador"]
    spec_yes, _keys = _steps({column: True})
    row_yes = next(row for row in spec_yes["steps"] if row["key"] == "ambassador")
    assert row_yes["value"] is True
    assert row_yes["display"] == "Да"

    spec_no, _keys = _steps({column: False})
    row_no = next(row for row in spec_no["steps"] if row["key"] == "ambassador")
    assert row_no["display"] == "Нет"


def test_text_answer_has_no_display_override(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_display_text.db")
    spec, _keys = _steps({"age": 19})
    row = next(row for row in spec["steps"] if row["key"] == "age")
    assert row.get("display") in (None, "")


# ── п.5 «Другое: напиши свой вариант — ПИСАТЬ НЕГДЕ» ──────────────────────────────────────

def test_other_option_published_for_step_with_other_in_options(tmp_path):
    _ready(tmp_path)
    spec = asyncio.run(step_spec("source", "full", None, flags=_ALL_ON_FLAGS))
    assert spec["degraded_kind"] == "select"
    assert spec["other_option"] == reg_engine.OTHER_OPTION
    assert reg_engine.OTHER_OPTION in spec["options"]


def test_other_option_published_for_other_allowed_step(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_other_allowed.db")
    spec = asyncio.run(step_spec("local_committee", "full", None, flags=_ALL_ON_FLAGS))
    assert spec["other_allowed"] is True
    assert spec["other_option"] == reg_engine.OTHER_OPTION


def test_other_option_absent_for_closed_list_step(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_other_closed.db")
    spec = asyncio.run(step_spec("alumni_status", "full", None, flags=_ALL_ON_FLAGS))
    assert "other_option" not in spec


def test_other_option_absent_when_new_form_is_off(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_other_off.db", v2=False)
    spec = asyncio.run(step_spec("source", "full", None, flags=_ALL_OFF_FLAGS))
    assert spec["degraded_kind"] == "legacy"
    assert "other_option" not in spec


# ══════════════════════════════════════════════════════════════════════════════════════════
# Приёмка 16.09 — «анкета 2.0 в приложении»
# ══════════════════════════════════════════════════════════════════════════════════════════

# ── «в анкете в мини-аппе если заполнять сразу не пишется ФИО» ────────────────────────────

def test_full_name_is_the_first_step_of_the_app_wizard(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_full_name.db")
    spec = asyncio.run(form_spec({}, "full", None, ask_full_name=True))
    keys = [row["key"] for row in spec["steps"]]
    assert keys[0] == "full_name"
    assert keys.count("full_name") == 1


def test_full_name_step_carries_bot_label_prompt_and_validator(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_full_name_spec.db")
    spec = asyncio.run(form_spec({}, "full", None, ask_full_name=True))
    row = spec["steps"][0]
    assert row["label"] != row["key"], "подпись шага обязана быть человеческой"
    assert row["column"] == "full_name"
    assert row["required"] is True
    assert row["prompt"] == reg_engine.PROMPT_DEFAULTS["full_name"]
    assert reg_engine.validate_answer("full_name", "Мария")[1], "одно слово — не ФИО"
    assert reg_engine.validate_answer("full_name", "Иванова Мария") == ("Иванова Мария", None)


def test_chat_step_list_does_not_gain_full_name(tmp_path):
    """`enabled_steps` — общий список для бота: ФИО там означало бы второй вопрос об имени."""
    _ready(tmp_path, "reg_form_v2_uat_full_name_chat.db")
    enabled = asyncio.run(reg_engine.enabled_steps({"participant_type": "full"}))
    assert "full_name" not in enabled
    spec = asyncio.run(form_spec({}, "full", None))
    assert [row["key"] for row in spec["steps"]][0] != "full_name"


def test_app_asks_full_name_and_saves_it(client):
    resp = client.get("/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["steps"][0]["key"] == "full_name"

    patch = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": body["version"], "answers": {"full_name": "Иванова Мария"},
              "step": "full_name"},
    )
    assert patch.status_code == 200, patch.text
    row = asyncio.run(bot_db.get_reg_draft(UNREGISTERED_ID))
    assert row["answers"]["full_name"] == "Иванова Мария"
    # Указатель «следующий шаг» не должен залипнуть на ФИО (его нет в `enabled_steps`).
    assert row["step"] != "full_name"


def test_app_rejects_single_word_full_name_with_bot_text(client):
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "answers": {"full_name": "Мария"}, "step": "full_name"},
    )
    assert resp.status_code == 400
    assert resp.json()["errors"]["full_name"] == reg_engine.validate_answer("full_name", "Мария")[1]


def test_submit_without_full_name_is_400_with_human_text(client):
    _set("reg_q_age", "on")
    patch = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "answers": {"age": "20"}, "step": "age"},
    )
    assert patch.status_code == 200, patch.text
    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "invalid"
    assert "full_name" in body["errors"]
    assert body["errors"]["full_name"] == reg_engine.validate_answer("full_name", None)[1]


# ── «не показывается 4-й город» ───────────────────────────────────────────────────────────

def test_city_fork_in_app_sees_cities_added_after_start(client):
    """Живой стенд: в `.env` три города, менеджер завёл четвёртый в админке — бот его
    показывал, приложение нет. `reload_cities()` зовёт только процесс бота; веб обязан
    подтягивать справочник сам (`cities.ensure_cities_fresh` в `_load_context`)."""
    import cities

    cities.set_cities_for_test([
        {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    ])
    cities._cities_loaded_at = 0.0
    asyncio.run(bot_db.insert_city("msk", "Москва", "", 0))
    asyncio.run(bot_db.insert_city("spb", "Санкт-Петербург", "СПб", 1))
    asyncio.run(bot_db.insert_city("tyumen", "Тюмень", "Тюмень", 2))
    asyncio.run(bot_db.insert_city("kzn", "Казань", "Казань", 3))
    _set("event_city_enabled", "on")

    resp = client.get("/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    fork = [it for it in resp.json()["pre_items"] if it.get("field") == "event_city"]
    assert fork, "развилка города обязана быть, когда модуль включён"
    codes = [o["code"] for o in fork[0]["options"]]
    assert codes == ["msk", "spb", "tyumen", "kzn"], codes


# ── «при отправке текста в резюме пишется, что не дошло до сервера» ───────────────────────
#
# Дропзона резюме отдаёт текстовый ответ объектом `{text: "..."}` (`form.js::fileControl`), а
# карточка-композит — объектом `{step_key: value}`. `screens/form.js::goNext` различал их ПО
# ФОРМЕ значения, поэтому текст резюме уезжал колонкой «text»: `400 bad_field`, который клиент
# не умеет положить под поле, — делегат видел общий текст «не дошло до сервера».

def test_text_resume_body_from_app_is_accepted(client):
    """То, что клиент шлёт ПОСЛЕ фикса: обёртка лежит в значении колонки шага, не заменяет её."""
    _set("reg_q_resume", "on")
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"resume_text": {"text": "Два года в маркетинге"}},
              "step": "resume"},
    )
    assert resp.status_code == 200, resp.text
    row = asyncio.run(bot_db.get_reg_draft(DELEGATE_ID))
    assert row["answers"]["resume_text"] == "Два года в маркетинге"


def test_text_resume_body_of_old_client_is_the_reported_400(client):
    """Регресс-документация живого бага: «text» — не колонка анкеты, ответ 400 `bad_field`
    не несёт `errors`, и экран не может показать его под полем."""
    _set("reg_q_resume", "on")
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"text": "Два года в маркетинге"}, "step": "resume"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"reason": "bad_field", "field": "text"}


# ── «кнопку "Пропустить" ставим под местом, где вписываем ответ — внизу не видно» ─────────

def test_skip_button_lives_inside_the_field_not_in_the_footer():
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "field-skip" in text, "у кнопки пропуска должен быть свой класс под полем"
    idx_skip = text.index("if (spec.skip_label")
    footer = text[text.rindex('class: "task-actions"', 0, idx_skip):idx_skip]
    assert "skip_label" not in footer, footer
    block = text[idx_skip:]
    block = block[:block.index("const headerSettings")]
    assert "insertBefore" in block and "errorZone" in block, block
    assert "goSkip" in block, block


def test_skip_button_style_is_not_a_full_width_primary_cta():
    css = (MINIAPP_STATIC / "app.css").read_text(encoding="utf-8")
    rule = css[css.index(".field-skip {"):]
    rule = rule[:rule.index("}")]
    assert "width: auto" in rule, rule
    assert "var(--tap-min)" in rule, rule


# ── «при автозаполнении сразу переходить на следующий вопрос» ─────────────────────────────

def test_wizard_advances_on_committed_pick_through_the_same_go_next():
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "opts.commit" in text
    block = text[text.index("if (opts && opts.commit)"):]
    block = block[:block.index("});")]
    # Тот же переход, что кнопка «Далее», и только когда значение прошло клиентскую проверку.
    assert "goNext()" in block, block
    assert "currentMainDisabled()" in block, block


def test_composite_patch_is_recognised_by_spec_not_by_value_shape():
    """Структурный сторож: признак composite-патча — спека шага (`spec.composite`), а не
    форма значения; иначе `{text: ...}` дропзоны снова уедет колонкой «text»."""
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "isCompositePatch" in text
    branch = text[text.index("const isCompositePatch"):]
    branch = branch[:branch.index(";")]
    assert "spec.composite" in branch, branch

"""Видимость и отображение заданий, план 32-04 (D-02, D-24, D-25, D-27, D-28).

Три группы:
- «без срока» — задание с меткой `database.db.NO_DEADLINE_AT` нигде не показывает служебную
  метку, не считается просроченным, не получает штрафа (D-27);
- штраф на карточке ДО сдачи — одна формула, видна заранее, при нулевом проценте карточка не
  меняется вовсе (D-25);
- видимость/порядок для амбассадора — ОДНА функция фильтра и ОДИН пересорт для бота и Mini
  App разом (D-28, D-24, D-31/D-38 через `eligible_wave_ids`).

Конвенция вызова — как в tests/test_game_task_order_260919.py: прямые вызовы чистых функций
`game_labels`, БД поднимается только там, где нужен реестр (`get_setting_typed`); pytest-asyncio
в окружении нет, асинхронные функции гоняются через `asyncio.run`.
"""
import asyncio

import game_labels
from config import config
from database import db


ADMIN_ID = 320401


def _db_ready(tmp_path, name="test_game_task_visibility_32.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _task(**kwargs) -> dict:
    defaults = dict(
        id=1, text="Описание задания", title="Задание",
        category="Light", coins=100, proof_type="photo",
        deadline_at="2099-01-01 00:00:00",
    )
    defaults.update(kwargs)
    return defaults


# ── «без срока»: task_deadline / task_deadline_short / task_deadline_text ─────────────────

def test_no_deadline_marker_gives_none_not_the_sentinel_date():
    task = _task(deadline_at=db.NO_DEADLINE_AT)
    assert game_labels.task_deadline(task) is None
    assert game_labels.task_has_deadline(task) is False


def test_no_deadline_short_is_empty_string_not_raw_label():
    task = _task(deadline_at=db.NO_DEADLINE_AT)
    assert game_labels.task_deadline_short(task) == ("", False)


def test_task_with_real_deadline_still_has_deadline():
    task = _task(deadline_at="2099-01-01 00:00:00")
    assert game_labels.task_has_deadline(task) is True
    assert game_labels.task_deadline(task) is not None


def test_garbage_and_missing_deadline_behave_as_before():
    # Мусор и отсутствующее поле — НЕ то же самое, что «дедлайна нет» (сентинел): поведение
    # для них не менялось этим планом (только сентинел получил новую ветку).
    assert game_labels.task_deadline_short(_task(deadline_at="ерунда")) == ("ерунда", False)
    assert game_labels.task_deadline_short({"id": 1}) == ("—", False)


def test_no_deadline_text_uses_registry_key(tmp_path):
    _db_ready(tmp_path)
    task = _task(deadline_at=db.NO_DEADLINE_AT)
    text = asyncio.run(game_labels.task_deadline_text(task))
    assert text == "без срока"  # дефолт game_task_no_deadline_text из settings_schema.py


def test_deadline_text_is_short_date_when_deadline_exists(tmp_path):
    _db_ready(tmp_path)
    task = _task(deadline_at="2026-05-05 12:00:00")
    assert asyncio.run(game_labels.task_deadline_text(task)) == "05.05"


# ── task_deadline_admin: менеджерский, синхронный, без реестра ────────────────────────────

def test_deadline_admin_no_deadline_both_formats():
    task = _task(deadline_at=db.NO_DEADLINE_AT)
    assert game_labels.task_deadline_admin(task) == "без срока"
    assert game_labels.task_deadline_admin(task, fmt="%d.%m.%Y %H:%M") == "без срока"


def test_deadline_admin_bot_format():
    task = _task(deadline_at="2026-12-31 23:59:00")
    assert game_labels.task_deadline_admin(task) == "31.12 23:59"


def test_deadline_admin_web_editor_format():
    task = _task(deadline_at="2026-12-31 23:59:00")
    assert game_labels.task_deadline_admin(task, fmt="%d.%m.%Y %H:%M") == "31.12.2026 23:59"


def test_deadline_admin_does_not_touch_registry(tmp_path):
    # Реестр НЕ поднят (config.DB_PATH не выставлен на реальную БД) — если бы помощник читал
    # настройку, вызов упал бы. Синхронная функция, значит просто отработает.
    task = _task(deadline_at=db.NO_DEADLINE_AT)
    assert game_labels.task_deadline_admin(task) == "без срока"


def test_deadline_admin_fail_soft_on_garbage():
    assert game_labels.task_deadline_admin(_task(deadline_at="ерунда")) == "ерунда"
    assert game_labels.task_deadline_admin({"id": 1}) == "—"


# ── задание без срока никогда не просрочено и стоит в хвосте открытых ─────────────────────

def test_no_deadline_task_not_in_overdue_group():
    now = game_labels.msk_now()
    from datetime import timedelta
    rows = [
        _task(id=1, deadline_at=db.NO_DEADLINE_AT),
        _task(id=2, deadline_at=(now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")),  # просрочено
        _task(id=3, deadline_at=(now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")),  # открыто
    ]
    order = [t["id"] for t in game_labels.sort_tasks_for_delegate(rows)]
    # Открытые (3, потом 1 — без срока в хвосте своей группы) идут раньше просроченного (2).
    assert order == [3, 1, 2]


# ── penalized_coins: единственная формула штрафа ───────────────────────────────────────────

def test_penalized_coins_rounds_down_and_clamps():
    assert game_labels.penalized_coins(100, 30) == 70
    assert game_labels.penalized_coins(99, 30) == 70  # 99*30//100 = 29 -> 99-29 = 70, вниз
    assert game_labels.penalized_coins(100, 0) == 100
    assert game_labels.penalized_coins(100, 150) == 0  # процент выше 100 приводится к границе
    assert game_labels.penalized_coins(100, -10) == 100  # отрицательный приводится к 0


# ── penalty_hint_line: видна заранее, молчит при нулевом штрафе или без срока ──────────────

def test_penalty_hint_line_none_without_deadline(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("game_late_penalty_percent", "30"))
    task = _task(deadline_at=db.NO_DEADLINE_AT, coins=100)
    assert asyncio.run(game_labels.penalty_hint_line(task)) is None


def test_penalty_hint_line_none_when_percent_zero(tmp_path):
    _db_ready(tmp_path)
    # game_late_penalty_percent не выставлен -> дефолт 0 (settings_schema.py).
    task = _task(deadline_at="2099-01-01 00:00:00", coins=100)
    assert asyncio.run(game_labels.penalty_hint_line(task)) is None


def test_penalty_hint_line_shows_both_amounts(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("game_late_penalty_percent", "30"))
    task = _task(deadline_at="2099-01-01 00:00:00", coins=100)
    line = asyncio.run(game_labels.penalty_hint_line(task))
    assert line is not None
    assert "70" in line
    assert "100" in line


# ── карточка: без срока, штраф, байт-в-байт при нулевом проценте ───────────────────────────

def test_card_has_no_sentinel_substring_for_no_deadline_task(tmp_path):
    _db_ready(tmp_path)
    task = _task(deadline_at=db.NO_DEADLINE_AT)
    card = asyncio.run(game_labels.render_task_card_text(task, "новое", None))
    assert "9999" not in card
    assert "без срока" in card


def test_card_byte_identical_at_zero_percent(tmp_path):
    """При game_late_penalty_percent = 0 (дефолт на каждом живом событии, CLAUDE.md) карточка
    задания СО СРОКОМ обязана остаться байт-в-байт такой же, как до этого плана — строка
    собирается вручную по прежней формуле («до {dd.mm}», без строки штрафа) и сравнивается с
    результатом обновлённой render_task_card_text."""
    _db_ready(tmp_path)
    from settings_schema import get_setting_typed
    task = _task(deadline_at="2026-05-05 12:00:00", coins=20, category="Medium")

    async def _old_style_expected() -> str:
        import html
        title = html.escape(db.task_title(task))
        category = await game_labels.category_label(task["category"])
        deadline, overdue = game_labels.task_deadline_short(task)
        lines = [f"<b>{title}</b>", f"{category} · {task['coins']}🪙 · до {deadline}"]
        if overdue:
            lines.append(await get_setting_typed("game_task_overdue_hint_text"))
        status_label = await get_setting_typed("game_task_detail_status_label")
        lines.append(status_label.format(status="новое"))
        lines.append(f"Нужно прислать: {await game_labels.proof_types_label(task.get('proof_type'))}")
        lines.append("")
        lines.append(f"<blockquote expandable>{html.escape(str(task['text']))}</blockquote>")
        return "\n".join(lines)

    expected = asyncio.run(_old_style_expected())
    actual = asyncio.run(game_labels.render_task_card_text(task, "новое", None))
    assert actual == expected


def test_card_contains_penalty_amounts_at_nonzero_percent(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("game_late_penalty_percent", "30"))
    task = _task(deadline_at="2099-01-01 00:00:00", coins=100)
    card = asyncio.run(game_labels.render_task_card_text(task, "новое", None))
    assert "70" in card
    assert "100" in card


# ── visible_tasks_for: одна функция видимости для бота и Mini App ─────────────────────────

def test_non_ambassador_does_not_see_ambassador_only_task():
    tasks = [_task(id=1, audience="ambassadors"), _task(id=2, audience="all")]
    visible = game_labels.visible_tasks_for(tasks, is_ambassador=False, eligible_wave_ids=set())
    assert [t["id"] for t in visible] == [2]


def test_ambassador_sees_ambassador_only_task():
    tasks = [_task(id=1, audience="ambassadors"), _task(id=2, audience="all")]
    visible = game_labels.visible_tasks_for(tasks, is_ambassador=True, eligible_wave_ids=set())
    assert {t["id"] for t in visible} == {1, 2}


def test_empty_audience_column_reads_as_all():
    tasks = [_task(id=1, audience=None), _task(id=2, audience="")]
    visible = game_labels.visible_tasks_for(tasks, is_ambassador=False, eligible_wave_ids=set())
    assert {t["id"] for t in visible} == {1, 2}


def test_ambassador_joined_mid_wave_does_not_see_that_wave_but_sees_wave_free_tasks():
    # D-31: вступил посреди волны 5 -> волна 5 недоступна (не в eligible_wave_ids), задания вне
    # волн (wave_id пуст) видны всегда. D-38: то же правило закрывает вернувшегося.
    tasks = [
        _task(id=1, wave_id=5),
        _task(id=2, wave_id=None),
        _task(id=3, wave_id=6),
    ]
    visible = game_labels.visible_tasks_for(tasks, is_ambassador=True, eligible_wave_ids={6})
    assert {t["id"] for t in visible} == {2, 3}


def test_non_ambassador_wave_filter_does_not_apply():
    # Не-амбассадору фильтр по волне не нужен вовсе — его отсекает audience, а не wave_id.
    tasks = [_task(id=1, wave_id=5, audience="all")]
    visible = game_labels.visible_tasks_for(tasks, is_ambassador=False, eligible_wave_ids=set())
    assert [t["id"] for t in visible] == [1]


def test_visible_tasks_for_does_not_mutate_input():
    tasks = [_task(id=1, audience="ambassadors"), _task(id=2)]
    before = list(tasks)
    game_labels.visible_tasks_for(tasks, is_ambassador=False, eligible_wave_ids=set())
    assert tasks == before


# ── sort_tasks_for_ambassador: перестановка, а не фильтр ───────────────────────────────────

def test_sort_for_ambassador_is_a_permutation_not_a_filter():
    tasks = [_task(id=1, category="Light"), _task(id=2, category="Referral", audience="ambassadors"),
              _task(id=3, category="Hard")]
    sorted_tasks = game_labels.sort_tasks_for_ambassador(tasks, is_ambassador=True, path="invite")
    assert {t["id"] for t in sorted_tasks} == {1, 2, 3}
    assert len(sorted_tasks) == len(tasks)


def test_sort_for_ambassador_path_none_matches_delegate_sort_byte_for_byte():
    tasks = [_task(id=1, category="Light"), _task(id=2, category="Referral")]
    plain = game_labels.sort_tasks_for_delegate(tasks)
    ambassador_order = game_labels.sort_tasks_for_ambassador(tasks, is_ambassador=False, path=None)
    assert [t["id"] for t in ambassador_order] == [t["id"] for t in plain]


def test_sort_for_ambassador_invite_path_puts_referral_above_same_deadline_light():
    tasks = [
        _task(id=1, category="Light", deadline_at="2099-01-01 00:00:00"),
        _task(id=2, category="Referral", deadline_at="2099-01-01 00:00:00"),
    ]
    order = game_labels.sort_tasks_for_ambassador(tasks, is_ambassador=True, path="invite")
    assert [t["id"] for t in order] == [2, 1]


def test_sort_for_ambassador_content_path_puts_non_referral_above_referral():
    tasks = [
        _task(id=1, category="Referral", deadline_at="2099-01-01 00:00:00"),
        _task(id=2, category="Light", deadline_at="2099-01-01 00:00:00"),
    ]
    order = game_labels.sort_tasks_for_ambassador(tasks, is_ambassador=True, path="content")
    assert [t["id"] for t in order] == [2, 1]


def test_ambassador_block_always_on_top_regardless_of_path():
    tasks = [
        _task(id=1, category="Referral", audience="all"),
        _task(id=2, category="Light", audience="ambassadors"),
    ]
    for path in (None, "invite", "content"):
        order = game_labels.sort_tasks_for_ambassador(tasks, is_ambassador=True, path=path)
        assert order[0]["id"] == 2, f"сломалось при path={path!r}"


def test_path_does_not_change_coins_or_membership():
    tasks = [_task(id=1, category="Light", coins=10), _task(id=2, category="Referral", coins=20)]
    order = game_labels.sort_tasks_for_ambassador(tasks, is_ambassador=True, path="invite")
    coins_by_id = {t["id"]: t["coins"] for t in order}
    assert coins_by_id == {1: 10, 2: 20}


# ── ambassador_block_index ─────────────────────────────────────────────────────────────────

def test_ambassador_block_index_counts_leading_ambassador_tasks():
    tasks = [
        _task(id=1, category="Light", audience="ambassadors"),
        _task(id=2, category="Referral", audience="ambassadors"),
        _task(id=3, category="Hard", audience="all"),
    ]
    order = game_labels.sort_tasks_for_ambassador(tasks, is_ambassador=True, path=None)
    assert game_labels.ambassador_block_index(order) == 2


def test_ambassador_block_index_zero_when_no_ambassador_tasks():
    tasks = [_task(id=1, audience="all"), _task(id=2, audience="all")]
    order = game_labels.sort_tasks_for_ambassador(tasks, is_ambassador=True, path=None)
    assert game_labels.ambassador_block_index(order) == 0


# ── _PATH_CATEGORIES: путь — не новое поле задания ──────────────────────────────────────────

def test_path_categories_is_commented_not_a_new_task_field():
    # Словарь опирается на существующую ось category — задание без пути (path=None) вообще не
    # проходит через него (_PATH_CATEGORIES.get(None, ()) == ()).
    assert game_labels._PATH_CATEGORIES.get(None, ()) == ()
    assert "Referral" in game_labels._PATH_CATEGORIES["invite"]
    assert "Referral" not in game_labels._PATH_CATEGORIES["content"]

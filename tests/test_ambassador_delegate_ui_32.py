"""Phase 32 (32-06): делегатская часть амбассадорского слоя — блок заданий/порядок по пути в
чате и в Mini App (D-24/D-27/D-28/D-36), экран рейтинга волны (D-29), путь/выход/возврат
(D-24/D-32/D-38).

Задача 1 — видимость и порядок ОБЕИХ поверхностей (бот + Mini App): амбассадорское задание
никогда не протекает не-амбассадору ни в список, ни в карточку, ни в сдачу; путь меняет только
порядок; задание без срока показывается словами; структурный сторож ловит забытый фильтр рядом
с `list_active_tasks`.

Харнесс Mini App — тот же, что у `tests/test_miniapp_delegate.py` (`TestClient` + `make_init_data`,
фикстура `client` переиспользуется отсюда, второй харнесс не заводим). Бот — прямой вызов
`handlers.user_actions._game_task_list_screen`, как в `tests/test_game_task_order_260919.py`.
"""
from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from database import db as bot_db

from tests.test_miniapp_delegate import client  # noqa: F401 — переиспользуемая фикстура
from tests.test_miniapp_routes import DELEGATE_ID, _hdr

from handlers import user_actions as ua_mod

ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _task(title: str, *, days: int | None = 3, audience: str = "all",
          wave_id: int | None = None, coins: int = 10) -> int:
    deadline = _fmt(datetime.now() + timedelta(days=days)) if days is not None else bot_db.NO_DEADLINE_AT
    return _run(bot_db.create_task(
        f"{title} — описание", "Light", coins, "photo", deadline, None,
        title=title, wave_id=wave_id, audience=audience,
    ))


def _make_ambassador(telegram_id: int, since: str | None = None) -> None:
    _run(bot_db.set_ambassador_flag(telegram_id, active=True, at=since or _fmt(datetime.now())))


# ── структурный сторож: список.py без visible_tasks_for рядом с list_active_tasks — дыра ────

def _functions_calling(source: str, symbol: str) -> dict[str, str]:
    """Имя функции -> её исходный текст, для каждой (async) функции, чьё тело буквально
    содержит вызов `symbol(`. Тот же приём AST-разбора, что у соседних сторожей проекта
    (`tests/test_registration_send_guard_260906.py`) — не построчный grep, не спотыкается о
    перенос строк в многострочном вызове."""
    tree = ast.parse(source)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seg = ast.get_source_segment(source, node) or ""
            if f"{symbol}(" in seg:
                out[node.name] = seg
    return out


def _assert_visibility_paired(path: Path):
    source = path.read_text(encoding="utf-8")
    callers = _functions_calling(source, "list_active_tasks")
    missing = [name for name, seg in callers.items() if "visible_tasks_for(" not in seg]
    assert not missing, f"{path}: {missing} зовут list_active_tasks без visible_tasks_for рядом"


def test_visibility_guard_bot_list_screen():
    _assert_visibility_paired(ROOT / "handlers" / "user_actions.py")


def test_visibility_guard_miniapp_tasks_router():
    _assert_visibility_paired(ROOT / "miniapp" / "routers" / "tasks.py")


def test_visibility_guard_catches_broken_sample():
    """Сторож проверяет сам себя: искусственно испорченный исходник (список без фильтра
    рядом) обязан провалить ту же проверку — иначе сторож ничего не ловит."""
    broken = (
        "async def bad_screen(x):\n"
        "    tasks = await list_active_tasks()\n"
        "    return tasks\n"
    )
    callers = _functions_calling(broken, "list_active_tasks")
    assert callers and all("visible_tasks_for(" not in seg for seg in callers.values())


# ── бот: амбассадорское задание не протекает не-амбассадору, у амбассадора — блок наверху ──

def test_bot_list_hides_ambassador_task_from_regular_delegate(client):  # noqa: F811
    _task("Обычное")
    amb_task = _task("Только амбассадорам", audience="ambassadors")
    assert amb_task  # задание реально создано (id ненулевой)
    text, _kb = _run(ua_mod._game_task_list_screen(DELEGATE_ID))
    assert "Только амбассадорам" not in text


def test_bot_list_shows_ambassador_block_above_regular_for_ambassador(client):  # noqa: F811
    _make_ambassador(DELEGATE_ID)
    _task("Обычное")
    _task("Амбассадорское", audience="ambassadors")
    text, _kb = _run(ua_mod._game_task_list_screen(DELEGATE_ID))
    assert "Амбассадорское" in text and "Обычное" in text
    assert text.index("Амбассадорское") < text.index("Обычное")


def test_bot_list_header_appears_once_only_when_block_nonempty(client):  # noqa: F811
    _make_ambassador(DELEGATE_ID)
    _task("Обычное")
    _task("Амбассадорское раз", audience="ambassadors")
    _task("Амбассадорское два", audience="ambassadors")
    text, _kb = _run(ua_mod._game_task_list_screen(DELEGATE_ID))
    header = "Задания амбассадоров"
    assert text.count(header) == 1


def test_bot_list_empty_ambassador_block_is_byte_identical_to_plain_list(client):  # noqa: F811
    """Обычный делегат без единого амбассадорского задания видит РОВНО тот же экран, что и до
    этого плана — заголовок блока нигде не всплывает, когда блок пуст."""
    _task("Просто задание")
    text, _kb = _run(ua_mod._game_task_list_screen(DELEGATE_ID))
    assert "Задания амбассадоров" not in text


def test_bot_list_wave_task_hidden_from_ambassador_who_joined_after_wave_start(client):  # noqa: F811
    wave_id = _run(bot_db.create_wave(
        starts_at="2026-01-10 00:00:00", ends_at="2026-01-20 00:00:00",
    ))
    _run(bot_db.set_wave_state(wave_id, "active"))
    _make_ambassador(DELEGATE_ID, since="2026-01-15 00:00:00")  # вступил ПОСЛЕ старта волны
    _task("Задание волны", audience="ambassadors", wave_id=wave_id)
    text, _kb = _run(ua_mod._game_task_list_screen(DELEGATE_ID))
    assert "Задание волны" not in text


def test_bot_list_task_without_deadline_shows_words_not_raw_marker(client):  # noqa: F811
    _task("Бессрочное", days=None)
    text, _kb = _run(ua_mod._game_task_list_screen(DELEGATE_ID))
    assert "без срока" in text
    assert bot_db.NO_DEADLINE_AT not in text


def test_bot_submit_prompt_no_deadline_task_never_shows_overdue_warning(client):  # noqa: F811
    """Задание без срока (метка `NO_DEADLINE_AT`) не имеет права показать «⏰ Срок сдачи
    вышел» — единственный разбор строки живёт в `game_labels.task_deadline_short`, второй
    собственный `strptime` по `deadline_at` из хендлера убран этим планом."""
    tid = _task("Без срока", days=None)

    class _FakeMsg:
        def __init__(self):
            self.from_user = type("U", (), {"id": DELEGATE_ID})()
            self.data = f"mytask_submit:{tid}"
            self.answers = []

        async def answer(self, text, **kw):
            self.answers.append(text)
            return type("Sent", (), {"message_id": 1})()

    class _FakeCallback:
        def __init__(self, msg):
            self.data = msg.data
            self.from_user = msg.from_user
            self.message = msg

        async def answer(self, *a, **kw):
            pass

    class _FakeState:
        def __init__(self):
            self._data = {}

        async def update_data(self, **kw):
            self._data.update(kw)

        async def get_data(self):
            return self._data

        async def set_state(self, *_a):
            pass

    msg = _FakeMsg()
    cb = _FakeCallback(msg)
    _run(ua_mod.mytask_submit_start(cb, _FakeState()))
    assert msg.answers, "хендлер обязан отправить промпт"
    assert "Срок сдачи вышел" not in msg.answers[0]


# ── Mini App: то же правило видимости, список/карточка/сдача ────────────────────────────────

def test_miniapp_list_hides_ambassador_task_from_regular_delegate(client):  # noqa: F811
    _task("Открытое всем")
    _task("Амбассадорское", audience="ambassadors")
    resp = client.get("/app/api/tasks", headers=_hdr(DELEGATE_ID))
    titles = {i["title"] for i in resp.json()["items"]}
    assert "Амбассадорское" not in titles and "Открытое всем" in titles


def test_miniapp_list_shows_ambassador_block_first_for_ambassador(client):  # noqa: F811
    _make_ambassador(DELEGATE_ID)
    regular = _task("Открытое всем")
    amb = _task("Амбассадорское", audience="ambassadors")
    items = client.get("/app/api/tasks", headers=_hdr(DELEGATE_ID)).json()["items"]
    ids = [i["id"] for i in items]
    assert ids.index(amb) < ids.index(regular)


def test_miniapp_card_404_for_ambassador_only_task_direct_hit_by_regular_delegate(client):  # noqa: F811
    """T-32-06-02: прямой запрос карточки по id обходит список — карточка обязана
    перепроверить видимость сама, а не полагаться на то, что делегат не найдёт ссылку."""
    amb_task = _task("Секретное", audience="ambassadors")
    resp = client.get(f"/app/api/tasks/{amb_task}", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 404 and resp.json() == {"reason": "task_not_found"}


def test_miniapp_submit_404_for_ambassador_only_task_direct_hit_by_regular_delegate(client):  # noqa: F811
    amb_task = _task("Секретное для сдачи", audience="ambassadors")
    resp = client.post(
        "/app/api/submissions",
        json={"task_id": amb_task, "parts": [{"kind": "text", "content": "ответ"}]},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 404 and resp.json() == {"reason": "task_not_found"}


def test_miniapp_card_visible_for_ambassador(client):  # noqa: F811
    _make_ambassador(DELEGATE_ID)
    amb_task = _task("Видно амбассадору", audience="ambassadors")
    resp = client.get(f"/app/api/tasks/{amb_task}", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200


def test_miniapp_task_without_deadline_shows_words(client):  # noqa: F811
    t = _task("Бессрочное веб", days=None)
    item = client.get(f"/app/api/tasks/{t}", headers=_hdr(DELEGATE_ID)).json()
    assert "без срока" in item["card_text"]


# ── Задача 2: экран рейтинга волны (D-29) ────────────────────────────────────────────────────

OTHER_AMB_ID = 941777  # второй амбассадор той же волны — для проверки скрытия имён


class _FakeMessage:
    def __init__(self):
        self.edits: list[tuple[str, dict]] = []

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw))


class _FakeWaveCallback:
    def __init__(self, user_id: int, data: str = "ambwave"):
        self.data = data
        self.from_user = type("U", (), {"id": user_id})()
        self.message = _FakeMessage()
        self.alerts: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append((text, show_alert))


def _active_wave(days_ago_start: int = 1, days_ahead_end: int = 5) -> int:
    now = datetime.now()
    wave_id = _run(bot_db.create_wave(
        starts_at=_fmt(now - timedelta(days=days_ago_start)),
        ends_at=_fmt(now + timedelta(days=days_ahead_end)),
    ))
    _run(bot_db.set_wave_state(wave_id, "active"))
    return wave_id


def _credit_wave_task(uid: int, wave_id: int, coins: int) -> None:
    task_id = _task("Задание волны для рейтинга", wave_id=wave_id, audience="ambassadors")
    _run(bot_db.add_coins(uid, coins, task_id=task_id))


def test_wave_rating_button_shown_for_ambassador_with_active_eligible_wave(client):  # noqa: F811
    wave_id = _active_wave()
    _make_ambassador(DELEGATE_ID, since=_fmt(datetime.now() - timedelta(days=10)))
    assert wave_id
    _task("Обычное")
    _text, kb = _run(ua_mod._game_task_list_screen(DELEGATE_ID))
    labels = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "ambwave" in labels


def test_wave_rating_button_absent_without_active_wave(client):  # noqa: F811
    _make_ambassador(DELEGATE_ID)
    _task("Обычное")
    _text, kb = _run(ua_mod._game_task_list_screen(DELEGATE_ID))
    labels = [b.callback_data for row in (kb.inline_keyboard if kb else []) for b in row]
    assert "ambwave" not in labels


def test_wave_rating_screen_shows_top_and_own_line_when_names_on(client):  # noqa: F811
    wave_id = _active_wave()
    since = _fmt(datetime.now() - timedelta(days=10))
    _make_ambassador(DELEGATE_ID, since=since)
    _make_ambassador(OTHER_AMB_ID, since=since)
    _run(bot_db.add_user({
        "telegram_id": OTHER_AMB_ID, "full_name": "Второй Амбассадор",
        "registration_date": "2026-08-01",
    }))
    _run(bot_db.set_ambassador_flag(OTHER_AMB_ID, active=True, at=since))
    _credit_wave_task(DELEGATE_ID, wave_id, 30)
    _credit_wave_task(OTHER_AMB_ID, wave_id, 50)

    cb = _FakeWaveCallback(DELEGATE_ID)
    _run(ua_mod.show_wave_rating(cb))
    assert not any(show_alert for _t, show_alert in cb.alerts), "участник не должен получать alert"
    assert cb.message.edits, "экран обязан перерисоваться"
    text = cb.message.edits[0][0]
    assert "Второй Амбассадор" in text  # тумблер включён по умолчанию — топ с именами


def test_wave_rating_hides_other_names_when_toggle_off_but_keeps_own_line(client):  # noqa: F811
    wave_id = _active_wave()
    since = _fmt(datetime.now() - timedelta(days=10))
    _run(bot_db.add_user({
        "telegram_id": OTHER_AMB_ID, "full_name": "Скрытый Сосед",
        "registration_date": "2026-08-01",
    }))
    _make_ambassador(DELEGATE_ID, since=since)
    _run(bot_db.set_ambassador_flag(OTHER_AMB_ID, active=True, at=since))
    _credit_wave_task(DELEGATE_ID, wave_id, 10)
    _credit_wave_task(OTHER_AMB_ID, wave_id, 90)
    _run(bot_db.set_setting("wave_rating_show_names", "off"))

    cb = _FakeWaveCallback(DELEGATE_ID)
    _run(ua_mod.show_wave_rating(cb))
    text = cb.message.edits[0][0]
    assert "Скрытый Сосед" not in text
    assert not any(show_alert for _t, show_alert in cb.alerts)


def test_wave_rating_non_ambassador_gets_alert_and_no_redraw(client):  # noqa: F811
    _active_wave()
    cb = _FakeWaveCallback(DELEGATE_ID)
    _run(ua_mod.show_wave_rating(cb))
    assert cb.alerts and cb.alerts[0][1] is True  # show_alert
    assert not cb.message.edits


def test_wave_rating_ambassador_joined_mid_wave_gets_alert_not_data(client):  # noqa: F811
    """T-32-06-01: экран мог быть отрисован до того, как волна началась — гейт обязан
    перепроверить участие заново, а не доверять факту, что кнопка вообще существует."""
    wave_id = _active_wave(days_ago_start=5)
    wave = _run(bot_db.get_wave(wave_id))
    after_start = (datetime.strptime(wave["starts_at"], "%Y-%m-%d %H:%M:%S") + timedelta(days=1))
    _make_ambassador(DELEGATE_ID, since=_fmt(after_start))
    cb = _FakeWaveCallback(DELEGATE_ID)
    _run(ua_mod.show_wave_rating(cb))
    assert cb.alerts and not cb.message.edits


def test_wave_rating_no_active_wave_shows_closed_text_not_alert(client):  # noqa: F811
    _make_ambassador(DELEGATE_ID)
    cb = _FakeWaveCallback(DELEGATE_ID)
    _run(ua_mod.show_wave_rating(cb))
    assert not any(show_alert for _t, show_alert in cb.alerts)
    assert cb.message.edits
    text = cb.message.edits[0][0]
    assert "нет активной волны" in text.lower() or "волн" in text.lower()

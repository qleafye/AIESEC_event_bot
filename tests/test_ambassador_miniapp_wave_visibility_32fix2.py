"""32-FIX-common-2 (хвост CR-03): Mini App теперь считает `open_wave_ids` так же, как бот
(`services.ambassador_waves.wave_visibility_ids`), а не только `eligible_wave_ids` для
амбассадора — до этой правки задание черновой/будущей/уже закрытой волны было видно и сдаваемо
в Mini App сразу после создания (`miniapp/routers/tasks.py::_ambassador_gate`, `miniapp/routers/
submissions.py::create_submission_route`).

Матрица паритета «бот = Mini App»: состояние волны (draft / active-будущая / active-идущая /
closing) × роль (амбассадор с `ambassador_since` до старта волны / обычный делегат) × аудитория
задания (`all` / `ambassadors`) — список, карточка и сдача Mini App обязаны совпадать с
`game_labels.task_visible_to` (та же функция, которую зовёт бот, WR-08)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from database import db as bot_db
from game_labels import task_visible_to

from tests.test_miniapp_delegate import client  # noqa: F401 — переиспользуемая фикстура
from tests.test_miniapp_routes import DELEGATE_ID, _hdr


def _run(coro):
    return asyncio.run(coro)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _wave(state: str) -> int:
    """Волна в одном из четырёх состояний ревью: `draft`, `active` с `starts_at` в будущем
    («ещё не началась»), `active` с `starts_at` в прошлом («идёт прямо сейчас»), `closing`."""
    now = datetime.now()
    if state == "draft":
        starts_at, ends_at = _fmt(now + timedelta(days=5)), _fmt(now + timedelta(days=15))
    elif state == "active-future":
        starts_at, ends_at = _fmt(now + timedelta(days=3)), _fmt(now + timedelta(days=13))
    elif state == "active-ongoing":
        starts_at, ends_at = _fmt(now - timedelta(days=2)), _fmt(now + timedelta(days=8))
    elif state == "closing":
        starts_at, ends_at = _fmt(now - timedelta(days=10)), _fmt(now - timedelta(days=1))
    else:
        raise ValueError(state)
    wave_id = _run(bot_db.create_wave(starts_at=starts_at, ends_at=ends_at))
    db_state = "draft" if state == "draft" else ("closing" if state == "closing" else "active")
    if db_state != "draft":
        _run(bot_db.set_wave_state(wave_id, db_state))
    return wave_id


def _task_for_wave(wave_id: int, audience: str) -> int:
    deadline = _fmt(datetime.now() + timedelta(days=30))
    return _run(bot_db.create_task(
        "Задание волны — описание", "Light", 10, "photo", deadline, None,
        title="Задание волны", wave_id=wave_id, audience=audience,
    ))


def _set_ambassador(telegram_id: int, is_ambassador: bool) -> None:
    if is_ambassador:
        # `ambassador_since` заведомо раньше самой ранней волны матрицы (10 дней назад для
        # closing) — иначе D-31 («вступил посреди волны») дал бы `wave_eligible = False` и
        # смазал бы матрицу вторым правилом поверх проверяемого.
        since = _fmt(datetime.now() - timedelta(days=30))
        _run(bot_db.set_ambassador_flag(telegram_id, active=True, at=since))
    else:
        _run(bot_db.set_ambassador_flag(telegram_id, active=False, at=_fmt(datetime.now())))


WAVE_STATES = ["draft", "active-future", "active-ongoing", "closing"]
AUDIENCES = ["all", "ambassadors"]
ROLES = [True, False]  # is_ambassador


@pytest.mark.parametrize("wave_state", WAVE_STATES)
@pytest.mark.parametrize("audience", AUDIENCES)
@pytest.mark.parametrize("is_ambassador", ROLES)
def test_miniapp_matches_bot_task_visibility(client, wave_state, audience, is_ambassador):  # noqa: F811
    wave_id = _wave(wave_state)
    task_id = _task_for_wave(wave_id, audience)
    _set_ambassador(DELEGATE_ID, is_ambassador)
    user = _run(bot_db.get_user(DELEGATE_ID))

    expected = _run(task_visible_to(user, _run(bot_db.get_task(task_id))))

    list_resp = client.get("/app/api/tasks", headers=_hdr(DELEGATE_ID))
    ids_in_list = {i["id"] for i in list_resp.json()["items"]}
    assert (task_id in ids_in_list) == expected, (
        f"список: wave={wave_state} audience={audience} amb={is_ambassador} "
        f"ожидалось visible={expected}"
    )

    card_resp = client.get(f"/app/api/tasks/{task_id}", headers=_hdr(DELEGATE_ID))
    assert (card_resp.status_code == 200) == expected, (
        f"карточка: wave={wave_state} audience={audience} amb={is_ambassador} "
        f"код={card_resp.status_code}"
    )

    submit_resp = client.post(
        "/app/api/submissions",
        json={"task_id": task_id, "parts": [{"kind": "text", "content": "ответ"}]},
        headers=_hdr(DELEGATE_ID),
    )
    if expected:
        assert submit_resp.status_code != 404, submit_resp.text
    else:
        assert submit_resp.status_code == 404 and submit_resp.json() == {"reason": "task_not_found"}

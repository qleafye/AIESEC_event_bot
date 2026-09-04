"""Phase 19 Plan 03 (WEBAPP-01, D-07/D-08): читающие делегатские маршруты Mini App —
профиль, задания и карточка, баланс/история/рейтинг. Харнесс — `tests/test_miniapp_routes.py`
(`TestClient` + `make_init_data`). Все данные сидируются прямо в БД через `database.db`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from database import db as bot_db

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    GAME_MANAGER_ID,
    PENDING_ID,
    REJECTED_ID,
    UNREGISTERED_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)

OTHER_ID = 900110  # второй одобренный делегат (для рейтинга)


def _run(coro):
    return asyncio.run(coro)


async def _sql(query: str, params=()):
    async with bot_db._connect() as conn:
        await conn.execute(query, params)
        await conn.commit()


def _fill_profile(telegram_id: int, **columns):
    sets = ", ".join(f"{c} = ?" for c in columns)
    _run(_sql(f"UPDATE users SET {sets} WHERE telegram_id = ?", (*columns.values(), telegram_id)))


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_delegate.db")
    _standard_seed()
    _seed(users=[(OTHER_ID, "approved")])
    return _client(_cfg(db_path))


# ── профиль (D-08) ──────────────────────────────────────────────────────────────────────

def test_profile_returns_labeled_nonempty_fields_and_edit_cta(client):
    # reg_q_phone/reg_q_city по умолчанию выключены в реестре (владелец 03.09: профиль теперь
    # фильтрует вопросы по reg_engine.enabled_steps — как и мастер анкеты, вопрос выключенного
    # шага не показывается, даже если в колонке случайно осталось значение); включаем явно,
    # чтобы протестировать именно показ ЗАПОЛНЕННОГО включённого вопроса.
    _set("reg_q_phone", "on")
    _set("reg_q_city", "on")
    _fill_profile(DELEGATE_ID, phone="+7 999", city="Москва", email="", work_status=1,
                  resume_file_id="AgACfile", receipt_file_id="AgACrcpt", payment_status="paid")
    resp = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_key = {f["key"]: f for f in body["fields"]}
    contacts_by_key = {c["key"]: c for c in body["contacts"]}
    # Phase 23.1-05: телефон/работа — раздел «Контакты», не «Анкета» (см. тест ниже про
    # _CONTACT_LABEL_KEYS); в fields их больше нет, чтобы вопрос не показывался дважды.
    assert contacts_by_key["reg_q_phone"] == {"key": "reg_q_phone", "label": "\U0001f4f1 Телефон", "value": "+7 999"}
    assert contacts_by_key["reg_q_work"]["value"] == "Да"
    assert "reg_q_phone" not in by_key and "reg_q_work" not in by_key
    assert by_key["reg_q_city"]["value"] == "Москва"
    assert "reg_q_email" not in by_key and "reg_q_email" not in contacts_by_key  # пустое — не показываем
    assert "reg_q_resume" not in by_key  # только file_id — не текст/ссылка
    # служебных колонок в ответе нет нигде
    assert "AgACfile" not in resp.text and "AgACrcpt" not in resp.text
    assert body["status"] == "approved" and body["status_label"] == "Одобрена"
    # Модуль оплаты выключен (дефолт реестра) -> статуса оплаты как понятия нет: сервер шлёт
    # пустые значения, профиль не рисует «Не оплатил» (фикс 03d62a8 по живой приёмке 19-10).
    assert body["payment_status"] == "" and body["payment_status_label"] == ""
    # D-24 (план 21-11): правка — экран #/form внутри приложения, не deep-link в бота.
    assert "edit_deeplink" not in body and "edit_hint" not in body
    assert body["can_edit"] is True
    assert body["edit_cta_text"]  # дефолт реестра reg_form_profile_edit_cta_text

    _set("payment_enabled", "on")
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    assert body["payment_status"] == "paid" and body["payment_status_label"] == "Оплатил"


# ── плита профиля (план 23.1-05, UI-REDESIGN-05): монограмма, город, контакты отдельно,
# прогресс анкеты, метастрока дат, D-10 «одобрена {date}» ──────────────────────────────────

def test_profile_initials_two_words_and_one_word(client):
    _fill_profile(DELEGATE_ID, full_name="Иван Петров")
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    assert body["initials"] == "ИП"

    _fill_profile(DELEGATE_ID, full_name="Иван")
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    assert body["initials"] == "И"

    # Quick 260904-aup (UAT D10): пустой full_name больше не даёт пустую монограмму — initials
    # считается от display_name, а display_name падает на first_name из initData (макет
    # test_miniapp_auth.make_init_data по умолчанию несёт "Тест"), когда анкеты ещё нет.
    _fill_profile(DELEGATE_ID, full_name="")
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    assert body["display_name"] == "Тест"
    assert body["initials"] == "Т"


# ── аватар и имя в плите (UAT D10, quick 260904-aup) ─────────────────────────────────────────

def test_profile_avatar_url_from_positive_cache_no_network(client):
    # Позитивный кеш (avatar_file_id уже записан) — resolve_avatar не ходит в сеть.
    _fill_profile(DELEGATE_ID, avatar_file_id="AgACavatar")
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    assert body["avatar_url"] == "/app/api/file/AgACavatar"


def test_profile_avatar_url_none_with_fresh_negative_cache(client):
    # Свежий отрицательный кеш («фото нет», проверено недавно) — тоже без сети, avatar_url None.
    _fill_profile(DELEGATE_ID, avatar_checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    assert body["avatar_url"] is None


def test_profile_display_name_falls_back_to_first_name_then_registry_default(client):
    _fill_profile(DELEGATE_ID, full_name="Иван Петров")
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    assert body["display_name"] == "Иван Петров"

    # full_name пуст (анкета ещё не подана) -> first_name из initData.
    _fill_profile(DELEGATE_ID, full_name="")
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID, first_name="Мария")).json()
    assert body["display_name"] == "Мария"
    assert body["initials"] == "М"

    # Оба источника пусты -> дефолт реестра miniapp_profile_greeting_fallback_text, монограмма
    # всё равно непуста (фолбэк — не второй пустой исход).
    body = client.get(
        "/app/api/profile", headers=_hdr(DELEGATE_ID, user_extra={"first_name": ""}),
    ).json()
    assert body["display_name"] == "Привет!"
    assert body["initials"] == "П"


def test_profile_contacts_and_fields_do_not_overlap(client):
    # reg_q_phone/reg_q_email по умолчанию выключены (см. комментарий в тесте выше).
    _set("reg_q_phone", "on")
    _set("reg_q_email", "on")
    _fill_profile(DELEGATE_ID, phone="+7 999", email="a@b.ru", work_status=1, city="Москва")
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    contact_keys = {c["key"] for c in body["contacts"]}
    field_keys = {f["key"] for f in body["fields"]}
    assert not (contact_keys & field_keys)
    assert contact_keys == {"reg_q_phone", "reg_q_email", "reg_q_work"}
    # фиксированный порядок email/phone/work, а не порядок REG_LABELS
    assert [c["key"] for c in body["contacts"]] == ["reg_q_email", "reg_q_phone", "reg_q_work"]


def test_profile_short_track_shows_only_short_track_questions(client):
    """Владелец 03.09 (стенд с телефона): профиль делегата короткого трека показывал ответы
    ЛЮБОГО шага анкеты — например `university` (глобально `reg_q_university` default "on"),
    хотя короткий трек его вообще не спрашивает (`is_step_enabled_for_track` для short не
    откатывается на глобальное значение при отсутствии `__short`-ключа) — если в колонке
    `users` случайно осталось непустое значение (например после смены трека при повторной
    регистрации). Профиль теперь фильтрует по `reg_engine.enabled_steps` — тот же движок и тот
    же приём (трек аргументом, не из ответов), что фикс 797b0f0 сделал для мастера анкеты."""
    _set("reg_q_age__short", "on")
    _set("reg_q_vk__short", "on")
    _fill_profile(DELEGATE_ID, participant_type="short", age=21, vk_username="@ivan",
                  university="МГУ", phone="+7 999")
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    field_keys = {f["key"] for f in body["fields"]}
    assert field_keys == {"reg_q_age", "reg_q_vk"}
    assert "reg_q_university" not in field_keys
    by_key = {f["key"]: f for f in body["fields"]}
    assert by_key["reg_q_age"]["value"] == "21"
    assert by_key["reg_q_vk"]["value"] == "@ivan"
    # reg_q_phone не включён для короткого трека (нет __short-override) -> не показан нигде,
    # даже с непустым значением в колонке.
    assert body["contacts"] == []
    assert body["form_total"] == 2


def test_profile_short_mode_null_track_shows_only_short_questions_d4(client):
    """UAT D4 regression guard (quick 260904-aup Task 2): владелец 03.09 сообщил «в веб
    подсасываются вопросы с полной формы, даже если выбран short-трек» — по словам владельца,
    тот же корень, что D16 (quick 260904-3vm), уже починен в `_enabled_label_keys`
    (`resolve_track` зовётся, когда трек делегата пуст). Этот тест — сторож поверх фикса, не
    новый фикс: `participant_type=None` (делегат без своего трека) при глобальном
    `registration_mode=short` обязан видеть только короткий набор вопросов. Если сторож
    покраснеет — чинить точку резолва трека в профиле, а не заводить фильтр-заплату."""
    _set("registration_mode", "short")
    _set("reg_q_age__short", "on")
    _set("reg_q_vk__short", "on")
    _fill_profile(DELEGATE_ID, participant_type=None, age=21, vk_username="@ivan", university="МГУ")
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    field_keys = {f["key"] for f in body["fields"]}
    assert field_keys == {"reg_q_age", "reg_q_vk"}
    assert "reg_q_university" not in field_keys
    assert body["form_total"] == 2


def test_profile_form_progress_reflects_filled_and_total(client):
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    assert body["form_filled"] <= body["form_total"]
    assert "{filled}" not in body["form_progress_text"] and "{total}" not in body["form_progress_text"]


def test_profile_city_label_falls_back_to_free_text_when_module_off(client):
    _fill_profile(DELEGATE_ID, city="Казань", event_city=None)
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    # event_city_enabled выключен по умолчанию -> свободный текст users.city как есть.
    assert body["city_label"] == "Казань"


def test_profile_form_meta_text_none_without_any_date(client):
    _fill_profile(DELEGATE_ID, registration_date=None, edited_at=None, approved_at=None)
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    assert body["form_meta_text"] is None


def test_profile_approved_text_shown_only_when_approved_at_set(client):
    _fill_profile(DELEGATE_ID, registration_date="2026-08-20 10:00:00", edited_at=None,
                  approved_at=None)
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    assert "одобрена" not in (body["form_meta_text"] or "")

    _fill_profile(DELEGATE_ID, approved_at="2026-08-25 12:00:00")
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    assert "одобрена" in body["form_meta_text"]


@pytest.mark.parametrize("user_id,kind", [(PENDING_ID, "pending"), (REJECTED_ID, "rejected"),
                                          (UNREGISTERED_ID, "unregistered")])
def test_profile_gated_for_non_approved(client, user_id, kind):
    resp = client.get("/app/api/profile", headers=_hdr(user_id))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "delegate_gate", "kind": kind}


def test_profile_section_off(client):
    _set("miniapp_section_profile", "off")
    resp = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "section_off"


# ── плитка «📝 Анкета» в хабе (D-08/D-24, план 21-11) ────────────────────────────────────
# Плитка строится в hub.js циклом по visibleNav(), который фильтрует NAV по
# me.sections[item.section] — тумблер миниapp_section_form решает видимость через тот же
# /app/api/me, который читает hub.js; отдельного экрана-теста для DOM плитки нет (харнесс
# фазы 19 не рендерит JS), сторож — сам признак раздела в контракте /app/api/me.

def test_form_section_visible_by_default_and_hidden_by_toggle(client):
    body = client.get("/app/api/me", headers=_hdr(DELEGATE_ID)).json()
    assert body["sections"]["form"] is True  # дефолт реестра miniapp_section_form = on

    _set("miniapp_section_form", "off")
    body = client.get("/app/api/me", headers=_hdr(DELEGATE_ID)).json()
    assert body["sections"]["form"] is False


# ── /app/api/me: статус анкеты, доступ, дом приложения (gap closure фазы 21, D-24) ───────

def _me(client, telegram_id):
    return client.get("/app/api/me", headers=_hdr(telegram_id)).json()


def test_me_form_status_contract(client):
    from reg_labels import STATUS_LABELS

    body = _me(client, UNREGISTERED_ID)
    assert body["form_status"] == "none"
    assert body["form_first"] is True
    assert body["form_access"] is True
    assert body["form_status_label"] == ""

    _run(bot_db.upsert_reg_draft(UNREGISTERED_ID, kind="new", source="miniapp"))
    body = _me(client, UNREGISTERED_ID)
    assert body["form_status"] == "draft"
    assert body["form_first"] is True

    body = _me(client, PENDING_ID)
    assert body["form_status"] == "pending"
    assert body["form_first"] is False
    assert body["form_status_label"] == STATUS_LABELS["pending"]

    body = _me(client, DELEGATE_ID)
    assert body["form_status"] == "approved"
    assert body["form_first"] is False

    body = _me(client, REJECTED_ID)
    assert body["form_status"] == "rejected"
    assert body["form_first"] is False
    assert body["form_access"] is True


def test_me_form_first_false_for_manager_without_own_application(client):
    """Находка 21-13: у менеджера (`caps` не пустой, строки `users` нет) `form_status`
    тоже "none" — без гварда его бы кидало на анкету вместо хаба. `form_access` не меняется:
    плитка «Анкета» видна и менеджеру."""
    body = _me(client, GAME_MANAGER_ID)
    assert body["form_status"] == "none"
    assert body["form_access"] is True
    assert body["form_first"] is False


def test_me_form_first_off_when_section_off(client):
    _set("miniapp_section_form", "off")
    body = _me(client, UNREGISTERED_ID)
    assert body["form_first"] is False
    assert body["form_access"] is True
    assert body["sections"]["form"] is False


def test_me_form_access_false_via_cookie_and_staff_only(tmp_path):
    from tests.test_miniapp_routes import ADMIN_ID, _cookie_client

    db_path = _use_tmp_db(tmp_path, "miniapp_delegate_me.db")
    _standard_seed()
    cfg = _cfg(db_path)
    body = _cookie_client(cfg, ADMIN_ID).get("/app/api/me").json()
    assert body["form_access"] is False
    assert body["form_first"] is False

    _set("miniapp_staff_only", "on")
    body = _client(cfg).get("/app/api/me", headers=_hdr(UNREGISTERED_ID)).json()
    assert body["form_access"] is False
    assert body["form_first"] is False


# ── задания ─────────────────────────────────────────────────────────────────────────────

def _deadline(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _task(title: str, *, days: int = 3, city: str | None = None, category: str = "Light",
          coins: int = 10, proof: str = "photo") -> int:
    return _run(bot_db.create_task(
        f"{title} — описание", category, coins, proof, _deadline(days), None,
        event_city=city, title=title,
    ))


def _submission(task_id: int, user_id: int, status: str = "pending", coins_awarded=None) -> int:
    sid = _run(bot_db.create_submission(task_id, user_id, "text", "ok", "2026-08-20 10:00:00"))
    assert sid
    _run(_sql("UPDATE game_submissions SET status = ?, coins_awarded = ? WHERE id = ?",
              (status, coins_awarded, sid)))
    return sid


def _tasks(client, user_id=DELEGATE_ID, **params):
    resp = client.get("/app/api/tasks", params=params, headers=_hdr(user_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_task_list_all_five_states_and_overdue_stays(client):
    t_new = _task("Новое")
    t_pending = _task("На проверке")
    _submission(t_pending, DELEGATE_ID, "pending")
    t_approved = _task("Принято")
    _submission(t_approved, DELEGATE_ID, "approved", coins_awarded=10)
    t_rejected = _task("Отклонено")
    _submission(t_rejected, DELEGATE_ID, "rejected")
    t_overdue = _task("Просрочено", days=-2)
    # чужая сдача не влияет на мой статус (T-19-12)
    _submission(t_new, OTHER_ID, "approved", coins_awarded=10)

    body = _tasks(client)
    by_id = {i["id"]: i for i in body["items"]}
    assert body["total"] == 5 and len(by_id) == 5
    assert by_id[t_new]["status"] == "new" and by_id[t_new]["can_submit"] is True
    assert by_id[t_pending]["status"] == "pending" and by_id[t_pending]["can_submit"] is False
    assert by_id[t_approved]["status"] == "approved" and by_id[t_approved]["coins_awarded"] == 10
    assert by_id[t_rejected]["status"] == "rejected" and by_id[t_rejected]["attempt"] == 1
    assert by_id[t_rejected]["can_submit"] is True  # лимит перезаливов не задан
    assert by_id[t_overdue]["overdue"] is True and by_id[t_overdue]["status"] == "new"
    assert all(i["overdue"] is False for tid, i in by_id.items() if tid != t_overdue)
    assert by_id[t_new]["category_label"] and by_id[t_new]["category_label"] != "Light"  # RU из реестра
    assert by_id[t_new]["title"] == "Новое" and by_id[t_new]["coins"] == 10
    assert body["empty_text"] is None


def test_task_list_rejected_limit_reached_blocks_submit(client):
    _set("game_resubmit_limit", "1")
    t = _task("Лимит")
    _submission(t, DELEGATE_ID, "rejected")
    item = _tasks(client)["items"][0]
    assert item["status"] == "rejected" and item["attempt"] == 1 and item["limit"] == 1
    assert item["can_submit"] is False


def test_task_list_empty_text_from_registry(client):
    _set("game_task_list_empty", "Пока пусто, загляни позже")
    body = _tasks(client)
    assert body["items"] == [] and body["total"] == 0
    assert body["empty_text"] == "Пока пусто, загляни позже"


def test_task_list_pagination_and_limit_ceiling(client):
    ids = [_task(f"Задание {i}", days=i + 1) for i in range(3)]
    page = _tasks(client, limit="1", offset="1")
    assert [i["id"] for i in page["items"]] == [ids[1]]  # сортировка по дедлайну
    assert page["total"] == 3 and page["limit"] == 1 and page["offset"] == 1
    assert _tasks(client, limit="999")["limit"] == 50
    junk = _tasks(client, limit="abc", offset="-5")
    assert junk["limit"] == 25 and junk["offset"] == 0 and len(junk["items"]) == 3


def test_task_list_city_scope_mirrors_bot(client):
    mine = _task("Всем", city=None)
    msk = _task("Москва", city="msk")
    spb = _task("Питер", city="spb")
    # модуль городов выключен — видно всё
    assert {i["id"] for i in _tasks(client)["items"]} == {mine, msk, spb}
    # включён — делегат без event_city = город по умолчанию (msk): чужой город не виден
    _set("event_city_enabled", "on")
    assert {i["id"] for i in _tasks(client)["items"]} == {mine, msk}
    _fill_profile(DELEGATE_ID, event_city="spb")
    assert {i["id"] for i in _tasks(client)["items"]} == {mine, spb}


def test_task_list_archived_not_shown(client):
    t = _task("Архив")
    _run(_sql("UPDATE game_tasks SET archived_at = '2026-08-01 00:00:00' WHERE id = ?", (t,)))
    assert _tasks(client)["total"] == 0
    resp = client.get(f"/app/api/tasks/{t}", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 404 and resp.json() == {"reason": "task_not_found"}


@pytest.mark.parametrize("user_id,kind", [(PENDING_ID, "pending"), (REJECTED_ID, "rejected")])
def test_task_list_gated(client, user_id, kind):
    resp = client.get("/app/api/tasks", headers=_hdr(user_id))
    assert resp.status_code == 403 and resp.json() == {"reason": "delegate_gate", "kind": kind}


def test_task_list_section_off(client):
    _set("miniapp_section_tasks", "off")
    resp = client.get("/app/api/tasks", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403 and resp.json() == {"reason": "section_off", "section": "tasks"}


def test_task_card_uses_shared_render(client):
    import game_labels

    t = _task("Карточка", proof="photo,link")
    _submission(t, DELEGATE_ID, "rejected")
    _set("game_resubmit_limit", "3")
    resp = client.get(f"/app/api/tasks/{t}", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    task = _run(bot_db.get_task(t))
    expected = _run(game_labels.render_task_card_text(task, "новое · попытка 1 из 3", 1))
    assert body["card_text"] == expected
    assert body["status_line"] == "новое · попытка 1 из 3"
    assert body["proof_hint"] == _run(game_labels.proof_types_label("photo,link"))
    assert body["can_submit"] is True and body["attempt"] == 1
    assert body["text"] == "Карточка — описание"


def test_task_card_pending_and_missing(client):
    t = _task("Сдано")
    _submission(t, DELEGATE_ID, "pending")
    body = client.get(f"/app/api/tasks/{t}", headers=_hdr(DELEGATE_ID)).json()
    assert body["status"] == "pending" and body["can_submit"] is False
    assert body["status_line"] == "на проверке"
    assert client.get("/app/api/tasks/424242", headers=_hdr(DELEGATE_ID)).status_code == 404
    assert client.get("/app/api/tasks/abc", headers=_hdr(DELEGATE_ID)).status_code == 422


# ── плита карточки задания (план 23.1-05, UI-REDESIGN-06): остаток срока, подписи блока
# «нужно прислать», строка проверки ────────────────────────────────────────────────────────

def test_task_card_deadline_left_text_present_for_future_deadline(client):
    from miniapp.timeutil import today_msk

    t = _task("Свежее", days=5)
    task = _run(bot_db.get_task(t))
    target = datetime.strptime(task["deadline_at"], "%Y-%m-%d %H:%M:%S").date()
    expected_days = (target - today_msk()).days
    body = client.get(f"/app/api/tasks/{t}", headers=_hdr(DELEGATE_ID)).json()
    assert body["deadline_left_text"] == f"осталось {expected_days} дн."
    assert body["overdue"] is False


def test_task_card_deadline_left_text_none_when_overdue(client):
    t = _task("Просрочено", days=-2)
    body = client.get(f"/app/api/tasks/{t}", headers=_hdr(DELEGATE_ID)).json()
    assert body["overdue"] is True
    assert body["deadline_left_text"] is None
    assert body["overdue_hint"]  # своя строка уже есть — не дублируем вторым текстом


def test_task_card_ships_todo_proof_and_review_texts(client):
    t = _task("Тексты карточки")
    body = client.get(f"/app/api/tasks/{t}", headers=_hdr(DELEGATE_ID)).json()
    assert body["todo_eyebrow"]
    assert body["proof_eyebrow"]
    assert body["proof_note"]
    assert body["review_note"]


# ── монеты ──────────────────────────────────────────────────────────────────────────────

def test_balance_without_operations_rank_is_null(client):
    resp = client.get("/app/api/coins/balance", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"balance": 0, "rank": None, "participants": 0}


def test_balance_rank_and_participants(client):
    _run(bot_db.add_coins(OTHER_ID, 30, "много", source="manual"))
    _run(bot_db.add_coins(DELEGATE_ID, 10, None, source="task"))
    body = client.get("/app/api/coins/balance", headers=_hdr(DELEGATE_ID)).json()
    assert body == {"balance": 10, "rank": 2, "participants": 2}


def test_history_paginated_with_source_labels(client):
    for i in range(4):
        _run(bot_db.add_coins(DELEGATE_ID, i + 1, f"причина {i}" if i % 2 else None,
                              source="manual" if i < 2 else "task"))
    body = client.get("/app/api/coins/history", params={"limit": "2"}, headers=_hdr(DELEGATE_ID)).json()
    assert body["total"] == 4 and len(body["items"]) == 2 and body["limit"] == 2
    assert [i["delta"] for i in body["items"]] == [4, 3]  # новые сверху
    assert body["items"][0]["reason"] == "причина 3" and body["items"][0]["source_label"] == "задание"
    assert body["items"][1]["reason"] is None
    page2 = client.get("/app/api/coins/history", params={"limit": "2", "offset": "2"},
                       headers=_hdr(DELEGATE_ID)).json()
    assert [i["delta"] for i in page2["items"]] == [2, 1]
    assert page2["items"][1]["source_label"] == "вручную"
    assert body["empty_text"] is None
    # чужие операции не попадают
    _run(bot_db.add_coins(OTHER_ID, 99, "чужое", source="manual"))
    assert client.get("/app/api/coins/history", headers=_hdr(DELEGATE_ID)).json()["total"] == 4


def test_history_empty_text(client):
    body = client.get("/app/api/coins/history", headers=_hdr(DELEGATE_ID)).json()
    assert body["items"] == [] and body["total"] == 0 and body["empty_text"]


def test_leaderboard_capped_at_50_with_me_block(client):
    for n in range(60):
        uid = 910000 + n
        _run(_sql("INSERT INTO users (telegram_id, full_name, status) VALUES (?, ?, 'approved')",
                  (uid, f"Участник {n}")))
        _run(bot_db.add_coins(uid, 100 + n, None, source="task"))
    _run(bot_db.add_coins(DELEGATE_ID, 5, None, source="task"))
    resp = client.get("/app/api/leaderboard", params={"limit": "500"}, headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 50
    assert body["items"][0] == {"rank": 1, "name": "Участник 59", "balance": 159, "is_me": False}
    assert body["me"] == {"rank": 61, "balance": 5}
    assert body["total"] == 61
    assert not any(i["is_me"] for i in body["items"])
    small = client.get("/app/api/leaderboard", params={"limit": "3"}, headers=_hdr(DELEGATE_ID)).json()
    assert len(small["items"]) == 3
    # ни телефонов, ни e-mail
    assert "phone" not in resp.text and "email" not in resp.text


def test_leaderboard_marks_me(client):
    _run(bot_db.add_coins(DELEGATE_ID, 7, None, source="task"))
    body = client.get("/app/api/leaderboard", headers=_hdr(DELEGATE_ID)).json()
    assert body["items"] == [{"rank": 1, "name": f"User {DELEGATE_ID}", "balance": 7, "is_me": True}]
    assert body["me"] == {"rank": 1, "balance": 7} and body["empty_text"] is None


def test_leaderboard_empty_text_and_section_off(client):
    body = client.get("/app/api/leaderboard", headers=_hdr(DELEGATE_ID)).json()
    assert body["items"] == [] and body["empty_text"] and body["me"]["rank"] is None
    _set("miniapp_section_leaderboard", "off")
    resp = client.get("/app/api/leaderboard", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403 and resp.json()["reason"] == "section_off"
    _set("miniapp_section_coins", "off")
    assert client.get("/app/api/coins/balance", headers=_hdr(DELEGATE_ID)).status_code == 403
    assert client.get("/app/api/coins/history", headers=_hdr(DELEGATE_ID)).status_code == 403


def test_coins_gated_for_pending(client):
    for path in ("/app/api/coins/balance", "/app/api/coins/history", "/app/api/leaderboard"):
        resp = client.get(path, headers=_hdr(PENDING_ID))
        assert resp.status_code == 403 and resp.json()["kind"] == "pending", path

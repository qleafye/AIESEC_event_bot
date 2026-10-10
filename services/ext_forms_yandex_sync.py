"""Дочитывание ответов Яндекс Форм: очередь, бэкфилл, страховочная сверка, обновление токена.

Вебхук Яндекса — только сигнал «пришёл ответ» (кладёт answer_id в `external_form_pending`).
Сами данные берутся из API под токеном менеджера: `drain_pending` разбирает очередь, а
`reconcile_form` / `backfill_form` ставят в ту же очередь id, которых в базе ещё нет, —
вебхук, сверка и бэкфилл сходятся в один путь.

Личные формы (`ingest_mode = 'push'`) в API не ходят: ответ лежит в `payload` строки очереди
(его положил приёмник вебхука), `drain_pending` разбирает его тем же `ingest_answer`; сверка и
бэкфилл такие формы пропускают.

Исходы строки очереди:
- сохранена -> строка удалена;
- сеть / 5xx / 429 -> строка остаётся, attempts + 1, пауза `backoff_seconds`;
- 404 (ответ удалён или id чужой) -> строка удалена;
- 401 / 403 -> подключение `needs_reauth`, строки ждут повторного входа (пауза 30 минут,
  attempts не растёт, запросов к API нет).

В `last_error` и в лог идёт только reason-код, без текста исключений и содержимого анкеты.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from database import ext_forms_db as ef
from services import ext_forms_yandex as yx
from services.ext_forms_ingest import ingest_answer
from services.ext_forms_match import rematch_unmatched
from services.ext_forms_parse import parse_push_body, parse_yandex_answer
from services.sheet_arrival_sync import backoff_seconds
from services.infra.timeutil import msk_now
from services.ext_forms_yandex import YandexApiError

logger = logging.getLogger(__name__)

_FMT = "%Y-%m-%d %H:%M:%S"
REFRESH_AHEAD = timedelta(days=7)
REAUTH_PAUSE = timedelta(minutes=30)
_AUTH_REASONS = {"unauthorized", "forbidden", "bad_code", "no_app_keys"}


_REAUTH_TEXT = "Яндекс не пускает бота — войдите заново: «🔑 Войти через Яндекс»"
_RETRY_TEXT = "Яндекс сейчас не отвечает — бот повторит сам через 10 минут"
_SYNC_ERROR_TEXT = {
    "unauthorized": _REAUTH_TEXT,
    "forbidden": _REAUTH_TEXT,
    "bad_code": _REAUTH_TEXT,
    "no_app_keys": "Ключи приложения Яндекса не заданы — заполните их в «📝 Внешние формы»",
    "rate_limited": _RETRY_TEXT,
    "upstream_unavailable": _RETRY_TEXT,
    "bad_response": _RETRY_TEXT,
    "reconcile_error": "Не удалось сверить ответы — бот повторит сам через 10 минут",
}


def sync_error_text(reason: str) -> str:
    """Что видит менеджер вместо reason-кода: что случилось и что делать."""
    return _SYNC_ERROR_TEXT.get(reason) or _SYNC_ERROR_TEXT["reconcile_error"]


def _fmt(dt: datetime) -> str:
    return dt.strftime(_FMT)


def _expires_soon(expires_at: str | None, now: datetime) -> bool:
    if not expires_at:
        return False
    try:
        exp = datetime.strptime(expires_at, _FMT)
    except ValueError:
        return False
    return exp - now < REFRESH_AHEAD


async def ensure_fresh_token(conn: dict) -> dict | None:
    """Подключение с действующим токеном; None — нужен повторный вход менеджера."""
    if conn.get("status") == "needs_reauth":
        return None
    now = msk_now().replace(tzinfo=None)
    if not _expires_soon(conn.get("expires_at"), now):
        return conn
    try:
        new = await yx.refresh_token(conn)
    except YandexApiError as e:
        if e.reason in _AUTH_REASONS:
            await ef.set_connection_status(conn["id"], "needs_reauth", alerted_at=None)
            logger.warning("ext_forms: подключение %s требует входа (%s)", conn["id"], e.reason)
            return None
        logger.warning("ext_forms: токен подключения %s не обновлён (%s)", conn["id"], e.reason)
        return conn  # старый ещё действует — попробуем в следующий тик
    await ef.update_connection_tokens(
        conn["id"], new["access_token"], new.get("refresh_token"), new.get("expires_at"))
    return {**conn, **new, "status": "ok"}


async def _postpone(row_id: int, until: datetime) -> None:
    """Сдвинуть строку без увеличения attempts (ожидание входа менеджера)."""
    await ef._exec("UPDATE external_form_pending SET next_try_at = ? WHERE id = ?",
                   (_fmt(until), row_id))


async def _drain_push_row(row: dict, form: dict, now: datetime, counts: dict) -> None:
    """Строка очереди личной формы: ответ уже лежит в payload, API Яндекса не нужен."""
    aid = row["answer_id"]
    payload = row.get("payload")
    if not payload:
        await ef.drop_pending(row["id"])
        counts["dropped"] += 1
        logger.info("ext_forms: push-строка %s формы %s без тела, снята (push_no_payload)",
                    aid, form["id"])
        return
    try:
        parsed = parse_push_body(json.loads(payload), header_answer_id=aid)
        if parsed["items"] is None:
            # Тело не разобрали — не выбрасываем: после правки разбора строка дойдёт сама.
            await _postpone(row["id"], now + timedelta(minutes=10))
            counts["retry"] += 1
            logger.info("ext_forms: push-строка %s формы %s без разобранных ответов, отложена",
                        aid, form["id"])
            return
        await ingest_answer(form, answer_id=aid, answered_at=parsed["created"],
                            items=parsed["items"], raw=payload)
        if form.get("push_warning"):
            await ef.set_form_push_warning(form["id"], None)
            form["push_warning"] = None
    except Exception as e:  # noqa: BLE001 — строка не должна теряться из-за одного разбора
        await ef.fail_pending(
            row["id"], "ingest_error",
            _fmt(now + timedelta(seconds=backoff_seconds(row["attempts"] + 1))))
        counts["retry"] += 1
        logger.warning("ext_forms: push-ответ %s формы %s не сохранён (%s)",
                       aid, form["id"], type(e).__name__)
        return
    await ef.drop_pending(row["id"])
    counts["done"] += 1


async def drain_pending(limit: int = 50) -> dict:
    counts = {"done": 0, "retry": 0, "dropped": 0, "reauth": 0}
    now = msk_now().replace(tzinfo=None)
    rows = await ef.list_due_pending(_fmt(now), limit)
    conns: dict[int, dict | None] = {}
    forms: dict[int, dict | None] = {}

    for row in rows:
        fid = row["form_id"]
        if fid not in forms:
            forms[fid] = await ef.get_form(fid)
        form = forms[fid]
        if form is None:
            await ef.drop_pending(row["id"])
            counts["dropped"] += 1
            continue

        if form.get("ingest_mode") == "push":
            await _drain_push_row(row, form, now, counts)
            continue

        cid = form.get("connection_id")
        if cid is None:
            await ef.fail_pending(row["id"], "no_connection",
                                  _fmt(now + timedelta(seconds=backoff_seconds(row["attempts"] + 1))))
            counts["retry"] += 1
            continue
        if cid not in conns:
            raw_conn = await ef.get_connection(cid)
            conns[cid] = await ensure_fresh_token(raw_conn) if raw_conn else None
        conn = conns[cid]
        if conn is None:
            await _postpone(row["id"], now + REAUTH_PAUSE)
            counts["reauth"] += 1
            continue

        aid = row["answer_id"]
        try:
            raw = await yx.get_answer(conn, aid)
        except YandexApiError as e:
            if e.reason == "not_found":
                await ef.drop_pending(row["id"])
                counts["dropped"] += 1
                logger.info("ext_forms: ответ %s формы %s не найден, строка снята", aid, fid)
            elif e.reason in ("unauthorized", "forbidden"):
                await ef.set_connection_status(cid, "needs_reauth", alerted_at=None)
                conns[cid] = None
                await _postpone(row["id"], now + REAUTH_PAUSE)
                counts["reauth"] += 1
                logger.warning("ext_forms: подключение %s требует входа (%s)", cid, e.reason)
            else:
                await ef.fail_pending(
                    row["id"], e.reason,
                    _fmt(now + timedelta(seconds=backoff_seconds(row["attempts"] + 1))))
                counts["retry"] += 1
                logger.warning("ext_forms: ответ %s формы %s: повтор (%s)", aid, fid, e.reason)
            continue

        try:
            answered_at, items = parse_yandex_answer(raw)
            await ingest_answer(form, answer_id=aid, answered_at=answered_at, items=items,
                                raw=json.dumps(raw, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001 — строка не должна теряться из-за одного разбора
            await ef.fail_pending(
                row["id"], "ingest_error",
                _fmt(now + timedelta(seconds=backoff_seconds(row["attempts"] + 1))))
            counts["retry"] += 1
            logger.warning("ext_forms: ответ %s формы %s не сохранён (%s)", aid, fid, type(e).__name__)
            continue
        await ef.drop_pending(row["id"])
        counts["done"] += 1
    return counts


async def reconcile_form(form: dict) -> int:
    """Недостающие в базе answer_id формы -> очередь. Возвращает число новых строк очереди."""
    if form.get("ingest_mode") == "push":
        return 0  # личная форма: API Яндекса её не читает, ответы приходят телом вебхука
    raw_conn = await ef.get_connection(form["connection_id"]) if form.get("connection_id") else None
    conn = await ensure_fresh_token(raw_conn) if raw_conn else None
    if conn is None:
        raise YandexApiError("unauthorized")
    ids = await yx.list_answer_ids(conn, form["external_id"])
    known = await ef.known_answer_ids(form["id"])
    now = _fmt(msk_now().replace(tzinfo=None))
    added = 0
    for aid in ids:
        if aid in known:
            continue
        if await ef.enqueue_pending(form["id"], aid, None, now):
            added += 1
    await ef.set_form_sync(form["id"], last_sync_at=now, sync_error=None)
    return added


async def backfill_form(form_id: int) -> int:
    """Ответы, пришедшие до подключения формы (вызывает мастер подключения)."""
    form = await ef.get_form(form_id)
    if form is None:
        return 0
    return await reconcile_form(form)


async def reconcile_all() -> dict:
    enqueued = 0
    for form in await ef.list_active_forms("yandex"):
        if form.get("ingest_mode") == "push":
            continue
        raw_conn = await ef.get_connection(form["connection_id"]) if form.get("connection_id") else None
        if raw_conn is None or raw_conn.get("status") == "needs_reauth":
            continue
        try:
            enqueued += await reconcile_form(form)
        except YandexApiError as e:
            if e.reason in ("unauthorized", "forbidden"):
                # Как и drain_pending: без этого алерт «войдите заново» мог не уйти.
                await ef.set_connection_status(raw_conn["id"], "needs_reauth", alerted_at=None)
            await ef.set_form_sync(form["id"], last_sync_at=form.get("last_sync_at"),
                                   sync_error=sync_error_text(e.reason))
            logger.warning("ext_forms: сверка формы %s не удалась (%s)", form["id"], e.reason)
        except Exception as e:  # noqa: BLE001
            await ef.set_form_sync(form["id"], last_sync_at=form.get("last_sync_at"),
                                   sync_error=sync_error_text("reconcile_error"))
            logger.warning("ext_forms: сверка формы %s: %s", form["id"], type(e).__name__)
    rematched = await rematch_unmatched()
    # Делегации вузов: страховка хука приёма — оценить ответы без оценки и снова поискать людей.
    swept: dict = {}
    try:
        from services.delegations import sweep_pending
        swept = await sweep_pending()
    except Exception as e:  # noqa: BLE001
        logger.warning("delegations: sweep после сверки: %s", type(e).__name__)
    return {"enqueued": enqueued, "rematched": rematched, "swept": swept}

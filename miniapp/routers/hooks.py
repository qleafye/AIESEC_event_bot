"""Приёмник вебхука Яндекс Форм: `POST /app/hooks/yform/{secret}`.

Вебхук — только сигнал «пришёл новый ответ»: содержимое тела не используется (кроме `id`
JSON-RPC для ответа), сами ответы бот забирает по API. Приёмник проверяет секрет из пути и
`X-Form-Id`, кладёт строку в `external_form_pending` (идемпотентно по UNIQUE(form_id, answer_id))
и сразу отвечает — у Яндекса таймаут 5 секунд. Сетевых вызовов здесь нет.

Аутентификации делегата/менеджера нет: доступ только по секрету в пути. Неверный секрет или
чужой `X-Form-Id` — 403, а не 404: 404 Яндекс ретраит.

Личная форма (`ingest_mode = 'push'`, API Яндекса её не читает): ответ приходит в теле запроса
интеграции. Приёмник кладёт тело в `external_form_pending.payload` и отвечает сразу; разбор,
сопоставление и уведомления делает процесс бота. `X-Form-Id` сверяется, только если пришёл.
В лог — лишь длина тела и имена ключей, значений анкеты там нет. Путь `/app/hooks/` выведен из-под
`_enabled_gate`, поэтому приёмник работает и при выключенном Mini App.
"""
from __future__ import annotations

import hmac
import json
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from database import ext_forms_db
from services.ext_forms.ext_forms_parse import parse_push_body
from miniapp.timeutil import now_msk_naive

logger = logging.getLogger(__name__)

router = APIRouter()

_FORM_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_ANSWER_ID_MAX = 20
_DELIVERY_ID_MAX = 100


def _forbidden() -> HTTPException:
    return HTTPException(403, detail={"reason": "forbidden"})


def _rpc(result: str, rpc_id) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "result": result, "id": rpc_id})


def _rpc_id(raw: bytes):
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 — тело не JSON: id просто null
        return None
    if isinstance(data, dict):
        value = data.get("id")
        if value is None or isinstance(value, (int, str)):
            return value
    return None


_PUSH_BODY_MAX = 256 * 1024
_PUSH_NO_ANSWERS = (
    "Последний запрос от Яндекса пришёл без ответов — проверьте параметры интеграции"
)


def _form_id_matches(candidate: str, expected: str) -> bool:
    return bool(_FORM_ID_RE.match(candidate)) and hmac.compare_digest(
        candidate.encode(), expected.encode())


def _body_keys(data) -> list[str]:
    """Имена ключей верхнего уровня и params — единственное, что из тела попадает в лог."""
    if not isinstance(data, dict):
        return []
    keys = [str(k)[:40] for k in data]
    if isinstance(data.get("params"), dict):
        keys += ["params." + str(k)[:40] for k in data["params"]]
    return keys[:30]


async def _push_hook(form: dict, request: Request) -> JSONResponse:
    """Личная форма: ответ приходит телом запроса интеграции. Тело кладём в очередь как есть;
    разбор и сохранение делает процесс бота (drain_pending)."""
    raw = await request.body()
    rpc_id = _rpc_id(raw)
    expected = str(form.get("external_id") or "")

    # Заголовок X-Form-Id для личной формы не гарантирован: сверяем, только если пришёл.
    header_form_id = (request.headers.get("X-Form-Id") or "").strip()
    if header_form_id and not _form_id_matches(header_form_id, expected):
        raise _forbidden()

    if form.get("status") != "active":
        return _rpc("ignored", rpc_id)

    if len(raw) > _PUSH_BODY_MAX:
        logger.warning("yform push: слишком большое тело, form_id=%s длина=%s", form["id"], len(raw))
        return _rpc("ok", rpc_id)

    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        data = None
    parsed = parse_push_body(data, header_answer_id=request.headers.get("X-Form-Answer-Id"))
    if parsed["form_id"] is not None and not _form_id_matches(parsed["form_id"], expected):
        raise _forbidden()

    if parsed["answer_id"] is None:
        logger.info("yform push без answer_id: form_id=%s длина=%s ключи=%s",
                    form["id"], len(raw), _body_keys(data))
        return _rpc("ok", rpc_id)
    if parsed["items"] is None:
        # Тело всё равно кладём в очередь: разбор можно поправить, а запрос Яндекс не повторит.
        await ext_forms_db.set_form_push_warning(form["id"], _PUSH_NO_ANSWERS)
        logger.info("yform push без ответов: form_id=%s длина=%s ключи=%s",
                    form["id"], len(raw), _body_keys(data))

    delivery_id = (request.headers.get("X-Delivery-Id") or "").strip()[:_DELIVERY_ID_MAX] or None
    now = now_msk_naive().strftime("%Y-%m-%d %H:%M:%S")
    created = await ext_forms_db.enqueue_pending(
        form["id"], parsed["answer_id"], delivery_id, now,
        payload=raw.decode("utf-8", errors="replace"))
    logger.info("yform push: form_id=%s answer_id=%s new=%s", form["id"], parsed["answer_id"], created)
    return _rpc("ok", rpc_id)


@router.post("/app/hooks/yform/{secret}")
async def yform_hook(secret: str, request: Request):
    form = await ext_forms_db.get_form_by_secret(secret)
    if not form or form.get("platform") != "yandex":
        raise _forbidden()

    if form.get("ingest_mode") == "push":
        return await _push_hook(form, request)

    header_form_id = (request.headers.get("X-Form-Id") or "").strip()
    expected = str(form.get("external_id") or "")
    if not _FORM_ID_RE.match(header_form_id) or not hmac.compare_digest(
        header_form_id.encode(), expected.encode()
    ):
        raise _forbidden()

    rpc_id = _rpc_id(await request.body())

    if form.get("status") != "active":
        return _rpc("ignored", rpc_id)

    answer_id = (request.headers.get("X-Form-Answer-Id") or "").strip()
    if not answer_id.isascii() or not answer_id.isdigit() or len(answer_id) > _ANSWER_ID_MAX:
        return _rpc("ok", rpc_id)  # сигнал без id бесполезен, ретраи не нужны

    delivery_id = (request.headers.get("X-Delivery-Id") or "").strip()[:_DELIVERY_ID_MAX] or None
    now = now_msk_naive().strftime("%Y-%m-%d %H:%M:%S")
    created = await ext_forms_db.enqueue_pending(form["id"], answer_id, delivery_id, now)
    logger.info("yform hook: form_id=%s answer_id=%s new=%s", form["id"], answer_id, created)
    return _rpc("ok", rpc_id)

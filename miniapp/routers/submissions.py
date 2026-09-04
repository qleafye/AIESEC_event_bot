"""Phase 19 (D-03/D-05): сдача задания из Mini App — загрузка частей и финализация.

`POST /app/api/uploads` — ОДИН маршрут на два сценария (зависимость `upload_actor`):
одобренный делегат прикладывает часть сдачи ЛИБО менеджер с `moderate_game` грузит обложку
задания (план 19-06). Файл уходит через Bot API в чат САМОГО загружающего с подписью из
реестра (делегат — «копия сдачи», менеджер — «загружено из приложения»), ответ несёт
`file_id` и `part_token` — подпись сервера `HMAC(session_secret, telegram_id:kind:file_id)`.
Без неё на финализации делегат мог бы подсунуть чужой `file_id` (T-19-18). `require_section`
здесь не вешается: раздел проверяют потребители (`/submissions` — «tasks», план 19-06 —
«admin_tasks»).

Лимиты (RESEARCH Pitfall 7, D-03): единый потолок сервиса 20 МБ (`getFile` больше не отдаст
— менеджер не смог бы открыть файл из приложения), фото ≤10 МБ идёт `sendPhoto`, крупнее или
не-изображение — `sendDocument`; подпись ≤1024 (Bot API). Реальный сторож от тела произвольного
размера — `miniapp.body_limit.BodyLimitMiddleware` (ASGI-уровень, обрывает приём ДО
`request.form()`, в том числе для chunked-запроса без `Content-Length` — 260824-8qw, HG-01);
здесь остаются два эшелона подстраховки, которые не зависят от того, подключён ли middleware:
дешёвая предварительная отсечка по `Content-Length` и `_read_capped` — чтение чанками с тем же
потолком.

`POST /app/api/submissions` — зеркало `handlers.user_actions.finalize_game_submission`
(импортировать нельзя — aiogram): та же модель частей (`kind` photo|document|text|link, тот
же `content_type` первой части, тот же `ord`), `create_submission -> None` -> 409
`already_submitted` (partial UNIQUE, гонка с ботом — D-05), уведомление менеджерам — через
`miniapp_outbox` (`submission_created`), его делает бот (D-01). Время — `datetime.now()`
без TZ, ровно как бот: сервис обязан жить в той же TZ контейнера.

`POST /app/api/uploads?target=resume` (план 21-10, D-05, Pattern 5) — ТОТ ЖЕ маршрут, третий
сценарий: делегат с живым черновиком анкеты (`upload_actor` третья ветка, `miniapp/deps.py`)
грузит резюме. Расширение/размер проверяет `reg_engine.is_allowed_resume`/`resume_too_large`
(лимит 10 МБ — как у бота, а не общий потолок сервиса 20 МБ, RESEARCH Pattern 5), файл уходит
`sendDocument` с подписью `miniapp_upload_caption_resume`, `file_id`/имя кладутся ПРЯМО в
`reg_drafts.answers` (`upsert_reg_draft`, allowlist-колонки `resume_file_id`/`resume_file_name`
— finalize_data подхватит их тем же путём, что и текстовое резюме), реальная загрузка в
Nextcloud — заботa бота через outbox `reg_resume_upload` (веб-процесс в Nextcloud не ходит).
Общий ASGI-потолок тела маршрута (`BodyLimitMiddleware`, 20 МБ) не расширяется под резюме
отдельно — 10 МБ резюме укладываются в него с запасом, отдельной записи в таблице лимитов
`miniapp/main.py` заводить не нужно.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import reg_engine
from cities import cities_module_on, normalize_city
from database.db import add_submission_part, create_submission, get_reg_draft, get_task, get_user, upsert_reg_draft
from settings_schema import get_setting_typed

from miniapp import telegram_api
from miniapp.config import MAX_PARTS, MAX_TEXT_PART, MAX_UPLOAD_BYTES, MULTIPART_SLACK, PHOTO_MAX_BYTES
from miniapp.deps import Principal, UploadActor, delegate_gate, require_section, upload_actor
from miniapp.outbox import enqueue
from miniapp.routers.tasks import submission_state
from miniapp.telegram_api import TelegramApiError

logger = logging.getLogger(__name__)
router = APIRouter()

# handlers/user_actions.py::_LEGACY_CONTENT_TYPE — content_type первой части (паритет с ботом).
LEGACY_CONTENT_TYPE = {"photo": "photo", "document": "pdf", "text": "text", "link": "link"}
FILE_KINDS = frozenset({"photo", "document"})
TEXT_KINDS = frozenset({"text", "link"})

CAPTION_MAX = 1024
READ_CHUNK = 1024 * 1024


# ── part_token ───────────────────────────────────────────────────────────────────────────

def make_part_token(secret: str, telegram_id: int, kind: str, file_id: str) -> str:
    msg = f"{telegram_id}:{kind}:{file_id}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def check_part_token(secret: str, telegram_id: int, kind: str, file_id: str, token: str | None) -> bool:
    if not token or not isinstance(token, str):
        return False
    return hmac.compare_digest(make_part_token(secret, telegram_id, kind, file_id), token)


def _too_large() -> HTTPException:
    return HTTPException(413, {"reason": "too_large", "limit": MAX_UPLOAD_BYTES})


async def _read_capped(upload) -> bytes:
    """Чанками по 1 МБ с потолком — даже если Content-Length соврал или его нет (chunked)."""
    buf = bytearray()
    while True:
        chunk = await upload.read(READ_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_UPLOAD_BYTES:
            raise _too_large()
    return bytes(buf)


def _classify_upload(content_type: str | None, size: int) -> str:
    ct = (content_type or "").lower()
    return "photo" if ct.startswith("image/") and size <= PHOTO_MAX_BYTES else "document"


def _extract_file_id(kind: str, result: dict) -> str | None:
    if kind == "photo":
        photos = result.get("photo") or []
        if photos and isinstance(photos[-1], dict):
            return photos[-1].get("file_id")
        # Telegram мог переклассифицировать «картинку» в документ — берём что есть.
    doc = result.get("document") or {}
    return doc.get("file_id") if isinstance(doc, dict) else None


# ── GET /app/api/uploads/limits ──────────────────────────────────────────────────────────

@router.get("/app/api/uploads/limits")
async def upload_limits(actor: UploadActor = Depends(upload_actor)) -> dict:
    """Потолки и тексты для экрана сдачи — фронт не хранит ни чисел, ни текстов: проверка
    `file.size` ДО отправки показывает `miniapp_upload_too_large_text` из реестра."""
    return {
        "max_bytes": MAX_UPLOAD_BYTES,
        "photo_max_bytes": PHOTO_MAX_BYTES,
        "max_parts": MAX_PARTS,
        "max_text": MAX_TEXT_PART,
        "too_large_text": await get_setting_typed("miniapp_upload_too_large_text"),
        "empty_hint": await get_setting_typed("game_proof_empty_hint"),
    }


# ── POST /app/api/uploads?target=resume — резюме (план 21-10, D-05, Pattern 5) ────────────

async def _upload_resume(request: Request, actor: UploadActor, content: bytes, filename: str,
                          content_type: str) -> dict:
    """Третий сценарий одного и того же маршрута: файл резюме кладётся ПРЯМО в
    `reg_drafts.answers` (не возвращается фронту как `file_id`/`part_token` — резюме не часть
    сдачи геймы, второго round-trip'а через `PATCH /app/api/reg/draft` не нужно)."""
    draft = await get_reg_draft(actor.telegram_id)
    if draft is None:
        raise HTTPException(404, {"reason": "no_draft"})
    if not reg_engine.is_allowed_resume(filename):
        raise HTTPException(400, {
            "reason": "bad_type",
            "text": await get_setting_typed("reg_form_resume_wrong_type_text"),
        })
    if reg_engine.resume_too_large(len(content)):
        raise HTTPException(413, {
            "reason": "too_large",
            "text": await get_setting_typed("reg_form_resume_too_large_text"),
        })

    caption = (await get_setting_typed("miniapp_upload_caption_resume") or "")[:CAPTION_MAX]
    cfg = request.app.state.cfg
    try:
        result = await telegram_api.send_document(
            cfg, actor.telegram_id, content, filename, content_type, caption,
        )
    except TelegramApiError as exc:
        raise HTTPException(502, {"reason": "telegram_unavailable", "detail": exc.reason})

    file_id = _extract_file_id("document", result)
    if not file_id:
        logger.warning("uploads(resume): Bot API не вернул file_id")
        raise HTTPException(502, {"reason": "telegram_unavailable", "detail": "no_file_id"})

    await upsert_reg_draft(
        actor.telegram_id,
        kind=draft["kind"],
        participant_type=draft.get("participant_type"),
        event_city=draft.get("event_city"),
        step=draft.get("step"),
        patch={"resume_file_id": file_id, "resume_file_name": filename},
        source="miniapp",
    )
    await enqueue("reg_resume_upload", {
        "telegram_id": actor.telegram_id, "file_id": file_id, "filename": filename,
    })
    return {"file_id": file_id, "filename": filename}


# ── POST /app/api/uploads ────────────────────────────────────────────────────────────────

@router.post("/app/api/uploads")
async def upload_part(request: Request, actor: UploadActor = Depends(upload_actor)) -> dict:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_UPLOAD_BYTES + MULTIPART_SLACK:
        raise _too_large()

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(400, {"reason": "no_file"})
    content = await _read_capped(upload)
    if not content:
        raise HTTPException(400, {"reason": "no_file"})

    filename = (upload.filename or "file")[:255]
    content_type = upload.content_type or "application/octet-stream"

    target = request.query_params.get("target")
    if target == "resume":
        return await _upload_resume(request, actor, content, filename, content_type)

    kind = _classify_upload(upload.content_type, len(content))
    # Quick 260904-8o3 Task 2 (E3): `is_staff_upload` из `deps.upload_actor` отвечает на
    # вопрос «прошёл ли делегатский гейт», а не «зачем грузим» — менеджер, который сам
    # одновременно одобренный делегат (владелец, тестировщица), по нему ВСЕГДА делегат, тогда
    # ассет оформления настроек уходил бы с подписью про сдачу задания. Контекст загрузки
    # приходит от клиента явно (`target=settings_asset`, screens/settings.js::handleFileUpload);
    # порядок веток не трогаем — он же обслуживает резюме (выше) и сдачу геймы (staff/delegate).
    if target == "settings_asset":
        if "settings" not in actor.caps:
            raise HTTPException(403, {"reason": "not_editable"})
        caption_key = "miniapp_upload_caption_settings"
    else:
        caption_key = "miniapp_upload_caption_staff" if actor.is_staff_upload else "miniapp_upload_caption_delegate"
    caption = (await get_setting_typed(caption_key) or "")[:CAPTION_MAX]

    cfg = request.app.state.cfg
    try:
        if kind == "photo":
            result = await telegram_api.send_photo(
                cfg, actor.telegram_id, content, filename, content_type, caption,
            )
        else:
            result = await telegram_api.send_document(
                cfg, actor.telegram_id, content, filename, content_type, caption,
            )
    except TelegramApiError as exc:
        raise HTTPException(502, {"reason": "telegram_unavailable", "detail": exc.reason})

    file_id = _extract_file_id(kind, result)
    if not file_id:
        logger.warning("uploads: Bot API не вернул file_id (kind=%s)", kind)
        raise HTTPException(502, {"reason": "telegram_unavailable", "detail": "no_file_id"})
    # Если фото ушло как документ (Telegram сам решил) — kind остаётся тем, что мы
    # отправили: менеджер в боте соберёт медиа-группу по kind части.
    return {
        "kind": kind,
        "content": file_id,
        "part_token": make_part_token(cfg.session_secret, actor.telegram_id, kind, file_id),
    }


# ── POST /app/api/submissions ────────────────────────────────────────────────────────────

class PartIn(BaseModel):
    kind: str
    content: str = ""
    part_token: str | None = None
    caption: str | None = None


class SubmissionIn(BaseModel):
    task_id: int
    parts: list[PartIn] = Field(default_factory=list)


def _normalize_part(p: PartIn) -> dict:
    if p.kind in FILE_KINDS:
        return {"kind": p.kind, "content": p.content, "caption": (p.caption or None) and p.caption[:CAPTION_MAX]}
    if p.kind in TEXT_KINDS:
        text = p.content or ""
        # Та же классификация, что `_classify_part`: ссылка — по префиксу, независимо от
        # того, что прислал фронт.
        kind = "link" if text.strip().lower().startswith(("http://", "https://")) else "text"
        return {"kind": kind, "content": text[:MAX_TEXT_PART], "caption": None}
    raise HTTPException(400, {"reason": "bad_part", "kind": p.kind})


@router.post("/app/api/submissions")
async def create_submission_route(
    body: SubmissionIn, request: Request,
    p: Principal = Depends(delegate_gate),
    _: Principal = Depends(require_section("tasks")),
) -> dict:
    cfg = request.app.state.cfg
    task = await get_task(body.task_id)
    if task is None or task.get("archived_at"):
        raise HTTPException(404, {"reason": "task_not_found"})
    user = await get_user(p.telegram_id)
    # WR-06 (бот, mytask_submit_start): задание другого города для делегата не существует.
    if await cities_module_on() and task.get("event_city"):
        if normalize_city((user or {}).get("event_city")) != normalize_city(task["event_city"]):
            raise HTTPException(404, {"reason": "task_not_found"})

    if not body.parts:
        raise HTTPException(400, {"reason": "empty", "hint": await get_setting_typed("game_proof_empty_hint")})
    if len(body.parts) > MAX_PARTS:
        raise HTTPException(400, {"reason": "too_many_parts", "limit": MAX_PARTS})

    parts = []
    for raw in body.parts:
        part = _normalize_part(raw)
        if part["kind"] in FILE_KINDS:
            if not part["content"] or not check_part_token(
                cfg.session_secret, p.telegram_id, part["kind"], part["content"], raw.part_token,
            ):
                raise HTTPException(403, {"reason": "bad_part_token"})
        parts.append(part)

    state = await submission_state(task["id"], p.telegram_id)
    if state["status"] in ("pending", "approved"):
        raise HTTPException(409, {"reason": "already_submitted"})
    if not state["can_submit"]:
        raise HTTPException(409, {"reason": "resubmit_limit", "limit": state["limit"]})

    first = parts[0]
    submission_id = await create_submission(
        task["id"], p.telegram_id,
        content_type=LEGACY_CONTENT_TYPE.get(first["kind"], "text"),
        content=first["content"] or "",
        submitted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    if submission_id is None:
        # T-09-01/D-05: гонка с ботом — индекс отверг вторую активную сдачу.
        raise HTTPException(409, {"reason": "already_submitted"})

    for i, part in enumerate(parts):
        await add_submission_part(submission_id, i, part["kind"], part["content"], part["caption"])

    submitter_name = (user or {}).get("full_name") or str(p.telegram_id)
    await enqueue("submission_created", {
        "submission_id": submission_id,
        "user_id": p.telegram_id,
        "task_id": task["id"],
        "task_text": task["text"],
        "submitter_name": submitter_name,
    })
    return {
        "submission_id": submission_id,
        "accepted_text": await get_setting_typed("game_submit_accepted_text"),
    }

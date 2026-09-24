"""Бэклог чек-ина №7: тренировочный режим сканера и лист учебных QR.

Учебный QR — обычная строка формата `services.checkin.build_payload`
(«метка·имя·город·токен»), только токен из закрытого набора `TRAIN-…` (`TRAINING_TOKENS`).
Настоящий токен делегата — `secrets.token_urlsafe` в `users.checkin_token`, с «TRAIN-» он не
совпадёт: в БД такого токена нет, отметку по нему поставить нельзя в принципе.

Пять видов (порядок = порядок на листе): 🟢 одобрен, 🟡 уже был сегодня, 🔴 не одобрен,
🔴 прошлый сезон, 🔴 чужое событие. У «чужого события» метка намеренно НЕ наша
(`FOREIGN_TAG`) — приложение-сканер и загрузка CSV видят его ровно как настоящий чужой QR.

Как узнаётся учебный токен — через реестр резолверов `services.checkin.register_token_resolver`
(первый его потребитель): резолвер отдаёт `kind="training"`, `id` = вид учебного QR, `denial` —
тот же код отказа, что был бы у настоящего делегата. Запись отметки для kind != "delegate" в
`resolve_scanned_user` не предусмотрена, поэтому вызывающие перехватывают учебный токен ДО неё:

- сканер Mini App (`miniapp/routers/checkin.py`): учебный QR в ЛЮБОЙ точке — только учебная
  плашка (`training_scan_response`), без записи и без строки журнала площадки;
- точка «🧪 Тренировка» (`TRAINING_POINT`) с настоящим QR — плашка по реальным данным
  (`preview_entry` — «отмечен»/«уже был» по сегодняшнему входу), тоже без записи;
- загрузка CSV в боте (`handlers/admin_checkin.py`): учебные коды считаются
  (`count_training_codes`) и выкидываются из записей (`drop_training_records`), в отчёте —
  строка «учебных: N (не записаны)».

Журнал площадки тренировку не видит вовсе — ни отметок, ни отказов: журнал читают как «что
было на входе», учебные сканы там были бы шумом (и попали бы в «кто сколько отметил»).

Тексты волонтёру — реестр (`checkin_training_*`, group "event"), перевод — `services.i18n`.
Лист A4 (`build_training_sheet`) рисует Pillow шрифтами Mini App (Lato/Raleway, кириллица в
сабсете есть); без Pillow вызывающий шлёт пять QR отдельными картинками (`training_qr_pngs`)."""
from __future__ import annotations

import io
import logging
import re
from datetime import timedelta
from pathlib import Path

import segno

from database.db import list_checkins_for_user
from services import i18n
from services.checkin import (
    DENIAL_REASON_TEXT,
    ENTRY_POINT,
    Resolved,
    build_payload,
    current_event_tag,
    register_token_resolver,
    resolve_token,
)
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

TRAINING_POINT = "training"
TRAINING_POINT_LABEL = "🧪 Тренировка"

# Вид учебного QR -> токен. Порядок = порядок строк на листе.
TRAINING_TOKENS = {
    "ok": "TRAIN-OK",
    "dup": "TRAIN-DUP",
    "pending": "TRAIN-PENDING",
    "past": "TRAIN-PAST",
    "foreign": "TRAIN-FOREIGN",
}
_KIND_BY_TOKEN = {tok: kind for kind, tok in TRAINING_TOKENS.items()}

# Метка «чужого события» учебного QR. Если менеджер вдруг назвал своё событие так же —
# `_foreign_tag` добавляет хвост, чтобы учебный «чужой» QR остался чужим.
FOREIGN_TAG = "OTHER26"

_TRAINING_NAME = "Учебный делегат"

# Вид -> (код отказа или None, цвет кружка на листе).
_KIND_DENIAL = {"ok": None, "dup": None, "pending": "not_approved", "past": "past_season", "foreign": "foreign_event"}
_KIND_COLOR = {
    "ok": (46, 160, 67), "dup": (230, 180, 0),
    "pending": (214, 48, 49), "past": (214, 48, 49), "foreign": (214, 48, 49),
}
_HINT_KEYS = {kind: f"checkin_training_hint_{kind}_text" for kind in TRAINING_TOKENS}

# Сколько минут назад «уже был» учебный делегат 🟡.
_DUP_MINUTES_AGO = 25

_TRAINING_CODE_RE = re.compile(
    "·(" + "|".join(re.escape(t) for t in TRAINING_TOKENS.values()) + r")(?![A-Za-z0-9_-])"
)


def is_training_token(token: str | None) -> bool:
    return (token or "").strip() in _KIND_BY_TOKEN


# ── Резолвер «токен -> учебный делегат» ──────────────────────────────────────────────────────

async def _training_resolver(token: str, **ctx) -> Resolved | None:
    kind = _KIND_BY_TOKEN.get((token or "").strip())
    if kind is None:
        return None
    return Resolved(
        kind="training", id=kind, telegram_id=None, city=None,
        full_name=_TRAINING_NAME, denial=_KIND_DENIAL[kind], user=None,
    )


def ensure_registered() -> None:
    """Подписать резолвер (повтор — no-op). Зовётся при импорте и перед каждым разбором:
    тесты чистят реестр (`clear_token_resolvers`), а учебный QR обязан узнаваться всегда."""
    register_token_resolver(_training_resolver)


ensure_registered()


async def resolve_training(token: str | None, **ctx) -> Resolved | None:
    """Учебный токен -> описание от резолвера (`kind="training"`), иначе None. Настоящий
    токен сюда не ходит — вызывающий сначала проверяет `is_training_token`."""
    if not is_training_token(token):
        return None
    ensure_registered()
    resolved = await resolve_token(token.strip(), **ctx)
    if resolved is None or resolved.get("kind") != "training":
        return None
    return resolved


# ── Плашки сканера ───────────────────────────────────────────────────────────────────────────

def _stamp(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def _demo_undo(lang: str, tr_map: dict) -> dict:
    """Кнопка «↩️ Отменить» на учебной плашке: фронт не ходит на сервер, а показывает
    `text` — отменять нечего, отметки нет."""
    from services import venue_log  # лениво: журнал площадки нужен только ради длины окна

    return {
        "demo": True,
        "seconds": venue_log.UNDO_WINDOW_SECONDS,
        "label": await i18n.tr_setting("checkin_undo_button_text", lang, tr_map) or "↩️",
        "text": await i18n.tr_setting("checkin_training_undo_demo_text", lang, tr_map) or "",
    }


async def _mark(res: dict, note_key: str, lang: str, tr_map: dict, hint: str | None = None) -> dict:
    res["training"] = True
    res["training_note"] = await i18n.tr_setting(note_key, lang, tr_map) or ""
    if hint:
        res["hint"] = hint
    return res


async def training_scan_response(resolved: Resolved, parsed: dict, lang: str, tr_map: dict) -> dict:
    """Плашка на учебный QR — та же, что показал бы вход настоящему делегату такого вида."""
    kind = resolved.get("id")
    denial = resolved.get("denial")
    now = msk_now()
    res: dict = {"full_name": parsed.get("full_name") or resolved.get("full_name"), "city": None}
    if kind == "ok":
        res.update(status="new", scanned_at=_stamp(now), undo=await _demo_undo(lang, tr_map))
    elif kind == "dup":
        res.update(status="duplicate", scanned_at=_stamp(now - timedelta(minutes=_DUP_MINUTES_AGO)))
    elif denial == "foreign_event":
        res.update(status="foreign_event", reason_text=DENIAL_REASON_TEXT["foreign_event"])
    else:
        res.update(status="denied", reason_text=DENIAL_REASON_TEXT.get(denial, denial))
    hint = await i18n.tr_setting(_HINT_KEYS.get(kind, ""), lang, tr_map) if kind in _HINT_KEYS else None
    return await _mark(res, "checkin_training_qr_note_text", lang, tr_map, hint)


async def preview_entry(user: dict, lang: str, tr_map: dict) -> dict:
    """Настоящий делегат в точке «Тренировка»: что показал бы вход — «уже был в ЧЧ:ММ», если
    сегодня вход уже есть, иначе «отмечен». Ничего не пишет."""
    today = msk_now().strftime("%Y-%m-%d")
    for row in await list_checkins_for_user(user["telegram_id"]):
        if row.get("point") == ENTRY_POINT and (row.get("day") or (row.get("scanned_at") or "")[:10]) == today:
            return {"status": "duplicate", "scanned_at": row.get("scanned_at")}
    return {"status": "new", "scanned_at": _stamp(msk_now()), "undo": await _demo_undo(lang, tr_map)}


async def as_training_point(res: dict, lang: str, tr_map: dict) -> dict:
    """Любой ответ сканера в точке «Тренировка» -> пометка «ничего не записано»."""
    return await _mark(res, "checkin_training_note_text", lang, tr_map)


async def training_point_entry(lang: str, tr_map: dict) -> dict:
    """Точка «🧪 Тренировка» для списка точек сканера — сразу после «🚪 Вход». Счётчика нет."""
    return {
        "point": TRAINING_POINT, "label": TRAINING_POINT_LABEL, "live": None,
        "count": None, "capacity": None,
        "note": await i18n.tr_setting("checkin_training_note_text", lang, tr_map) or "",
    }


# ── Загрузка CSV ─────────────────────────────────────────────────────────────────────────────

def count_training_codes(text: str) -> int:
    """Сколько разных учебных QR в выгрузке (любая метка, включая «чужое событие»)."""
    return len({m.group(1) for m in _TRAINING_CODE_RE.finditer(text or "")})


def drop_training_records(records: list[dict]) -> list[dict]:
    """Записи `find_checkin_records` без учебных кодов — их не отмечаем."""
    from services.checkin import parse_qr_payload

    return [r for r in records if not is_training_token(parse_qr_payload(r["qr"])["token"])]


def training_report_line(n: int) -> str:
    return f"🧪 Учебных QR: {n} (не записаны)"


def training_only_text(n: int) -> str:
    return (
        f"🧪 В файле только учебные QR: {n}. Приложение-сканер читает коды — ничего не отмечено. "
        "Настоящий файл загружайте так же."
    )


# ── Лист учебных QR ──────────────────────────────────────────────────────────────────────────

async def _foreign_tag() -> str:
    tag = await current_event_tag()
    return FOREIGN_TAG if tag != FOREIGN_TAG else FOREIGN_TAG + "X"


async def training_payloads() -> list[tuple[str, str]]:
    """[(вид, строка QR)] в порядке листа."""
    tag = await current_event_tag()
    foreign = await _foreign_tag()
    return [
        (kind, build_payload(foreign if kind == "foreign" else tag, _TRAINING_NAME, "—", token))
        for kind, token in TRAINING_TOKENS.items()
    ]


async def training_hints(lang: str, tr_map: dict) -> dict[str, str]:
    return {kind: await i18n.tr_setting(key, lang, tr_map) or "" for kind, key in _HINT_KEYS.items()}


def _qr_png(payload: str, scale: int = 6) -> bytes:
    buf = io.BytesIO()
    segno.make(payload).save(buf, kind="png", scale=scale, border=2)
    return buf.getvalue()


async def training_qr_pngs(lang: str, tr_map: dict) -> list[tuple[bytes, str]]:
    """Запасной путь без Pillow: [(png, подпись)] — пять отдельных картинок."""
    hints = await training_hints(lang, tr_map)
    return [(_qr_png(payload), hints[kind]) for kind, payload in await training_payloads()]


_FONTS_DIR = Path(__file__).resolve().parent.parent / "miniapp" / "static" / "fonts"
_A4 = (1240, 1754)  # 150 dpi
_MARGIN = 80
_QR_BOX = 250


def _strip_symbols(text: str) -> str:
    """Ведущие эмодзи шрифт листа не рисует (квадратики) — снимаем их."""
    from services.i18n_glossary import split_leading_symbols

    prefix, rest = split_leading_symbols((text or "").strip())
    return rest if prefix else (text or "").strip()


def _wrap(draw, text: str, font, width: int) -> list[str]:
    lines: list[str] = []
    for para in (text or "").split("\n"):
        line = ""
        for word in para.split():
            probe = f"{line} {word}".strip()
            if draw.textlength(probe, font=font) <= width or not line:
                line = probe
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


async def build_training_sheet(lang: str, tr_map: dict) -> bytes:
    """PNG листа A4: заголовок, пять строк «кружок цвета · QR · что увидит волонтёр и что
    делать», внизу — «ничего не записывается». Бросает ImportError без Pillow."""
    from PIL import Image, ImageDraw, ImageFont

    title = _strip_symbols(await i18n.tr_setting("checkin_training_sheet_title_text", lang, tr_map) or "")
    footer = _strip_symbols(await i18n.tr_setting("checkin_training_qr_note_text", lang, tr_map) or "")
    hints = await training_hints(lang, tr_map)

    img = Image.new("RGB", _A4, "white")
    draw = ImageDraw.Draw(img)
    f_title = ImageFont.truetype(str(_FONTS_DIR / "raleway-800.woff2"), 48)
    f_text = ImageFont.truetype(str(_FONTS_DIR / "lato-400.woff2"), 30)
    f_small = ImageFont.truetype(str(_FONTS_DIR / "lato-700.woff2"), 26)

    y = _MARGIN
    for line in _wrap(draw, title, f_title, _A4[0] - 2 * _MARGIN):
        draw.text((_MARGIN, y), line, font=f_title, fill="black")
        y += 60
    y += 20
    row_h = (_A4[1] - y - _MARGIN - 70) // len(TRAINING_TOKENS)
    text_x = _MARGIN + 60 + _QR_BOX + 40
    text_w = _A4[0] - text_x - _MARGIN
    for idx, (kind, payload) in enumerate(await training_payloads(), start=1):
        qr = segno.make(payload)
        size = qr.symbol_size(scale=1, border=2)[0]
        qr_img = Image.open(io.BytesIO(_qr_png(payload, scale=max(1, _QR_BOX // size)))).convert("RGB")
        cy = y + row_h // 2
        draw.ellipse((_MARGIN, cy - 22, _MARGIN + 44, cy + 22), fill=_KIND_COLOR[kind])
        draw.text((_MARGIN + 13, cy - 16), str(idx), font=f_small, fill="white")
        img.paste(qr_img, (_MARGIN + 60, y + (row_h - qr_img.height) // 2))
        lines = _wrap(draw, _strip_symbols(hints[kind]), f_text, text_w)
        ty = cy - len(lines) * 19
        for line in lines:
            draw.text((text_x, ty), line, font=f_text, fill="black")
            ty += 38
        y += row_h
    for line in _wrap(draw, footer, f_small, _A4[0] - 2 * _MARGIN):
        draw.text((_MARGIN, y + 10), line, font=f_small, fill=(90, 90, 90))
        y += 34

    out = io.BytesIO()
    img.save(out, format="PNG", dpi=(150, 150))
    return out.getvalue()

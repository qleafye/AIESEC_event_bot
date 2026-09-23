"""Квик 260923 (форум-чекин, D-01..D-04): персональный QR одобренного делегата.

`build_checkin_qr(user)` — ЕДИНСТВЕННАЯ точка, которая знает формат содержимого QR. Её
переиспользует и хендлер главного меню (`handlers/user_actions.py::show_my_checkin_qr`), и
БУДУЩАЯ рассылка накануне форума (не эта задача — сканер/загрузка CSV тоже не входят сюда,
только выпуск и выдача QR). Сигнатура рассчитана на переиспользование заранее: чистая функция
без aiogram-типов на входе (`dict` строки `users`), возвращает `(png_bytes, caption)` —
вызывающий сам решает, как отправить (`answer_photo`, будущая рассылка и т.п.).

Статус делегата («одобрен») эта функция НЕ проверяет — гейт D-02 живёт у вызывающего
(`ensure_registered` в хендлере меню; будущая рассылка обязана применить свой). Здесь только
генерация содержимого для уже допущенного делегата.

Формат текста внутри QR — `{tag}·{full_name}·{city}·{token}`, разделитель «·» (не запятая и не
пробел — оба встречаются в свободных полях ФИО/города). Порядок ЗАКРЕПЛЁН: будущий парсер CSV
сканера ищет метку события в НАЧАЛЕ строки и берёт токен ХВОСТОМ — последним полем после
последнего «·». Менять порядок нельзя без синхронной правки парсера."""
from __future__ import annotations

import io
import logging

import segno

from database.db import get_or_create_checkin_token
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_QR_SEP = "·"

_DEFAULT_CAPTION = (
    "🎟 Твой QR для отметки на форуме.\n\n"
    "Сохрани его заранее (например, сделай скриншот) — на площадке может не быть сети, а фото "
    "из чата открывается и без интернета."
)


async def _event_tag() -> str:
    """Метка события внутри QR: явный `checkin_event_tag`, если менеджер его задал; иначе —
    `event_season` без пробелов и апострофа («YL'26» -> «YL26»). Пустые обе настройки (событие
    ещё не сконфигурировано) дают дефолт «EVENT» — генерация QR не должна падать из-за пустого
    реестра, просто получится менее говорящая метка."""
    tag = (await get_setting_typed("checkin_event_tag") or "").strip()
    if tag:
        return tag
    season = (await get_setting_typed("event_season") or "").strip()
    if season:
        return season.replace("'", "").replace(" ", "")
    return "EVENT"


def build_payload(tag: str, full_name: str, city: str, token: str) -> str:
    """Чистая сборка строки QR — вынесена отдельно от `build_checkin_qr`, чтобы формат
    (порядок полей, разделитель «·», плейсхолдер «—» для пустых ФИО/города) был проверяем
    юнит-тестом без генерации самой картинки/обращения к БД. Токен ВСЕГДА последним полем —
    см. докстринг модуля."""
    full_name = (full_name or "").strip() or "—"
    city = (city or "").strip() or "—"
    return _QR_SEP.join([tag, full_name, city, token])


async def build_checkin_payload(user: dict) -> str | None:
    """Строка содержимого QR для делегата `user`, БЕЗ рендера картинки — переиспользуется и
    `build_checkin_qr` ниже, и юнит-тестом на формат. Генерирует и сохраняет `checkin_token`,
    если его ещё нет (лениво, `get_or_create_checkin_token`). `None`, если пользователя нет."""
    telegram_id = user["telegram_id"]
    token = await get_or_create_checkin_token(telegram_id)
    if not token:
        return None
    tag = await _event_tag()
    return build_payload(tag, user.get("full_name"), user.get("event_city"), token)


async def build_checkin_qr(user: dict) -> tuple[bytes, str]:
    """`(png_bytes, caption)` для делегата `user` (строка `database.db.get_user`). Генерирует
    и сохраняет `checkin_token`, если его ещё нет (лениво, `get_or_create_checkin_token`) —
    второй вызов для того же делегата отдаёт PNG с тем же токеном внутри.

    `caption` возвращается НЕ переведённым (русский, из реестра `checkin_qr_caption_text`) —
    перевод на английский, как и у остальных ответов делегату, делает вызывающий
    (`reg_i18n.tr_text`) перед отправкой."""
    payload = await build_checkin_payload(user)
    if payload is None:
        raise ValueError(f"build_checkin_qr: нет пользователя {user.get('telegram_id')}")

    qr = segno.make(payload)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=6, border=2)

    caption = await get_setting_typed("checkin_qr_caption_text") or _DEFAULT_CAPTION
    return buf.getvalue(), caption

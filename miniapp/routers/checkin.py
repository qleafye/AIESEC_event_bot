"""Phase 12 (FORUM-CHECKIN.md, D-08/D-12/D-13, идея №9): сканер отметки на форуме в Mini App.

Основной способ отметки на входе (D-08 — `Telegram.WebApp.showScanQrPopup`, непрерывный режим,
скан сразу ставит отметку). Право — `checkin` (капа заведена в `handlers/admin_caps.py`,
уже отражена в `dashboard/access.py::ALL_CAPABILITIES`, сторож `tests/test_dashboard_auth.py`
сравнивает оба списка), раздел-чекбокс `miniapp_section_checkin` (`SECTIONS` в
`miniapp/deps.py`) — обе проверки на КАЖДОЙ ручке, тот же приём, что у `admin_tasks.py`.

Городской скоуп — тот же приём, что `admin_tasks.py::bound_city`: суперадмин (ADMIN_IDS) не
ограничен НИКОГДА, модуль городов выключен -> ограничений нет, иначе -- `Principal.city`
(`staff.city`, привязка менеджера, НЕ бот-овский `admin_selected_city` — тот живёт в
aiogram-зависимом `handlers/admin_core.py`, сюда его импортировать нельзя). `/stats` строит ту
же разбивку по городам, что бот (`handlers/admin_checkin.py::_counter_line`) — те же
`count_checkins_by_point(city_scope=…)`/`count_approved_current_season(city_scope=…)`
(задача A2). Скан/ручная отметка (`/scan`/`/manual`) с D-26 (24.09) ПРОВЕРЯЮТ город делегата
против привязки менеджера И на входе, не только на сессиях — уточняет D-15: стойки физически не
разложены по городам, но волонтёр за стойкой всё равно городской; делегат другого города
получает отказ словами (`_entry_city_denial`), а не тихую отметку.

QR не нашего события (`services.checkin.current_event_tag()` не совпал с меткой в самом QR) —
отдельный код `foreign_event`, ПРОВЕРЯЕТСЯ ПЕРВЫМ, до поиска делегата по токену: токен внутри
чужого QR искать в нашей БД бессмысленно и рискованно (совпадение токенов между независимыми
событиями не исключено при достаточном числе форумов на одном боте).

Регистрация на месте (D-41, 27.09): `/onsite/*` — одобрение ОДНОГО человека у стойки
(`services.onsite_reg.approve_at_door`), список walk-in «Ждут на стойке» и QR короткой анкеты;
флаги `onsite_approve`/`onsite_register` в ответах скана/поиска — только при включённом
тумблере города стойки (D-36)."""
from __future__ import annotations

import base64
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from cities import (
    cities_module_on,
    city_label,
    city_label_or_none,
    city_scope,
    default_city_code,
    enabled_cities,
    normalize_city,
)
from database.db import (
    count_checkins_by_point,
    get_program_session,
    get_user,
    list_onsite_pending,
)
from reg_engine import is_past_season_row
from services.checkin import (
    DENIAL_REASON_TEXT,
    ENTRY_POINT,
    ENTRY_POINT_LABEL,
    checkin_denial,
    current_event_tag,
    parse_qr_payload,
    record_arrival,
    resolve_scanned_user,
)
from services import checkin_arrival, checkin_training, i18n
from services import venue_log
from services.onsite_reg import (
    approve_at_door,
    is_pending_walkin,
    onsite_enabled,
    refine_denial,
    rejected_reason_text,
    walkin_link,
    walkin_qr_png,
)
from services.person_search import search_people
from settings_schema import get_setting_typed
from services.program import checkin_session_points

from miniapp.deps import Principal, require_cap, require_section
from miniapp.outbox import enqueue

router = APIRouter()
logger = logging.getLogger(__name__)


async def _forward_first_entry(result: dict) -> dict:
    """Первая отметка входа: слушатели `services.checkin.register_first_entry_listener` живут в
    процессе бота — событие уходит туда через outbox (`checkin_first_entry`), наружу во фронт
    не отдаётся."""
    event = result.pop("first_entry", None)
    if event is not None:
        # Fail-soft: отметка к этому моменту УЖЕ записана — сбой очереди не должен давать
        # сканеру 500 и повторный скан; теряется только уведомление слушателям.
        try:
            await enqueue("checkin_first_entry", event)
        except Exception:  # noqa: BLE001
            logger.exception("checkin: не удалось поставить checkin_first_entry в outbox")
    return result

def _staff_name(p: Principal) -> str | None:
    return venue_log.staff_display_name(first_name=p.first_name, username=p.username)


# Статусы `record_arrival`, которые означают «не пропущен» (а не отметку/повтор).
_ARRIVAL_DENIAL_STATUSES = frozenset({"wrong_city", "wrong_day", "invalid_point"})


async def _log_denial(
    p: Principal, bound: str | None, code: str, *, point: str, source: str,
    user: dict | None = None,
) -> None:
    """Отказ -> строка «⛔ не пропустил(а)» в журнале площадки. Из данных делегата — только
    `telegram_id` (если найден). Город — стойки (`bound`), иначе делегата. Fail-soft: ответ
    сканеру не зависит от журнала."""
    try:
        await venue_log.log_denial(
            code, staff_id=p.telegram_id, staff_name=_staff_name(p),
            telegram_id=(user or {}).get("telegram_id"), city=bound, point=point, source=source,
        )
    except Exception:  # noqa: BLE001
        logger.exception("checkin: не записал отказ %s в журнал площадки", code)


async def _with_undo(result: dict, p: Principal) -> dict:
    """Идея №32: живая отметка (new/moved) получила строку журнала — фронт показывает на
    плашке кнопку «↩️ Отменить» на `undo_seconds` секунд. Подпись — из реестра, в переводе
    на язык волонтёра. Внутренний id журнала наружу уходит только как ключ отмены."""
    log_id = result.pop("log_id", None)
    if log_id:
        lang, tr_map = await i18n.context(p.telegram_id)
        result["undo"] = {
            "id": log_id,
            "seconds": venue_log.UNDO_WINDOW_SECONDS,
            "label": await i18n.tr_setting("checkin_undo_button_text", lang, tr_map) or "↩️",
        }
    return result


_SEARCH_LIMIT = 20
_SECTION = "checkin"
_CAP = "checkin"
# D-41 (ревью 28.09): одобрять у стойки — отдельное право. Волонтёру у двери зала хватает
# `checkin`; одобряют держатели `checkin_approve` (волонтёр регистрации) и `moderate_reg`.
_APPROVE_CAP = "checkin_approve"
_APPROVE_CAPS = frozenset({_APPROVE_CAP, "moderate_reg"})


def _can_approve(p: Principal) -> bool:
    return bool(p.caps & _APPROVE_CAPS)


async def _bound_city(request: Request, p: Principal) -> str | None:
    """Тот же приём, что `admin_tasks.py::bound_city` — суперадмин не ограничен никогда,
    модуль городов выключен -> ограничений нет, иначе -- город, к которому привязан менеджер."""
    if p.telegram_id in (request.app.state.cfg.admin_ids or ()):
        return None
    if not await cities_module_on() or not p.city:
        return None
    return normalize_city(p.city)


async def _resolve_scanner_city(bound: str | None) -> str | None:
    """Форум-ночь п.5 (D-18): тот же трёхветочный приём, что
    `handlers/admin_checkin.py::_resolve_checkin_screen_city` — привязанный город менеджера
    сразу его, модуль выключен -> единственный дефолтный город, иначе (суперадмин/непривязанный
    менеджер) -> `None`, экрану сканера нужен явный выбор города (`GET /points?city=`)."""
    if bound is not None:
        return bound
    if not await cities_module_on():
        return default_city_code()
    return None


async def _stand_city(request: Request, p: Principal, requested: str | None) -> str | None:
    """D-41: город стойки для регистрации на месте. Привязка волонтёра побеждает (D-26), иначе
    город, выбранный в сканере (если он среди включённых), иначе — тот же выбор, что у экрана."""
    bound = await _bound_city(request, p)
    if bound is not None:
        return bound
    if requested and any(c["code"] == requested for c in await enabled_cities()):
        return requested
    return await _resolve_scanner_city(None)


_ONSITE_APPROVE_CODES = frozenset({"not_approved", "past_season"})


class _OnsiteGate:
    """Тумблер «регистрация на месте» на один запрос: читается один раз на город."""

    def __init__(self, stand: str | None, bound: str | None, event_season: str | None,
                 can_approve: bool = True):
        self.stand = stand
        self.bound = bound
        self.event_season = event_season
        self.can_approve = can_approve
        self._cache: dict[str | None, bool] = {}

    async def enabled(self, city: str | None) -> bool:
        if city not in self._cache:
            self._cache[city] = await onsite_enabled(city)
        return self._cache[city]

    async def _may_approve(self, user: dict) -> bool:
        past = is_past_season_row(user, self.event_season)
        # walk-in без решения стойка одобряет, переводя в свой город (как делегата прошлого
        # сезона); обычная заявка чужого города — по-прежнему D-26.
        free = past or is_pending_walkin(user)
        other_city = self.bound and not free and normalize_city(user.get("event_city")) != self.bound
        city = self.stand or normalize_city(user.get("event_city"))
        return not other_city and await self.enabled(city)

    async def move_to(self, user: dict | None) -> str | None:
        """Подпись города стойки, если одобрение переведёт walk-in туда из другого города —
        сканер добавит это в подтверждение."""
        if not is_pending_walkin(user) or not self.stand:
            return None
        if normalize_city(user.get("event_city")) == self.stand:
            return None
        return await city_label(self.stand)

    async def flags(self, denial_code: str | None, user: dict | None) -> dict:
        """`onsite_approve` — заявка есть, но не одобрена/прошлого сезона, и волонтёр вправе её
        одобрить (та же проверка города, что в `approve_at_door`); `onsite_override` — заявку
        ОТКЛОНИЛ менеджер (код `rejected`): вместо обычной кнопки — «вопреки отказу» со своим
        подтверждением; `onsite_register` — человека нет в базе. Все — только при включённом
        тумблере города стойки."""
        approve = override = False
        if not self.can_approve:
            pass  # кнопок одобрения нет — нет права «Одобрять на месте»
        elif denial_code in _ONSITE_APPROVE_CODES and user:
            approve = await self._may_approve(user)
        elif denial_code == "rejected" and user:
            override = await self._may_approve(user)
        register = denial_code == "no_user" and await self.enabled(self.stand)
        return {"onsite_approve": bool(approve), "onsite_register": bool(register),
                "onsite_override": bool(override),
                "onsite_move_to": await self.move_to(user) if approve else None}


async def _onsite_gate(request: Request, p: Principal, requested: str | None) -> _OnsiteGate:
    return _OnsiteGate(
        await _stand_city(request, p, requested), await _bound_city(request, p),
        await get_setting_typed("event_season") or None, _can_approve(p),
    )


async def _denial_text(code: str, user: dict | None, p: Principal) -> str:
    """Текст отказа на плашке. Отклонённая заявка — из реестра, с причиной, на языке волонтёра;
    остальные коды — общая таблица `DENIAL_REASON_TEXT`."""
    if code == "rejected" and user:
        lang, tr_map = await i18n.context(p.telegram_id)
        return await rejected_reason_text(user, lang, tr_map)
    return DENIAL_REASON_TEXT.get(code, code)


async def _point_city_denial(bound: str | None, point: str) -> dict | None:
    """Ревью (D-15/D-18), уточнено D-26 (24.09): волонтёр, привязанный к городу (`bound`,
    результат `_bound_city`), не должен отмечать на СЕССИИ ДРУГОГО города — устаревший/ручной
    список точек в его сканере (`GET /points` отдаёт точки только своего города, но `point` в
    теле запроса ничем не проверен) иначе позволил бы это буквально одним POST-запросом. Только
    точки-СЕССИИ (`point` вида `"session:{id}"`) — «Вход» здесь не трогаем, для него отдельная
    проверка `_entry_city_denial` (сверяет ГОРОД ДЕЛЕГАТА, а не точки, D-26).

    Суперадмин и волонтёр без привязки к городу (`bound is None`) не ограничены — та же
    трёхветочная логика, что везде в этом модуле. `None`, если точка допустима (или это не
    точка-сессия вовсе, или сессия не найдена — `record_arrival` сам вернёт `invalid_point`)."""
    if not (point or "").startswith("session:"):
        return None
    if bound is None:
        return None
    try:
        session_id = int(point.split(":", 1)[1])
    except (ValueError, IndexError):
        return None
    session = await get_program_session(session_id)
    if session is None:
        return None
    if normalize_city(session["city"]) != bound:
        return {
            "status": "wrong_city_point",
            "reason_text": "Сессия другого города — выберите точку заново",
        }
    return None


async def _entry_city_denial(bound: str | None, user: dict) -> dict | None:
    """D-26 (решение владельца 24.09): волонтёр, привязанный к городу, работает ТОЛЬКО со своим
    городом — И НА ВХОДЕ (уточняет D-15/D-08: раньше вход не был ограничен вовсе, стойки входа
    физически не разложены по городам, но волонтёр за стойкой всё равно городской). Делегат
    ДРУГОГО города получает отказ словами вместо тихой отметки — волонтёр отправляет его к своей
    стойке/организаторам, а не отмечает по ошибке в чужом городе.

    Суперадмин и волонтёр без привязки (`bound is None`) не ограничены — без изменений.
    Переиспользует статус `wrong_city` (тот же тон/заголовок на экране, что у отказа сессии
    другого города в `record_arrival` — фронт уже умеет его показывать, см. `scanner.js`)."""
    if bound is None:
        return None
    delegate_city = normalize_city(user.get("event_city"))
    if delegate_city == bound:
        return None
    delegate_label = await city_label(delegate_city) if delegate_city else "—"
    return {
        "status": "wrong_city",
        "reason_text": (
            f"Делегат с форума в {delegate_label} — отправьте на стойку своего города/к "
            "организаторам"
        ),
    }


def _person_fields(user: dict) -> dict:
    return {
        "telegram_id": user.get("telegram_id"),
        "full_name": user.get("full_name") or "—",
        "city": user.get("event_city") or None,
        "university": user.get("university") or None,
        "username": user.get("username") or None,
    }


async def _training_preview(
    p: Principal, bound: str | None, *, parsed: dict | None = None, user: dict | None = None,
) -> dict:
    """Бэклог чек-ина №7: настоящий QR (или человек из поиска) в точке «🧪 Тренировка» —
    та же плашка, что дал бы вход, по реальным данным, но без записи отметки и без строки
    журнала площадки (ни отметки, ни отказа)."""
    lang, tr_map = await i18n.context(p.telegram_id)
    if parsed is not None:
        tag = parsed.get("tag") or ""
        if not tag or tag != await current_event_tag():
            return await checkin_training.as_training_point({
                "status": "foreign_event", "reason_text": DENIAL_REASON_TEXT["foreign_event"],
                "full_name": parsed.get("full_name") or None, "city": parsed.get("city") or None,
            }, lang, tr_map)
        user, denial_code = await resolve_scanned_user(
            parsed.get("token"), point=checkin_training.TRAINING_POINT, source="miniapp",
        )
    else:
        denial_code = await checkin_denial(user)
    if denial_code is not None:
        return await checkin_training.as_training_point({
            "status": "not_found" if denial_code == "no_user" else "denied",
            "reason_text": DENIAL_REASON_TEXT.get(denial_code, denial_code),
            "full_name": (user or {}).get("full_name") or (parsed or {}).get("full_name") or None,
            "city": (user or {}).get("event_city") or (parsed or {}).get("city") or None,
        }, lang, tr_map)
    res = await _entry_city_denial(bound, user) or await checkin_training.preview_entry(user, lang, tr_map)
    return await checkin_training.as_training_point({**res, **_person_fields(user)}, lang, tr_map)


class ScanBody(BaseModel):
    payload: str = ""
    point: str = ENTRY_POINT
    city: str | None = None  # город, выбранный в сканере (для кнопок регистрации на месте)


async def _with_city_label(res: dict) -> dict:
    """Плашка сканера: `city` — код (логика), `city_label` — подпись для человека. Всегда,
    даже при выключенном модуле городов — иначе на плашке «msk» (CLAUDE.md)."""
    res["city_label"] = await city_label_or_none(res.get("city"))
    return res


@router.post("/app/api/checkin/scan")
async def checkin_scan(
    body: ScanBody, request: Request,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    return await _with_city_label(await _scan(body, request, p))


async def _scan(body: ScanBody, request: Request, p: Principal) -> dict:
    point = body.point or ENTRY_POINT
    parsed = parse_qr_payload(body.payload)
    # Бэклог чек-ина №7: учебный QR в любой точке — только учебная плашка, ничего не пишется.
    training = await checkin_training.resolve_training(parsed.get("token"), point=point, source="miniapp")
    if training is not None:
        lang, tr_map = await i18n.context(p.telegram_id)
        return await checkin_training.training_scan_response(training, parsed, lang, tr_map)
    bound = await _bound_city(request, p)
    if point == checkin_training.TRAINING_POINT:
        return await _training_preview(p, bound, parsed=parsed)
    point_denial = await _point_city_denial(bound, point)
    if point_denial is not None:
        await _log_denial(p, bound, point_denial["status"], point=point, source="miniapp")
        return point_denial

    tag = parsed.get("tag") or ""
    if not tag or tag != await current_event_tag():
        await _log_denial(p, bound, "foreign_event", point=point, source="miniapp")
        return {
            "status": "foreign_event",
            "reason_text": DENIAL_REASON_TEXT["foreign_event"],
            "full_name": parsed.get("full_name") or None,
            "city": parsed.get("city") or None,
        }

    token = parsed.get("token")
    user, denial_code = await resolve_scanned_user(token, point=point, source="miniapp")
    denial_code = refine_denial(denial_code, user)
    if denial_code is not None:
        await _log_denial(p, bound, denial_code, point=point, source="miniapp", user=user)
        gate = await _onsite_gate(request, p, body.city)
        return {
            "status": "not_found" if denial_code == "no_user" else "denied",
            "reason_text": await _denial_text(denial_code, user, p),
            "full_name": (user or {}).get("full_name") or parsed.get("full_name") or None,
            "city": (user or {}).get("event_city") or parsed.get("city") or None,
            "telegram_id": (user or {}).get("telegram_id"),
            **await gate.flags(denial_code, user),
        }

    if not point.startswith("session:"):
        entry_denial = await _entry_city_denial(bound, user)
        if entry_denial is not None:
            await _log_denial(p, bound, entry_denial["status"], point=point, source="miniapp", user=user)
            return {**entry_denial, **_person_fields(user)}

    result = await _with_undo(await _forward_first_entry(await record_arrival(
        user, point, source="miniapp", by_staff_id=p.telegram_id, staff_name=_staff_name(p),
    )), p)
    if result.get("status") in _ARRIVAL_DENIAL_STATUSES:
        await _log_denial(p, bound, result["status"], point=point, source="miniapp", user=user)
    return {**result, **_person_fields(user)}


class ManualBody(BaseModel):
    telegram_id: int
    point: str = ENTRY_POINT
    city: str | None = None


@router.post("/app/api/checkin/manual")
async def checkin_manual(
    body: ManualBody, request: Request,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    """D-11/D-12: делегат найден поиском (телефон сел/нет QR под рукой), не сканом — та же
    отметка, источник `manual` отличает её в журнале (будущее B1-31)."""
    return await _with_city_label(await _manual(body, request, p))


async def _manual(body: ManualBody, request: Request, p: Principal) -> dict:
    point = body.point or ENTRY_POINT
    bound = await _bound_city(request, p)
    if point == checkin_training.TRAINING_POINT:
        return await _training_preview(p, bound, user=await get_user(body.telegram_id))
    point_denial = await _point_city_denial(bound, point)
    if point_denial is not None:
        await _log_denial(p, bound, point_denial["status"], point=point, source="manual")
        return point_denial

    user = await get_user(body.telegram_id)
    denial_code = refine_denial(await checkin_denial(user), user)
    if denial_code is not None:
        await _log_denial(p, bound, denial_code, point=point, source="manual", user=user)
        gate = await _onsite_gate(request, p, body.city)
        return {
            "status": "not_found" if denial_code == "no_user" else "denied",
            "reason_text": await _denial_text(denial_code, user, p),
            "full_name": (user or {}).get("full_name") if user else None,
            "city": (user or {}).get("event_city") if user else None,
            "telegram_id": (user or {}).get("telegram_id"),
            **await gate.flags(denial_code, user),
        }

    if not point.startswith("session:"):
        entry_denial = await _entry_city_denial(bound, user)
        if entry_denial is not None:
            await _log_denial(p, bound, entry_denial["status"], point=point, source="manual", user=user)
            return {**entry_denial, **_person_fields(user)}

    result = await _with_undo(await _forward_first_entry(await record_arrival(
        user, point, source="manual", by_staff_id=p.telegram_id, staff_name=_staff_name(p),
    )), p)
    if result.get("status") in _ARRIVAL_DENIAL_STATUSES:
        await _log_denial(p, bound, result["status"], point=point, source="manual", user=user)
    return {**result, **_person_fields(user)}


class UndoBody(BaseModel):
    id: int


@router.post("/app/api/checkin/undo")
async def checkin_undo(
    body: UndoBody,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    """Идея №32: волонтёр отменяет СВОЮ ПОСЛЕДНЮЮ отметку в окне отмены. Все проверки
    (чья, последняя ли, не истекло ли окно) — на сервере, `services.venue_log.undo_last_scan`;
    фронт только прячет кнопку по таймеру. Любой отказ — один человеческий текст из реестра:
    дальше снимает менеджер в боте."""
    lang, tr_map = await i18n.context(p.telegram_id)
    try:
        code = await venue_log.undo_last_scan(p.telegram_id, _staff_name(p), body.id)
    except Exception:  # noqa: BLE001 — сбой БД: волонтёру человеческий отказ, не 500
        logger.exception("checkin_undo: сбой отмены log_id=%s staff=%s", body.id, p.telegram_id)
        code = "error"
    if code == "ok":
        text = await i18n.tr_setting("checkin_undo_done_text", lang, tr_map)
        return {"status": "undone", "reason_text": text or "Отметка снята."}
    key = _UNDO_REFUSAL_KEYS.get(code, "checkin_undo_refused_text")
    text = await i18n.tr_setting(key, lang, tr_map)
    return {
        "status": "undo_refused", "code": code,
        "reason_text": text or "Отменить не получилось — попросите менеджера снять отметку.",
    }


@router.get("/app/api/checkin/net-texts")
async def checkin_net_texts(
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    """Идея №11: тексты полосы «сеть медленная» — сканер берёт их один раз при открытии
    экрана (пока сеть жива), в переводе на язык волонтёра. Инструкция — шпаргалка волонтёра."""
    lang, tr_map = await i18n.context(p.telegram_id)
    return {
        "text": await i18n.tr_setting("checkin_slow_net_text", lang, tr_map) or "",
        "help_label": await i18n.tr_setting("checkin_slow_net_help_button_text", lang, tr_map) or "",
        "help_text": await i18n.tr_setting("checkin_volunteer_guide_text", lang, tr_map) or "",
        # D-41/D-34: подписи регистрации на месте — в переводе на язык волонтёра.
        "onsite": {key: await i18n.tr_setting(key, lang, tr_map) or "" for key in _ONSITE_TEXT_KEYS},
    }


_ONSITE_TEXT_KEYS = (
    "onsite_approve_button_text",
    "onsite_approve_confirm_text",
    "onsite_register_button_text",
    "onsite_register_hint_text",
    "onsite_pending_title_text",
    "onsite_override_button_text",
    "onsite_override_confirm_text",
    "onsite_move_confirm_text",
)


# Код отказа отмены -> текст из реестра: «отметка уже изменилась» (её перенёс/снял другой),
# «не получилось» (сбой), остальное — «отменить уже нельзя».
_UNDO_REFUSAL_KEYS = {
    "gone": "checkin_undo_changed_text",
    "error": "checkin_undo_failed_text",
}


@router.get("/app/api/checkin/search")
async def checkin_search(
    request: Request, q: str = "", city: str | None = None,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    """D-12/D-13: поиск по фамилии (общий `services.person_search.search_people`, ё=е) —
    одобренные текущего сезона первыми (`eligible`), город/@username/вуз в каждой строке
    различают тёзок. Городской скоуп — как у сканирования (см. докстринг модуля)."""
    bound = await _bound_city(request, p)
    scope = city_scope(bound) if bound else None
    found = await search_people(q, city_scope=scope, limit=_SEARCH_LIMIT)
    gate = await _onsite_gate(request, p, city)
    if scope is not None and gate.can_approve:
        # Walk-in без решения, отсканировавший QR чужого города, стоит у ЭТОЙ стойки —
        # привязанный волонтёр находит его по фамилии и одобряет с переводом в свой город.
        seen = {row["user_id"] for row in found}
        for row in await search_people(q, limit=_SEARCH_LIMIT, include_started=False):
            if row["user_id"] not in seen and row["source"] == "users" and is_pending_walkin(
                await get_user(row["user_id"])
            ):
                found.append(row)

    items = []
    for row in found:
        user = await get_user(row["user_id"]) if row["source"] == "users" else None
        denial_code = refine_denial(await checkin_denial(user), user) if user is not None else "no_user"
        raw_city = row.get("city")
        items.append({
            "telegram_id": row["user_id"],
            "full_name": row.get("full_name") or "—",
            "city": raw_city,
            "city_label": await city_label_or_none(raw_city),
            "university": row.get("university"),
            "username": row.get("username"),
            "eligible": denial_code is None,
            "reason_text": None if denial_code is None else await _denial_text(denial_code, user, p),
            **await gate.flags(denial_code, user),
        })
    # Одобренные текущего сезона (eligible) — первыми (D-12); внутри каждой группы порядок
    # `search_people` (алфавит по имени) сохраняется — сортировка Python стабильна.
    items.sort(key=lambda it: 0 if it["eligible"] else 1)
    return {"items": items}


@router.get("/app/api/checkin/points")
async def checkin_points(
    request: Request, city: str | None = None,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    """Форум-ночь п.5 (D-18/D-20): точки отметки для сканера — «Вход» + сессии СЕГОДНЯ города
    волонтёра, «идут сейчас» — первыми. Городской скоуп — тот же трёхветочный приём, что
    `/stats` выше: привязанный менеджер получает свой город сразу (без выбора), суперадмин/
    непривязанный менеджер — список городов на выбор (`?city=`, отдаётся в `cities`), модуль
    выключен — единственный (дефолтный) город без выбора вовсе."""
    bound = await _bound_city(request, p)
    resolved = await _resolve_scanner_city(bound)
    cities_payload = None
    if resolved is None:
        enabled = await enabled_cities()
        if city and any(c["code"] == city for c in enabled):
            resolved = city
        else:
            cities_payload = [{"code": c["code"], "label": await city_label(c["code"])} for c in enabled]

    from services.timeutil import msk_now  # лениво: тесты замораживают «сейчас» в модуле

    points = [{
        "point": ENTRY_POINT, "label": ENTRY_POINT_LABEL, "live": None,
        # Вход каждый день: у точки «Вход» — сколько вошли СЕГОДНЯ.
        "count": await count_checkins_by_point(ENTRY_POINT, day=msk_now().strftime("%Y-%m-%d")),
        "capacity": None,
    }]
    # Бэклог чек-ина №7 (D-28 «Вход всегда первым»): «🧪 Тренировка» — сразу за входом.
    lang, tr_map = await i18n.context(p.telegram_id)
    points.append(await checkin_training.training_point_entry(lang, tr_map))
    if resolved is not None:
        for sp in await checkin_session_points(resolved):
            points.append({**sp, "count": await count_checkins_by_point(sp["point"])})
    return {
        "city": resolved, "cities": cities_payload, "points": points,
        # D-36: кнопки регистрации на месте в сканере — только при включённом тумблере города.
        "onsite_enabled": bool(resolved) and await onsite_enabled(resolved),
        "onsite_can_approve": _can_approve(p),
    }


@router.get("/app/api/checkin/stats")
async def checkin_stats(
    request: Request,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    """A2 (FORUM-CHECKIN.md): та же построчная разбивка по городам, что бот
    (`handlers/admin_checkin.py::_counter_line`) — три ветки: менеджер с городом-привязкой
    видит только свой (`cities: null`), модуль городов выключен — общий счётчик байт-в-байт
    как раньше, иначе — построчно по городам с хотя бы одним одобренным + Итого."""
    bound = await _bound_city(request, p)
    # Тот же счётчик, что у бота (`services.checkin_arrival`): одобренные текущего сезона со
    # входом; в день форума — вход сегодня (`today: true`), иначе — хоть один вход за форум.
    day = await checkin_arrival.counter_day()
    if bound is not None or not await cities_module_on():
        arrived, approved = await checkin_arrival.arrived_counts(
            city_scope(bound) if bound is not None else None, day,
        )
        return {"arrived": arrived, "approved": approved, "cities": None, "today": bool(day)}

    cities_out = []
    total_arrived = 0
    total_approved = 0
    for c in await enabled_cities():
        code = c["code"]
        arrived, approved = await checkin_arrival.arrived_counts(city_scope(code), day)
        if approved == 0:
            continue
        cities_out.append({
            "code": code, "label": await city_label(code), "arrived": arrived, "approved": approved,
        })
        total_arrived += arrived
        total_approved += approved
    return {"arrived": total_arrived, "approved": total_approved, "cities": cities_out,
            "today": bool(day)}


# ── регистрация на месте (D-41) ──────────────────────────────────────────────────────────

_ONSITE_APPROVED_TEXT = "Одобрен(а) на месте"
_LINK_ERROR_TEXT = "Не получилось собрать ссылку — откройте сканер заново."
_LINK_NO_CITY_TEXT = "Выберите город вверху экрана — у каждого города своя ссылка."


async def _onsite_off(p: Principal) -> dict:
    lang, tr_map = await i18n.context(p.telegram_id)
    text = await i18n.tr_setting("onsite_off_text", lang, tr_map)
    return {"status": "onsite_off", "reason_text": text or "Регистрация на месте выключена."}


class OnsiteApproveBody(BaseModel):
    # Один человек за запрос — списков id нет (урок инцидента 06.09 с тихим массовым одобрением).
    telegram_id: int
    city: str | None = None
    # Заявку отклонил менеджер: без явного «вопреки отказу» (отдельная кнопка и своё
    # подтверждение в сканере) сервер её не одобряет.
    override_reject: bool = False


@router.post("/app/api/checkin/onsite/approve")
async def onsite_approve(
    body: OnsiteApproveBody, request: Request,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    """D-41: волонтёр одобряет человека у стойки и сразу отмечает вход. Решение записано на
    него (журнал решений + журнал площадки, `approve_at_door`); лист, сообщение и QR человеку
    шлёт бот через outbox `onsite_approved`. Город стойки — привязка волонтёра (D-26), иначе
    выбранный в сканере; тумблер города проверяется здесь, на сервере (D-36). Право — не
    просто `checkin`, а «Одобрять на месте» (`checkin_approve` или `moderate_reg`)."""
    if not _can_approve(p):
        raise HTTPException(403, {"reason": "no_cap", "cap": _APPROVE_CAP})
    bound = await _bound_city(request, p)
    stand = await _stand_city(request, p, body.city)
    user = await get_user(body.telegram_id)
    if user is None:
        return {"status": "not_found", "reason_text": DENIAL_REASON_TEXT["no_user"]}

    res = await approve_at_door(
        user, city=stand, staff_id=p.telegram_id, staff_name=_staff_name(p), bound=bound,
        override_reject=body.override_reject,
    )
    status = res.get("status")
    if status == "rejected":
        # Город и тумблер approve_at_door уже проверил — волонтёр может пропустить только
        # отдельной кнопкой «вопреки отказу».
        return await _with_city_label({
            "status": "rejected", "reason_text": await _denial_text("rejected", user, p),
            "telegram_id": user["telegram_id"], "onsite_override": True, **_person_fields(user),
        })
    if status == "onsite_off":
        return await _with_city_label({**await _onsite_off(p), **_person_fields(user)})
    if status == "wrong_city":
        await _log_denial(p, bound, "wrong_city", point=ENTRY_POINT, source="manual", user=user)
    if status in ("wrong_city", "denied", "not_found"):
        res.pop("first_entry", None)
        return await _with_city_label({**res, **_person_fields(user)})

    # Событие onsite_approved (лист, сообщение, QR) сервис ставит сам сразу после флипа, а
    # повторное нажатие восстанавливает его без дубля (`ensure_onsite_outbox`).
    await _forward_first_entry(res)
    if res.get("onsite_approved"):
        res["reason_text"] = _ONSITE_APPROVED_TEXT
    fresh = await get_user(body.telegram_id) or user
    return await _with_city_label({**res, **_person_fields(fresh)})


@router.get("/app/api/checkin/onsite/pending")
async def onsite_pending(
    request: Request, city: str | None = None,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    """D-41: walk-in сегодняшнего дня своего города, ждущие одобрения у стойки. Телефон наружу
    не отдаётся — волонтёру хватает имени, вуза и @username (T-wt3-14)."""
    stand = await _stand_city(request, p, city)
    if not await onsite_enabled(stand):
        return {"items": [], "enabled": False}
    from services.timeutil import msk_now  # лениво: тесты замораживают «сейчас» в модуле

    rows = await list_onsite_pending(
        city_scope=city_scope(stand) if stand else None, day=msk_now().strftime("%Y-%m-%d"),
    )
    items = []
    for row in rows:
        items.append({
            "telegram_id": row["telegram_id"],
            "full_name": row.get("full_name") or "—",
            "username": (row.get("username") or "").lstrip("@") or None,
            "university": row.get("university") or None,
            "city_label": await city_label_or_none(row.get("event_city")),
            "registered_at": (row.get("registration_date") or "")[11:16],
        })
    return {"items": items, "enabled": True, "can_approve": _can_approve(p)}


@router.get("/app/api/checkin/onsite/link")
async def onsite_link(
    request: Request, city: str | None = None,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    """D-41: ссылка короткой анкеты своего города и её QR — data URI прямо в JSON: тег img
    не шлёт initData, картинка отдельным URL получила бы 401."""
    stand = await _stand_city(request, p, city)
    if stand is None and await cities_module_on():
        return {"status": "error", "reason_text": _LINK_NO_CITY_TEXT}
    if not await onsite_enabled(stand):
        return await _onsite_off(p)
    link = await walkin_link(request.app.state.cfg.bot_username, stand)
    if not link:
        return {"status": "error", "reason_text": _LINK_ERROR_TEXT}
    lang, tr_map = await i18n.context(p.telegram_id)
    qr = base64.b64encode(walkin_qr_png(link)).decode("ascii")
    return {
        "url": link,
        "qr": f"data:image/png;base64,{qr}",
        "hint": await i18n.tr_setting("onsite_register_hint_text", lang, tr_map) or "",
    }


__all__ = ["router"]

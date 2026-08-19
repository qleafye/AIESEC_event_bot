"""Phase 16 (16-04, GAME-UI-03): чистые рендеры и клавиатуры менеджерских экранов геймы —
карточка модерации сдачи (Экран 5), карточки/кнопки «🪙 Монеты вручную» (Экран 8), полосы
«📊 Статистика геймы» (Экран 9). Модуль БЕЗ роутера и хендлеров (как game_labels.py /
game_task_wizard.py): хендлеры остаются в handlers/admin_gamification.py и импортируют отсюда
под прежними именами (с подчёркиванием), поэтому существующие тесты вида
`admin_gamification._render_submission_card(...)` работают без правок.

Вынесено сюда, потому что admin_gamification.py упёрся в потолок размера
(tests/test_module_size_convention_260816.py) — потолок не поднимался, см. 16-04-SUMMARY.

Все рендеры, которые зовут синхронные хендлеры, остаются синхронными: асинхронные значения
(RU-категория, подпись типов подтверждения, баланс, город) резолвит вызывающий ОДИН раз и
передаёт параметром — приём «resolve once, caller hands down» (Phase 09.1/14). Дефолт каждого
нового параметра — None, рендер при этом байт-в-байт прежний.
"""
import html as html_module

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import parse_proof_types, task_title
from settings_schema import get_setting_typed

# ── Подписи типов подтверждения (синхронная копия) ──────────────────────────────────────────
# Human-readable labels for GAME_PROOF_TYPES (D-08/CLAUDE.md «для людей, не для прогеров»):
# the manager taps a labeled button, never types a proof-type code. Синхронные потребители:
# чекбоксы визарда (`_game_task_proof_kb`), экспорт истории в Sheets (`_build_game_history`)
# и дефолт `_render_submission_card` без `proof_label_text`. Карточка модерации (единственный
# делегат-видимый по смыслу текст здесь) получает подпись из реестра через
# game_labels.proof_types_label — её резолвит `_show_current_submission`.
_GAME_PROOF_LABELS = {
    "photo": "📷 Скриншот/фото",
    "pdf": "📄 PDF",
    "text": "✍️ Текст",
    "link": "🔗 Ссылка",
}


def _proof_types_label(raw: str | None) -> str:
    """Phase 09.1 (A): proof_type is now possibly-multiple/possibly-empty (D-01, "можно
    несколько или ни одного") -- shared by the wizard checkboxes and the moderation card's
    synchronous default."""
    codes = parse_proof_types(raw)
    if not codes:
        return "не важно"
    return " + ".join(_GAME_PROOF_LABELS[c] for c in codes)


# ── Экран 5: карточка модерации ─────────────────────────────────────────────────────────────
# CR-01 (09.1-REVIEW.md): hard ceilings so a submission card can never blow past Telegram's
# sendMessage limit (4096 chars). _CARD_PART_MAX truncates one rendered part; _CARD_MAX
# truncates the whole assembled card as a last-resort backstop.
_CARD_PART_MAX = 500
_CARD_MAX = 3800

# CR-02 (09.1-REVIEW.md): sendMediaGroup accepts 2-10 items -- 11+ raises and used to drop the
# whole group silently. MEDIA_GROUP_MAX chunks the resend; _MEDIA_CAPTION_MAX (Telegram's own
# caption limit is 1024) truncates a caption with a margin, since captions come straight from
# unvalidated delegate input.
MEDIA_GROUP_MAX = 10
_MEDIA_CAPTION_MAX = 1000


def _render_submission_card(row: dict, position: int, total: int, parts: list[dict] | None = None,
                             city_labels: tuple[str, str] | None = None,
                             attempt: tuple[int, int] | None = None,
                             category_label_text: str | None = None,
                             remaining: int | None = None,
                             proof_label_text: str | None = None) -> str:
    """HTML card for one pending submission; all free-text (task text, submitter name) escaped
    — T-09-12: this is the FIRST render of delegate-supplied content to a manager.

    Phase 09.1 (A): `parts` defaults to None so every pre-existing call site/test keeps the
    single content_type/content rendering byte-for-byte. Pass `parts` (from
    `get_submission_parts_or_legacy`) to render every part of a free-form submission instead.

    Phase 09.1 (B): `city_labels` (delegate_label, task_label) defaults to None so this stays
    byte-identical when the cities module is off. This function is synchronous and cannot
    itself call cities_module_on()/city_label() -- the caller (_show_current_submission)
    resolves both labels ONCE and hands them down, same "resolve once" shape the confirm
    card uses for gt_city_step_shown.

    Phase 14 (14-03, GAME-10): `attempt` (K, N) defaults to None so this stays byte-identical
    when `game_resubmit_limit` is unset/0 -- same "resolve once, caller hands down" shape.

    Phase 16 (16-04, GAME-UI-03): `category_label_text` (RU-подпись категории из реестра),
    `remaining` («Осталось: N» под шапкой, только когда > 0) и `proof_label_text` (подпись
    типов подтверждения из реестра) — все три резолвит `_show_current_submission`; дефолт None
    = сырая категория / без строки «Осталось» / синхронная копия подписей — байт-в-байт как
    до плана."""
    def esc(v):
        return html_module.escape(str(v)) if v not in (None, "", "-") else None

    header = f"🎮 <b>Сдача {position}/{total}</b>"
    lines = [header]
    if remaining is not None and remaining > 0:
        lines.append(f"Осталось: {remaining}")
    # Quick 260819-gtl (CONTEXT.md decision 6): "Задание: <title>" line, not a raw text
    # preview -- task photo is deliberately NOT duplicated here (the submission's own parts
    # are what the manager needs to see).
    title = task_title({"title": row.get("task_title"), "text": row.get("task_text")})
    lines += ["", f"Задание: {esc(title) or '—'}"]
    lines.append(f"Категория: {esc(category_label_text) or esc(row.get('task_category')) or '—'}")
    lines.append(f"Предложено: {row.get('task_coins')}🪙")
    name = esc(row.get("user_full_name")) or "—"
    uname = esc(row.get("user_username"))
    lines.append(f"👤 {name}" + (f" ({uname})" if uname else ""))
    if city_labels is not None:
        delegate_label, task_label = city_labels
        lines.append(f"🏙 Город делегата: {esc(delegate_label) or '—'}")
        lines.append(f"🎯 Кому задание: {esc(task_label) or '—'}")
    # proof_label_text приходит из реестра (менеджерский ввод, не HTML-ключ) -- экранируем.
    if proof_label_text is not None:
        proof_label = html_module.escape(proof_label_text)
    else:
        proof_label = _proof_types_label(row.get("task_proof_type"))
    lines.append(f"Тип подтверждения: {proof_label}")
    if parts is None:
        content_type = row.get("content_type")
        if content_type in ("text", "link"):
            lines.append(f"Содержимое: {esc(row.get('content')) or '—'}")
        elif content_type in ("photo", "pdf"):
            lines.append("Содержимое: см. файл ниже")
    elif not parts:
        lines.append("Содержимое: —")
    else:
        lines.append("Содержимое:")
        for part in parts:
            caption = esc(part.get("caption"))
            if part.get("kind") in ("text", "link"):
                # CR-01 «Важно 1»: truncate the RAW content, escape after -- slicing an
                # already-escaped string can split an HTML entity in half (&amp; -> &am) and
                # reproduce the exact parse_mode="HTML" failure this truncation defends against.
                raw = str(part.get("content") or "")
                if len(raw) > _CARD_PART_MAX:
                    raw = raw[:_CARD_PART_MAX] + "…"
                lines.append(f"• {esc(raw) or '—'}")
            else:
                # T-09-12/backward-compat: same "см. файл ниже" wording the pre-09.1 single-
                # content_type render used (tests/test_gamification_review_phase9.py asserts
                # this literal string).
                tail = f" ({caption})" if caption else ""
                lines.append(f"• см. файл ниже{tail}")
    # Phase 14 (14-03, GAME-08): task_archived_at is exposed on the queue rows by plan 14-01.
    # The submission itself is untouched by the archive — the manager still has to decide it.
    if row.get("task_archived_at"):
        lines.append("🗄 Задание в архиве — сдачу всё равно нужно решить")
    # Phase 14 (14-03, GAME-10): resolved once by the caller (limit==0/None -> attempt stays
    # None -> this line never appears, byte-identical to pre-phase behavior).
    if attempt is not None:
        k, n = attempt
        lines.append(f"🔁 Попытка {k} из {n}")
    # A-05 (созвон 13.08): дедлайн мягкий, бот сдачу принял — единственный ограничитель здесь
    # человек. Просрочка нигде не хранится, только вычисляется здесь при каждом рендере.
    submitted_at = row.get("submitted_at")
    deadline_at = row.get("task_deadline_at")
    if submitted_at and deadline_at and str(submitted_at) > str(deadline_at):
        lines.append(f"⏰ Сдано после дедлайна ({deadline_at}) — решение за вами")
    return "\n".join(lines)


def _submission_card_kb(submission_id: int, coins: int) -> InlineKeyboardMarkup:
    """Phase 16 (16-04, скетч Экран 5): «✅ Принять» без суммы в подписи — сумма уже читается
    строкой «Предложено: N🪙» на карточке, а при «✏️ Другая сумма» подпись не расходится с
    начисленным. `coins` оставлен в сигнатуре (публичная форма, тесты передают его), но этой
    кнопкой больше не отображается."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"grev_approve:{submission_id}"),
            InlineKeyboardButton(text="✏️ Другая сумма", callback_data=f"grev_approve_custom:{submission_id}"),
        ],
        [
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"grev_reject:{submission_id}"),
            InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"grev_skip:{submission_id}"),
        ],
    ])


# ── Экран 8: «🪙 Монеты вручную» ────────────────────────────────────────────────────────────

def _coinsman_person_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Начислить", callback_data="coinsman_sign:plus")],
        [InlineKeyboardButton(text="➖ Списать", callback_data="coinsman_sign:minus")],
        [InlineKeyboardButton(text="← Отмена", callback_data="coinsman_cancel")],
    ])


def _coinsman_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="coinsman_confirm")],
        [InlineKeyboardButton(text="← Отмена", callback_data="coinsman_cancel")],
    ])


def _parse_amount_presets(raw: str | None) -> list[int]:
    """`coins_manual_amount_presets` («5,10,20») -> [5, 10, 20]. Fail-soft (T-16-04-03): всё,
    что не положительное целое, молча пропускается — менеджер, испортивший настройку, видит
    меньше кнопок (или ни одной), свободный ввод суммы остаётся в любом случае."""
    presets: list[int] = []
    for piece in str(raw or "").split(","):
        piece = piece.strip()
        if piece.isdigit() and int(piece) > 0 and int(piece) not in presets:
            presets.append(int(piece))
    return presets


async def _coinsman_amount_kb(sign: str) -> InlineKeyboardMarkup:
    """Quick-pick сумм на шаге CoinsManual.amount: одна строка кнопок «+5 +10 +20» (или
    «-5 -10 -20» при списании), callback `coinsman_amount:<N>` — знак берётся из уже выбранного
    `cm_sign`, не из callback_data."""
    presets = _parse_amount_presets(await get_setting_typed("coins_manual_amount_presets"))
    prefix = "+" if sign == "plus" else "-"
    row = [InlineKeyboardButton(text=f"{prefix}{p}", callback_data=f"coinsman_amount:{p}") for p in presets]
    return InlineKeyboardMarkup(inline_keyboard=[row] if row else [])


def _render_coinsman_confirm_card(recipient_name: str, delta: int, reason: str, *,
                                  balance_before: int | None = None,
                                  city_label_text: str | None = None) -> str:
    """T-14-19: `reason` is a human-typed free text that will also be shown to the delegate
    with parse_mode="HTML" (_notify_manual_coins) -- escaped here too, not just once at the
    eventual delegate render.

    Phase 16 (16-04, скетч Экран 8): с `balance_before` строка суммы становится переходом
    «Баланс сейчас: 120🪙 → станет: 125🪙 (+5)» — менеджер видит итог ДО нажатия; с
    `city_label_text` к строке «Кому:» добавляется « · 🏙 Город». Оба None (дефолт) — карточка
    байт-в-байт прежняя (tests/test_coins_manual_260818.py)."""
    recipient = f"Кому: {recipient_name}"
    if city_label_text is not None:
        recipient += f" · 🏙 {html_module.escape(city_label_text)}"
    if balance_before is None:
        amount_line = f"Сумма: {delta:+d} монет(ы)"
    else:
        amount_line = f"Баланс сейчас: {balance_before}🪙 → станет: {balance_before + delta}🪙 ({delta:+d})"
    return (
        "🪙 <b>Подтвердите операцию</b>\n\n"
        f"{recipient}\n"
        f"{amount_line}\n"
        f"Причина: {html_module.escape(reason)}\n\n"
        "Делегат получит сообщение с суммой, причиной и новым балансом."
    )


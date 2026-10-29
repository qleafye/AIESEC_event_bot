"""Анкета из нескольких вопросов: чистая логика без БД и без Telegram-бота.

Типы вопросов (один вариант / несколько / шкала 1–5 / свободный ответ / таблица), лимиты,
валидаторы с понятными ошибками, рендер вопроса делегату (текст + inline-кнопки), переходы
цепочки и превью анкеты для менеджера. Нативные опросы Telegram не используются — только кнопки.

Вопрос приходит dict-ом в форме, которую отдаёт слой БД; прогресс draft: multi -> {"sel": [...]},
table -> {"row": k}. Все callback_data делегата начинаются с «sv:» и умещаются в 64 байта.
"""
from __future__ import annotations

import html as html_module

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

SURVEY_MAX_QUESTIONS = 15
QUESTION_MAX = 300
OPTION_MAX = 90
OPTIONS_MIN = 2
OPTIONS_MAX = 10
ROWS_MAX = 20
ANSWER_MAX = 1000
LABEL_MAX = 60
BUTTON_LABEL_MAX = 28
DEFAULT_NA_LABEL = "Не могу оценить"
DEFAULT_OUTRO = "Спасибо за ответы! 🧡"
SURVEY_LINK_BUTTON = "🔗 Ссылка и QR"
_START_PREFIX = "survey_"

TG_MESSAGE_MAX = 4096


def survey_start_payload(poll_id: int) -> str | None:
    if isinstance(poll_id, bool) or not isinstance(poll_id, int) or poll_id < 1:
        return None
    return f"{_START_PREFIX}{poll_id}"


def survey_link(bot_username: str | None, poll_id: int) -> str | None:
    """Ссылка t.me, которая открывает именно этот опрос. Нет имени бота — None:
    вызывающий говорит менеджеру, что сделать."""
    name = (bot_username or "").strip().lstrip("@")
    payload = survey_start_payload(poll_id)
    if not name or payload is None:
        return None
    return f"https://t.me/{name}?start={payload}"


def parse_survey_start(args: str | None) -> int | None:
    """Аргумент /start → id опроса. «survey_12» → 12. quiz, sessions, walkin и прочий текст → None."""
    raw = (args or "").strip()
    if not raw.startswith(_START_PREFIX):
        return None
    rest = raw[len(_START_PREFIX):]
    if not rest.isdigit() or rest != str(int(rest)):
        return None
    poll_id = int(rest)
    if poll_id < 1:
        return None
    return poll_id


QTYPE_LABELS: dict[str, str] = {
    "single": "Один вариант",
    "multi": "Несколько вариантов",
    "scale": "Шкала 1–5",
    "text": "Свободный ответ",
    "table": "Таблица",
}


def _esc(value: str | None) -> str:
    return html_module.escape(value or "")


# ---------------------------------------------------------------- валидаторы

def validate_question_text(text: str | None) -> str | None:
    value = (text or "").strip()
    if not value:
        return "Пришли текст вопроса одним сообщением, например: «Как тебе программа?»."
    if len(value) > QUESTION_MAX:
        return (f"Вопрос длиннее {QUESTION_MAX} символов (сейчас {len(value)}). "
                "Сократи и пришли ещё раз.")
    return None


def validate_option(text: str) -> str | None:
    value = (text or "").strip()
    if not value:
        return "Вариант не может быть пустым. Пришли текст варианта, например: «Да»."
    if len(value) > OPTION_MAX:
        return (f"Вариант длиннее {OPTION_MAX} символов (сейчас {len(value)}). "
                "Сократи и пришли ещё раз.")
    return None


def validate_options(opts: list[str]) -> str | None:
    if len(opts) < OPTIONS_MIN:
        return (f"Нужно минимум {OPTIONS_MIN} варианта, сейчас {len(opts)}. "
                "Пришли варианты, каждый с новой строки.")
    if len(opts) > OPTIONS_MAX:
        return (f"Вариантов может быть не больше {OPTIONS_MAX}, сейчас {len(opts)}. "
                "Убери лишние и пришли список ещё раз.")
    for opt in opts:
        err = validate_option(opt)
        if err:
            return err
    return None


def validate_rows(rows: list[str]) -> str | None:
    if not rows:
        return "Пришли хотя бы одну строку таблицы, каждую с новой строки, например: «Еда»."
    if len(rows) > ROWS_MAX:
        return (f"Строк может быть не больше {ROWS_MAX}, сейчас {len(rows)}. "
                "Убери лишние и пришли список ещё раз.")
    for row in rows:
        value = (row or "").strip()
        if not value:
            return "Строка таблицы не может быть пустой. Пришли список ещё раз."
        if len(value) > OPTION_MAX:
            return (f"Строка длиннее {OPTION_MAX} символов (сейчас {len(value)}). "
                    "Сократи и пришли список ещё раз.")
    return None


def validate_label(text: str | None) -> str | None:
    value = (text or "").strip()
    if len(value) > LABEL_MAX:
        return (f"Подпись длиннее {LABEL_MAX} символов (сейчас {len(value)}). "
                "Сократи и пришли ещё раз.")
    return None


def validate_answer_text(text: str | None) -> str | None:
    value = (text or "").strip()
    if not value:
        return f"Пришли ответ текстом одним сообщением (до {ANSWER_MAX} символов)."
    if len(value) > ANSWER_MAX:
        return (f"Ответ длиннее {ANSWER_MAX} символов (сейчас {len(value)}). "
                "Сократи и пришли ещё раз.")
    return None


# ---------------------------------------------------------------- рендер

def options_need_numbers(labels: list[str]) -> bool:
    return any(len(label) > BUTTON_LABEL_MAX for label in labels)


def _rows_of(buttons: list[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _skip_row(qid: int) -> list[InlineKeyboardButton]:
    return [_btn("Пропустить", f"sv:k:{qid}")]


def _options_block(question: dict, selected: list[int], *, multi: bool):
    """Текстовая часть (нумерованный список или '') и ряды кнопок вариантов."""
    qid = question["id"]
    labels = [o["t"] for o in question.get("options") or []]
    numbered = options_need_numbers(labels)
    lines = [f"{i + 1}. {_esc(t)}" for i, t in enumerate(labels)] if numbered else []
    buttons = []
    for i, label in enumerate(labels):
        caption = str(i + 1) if numbered else label
        if multi and i in selected:
            caption = f"✓ {caption}"
        buttons.append(_btn(caption, f"sv:o:{qid}:{i}"))
    rows = _rows_of(buttons, 5) if numbered else [[b] for b in buttons]
    return "\n".join(lines), rows


def render_question(question: dict, *, number: int, total: int,
                    draft: dict | None = None) -> tuple[str, InlineKeyboardMarkup | None]:
    qid = question["id"]
    qtype = question["qtype"]
    required = bool(question.get("required"))
    head = f"<b>Вопрос {number} из {total}</b>\n\n{_esc(question['text'])}"
    rows: list[list[InlineKeyboardButton]] = []
    text = head

    if qtype in ("single", "multi"):
        multi = qtype == "multi"
        sel = list((draft or {}).get("sel") or [])
        numbered_text, rows = _options_block(question, sel, multi=multi)
        if multi:
            limit = question.get("max_choices")
            text += f"\n\nМожно выбрать до {limit}." if limit else "\n\nМожно выбрать несколько вариантов."
            rows.append([_btn("Готово", f"sv:d:{qid}")])
        if numbered_text:
            text += "\n\n" + numbered_text
    elif qtype == "scale":
        lo, hi = question.get("scale_lo"), question.get("scale_hi")
        if lo or hi:
            text += f"\n\n1 — {_esc(lo or '')}\n5 — {_esc(hi or '')}"
        rows = [[_btn(str(i), f"sv:n:{qid}:{i}") for i in range(1, 6)]]
    elif qtype == "table":
        items = question.get("rows") or []
        idx = int((draft or {}).get("row") or 0)
        idx = max(0, min(idx, max(len(items) - 1, 0)))
        current = items[idx] if items else ""
        text += f"\n\nСтрока {idx + 1} из {len(items)}: <b>{_esc(current)}</b>\nОцени от 1 до 5."
        rows = [[_btn(str(i), f"sv:t:{qid}:{i}") for i in range(1, 6)],
                [_btn(question.get("na_label") or DEFAULT_NA_LABEL, f"sv:t:{qid}:0")]]
    else:  # text
        text += "\n\nНапиши ответ одним сообщением."

    if not required:
        rows.append(_skip_row(qid))
    return text, (InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)


def render_more_prompt(question: dict, option_index: int) -> tuple[str, InlineKeyboardMarkup | None]:
    option = question["options"][option_index]
    prompt = option.get("more") or ""
    text = (f"Ты выбрал(а): <b>{_esc(option['t'])}</b>\n\n{_esc(prompt)}\n\n"
            "Напиши ответ одним сообщением.")
    return text, InlineKeyboardMarkup(inline_keyboard=[_skip_row(question["id"])])


# ---------------------------------------------------------------- логика цепочки

def toggle_multi(selected: list[int], index: int, max_choices: int | None) -> tuple[list[int], str | None]:
    current = list(selected)
    if index in current:
        current.remove(index)
        return current, None
    if max_choices and len(current) >= max_choices:
        return current, f"Можно выбрать не больше {max_choices} — сними лишний вариант, чтобы выбрать другой."
    current.append(index)
    return current, None


def more_queue(question: dict, selected: list[int]) -> list[int]:
    chosen = set(selected)
    return [i for i, opt in enumerate(question.get("options") or [])
            if i in chosen and (opt.get("more") or "").strip()]


def _ordered(questions: list[dict]) -> list[dict]:
    return sorted(questions, key=lambda q: q.get("position", 0))


def next_question_id(questions: list[dict], current_qid: int) -> int | None:
    ordered = _ordered(questions)
    for i, q in enumerate(ordered):
        if q["id"] == current_qid:
            return ordered[i + 1]["id"] if i + 1 < len(ordered) else None
    return None


def question_number(questions: list[dict], qid: int) -> int:
    for i, q in enumerate(_ordered(questions), start=1):
        if q["id"] == qid:
            return i
    return 0


def invite_kb(poll_id: int, *, started: bool = False) -> InlineKeyboardMarkup:
    if started:
        button = _btn("▶️ Продолжить опрос", f"sv:c:{poll_id}")
    else:
        button = _btn("📝 Пройти опрос", f"sv:s:{poll_id}")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


# ---------------------------------------------------------------- превью для менеджера

def _preview_question(q: dict, k: int) -> str:
    qtype = q["qtype"]
    mark = "" if q.get("required") else " (необязательный)"
    lines = [f"<b>{k}. [{QTYPE_LABELS.get(qtype, qtype)}]</b> {_esc(q['text'])}{mark}"]
    if qtype == "multi" and q.get("max_choices"):
        lines.append(f"до {q['max_choices']}")
    for opt in q.get("options") or []:
        lines.append(f"• {_esc(opt['t'])}")
        if (opt.get("more") or "").strip():
            lines.append(f"   ↳ уточнение: {_esc(opt['more'])}")
    if qtype == "scale":
        lines.append(f"1 — {_esc(q.get('scale_lo') or '')}; 5 — {_esc(q.get('scale_hi') or '')}")
    if qtype == "table":
        for row in q.get("rows") or []:
            lines.append(f"• {_esc(row)}")
        lines.append(f"({_esc(q.get('na_label') or DEFAULT_NA_LABEL)})")
    return "\n".join(lines)


def _hard_split(block: str, limit: int) -> list[str]:
    parts: list[str] = []
    while len(block) > limit:
        cut = block.rfind("\n", 0, limit)
        cut = cut if cut > 0 else limit
        parts.append(block[:cut])
        block = block[cut:].lstrip("\n")
    if block:
        parts.append(block)
    return parts


def render_survey_preview(poll: dict, questions: list[dict]) -> list[str]:
    head = [f"<b>{_esc(poll.get('title'))}</b>"]
    intro = poll.get("intro_text") or poll.get("intro")
    outro = poll.get("outro_text") or poll.get("outro")
    if intro:
        head.append(f"Вступление: {_esc(intro)}")
    head.append(f"Финальный текст: {_esc(outro) if outro else _esc(DEFAULT_OUTRO)}")
    blocks = ["\n".join(head)]
    blocks += [_preview_question(q, k) for k, q in enumerate(_ordered(questions), start=1)]

    blocks = [part for b in blocks for part in _hard_split(b, TG_MESSAGE_MAX)]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= TG_MESSAGE_MAX:
            current = candidate
        else:
            chunks.append(current)
            current = block
    if current:
        chunks.append(current)
    return chunks

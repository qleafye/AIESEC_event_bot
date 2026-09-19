"""Мастер первой настройки события (квик 260915-4mu, решения владельца — см. план).

Тот же слой, что `settings_ops.py`: чистые структуры данных + чистые функции, БЕЗ FastAPI и
БЕЗ aiogram — весь async (чтение bot_settings через `_item_for`) остаётся в роутере
(`miniapp/routers/settings.py`). Единственный потребитель этого модуля — тот роутер; сам
файл не знает о HTTP и не знает о БД.

`STEPS` — порядок шагов из шпаргалки (`docs/ADMIN_CHEATSHEET.md`, разделы «Первая настройка
события» и «Первая настройка СкиллАп»): менеджер ведётся по нему шаг за шагом, а не читает
десять пунктов сам. Ключи полей — уже существующие ключи `SETTINGS_SCHEMA`, мастер не
изобретает новых (кроме `setup_wizard_dismissed`, см. `settings_schema.py`).

`TEXTS` — подписи каркаса экрана (заголовок, «Шаг N из M», кнопки и т.п.): это интерфейсный
текст, не настройка мероприятия, поэтому НЕ уезжает в реестр `bot_settings` — двадцать
нередактируемых по смыслу строк засорили бы группу `miniapp_settings_*` (см. докстринг
плана 260915-4mu). Экран (`screens/setup.js`) не заводит ни одной русской строки сам —
только эти подписи и подписи полей из `settings/all`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WizardStep:
    key: str                              # токен шага (в БД не пишется, id для фронта)
    title: str                            # подпись для менеджера
    hint: str                             # что это и зачем — одно-два предложения
    kind: str                             # "fields" | "note" | "link"
    fields: tuple[str, ...] = ()          # ключи SETTINGS_SCHEMA (порядок = порядок на экране)
    done_rule: str = "all"                # "all" — заданы все поля, "any" — хотя бы одно
    link: tuple[str, str] | None = None   # (hash, подпись кнопки) для kind == "link"
    event_types: frozenset[str] | None = None  # None = для любого типа
    requires: str | None = None           # ключ-тумблер, который должен быть "on"


# Порядок — из шпаргалки; вставка СкиллАп (13–16) идёт после общих текстов и до таблицы
# (план 260915-4mu, комментарий под таблицей шагов: «допустимо поднять сразу после menu,
# если удобнее по шпаргалке — но группа идёт слитно и в этом же внутреннем порядке»).
STEPS: tuple[WizardStep, ...] = (
    WizardStep(
        key="event_type",
        title="🎭 Тип события",
        hint="Первый шаг — от него зависит состав всех дальнейших шагов и пресет модулей.",
        kind="fields",
        fields=("event_type",),
    ),
    WizardStep(
        key="botfather",
        title="🤖 Оформление бота в @BotFather",
        hint="Аватар, описание и About бота настраиваются не здесь: @BotFather → /mybots → Edit Bot.",
        kind="note",
    ),
    WizardStep(
        key="event_info",
        title="🎪 Информация о событии",
        hint="Дата, время и площадка — то, что делегат видит в разделе «Информация о форуме».",
        kind="fields",
        fields=("event_name", "event_date", "event_time", "event_place_name", "event_place_address"),
    ),
    WizardStep(
        key="contacts",
        title="📞 Контакты оргкомитета",
        hint="Хотя бы один способ связаться с оргкомитетом — покажется делегату в разделе «Контакты».",
        kind="fields",
        fields=("contact_person", "contact_vk", "contact_tg"),
        done_rule="any",
    ),
    WizardStep(
        key="welcome",
        title="💬 Приветствие",
        hint="Текст и фото, которые делегат увидит при первом /start.",
        kind="fields",
        fields=("start_text", "start"),
    ),
    WizardStep(
        key="menu",
        title="📋 Главное меню",
        hint="Хотя бы один раздел меню — программа, спикеры, контакты, вопросы или рефералка.",
        kind="fields",
        fields=(
            "menu_info", "menu_program", "menu_speakers", "menu_contacts",
            "menu_question", "menu_faq", "menu_referral", "menu_invites",
        ),
        done_rule="any",
    ),
    WizardStep(
        key="reg_prompts",
        title="📝 Вопросы анкеты",
        hint="Состав вопросов регистрации и их тексты — свой раздел, мастер туда только ведёт.",
        kind="link",
        link=("#/settings/form", "Открыть раздел 📝 Анкета"),
    ),
    WizardStep(
        key="texts_after",
        title="✉️ Тексты после подачи заявки",
        hint="Что делегат увидит сразу после отправки анкеты, при одобрении и при отклонении.",
        kind="fields",
        fields=("reg_complete_text", "approve_text", "reject_text"),
    ),
    WizardStep(
        key="countdown",
        title="⏳ Дата отсчёта до форума",
        hint="От неё считается таймер обратного отсчёта на хабе делегата.",
        kind="fields",
        fields=("miniapp_hub_countdown_date",),
    ),
    WizardStep(
        key="cities",
        title="🏙 Города",
        hint="Список городов события — нужен для per-city настроек и деления делегатов.",
        kind="fields",
        fields=("city_options",),
        requires="event_city_enabled",
    ),
    WizardStep(
        key="consent",
        title="🧾 Согласия",
        hint="Список согласий и текст кнопки. PDF самих согласий грузятся в боте: /admin → 📝 Анкета → «🧾 PDF согласий».",
        kind="fields",
        fields=("consent_list", "consent_version", "consent_button_text"),
        requires="consent_enabled",
    ),
    WizardStep(
        key="payment",
        title="💳 Оплата",
        hint="Тарифы, реквизиты и дедлайн оплаты для делегата.",
        kind="fields",
        fields=("payment_options", "payment_requisites", "payment_deadline"),
        requires="payment_enabled",
    ),
    WizardStep(
        key="su_options",
        title="🎓 СкиллАп: направления и стек",
        hint="Списки направлений, стека, опыта, готовности и статуса образования анкеты СкиллАп.",
        kind="fields",
        fields=("stack_options", "experience_options", "readiness_options", "education_status_options"),
        event_types=frozenset({"skillup"}),
    ),
    WizardStep(
        key="su_score",
        title="🎓 СкиллАп: скоринг",
        hint="Хотя бы одно правило скоринга анкеты СкиллАп.",
        kind="fields",
        fields=(
            "score_it_fields", "score_senior_statuses", "score_readiness_counts",
            "score_experience_counts", "score_course_from", "score_stack_from",
        ),
        done_rule="any",
        event_types=frozenset({"skillup"}),
    ),
    WizardStep(
        key="su_resume",
        title="🎓 СкиллАп: резюме",
        hint="Разрешённые ссылки на резюме (whitelist доменов).",
        kind="fields",
        fields=("reg_resume_link_whitelist",),
        event_types=frozenset({"skillup"}),
    ),
    WizardStep(
        key="su_rebuild",
        title="🎓 СкиллАп: пересборка таблицы",
        hint="После первой настройки пересоберите таблицу под анкету СкиллАп: /admin → 📊 Данные → «♻️ Пересобрать таблицу».",
        kind="note",
        event_types=frozenset({"skillup"}),
    ),
    # Квик 260919-mlu: шаг был `kind="fields"` с полем `main_sheet_tab`. Имена вкладок больше
    # не правятся из приложения (settings_ops.EXCLUDED_KEYS) — перед записью надо спросить,
    # переименовать ли существующий лист, иначе строки остаются в брошенной вкладке, а этой
    # развилки в вебе нет. Шаг стал подсказкой со ссылкой на экран бота — ровно как соседние
    # `roles` и `su_rebuild`, которые тоже живут только в боте.
    WizardStep(
        key="sheet",
        title="📊 Вкладка таблицы",
        hint=(
            "Имя основной вкладки Google Sheets, куда пишутся заявки, задаётся в боте: "
            "/admin → 📊 Данные → «📄 Вкладки таблицы». Там же бот спросит, переименовать ли "
            "существующую вкладку вместе с данными."
        ),
        kind="note",
    ),
    WizardStep(
        key="roles",
        title="🔧 Роли и доступы команды",
        hint="Кто в команде что видит и может менять — свой раздел в боте: /admin → 🔧 Управление.",
        kind="note",
    ),
    WizardStep(
        key="theme",
        title="🎨 Оформление приложения",
        hint="Хотя бы один штрих — тема, лого или акцентный цвет.",
        kind="fields",
        fields=("miniapp_theme_preset", "miniapp_logo", "miniapp_accent"),
        done_rule="any",
    ),
)

# Подписи каркаса экрана (не bot_settings — см. докстринг файла). Плейсхолдеры {n}/{m}/
# {done}/{total} подставляет роутер/фронт по месту, JS ни одной строки не заводит сам.
TEXTS: dict[str, str] = {
    "title": "🚀 Первая настройка",
    "tile_label": "🚀 Первая настройка",
    "step_of_text": "Шаг {n} из {m}",
    "progress_note_text": "{done} из {total} готово",
    "next_text": "Дальше",
    "save_next_text": "Сохранить и дальше",
    "skip_text": "Пропустить",
    "back_text": "Назад",
    "done_text": "Готово",
    "all_steps_text": "Все шаги",
    "value_set_text": "задано",
    "value_default_text": "не задано",
    "open_section_text": "Открыть раздел",
    "hide_tile_text": "Скрыть плитку",
    "error_toast_text": "Не получилось сохранить — попробуйте ещё раз.",
}


def visible_steps(event_type: str | None, flags: dict[str, bool]) -> list[WizardStep]:
    """Шаги, видимые для текущего типа события и текущих тумблеров модулей — порядок
    исходного кортежа `STEPS` сохраняется, ничего не пересортировывается."""
    out: list[WizardStep] = []
    for step in STEPS:
        if step.event_types is not None and event_type not in step.event_types:
            continue
        if step.requires and not flags.get(step.requires):
            continue
        out.append(step)
    return out


def step_done(step: WizardStep, filled: dict[str, bool]) -> bool:
    """«Пройден» ли шаг: `note`/`link` — никогда (в прогресс не идут), `fields` — по
    `done_rule` ("all" — все поля шага заданы, "any" — хотя бы одно)."""
    if step.kind != "fields":
        return False
    if not step.fields:
        return False
    values = [bool(filled.get(key)) for key in step.fields]
    if step.done_rule == "any":
        return any(values)
    return all(values)

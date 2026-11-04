"""Литералы, которые видит делегат, — ключами реестра настроек, а не строками в коде.

Правило «0 хардкода»: менеджер правит любую фразу делегату сам, на экране бота, без разработчика.
До этой правки ответы экранов задач и сдачи, вопросов и FAQ, «важных рассылок», переноса заявки,
«Не пришёл», оплаты и загрузки чека, варианты анкеты и подписи развилки формата жили литералами в
`handlers/user_actions.py`, `handlers/payment.py`, `domain/regform/engine.py`, а тексты фоновых
напоминаний об оплате — в `services/scheduler.py` (`or (...)`).

Что сторожим:
- дефолт каждого нового ключа байт-в-байт равен прежнему литералу — у делегата с пустым реестром
  ничего не меняется;
- английский работает как раньше: дефолт находит ручной перевод (ярус A `UI_EN` или сид
  `FORM_DEFAULT_EN`, через ту же карту, что пишет `seed()`);
- каждый ключ стоит на экране своей группы в боте (иначе менеджер его не найдёт);
- свой текст менеджера доходит до делегата, пустой — возвращает дефолт;
- варианты анкеты без логики по значению правятся, с логикой — нет;
- AST-сторож: новая кнопка-литерал или полная фраза делегату в этих модулях роняет тест.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from config import config
from database import db
from domain.settings.delegate_text_fields import DELEGATE_TEXT_SCHEMA
from domain.settings.schema import SETTINGS_SCHEMA
from domain.settings.ui_text_fields import UI_TEXT_SCHEMA, ui_text
import domain.regform.engine as reg_engine
import domain.regform.options as reg_opts
from handlers.i18n import reg_i18n
from services.i18n import i18n
from services.i18n.i18n_form_manual import FORM_DEFAULT_EN
from tests._dbtpl import fast_init_db
from tests._paths import REPO_ROOT
from tests.test_i18n_literal_corpus_guard_260906 import _find_literal_calls

# Прежний литерал из кода -> ключ реестра. Дефолт ключа обязан совпасть байт-в-байт.
OLD_LITERALS: dict[str, str] = {
    # handlers/user_actions.py
    "not_registered_text": "Чтобы пользоваться ботом, сначала нужно зарегистрироваться. Отправь команду /start.",
    "returning_prev_season_fallback_text": "прошлом событии",
    "returning_update_hint_text": "Нажми /start, чтобы обновить анкету.",
    "game_submit_button_text": "📤 Сдать",
    "game_back_button_text": "◀️ Назад",
    "game_task_invalid_text": "Некорректное задание",
    "game_task_archived_text": "Это задание убрали в архив — сдать его больше нельзя. Загляни в «🎯 Задания», там актуальный список.",
    "game_task_archived_submit_text": "Это задание убрали в архив — сдать его больше нельзя. Загляни в «🎯 Мои задания», там актуальный список.",
    "game_task_not_visible_text": "Это задание сейчас тебе недоступно — загляни в «🎯 Задания».",
    "game_task_not_found_text": "Задание не найдено",
    "game_task_other_city_text": "Это задание для другого города",
    "game_submit_already_text": "Уже отправлено, ожидай проверки",
    "game_submit_limit_text": "Лимит попыток по этому заданию исчерпан ({limit}). Если считаешь, что это ошибка — напиши менеджеру через «❓ Задать вопрос».",
    "game_submit_deadline_passed_text": "⏰ Срок сдачи вышел. Отправить можно, но начислять баллы будет решать менеджер.",
    "action_cancelled_text": "Действие отменено.",
    "game_proof_unrecognized_text": "Не понял, пришли фото, документ, текст или ссылку.",
    "game_proof_overflow_text": "Больше {max_parts} частей в одну сдачу не влезет — нажми «✅ Готово», менеджер уже увидит присланное.",
    "game_task_gone_text": "Это задание больше не доступно.",
    "game_submit_race_text": "Уже отправлено — кто-то опередил на долю секунды. Обнови список заданий.",
    "game_proof_already_empty_text": "Уже пусто",
    "game_submit_cancelled_text": "Сдача отменена, части не сохранены.",
    "payment_not_owed_text": "Оплатили или оплата не требуется.",
    "faq_question_not_found_text": "Вопрос не найден.",
    "faq_question_gone_text": "Этот вопрос уже недоступен.",
    "faq_back_button_text": "← К вопросам",
    "ask_question_text_only_text": "Пожалуйста, отправь вопрос текстом.",
    "ask_question_send_failed_text": "Не удалось отправить вопрос, попробуйте позже.",
    "ask_question_no_recipients_text": "Администраторы не настроены.",
    "important_today_empty_text": "Сегодня важных рассылок не было.",
    "important_today_no_text_text": "[рассылка без текста]",
    "important_today_header_text": "❗ Важные рассылки за сегодня:",
    "wave_ended_note_text": "Волна закончилась — итоги готовятся.",
    "wave_rating_not_eligible_text": "Рейтинг волны виден только участникам текущей волны амбассадоров.",
    "ambassador_leave_confirm_yes_button_text": "Да, выйти",
    "ambassador_leave_confirm_no_button_text": "Отмена",
    "checkin_not_arrived_already_checked_text": "Ты уже отмечен(а) на входе в {time} — всё в порядке, хорошего дня!",
    "checkin_not_arrived_qr_button_text": "🎟 Мой QR",
    "regional_noshow_already_moved_text": "Уже перенесено.",
    "regional_noshow_wrong_city_text": "Заявка уже не в {city}.",
    "regional_noshow_confirm_question_text": "Перенести заявку в другой город: {target_city}? Анкету заново заполнять не нужно.",
    "regional_noshow_already_answered_text": "Предложение уже отвечено.",
    "regional_noshow_no_user_text": "Не нашли твою заявку — напиши организаторам.",
    "regional_noshow_unavailable_text": "Перенос сейчас недоступен — напиши организаторам.",
    "regional_noshow_failed_text": "Не получилось перенести заявку — напиши организаторам.",
    "regional_noshow_done_text": "Готово, твоя заявка теперь здесь: {target_city}. Даты и место — в меню.",
    "regional_noshow_rereview_text": "Заявку посмотрят ещё раз.",
    "regional_noshow_declined_text": "Хорошо, до встречи в следующий раз!",
    # handlers/payment.py
    "payment_requisites_label_text": "Реквизиты",
    "payment_pay_later_button_text": "⏭ Оплачу позже",
    "payment_option_invalid_text": "Некорректный вариант.",
    "payment_option_gone_text": "Вариант больше не доступен.",
    "payment_option_wrong_track_text": "Этот вариант недоступен для твоего трека.",
    "payment_deadline_label_text": "📅 Дедлайн",
    "payment_penalties_label_text": "Штрафы за отмену",
    "payment_receipt_bad_type_text": "❌ Принимается только PDF-документ. Для скриншота используй функцию отправки фото.",
    "payment_receipt_doc_too_large_text": "❌ Файл слишком большой (максимум 10 МБ). Пришли чек меньшего размера.",
    "payment_receipt_photo_too_large_text": "❌ Изображение слишком большое (максимум 10 МБ). Пришли чек меньшего размера.",
    "payment_receipt_rate_limited_text": "⏳ Слишком часто. Подожди пару секунд и попробуй снова.",
    "payment_receipt_invalid_text": "❌ Отправь чек оплаты (PDF-документ или фото).\nИли /start — вернуться в меню (загрузить чек можно будет позже).",
    # handlers/registration.py / domain/regform/options.py — развилка формата
    "party_track_full_label_text": "Полная регистрация",
    "party_track_overnight_label_text": "🎉 Гости с ночёвкой",
    "party_track_noovernight_label_text": "🎉 Гости без ночёвки",
    # services/scheduler.py — существующие ключи, у которых дефолт жил в коде
    "payment_reminder_text": "⏰ Напоминание об оплате участия!\n\nСрок оплаты истекает скоро. Загрузи чек оплаты через бота.",
    "payment_overdue_text": "⚠️ Срок оплаты участия истёк.\n\nЕсли ты ещё планируешь участвовать — загрузи чек через бота (кнопка «💳 Оплата» в меню) или свяжись с организатором.",
}
# Тексты всплывающих окон (callback.answer show_alert) — prompt предупреждает про 200 символов.
ALERT_KEYS = {
    "game_task_invalid_text", "game_task_archived_text", "game_task_archived_submit_text",
    "game_task_not_visible_text", "game_task_not_found_text", "game_task_other_city_text",
    "game_submit_already_text", "game_submit_limit_text", "game_proof_already_empty_text",
    "faq_question_not_found_text", "faq_question_gone_text", "wave_rating_not_eligible_text",
    "checkin_not_arrived_already_checked_text", "regional_noshow_already_moved_text",
    "regional_noshow_wrong_city_text", "regional_noshow_already_answered_text",
    "regional_noshow_no_user_text", "regional_noshow_unavailable_text",
    "payment_option_invalid_text", "payment_option_gone_text", "payment_option_wrong_track_text",
}
# Ключи, которые жили в реестре и раньше (поменялся только дефолт).
EXISTING_KEYS = {"payment_reminder_text", "payment_overdue_text"}

# Шаг анкеты -> константа-дефолт. Список вариантов без логики по значению — правится менеджером.
EDITABLE_OPTION_STEPS = {
    "department": reg_opts.DEPARTMENT_OPTIONS,
    "aiesec_role": reg_opts.AIESEC_ROLE_OPTIONS,
    "english_level": reg_opts.ENGLISH_LEVEL_OPTIONS,
    "housing": reg_opts.HOUSING_OPTIONS,
    "position": reg_opts.POSITION_OPTIONS,
    "local_committee": reg_opts.LOCAL_COMMITTEE_OPTIONS,
    "transport": reg_opts.TRANSPORT_OPTIONS,
}

# Варианты, на которых держится логика, остаются в коде: course — правило «Курс» edu-фильтра;
# arrival — «Заранее» включает вопрос о жилье; attendance_format/informal_day/work_status/
# case_optin — строгая проверка ответа (_MEMBERSHIP_STEPS); alumni_status — подсказки по
# значению; bed_sharing/needs_certificate/volunteer/ambassador — «Да»/«Нет» превращаются в признак.
LOGIC_OPTIONS_ALLOW = {
    "course", "arrival", "attendance_format", "informal_day", "work_status", "case_optin",
    "alumni_status", "bed_sharing", "needs_certificate", "volunteer", "ambassador",
}

# Кнопки-литералы, которые допустимы: их видит менеджер, а не делегат (итоги волны в scheduler.py).
BUTTON_ALLOW = {"🏁 Объявить итоги", "📋 Открыть сдачи на проверке"}

# Служебные слова составных строк («Страница 2 из 5», «новое · попытка 2 из 3», «до 01.09 ·
# остаток», «Волна …»): их перевод держит ярус A (UI_EN), а править отдельное слово менеджеру
# бессмысленно — смысл даёт только вся строка.
TIER_A_ALLOW = {"Страница", "из", "до", "попытка", "новое", "на проверке", "остаток", "Волна"}

BUTTON_SCANNED = [
    REPO_ROOT / "handlers" / "user_actions.py",
    REPO_ROOT / "handlers" / "payment.py",
    REPO_ROOT / "services" / "scheduler.py",
    REPO_ROOT / "services" / "forum" / "checkin_not_arrived.py",
]
TEXT_SCANNED = [REPO_ROOT / "handlers" / "user_actions.py", REPO_ROOT / "handlers" / "payment.py"]

# Карта src_hash -> en, как её пишет `services.i18n.i18n_form_manual.seed()` (без БД).
_TR_MAP = {i18n.src_hash(ru): en for ru, en in FORM_DEFAULT_EN.items()}

# Экран бота -> группы ключей (токен экрана в admin_settings.SETTINGS_GROUPS).
_SCREEN_BY_GROUP = {"event": "event", "game": "game", "reg": "apps", "pay": "pay", "party": "party"}


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "delegate_literals.db")
    fast_init_db()


def test_defaults_equal_old_literals():
    bad = {k: SETTINGS_SCHEMA[k].get("default") for k, v in OLD_LITERALS.items()
           if k not in SETTINGS_SCHEMA or SETTINGS_SCHEMA[k].get("default") != v}
    assert not bad, f"дефолт не равен прежнему литералу: {bad}"
    text_keys = {k for k, e in DELEGATE_TEXT_SCHEMA.items() if e["type"] == "text"}
    assert text_keys == set(OLD_LITERALS) - EXISTING_KEYS


def test_new_keys_not_shadowing():
    assert not set(DELEGATE_TEXT_SCHEMA) & set(UI_TEXT_SCHEMA)
    for key, entry in DELEGATE_TEXT_SCHEMA.items():
        assert SETTINGS_SCHEMA[key] is entry, f"{key} перезаписан другой записью реестра"


def test_english_by_manual_layer():
    for key in OLD_LITERALS:
        default = SETTINGS_SCHEMA[key]["default"]
        assert reg_i18n.tr_text(default, "en", _TR_MAP) != default, f"{key}: нет ручного перевода"


def test_prompts_human_and_free_of_key_codes():
    for key, entry in DELEGATE_TEXT_SCHEMA.items():
        assert entry["label"] and "_" not in entry["label"], key
        assert key not in entry["prompt"], key
        if entry["type"] == "text":
            assert entry["default"] in entry["prompt"], f"{key}: prompt не показывает текст по умолчанию"
    for key in ALERT_KEYS:
        assert "200 символов" in SETTINGS_SCHEMA[key]["prompt"], key
    for key in EXISTING_KEYS:
        assert "Если очистить поле, вернётся текст по умолчанию" in SETTINGS_SCHEMA[key]["prompt"]


def test_keys_on_bot_screens():
    from handlers.settings.admin_settings import _settings_group_keys
    from domain.settings.amb_fields import AMB_FIELD_ORDER

    for key, entry in DELEGATE_TEXT_SCHEMA.items():
        if entry["type"] == "list":
            assert key in _settings_group_keys("reg"), f"{key} не на экране «📝 Регистрация»"
        elif entry["group"] == "amb":
            assert key in AMB_FIELD_ORDER, key
        else:
            screen = _SCREEN_BY_GROUP[entry["group"]]
            assert key in _settings_group_keys(screen), f"{key} не на экране {screen}"


def test_option_lists_default_and_custom(tmp_path):
    _ready(tmp_path)
    for step, const in EDITABLE_OPTION_STEPS.items():
        assert SETTINGS_SCHEMA[f"{step}_options"]["default"] is None
        assert asyncio.run(reg_engine.options(step)) == list(const), step
    asyncio.run(db.set_setting("housing_options", "Хостел\nДома"))
    assert asyncio.run(reg_engine.options("housing")) == ["Хостел", "Дома"]
    for step in LOGIC_OPTIONS_ALLOW:
        assert f"{step}_options" not in SETTINGS_SCHEMA, f"{step}: на этом списке держится логика"


def test_chat_keyboard_follows_options(tmp_path):
    _ready(tmp_path)
    from keyboards.builders import get_housing_kb

    asyncio.run(db.set_setting("housing_options", "Хостел\nДома"))
    kb = get_housing_kb(asyncio.run(reg_engine.options("housing")))
    texts = [b.text for row in kb.keyboard for b in row]
    assert "Хостел" in texts and "Дома" in texts
    default_texts = [b.text for row in get_housing_kb().keyboard for b in row]
    assert all(o in default_texts for o in reg_opts.HOUSING_OPTIONS)


def test_party_labels_follow_settings(tmp_path):
    _ready(tmp_path)
    from handlers import registration

    before = asyncio.run(reg_engine.party_track_options())
    assert before == [{"code": c, "label": lbl} for c, lbl in reg_opts.PARTY_TRACK_OPTIONS]
    asyncio.run(db.set_setting("party_track_overnight_label_text", "🎉 С ночёвкой"))
    after = {o["code"]: o["label"] for o in asyncio.run(reg_engine.party_track_options())}
    assert after["party_overnight"] == "🎉 С ночёвкой"
    rows = registration._party_fork_kb().inline_keyboard
    assert [r[0].text for r in rows] == [lbl for _, lbl in reg_opts.PARTY_TRACK_OPTIONS]
    assert all(r[0].callback_data.startswith("party_pick:") for r in rows)
    custom = registration._party_fork_kb(after).inline_keyboard
    assert custom[1][0].text == "🎉 С ночёвкой"


def test_custom_values_reach_delegate(tmp_path):
    _ready(tmp_path)
    from handlers import payment, user_actions

    assert payment._pay_later_btn("ru", {}, label="⏭ Позже").text == "⏭ Позже"
    btn = payment._pay_later_btn("ru", {})
    assert btn.text == "⏭ Оплачу позже" and btn.callback_data == "pay_later"
    assert "📋 Банк:" in payment._format_requisites_block("x", "ru", {}, label="Банк")
    assert "📋 Реквизиты:" in payment._format_requisites_block("x", "ru", {})
    kb = user_actions._game_task_card_kb(1, True, "ru", {}, labels=("📤 Отправить", "◀️ К списку"))
    assert [r[0].text for r in kb.inline_keyboard] == ["📤 Отправить", "◀️ К списку"]
    kb = user_actions._game_task_card_kb(1, True, "ru", {})
    assert [r[0].text for r in kb.inline_keyboard] == ["📤 Сдать", "◀️ Назад"]
    asyncio.run(db.set_setting("game_submit_already_text", "  "))
    assert asyncio.run(ui_text("game_submit_already_text")) == "Уже отправлено, ожидай проверки"
    asyncio.run(db.set_setting("payment_reminder_text", ""))
    assert asyncio.run(ui_text("payment_reminder_text")) == OLD_LITERALS["payment_reminder_text"]


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_no_delegate_button_literals():
    bad = []
    for path in BUTTON_SCANNED:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _call_name(node) in ("InlineKeyboardButton", "KeyboardButton")):
                continue
            for kw in node.keywords:
                value = kw.value
                if kw.arg == "text" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    if any(ch.isalpha() for ch in value.value) and value.value not in BUTTON_ALLOW:
                        bad.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {value.value!r}")
    assert not bad, (
        "Подпись кнопки делегату литералом — вынеси в ключ реестра "
        "(domain/settings/delegate_text_fields.py):\n  " + "\n  ".join(bad)
    )


def test_no_delegate_text_literals():
    bad = []
    for path in TEXT_SCANNED:
        for func, lineno, text in _find_literal_calls(path):
            if text not in TIER_A_ALLOW:
                bad.append(f"{path.relative_to(REPO_ROOT)}:{lineno} reg_i18n.{func}({text!r})")
    assert not bad, (
        "Фраза делегату литералом — вынеси в ключ реестра "
        "(domain/settings/delegate_text_fields.py):\n  " + "\n  ".join(bad)
    )


def test_scheduler_has_no_payment_reminder_literals():
    src = (REPO_ROOT / "services" / "scheduler.py").read_text(encoding="utf-8")
    assert "Напоминание об оплате участия" not in src
    assert "Срок оплаты участия истёк" not in src
    assert "_PAY_LATER_BTN" not in Path(REPO_ROOT / "handlers" / "payment.py").read_text(encoding="utf-8")

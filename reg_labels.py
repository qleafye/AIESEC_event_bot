"""Подписи анкеты — корневой модуль без aiogram (Phase 19, Mini App).

Перенос из `handlers/reg_schema.py` (значения байт-в-байт, не копия: `reg_schema`
реэкспортирует ЭТИ объекты, тест `tests/test_miniapp_labels_drift.py` сверяет `is`).
Причина выноса: пакетный `handlers/__init__.py` при импорте любого `handlers.x` тянет
`registration, user_actions, admin, payment`, то есть aiogram, — а веб-процесс Mini App
обязан оставаться aiogram-free (D-01). Соседи по корню — `settings_schema.py`, `cities.py`.

Здесь только литеральные словари — никакой логики, никаких импортов проекта.
"""

# Подписи вопросов анкеты: ключ реестра reg_q_* -> человеческая подпись (с эмодзи).
REG_LABELS = {
    "reg_q_age": "\U0001f382 Возраст",
    "reg_q_vk": "\U0001f535 ВК",
    "reg_q_email": "\U0001f4e7 Email",
    "reg_q_phone": "\U0001f4f1 Телефон",
    "reg_q_city": "\U0001f3d9 Город",
    "reg_q_source": "\U0001f4e2 Источник",
    "reg_q_lc": "\U0001f3e2 Лок. комитет",
    "reg_q_position": "\U0001f454 Позиция",
    "reg_q_education": "\U0001f393 Образование",
    "reg_q_university": "\U0001f3eb ВУЗ",
    "reg_q_course": "\U0001f4d6 Курс",
    "reg_q_specialty": "\U0001f4dd Специальность",
    "reg_q_work": "\U0001f4bc Работа",
    "reg_q_work_sphere": "\U0001f3ed Сфера работы",
    "reg_q_skills": "\U0001f4a1 Навыки",
    "reg_q_expectations": "\U0001f4ac Ожидания (общие)",
    "reg_q_informal_day": "\U0001f3d5 Неформальный день",
    "reg_q_attendance": "\U0001f4cd Формат",
    "reg_q_comments": "\U0001f4ac Доп. комментарии",
    "reg_q_department": "🏢 Департамент",
    "reg_q_aiesec_role": "🎖 Позиция AIESEC",
    "reg_q_certificate": "📄 Справка в ВУЗ",
    "reg_q_alumni_status": "🎓 Аламни/айсекер",
    "reg_q_english": "🇬🇧 Англ. язык",
    "reg_q_allergies": "🤧 Аллергии",
    "reg_q_food": "🥗 Питание",
    "reg_q_arrival": "🚌 Приезд",
    "reg_q_housing": "🏠 Проживание",
    "reg_q_bed_sharing": "🛏 Общая кровать",
    "reg_q_bed_partner": "🛏 Сосед по кровати",
    "reg_q_transport": "🚗 Трансфер",
    "reg_q_payment_date": "💳 Дата оплаты",
    "reg_q_cc_shop": "🛍 CC-shop",
    "reg_q_exp_organizers": "💬 Ожидания: организация",
    "reg_q_exp_content": "💬 Ожидания: контент",
    "reg_q_volunteer": "🙋 Волонтёр",
    "reg_q_arrival_date": "📅 Дата приезда",
    "reg_q_birth_date": "🎂 Дата рождения",
    "reg_q_study_field": "🎯 Направление обучения",
    "reg_q_goal": "🎯 Цель участия",
    "reg_q_formats": "📋 Форматы форума",
    "reg_q_ambassador": "🧡 Амбассадор",
    "reg_q_resume": "\U0001f4c4 Резюме",
}

# Статус заявки делегата (модерация) -> подпись. Значения совпадают со
# списком значений выпадашки в services.sheets.STATUS_LABELS.
STATUS_LABELS = {"pending": "Новая", "approved": "Одобрена", "rejected": "Отклонена"}

# Статус оплаты (users.payment_status) -> подпись. Единственный источник: рассылки
# (`handlers/admin_broadcasts.py`) и профиль Mini App читают отсюда.
PAYMENT_STATUS_LABELS = {
    "not_paid": "Не оплатил", "overdue": "Просрочил",
    "receipt_sent": "Чек на проверке", "paid": "Оплатил",
}

__all__ = ["REG_LABELS", "STATUS_LABELS", "PAYMENT_STATUS_LABELS"]

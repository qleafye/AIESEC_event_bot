"""Phase 21 (21-01, FORM-SYNC-01): литеральные списки вариантов ответа анкеты — корневой
модуль без зависимости на бот-фреймворк, сосед `settings_schema.py`/`cities.py`/`reg_labels.py`.

Перенос из `keyboards/builders.py` (списки, живущие внутри `*_kb`-функций) и из
`handlers/registration.py::_ask_step` (списки, встроенные прямо в `_reply_kb([...])` для
шагов, у которых нет отдельной функции-клавиатуры в builders.py: alumni_status, bed_sharing,
transport, ambassador). Состав — дословно из текущего кода (перенос, не переписывание);
служебные слова «Пропустить»/«Другое»/«Отмена» сюда НЕ входят — они добавляются флагами
add_skip/add_other у клавиатуры бота (`keyboards/builders.py`) и станут флагами
skip_allowed/other_allowed у `reg_engine.step_spec()` (Pitfall 10, RESEARCH).

Здесь только литералы — никакой логики, никаких импортов бот-фреймворка и проекта.
"""

# Развилка формата участия (pre-flow, не шаг REG_FLOW): (код трека, подпись кнопки).
# Единственный источник подписей развилки для бота (`handlers/registration.py::_party_fork_kb`)
# и веба (`reg_engine.party_track_options`) — перенос литералов из `_party_fork_kb` байт-в-байт.
# Перевод подписей в реестр настроек (группа «🎉 Party») — follow-up, здесь только перенос.
PARTY_TRACK_OPTIONS = [
    ("full", "Полная регистрация"),
    ("party_overnight", "\U0001f389 Гости с ночёвкой"),
    ("party_noovernight", "\U0001f389 Гости без ночёвки"),
]

DEFAULT_SOURCE_OPTIONS = [
    "Соцсети Юлид",
    "Соцсети АЙСЕК",
    "Университетские каналы",
    "Рассказал друг/знакомый",
    "Узнал от амбассадора",
    "Узнал от блогера",  # 2026-08-17: Instagram bloggers without direct reg link — track this channel
    "Другое",
]

EDUCATION_STATUS_OPTIONS = [
    "Да, в ВУЗе или колледже",
    "Нет, завершил(а) обучение",
    "Нет, не получал(а) образование",
]

COURSE_OPTIONS = ["1", "2", "3", "4", "5+", "Магистратура/Аспирантура"]

DEPARTMENT_OPTIONS = ["OGV", "OGT", "MKT", "F&L", "BD", "LCP", "EwA"]

AIESEC_ROLE_OPTIONS = ["Member", "TL", "Manager", "VP", "LCP", "Coordinator"]

ENGLISH_LEVEL_OPTIONS = ["Начальный", "Средний", "Продвинутый", "Свободный"]

ARRIVAL_OPTIONS = ["В дни конфы", "Заранее", "После"]

HOUSING_OPTIONS = ["Хост", "Сам(а)", "Не нужно"]

POSITION_OPTIONS = ["Айсекер", "Аламни", "Друг АЙСЕК"]

ATTENDANCE_FORMAT_OPTIONS = ["Offline", "Online"]

INFORMAL_DAY_OPTIONS = ["Да", "Нет", "Буду только в онлайне"]

LOCAL_COMMITTEE_OPTIONS = ["EG", "SPUEF", "Moscow", "Tyumen", "Ufa", "Ekaterinburg"]

# Ниже — списки, сегодня встроенные прямо в handlers/registration.py::_ask_step (не в
# builders.py); своей функции-клавиатуры у них нет. keyboards/builders.py их пока не
# импортирует (реальную развязку делает план 21-01 Task 3 вместе с переносом _ask_step в
# reg_engine.step_spec()) — здесь они уже определены как единая точка правды.
TRANSPORT_OPTIONS = ["Трансфер до площадки", "Самостоятельно"]

ALUMNI_STATUS_OPTIONS = ["Аламни", "Айсекер", "Ни то, ни другое"]

BED_SHARING_OPTIONS = ["Да", "Нет"]

AMBASSADOR_OPTIONS = ["Да!", "Пока нет"]

YES_NO_OPTIONS = ["Да", "Нет"]

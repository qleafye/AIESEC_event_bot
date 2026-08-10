"""Phase 07.2 (CITY-02) — СВОДНЫЙ MODULE-OFF ПАРИТЕТ.

Назначение файла (контракт, а не просто набор проверок):

    «Фаза 07.2 не изменила поведение бота, пока менеджер не включил модуль городов.»

Фаза 07.2 тронула пять поверхностей админки (обе очереди модерации, массовое одобрение,
выгрузку CSV, статистику, конструктор фильтров рассылки) плюс шапку панели. На живом боте с
~590 записями главный риск фазы — не «город не заработал», а «сломалось то, что работало».
Поэтому паритет обязан быть ИСПОЛНЯЕМЫМ тестом, а не рассуждением в SUMMARY.

Каждый тест здесь прогоняется при ВЫКЛЮЧЕННОМ модуле городов, причём `event_city_enabled`
НАМЕРЕННО не выставляется даже в "off" — база остаётся свежей и дефолтной. Так тест ловит и
случайное изменение дефолта тумблера, и любое безусловное сужение запроса по городу. Если
кто-то в будущем сделает городской фильтр безусловным, ЭТОТ файл должен упасть первым.

Данные во всех сценариях специально «смешанные»: в базе есть строки с `event_city` = NULL
(бэклог, поданный до появления городов), 'msk', 'spb' и 'tyumen'. С выключенным модулем ни
одна поверхность не имеет права их различать.

pytest-asyncio в этом окружении нет — каждый async-хелпер гоняется через asyncio.run(), а
config.DB_PATH указывает на файл в tmp_path; та же конвенция, что в
tests/test_city_admin_phase72.py и tests/test_city_export_stats_phase72.py.
"""
import asyncio
import inspect

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin as admin_mod
from services import reminders as reminders_mod
import cities


ADMIN_ID = 940101


def _admin_ready(tmp_path):
    """Свежая база + дефолтные настройки. `event_city_enabled` НЕ выставляется намеренно."""
    config.DB_PATH = str(tmp_path / "test_city_offparity72.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0
        self.answers_sent = []
        self.answer_markups = []
        self.documents = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.text = text
        self.markup = reply_markup

    async def answer_document(self, document, caption=None):
        self.documents.append((document, caption))


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.bot = None
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _seed(telegram_id, event_city, *, registration_date, status="pending",
          payment_status=None, full_name=None):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "full_name": full_name or f"User {telegram_id}",
        "registration_date": registration_date,
        "event_city": event_city,
    }))
    asyncio.run(db.set_user_status(telegram_id, status))
    if payment_status is not None:
        asyncio.run(db.update_payment_status(telegram_id, payment_status))


def _seed_mixed_cities(tmp_path):
    """Пять заявок во всех четырёх состояниях `event_city`; самая ранняя — питерская.

    Порядок дат выбран так, чтобы «первая в очереди» была НЕ из города по умолчанию: если
    кто-то сделает фильтр безусловным, очередь молча начнёт с московской заявки и тест упадёт.
    """
    _admin_ready(tmp_path)
    _seed(3, "spb", registration_date="2026-01-01 09:00:00", full_name="Питерская Первая")
    _seed(1, None, registration_date="2026-01-02 09:00:00")
    _seed(2, "msk", registration_date="2026-01-03 09:00:00")
    _seed(4, "spb", registration_date="2026-01-04 09:00:00")
    _seed(5, "tyumen", registration_date="2026-01-05 09:00:00")


# ── Поверхность 1: реестр городов / выбор админа ────────────────────────────────────────

def test_admin_selected_city_is_none_even_with_a_stored_choice(tmp_path):
    """Даже если админ УЖЕ выбирал город (запись пережила выключение модуля), с выключенным
    модулем скоуп обязан быть None — «ничего не фильтруется»."""
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", "spb"))
    assert asyncio.run(cities.admin_selected_city(ADMIN_ID)) is None
    assert asyncio.run(admin_mod._admin_city_scope(ADMIN_ID)) is None
    assert asyncio.run(admin_mod._admin_city_label(ADMIN_ID)) is None


# ── Поверхность 2: шапка админ-панели ───────────────────────────────────────────────────

def test_admin_keyboard_for_is_the_plain_admin_keyboard(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", "spb"))
    scoped = asyncio.run(admin_mod.admin_keyboard_for(ADMIN_ID))
    plain = admin_mod.build_admin_keyboard()
    assert [[(b.text, b.callback_data) for b in row] for row in scoped.inline_keyboard] == \
           [[(b.text, b.callback_data) for b in row] for row in plain.inline_keyboard]
    flat = [b.callback_data for row in scoped.inline_keyboard for b in row]
    assert "admin_city_switch" not in flat


# ── Поверхность 3: обе очереди модерации (счётчики + первая карточка) ────────────────────

def test_pending_counts_cover_every_city(tmp_path):
    _seed_mixed_cities(tmp_path)
    assert asyncio.run(db.get_pending_count()) == 5

    _admin_ready(tmp_path)
    _seed(1, None, registration_date="2026-01-01 09:00:00",
          status="approved", payment_status="receipt_sent")
    _seed(2, "msk", registration_date="2026-01-02 09:00:00",
          status="approved", payment_status="receipt_sent")
    _seed(3, "spb", registration_date="2026-01-03 09:00:00",
          status="approved", payment_status="receipt_sent")
    _seed(4, "tyumen", registration_date="2026-01-04 09:00:00",
          status="approved", payment_status="receipt_sent")
    assert asyncio.run(db.get_receipt_pending_count()) == 4


def test_applications_queue_shows_the_oldest_row_regardless_of_its_city(tmp_path):
    _seed_mixed_cities(tmp_path)
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    # 5 заявок всех городов в счётчике, первой показана самая ранняя (питерская)
    assert target.text.startswith("📋 <b>Заявка 1/5</b>\n")
    assert "Питерская Первая" in target.text
    assert "🏙" not in target.text.split("\n")[0]


def test_receipts_queue_header_has_no_city_marker(tmp_path):
    _admin_ready(tmp_path)
    _seed(3, "spb", registration_date="2026-01-01 09:00:00",
          status="approved", payment_status="receipt_sent")
    _seed(1, None, registration_date="2026-01-02 09:00:00",
          status="approved", payment_status="receipt_sent")
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(target, state))
    assert target.text.startswith("🧾 <b>Чек 1/2</b>\n")
    assert "🏙" not in target.text.split("\n")[0]


def test_empty_queues_keep_the_pre_phase_literals(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    apps = FakeMessage()
    asyncio.run(admin_mod._show_current_card(apps, state))
    assert apps.answers_sent[-1] == "✅ Заявок нет."
    receipts = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(receipts, state))
    assert receipts.answers_sent[-1] == "✅ Чеков на проверке нет."


# ── Поверхность 4: массовое одобрение ───────────────────────────────────────────────────

def test_appr_all_confirm_text_equals_the_pre_phase_literal(tmp_path):
    """Строгое равенство, а не вхождение: город в тексте подтверждения — это ровно то, чего
    в module-off варианте быть НЕ должно, и `in` такую регрессию пропустит."""
    _seed_mixed_cities(tmp_path)
    total = asyncio.run(db.get_pending_count())
    cb = FakeCallback("appr_all")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    assert cb.message.text == f"Одобрить все {total} заявок?"


def test_appr_all_yes_approves_every_city(tmp_path):
    _seed_mixed_cities(tmp_path)
    cb = FakeCallback("appr_all_yes")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_yes(cb, state))

    async def check():
        for tid in (1, 2, 3, 4, 5):
            user = await db.get_user(tid)
            assert user["status"] == "approved", tid

    asyncio.run(check())
    assert asyncio.run(db.get_pending_count()) == 0


# ── Поверхность 5: выгрузка CSV ─────────────────────────────────────────────────────────

def test_csv_export_filename_and_caption_are_the_pre_phase_literals(tmp_path):
    _seed_mixed_cities(tmp_path)
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", "spb"))
    cb = FakeCallback("admin_export_csv")
    asyncio.run(admin_mod.show_admin_export(cb))
    document, caption = cb.message.documents[0]
    assert document.filename == "users.csv"
    assert caption == "База данных пользователей"

    import csv as csv_mod
    import io as io_mod
    body = list(csv_mod.reader(
        io_mod.StringIO(document.data.decode("utf-8-sig")), delimiter=";"))[1:]
    assert {row[0] for row in body} == {"1", "2", "3", "4", "5"}


# ── Поверхность 6: экран статистики ─────────────────────────────────────────────────────

def test_render_stats_text_has_no_city_block(tmp_path):
    _seed_mixed_cities(tmp_path)
    text = asyncio.run(admin_mod.render_stats_text())
    assert "По городам" not in text
    assert "Итого" not in text


# ── Поверхность 7: конструктор фильтров рассылки ────────────────────────────────────────

def test_filter_menu_keyboard_has_no_city_button(tmp_path):
    _admin_ready(tmp_path)
    filters = [{"field": "status", "value": "approved"}]
    target = FakeMessage()
    asyncio.run(admin_mod._render_filter_menu(target, filters, edit=True))
    expected = admin_mod._filter_menu_kb(filters)
    assert [[(b.text, b.callback_data) for b in row] for row in target.markup.inline_keyboard] == \
           [[(b.text, b.callback_data) for b in row] for row in expected.inline_keyboard]
    flat = [b.callback_data for row in target.markup.inline_keyboard for b in row]
    assert "filter_f_event_city" not in flat


# ── Зафиксированное решение: периодическое напоминание менеджеру остаётся ГЛОБАЛЬНЫМ ─────

def test_manager_pending_reminder_stays_global():
    """РЕШЕНИЕ ФАЗЫ 07.2, зафиксированное тестом, а не подразумеваемое.

    `services/reminders.py::pending_reminder_loop` НЕ фильтруется городом и не должен.

    Обоснование:
    * цикл рассылает счётчик заявок ВСЕМ `config.ADMIN_IDS` одним текстом — «напоминание про
      свой город» требует знания, какой менеджер за какой город отвечает, а привязка
      админ→город явно отложена в Phase 8 (роли/capabilities, 07.2-CONTEXT.md);
    * персонализировать напоминание «последним выбранным городом» было бы ХУЖЕ глобального:
      менеджер, случайно переключивший город на другой, молча перестал бы получать
      напоминания про остальные и никогда бы об этом не узнал;
    * технически это бесплатно: `get_pending_count` получила `city_scope=None` по умолчанию,
      поэтому у `services/reminders.py` нулевой диф за всю фазу.

    Если Phase 8 будет менять это поведение — она обязана поменять и этот тест ОСОЗНАННО.
    """
    src = inspect.getsource(reminders_mod.pending_reminder_loop)
    assert "get_pending_count()" in src
    assert "city_scope" not in src
    module_src = inspect.getsource(reminders_mod)
    assert "import cities" not in module_src
    assert "from cities" not in module_src


# ── Doc-safety: ADMIN_GUIDE.md обязан описывать РЕАЛЬНЫЕ экраны и кнопки ─────────────────
#
# Прецедент — tests/test_city_admin_phase71.py::test_admin_guide_documents_city_deep_links...
# Гайд менеджера — это инструкция к необратимым массовым действиям (T-072-18): если он
# разъедется с кодом, менеджер нажмёт не то, что прочитал.

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_admin_guide_no_longer_promises_city_filters_as_future_work():
    """Строка «появятся отдельной фазой позже» стала ЛОЖЬЮ после фазы 07.2."""
    assert "появятся отдельной фазой позже" not in _read("ADMIN_GUIDE.md")


def test_admin_guide_documents_the_city_switcher_and_its_scope():
    guide = _read("ADMIN_GUIDE.md")
    admin_src = _read("handlers/admin.py")
    # Экран переключателя, описанный в гайде, существует в коде
    assert "admin_city_switch" in admin_src
    assert "🏙 Город:" in guide and "🏙 Город:" in admin_src
    # Что фильтруется
    for token in ("📋 Заявки", "🧾 Чеки", "📄 Экспорт CSV", "Одобрить все"):
        assert token in guide, token
        assert token in admin_src, token
    # Фильтр рассылки: подпись в гайде совпадает с подписью кнопки в коде
    assert "Город мероприятия" in guide
    assert "🏙 Город мероприятия" in admin_src
    assert "filter_f_event_city" in admin_src


def test_admin_guide_states_what_is_deliberately_not_city_scoped():
    guide = _read("ADMIN_GUIDE.md")
    assert "По городам" in guide                    # статистика — сравнение городов
    assert "Незавершённые" in guide                 # пишет все города за проход
    # предупреждение «после включения модуля очередь показывает город по умолчанию»
    assert "заявки не пропали" in guide.lower()


def test_readme_mentions_the_per_city_admin_panel_and_its_storage_key():
    readme = _read("README.md")
    assert "Погородная админка" in readme
    assert "admin_city__" in readme
    assert "admin_city__" in _read("cities.py")

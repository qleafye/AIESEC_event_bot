"""«👥 Список участников» в «📊 Данные»: CSV одобренных делегатов текущего сезона для
менеджера, который не сидит в таблице (отдать волонтёрам, вставить в свой чек-лист).

Без телефона и служебных полей — только то, что менеджеру нужно, чтобы узнать человека.
Шов той же формы, что соседние: своего `Router()` нет, хендлер на общем `admin.router`,
модуль импортируется хвостом `handlers/admin.py`. Выгрузка скоупится городом шапки админки,
как «📄 Экспорт CSV»."""
import csv
import io

from aiogram import F, types
from aiogram.types import BufferedInputFile

from database.db import export_participants_csv

from handlers.admin import router
from handlers.settings.admin_core import _admin_city_view
from domain.settings.schema import get_setting_typed
from services.infra.timeutil import msk_now


async def _city_label(raw) -> str:
    """Сырое event_city -> название города (db.py сам cities не импортирует)."""
    from domain.cities import city_label_or_none, normalize_city
    return await city_label_or_none(normalize_city(raw)) or ""

EMPTY_TEXT = ("Одобренных в текущем сезоне пока нет. Список появится, когда вы одобрите "
              "первые заявки в разделе «📋 Заявки».")
CAPTION = "Список участников — одобренные текущего сезона"


@router.callback_query(F.data == "admin_export_participants")
async def export_participants(callback: types.CallbackQuery):
    scope, _label = await _admin_city_view(callback.from_user.id)
    with_payment = await get_setting_typed("payment_enabled") == "on"
    headers, rows = await export_participants_csv(city_scope=scope, with_payment=with_payment,
                                                  city_label=_city_label)
    if not rows:
        await callback.answer(EMPTY_TEXT, show_alert=True)
        return
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    filename = f"uchastniki-{msk_now().strftime('%Y-%m-%d')}.csv"
    document = BufferedInputFile(output.getvalue().encode('utf-8-sig'), filename=filename)
    await callback.message.answer_document(document, caption=f"{CAPTION}: {len(rows)}")
    await callback.answer()

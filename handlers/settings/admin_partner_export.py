"""«🤝 Экспорт для партнёров» в «📊 Данные».

Колонки и фильтр одобренных живут в database/partner_export.py. Здесь только
кнопка и файл. Город шапки — тот же, что у полного экспорта.
"""
import csv
import html
import io

from aiogram import F, types
from aiogram.types import BufferedInputFile

from database.partner_export import export_partner_csv
from handlers.admin import router
from handlers.settings.admin_core import _admin_city_view


@router.callback_query(F.data == "admin_export_partner")
async def show_partner_export(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    scope, label = await _admin_city_view(admin_id)
    headers, rows = await export_partner_csv(city_scope=scope)
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    filename = "partners.csv" if scope is None else f"partners_{scope[0]}.csv"
    where = (
        "Одобренные текущего сезона" if label is None
        else f"Одобренные текущего сезона — {html.escape(str(label))}"
    )
    caption = f"{where}: {len(rows)}. Служебные поля в этот файл не входят."
    document = BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename=filename)
    await callback.message.answer_document(document, caption=caption)
    await callback.answer()

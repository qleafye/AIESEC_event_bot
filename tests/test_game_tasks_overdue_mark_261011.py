"""«🎯 Задания» у менеджера (приёмка 10.10): просроченные висели среди активных без пометки.
Сдачи после срока принимаются, поэтому задание остаётся в «Активных», но с «⌛ срок вышел» и в
конце списка; номер строки и кнопок «№N» идёт по тому же порядку."""
import asyncio

from config import config
from database import db
from handlers.game import admin_gamification
from tests._dbtpl import fast_init_db

ADMIN_ID = 941101


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "overdue_mark.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _mk(title, deadline_at):
    return asyncio.run(db.create_task(
        f"Текст {title}", "Light", 10, "text", deadline_at, ADMIN_ID, title=title,
    ))


def test_overdue_tasks_marked_and_moved_to_the_end(tmp_path):
    _ready(tmp_path)
    old_id = _mk("Старое", "2020-01-01 23:59:00")
    live_id = _mk("Живое", "2099-01-01 23:59:00")
    none_id = _mk("Бессрочное", db.NO_DEADLINE_AT)

    text, kb = asyncio.run(admin_gamification._game_tasks_screen())
    lines = text.split("\n\n")
    old_line = next(chunk for chunk in lines if "Старое" in chunk)
    assert "⌛ срок вышел" in old_line
    assert all("⌛" not in chunk for chunk in lines if "Живое" in chunk or "Бессрочное" in chunk)
    assert text.index("Старое") > text.index("Живое") and text.index("Старое") > text.index("Бессрочное")

    # №N у кнопок совпадает с номером строки: последняя строка — просроченная.
    archive_cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data.startswith("gtarchive:")]
    assert archive_cbs[-1] == f"gtarchive:{old_id}"
    assert set(archive_cbs) == {f"gtarchive:{i}" for i in (old_id, live_id, none_id)}
    assert old_line.startswith("3. ")

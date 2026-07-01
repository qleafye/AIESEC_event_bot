"""Per-LC payment requisites (each Local Committee collects to its own card)."""
import asyncio

import database.db as db
import handlers.payment as pay


def _use_tmp_db(tmp_path):
    db.config.DB_PATH = str(tmp_path / "lc_req.db")


def test_parse_lc_requisites_skips_blank_and_pipeless():
    raw = "Moscow | Сбер 1234, Иван\n\nbadline-no-pipe\nSPUEF|Тинькофф 5678, Пётр\nNoReq|"
    parsed = pay._parse_lc_requisites(raw)
    assert parsed == {"moscow": "Сбер 1234, Иван", "spuef": "Тинькофф 5678, Пётр"}


def test_resolve_uses_lc_card_case_insensitive(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("payment_requisites", "ОБЩАЯ КАРТА"))
    asyncio.run(db.set_setting("payment_requisites_by_lc", "Moscow | КАРТА МСК"))
    asyncio.run(db.add_user({"telegram_id": 1, "full_name": "A B",
                             "registration_date": "2026-07-01", "local_committee": "moscow"}))
    assert asyncio.run(pay._resolve_requisites(1)) == "КАРТА МСК"


def test_resolve_falls_back_to_shared_when_lc_unmatched(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("payment_requisites", "ОБЩАЯ КАРТА"))
    asyncio.run(db.set_setting("payment_requisites_by_lc", "Moscow | КАРТА МСК"))
    asyncio.run(db.add_user({"telegram_id": 2, "full_name": "A B",
                             "registration_date": "2026-07-01", "local_committee": "Ufa"}))
    assert asyncio.run(pay._resolve_requisites(2)) == "ОБЩАЯ КАРТА"


def test_resolve_falls_back_when_no_lc_map(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("payment_requisites", "ОБЩАЯ КАРТА"))
    asyncio.run(db.add_user({"telegram_id": 3, "full_name": "A B",
                             "registration_date": "2026-07-01", "local_committee": "Moscow"}))
    assert asyncio.run(pay._resolve_requisites(3)) == "ОБЩАЯ КАРТА"

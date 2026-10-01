"""Приёмка 01.10, SOS: «🙋 Беру» снимается с карточки после захвата; первый текст делегата не
дублируется в тред (он уже в карточке); дозапись в личку — одним ответом на копию карточки;
список SOS показывает суть заявки и кто/когда взял и решил."""
from __future__ import annotations

from handlers import admin_sos, sos as sos_handlers
from services import sos as sos_service
from database import db
from tests.test_sos_260924 import (
    ADMIN_ID, DELEGATE_ID, FakeBot, FakeMessage, _add_delegate, _collecting_state, _ready, _run,
)


def _cb_datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_card_kb_drops_claim_button_after_claim(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    bot = FakeBot()
    _run(sos_service.post_card(bot, rid))  # чат не привязан -> копии в личку
    _run(db.claim_sos_report(rid, ADMIN_ID, "Админ"))
    _run(sos_service.refresh_card(bot, rid))
    kb = bot.edited[-1][3]["reply_markup"]
    # «🙋 Беру» снят; вместо него «🔁 Перехватить» — на случай, если взявший пропал.
    assert _cb_datas(kb) == [f"sos_takeover:{rid}", f"sos_resolve:{rid}"]

    _run(db.resolve_sos_report(rid, ADMIN_ID, "Админ"))
    _run(sos_service.refresh_card(bot, rid))
    assert bot.edited[-1][3]["reply_markup"] is None


def test_open_card_keeps_both_buttons():
    kb = sos_service.build_card_kb(7)
    assert _cb_datas(kb) == ["sos_claim:7", "sos_resolve:7"]


def test_dm_mode_first_text_not_relayed_then_threaded_reply(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    bot = FakeBot()
    _run(sos_service.post_card(bot, rid))
    copies = dict(_run(db.list_sos_card_copies(rid)))
    assert ADMIN_ID in copies

    state = _collecting_state(DELEGATE_ID, rid)
    first = FakeMessage(text="Потерял бейдж", user_id=DELEGATE_ID)
    first.bot = bot
    sent_before = len(bot.sent)
    _run(sos_handlers.sos_collecting_step(first, state))
    assert first.copies == []
    assert len(bot.sent) == sent_before  # ни подписи «Делегат дополнил», ни копии

    second = FakeMessage(text="Я у Большого зала", user_id=DELEGATE_ID)
    second.bot = bot
    _run(sos_handlers.sos_collecting_step(second, state))
    assert second.copies == [(ADMIN_ID, copies[ADMIN_ID])]
    assert not any("Делегат дополнил" in t for _c, t, _k in bot.sent[sent_before:])


def test_sos_list_shows_details_and_who_when(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.add_sos_details(rid, text="Потерял бейдж у Большого зала"))
    _run(db.claim_sos_report(rid, ADMIN_ID, "Иван Оргов"))
    _run(db.resolve_sos_report(rid, ADMIN_ID, "Мария Оргова"))
    text, _kb = _run(admin_sos.render_sos_screen(ADMIN_ID))
    assert "«Потерял бейдж у Большого зала»" in text
    assert "взял(а) Иван Оргов в " in text
    assert "решено: Мария Оргова в " in text
    assert "подробности ещё не прислали" not in text


def test_card_shows_application_status_and_english_reader():
    from services.sos import render_card_text

    report = {"id": 8, "telegram_id": 1, "created_at": "2026-10-03 10:00:00"}
    pending = render_card_text(report, {"full_name": "А", "status": "pending", "lang": "ru"})
    assert "⏳ Заявка на рассмотрении" in pending and "английском" not in pending
    rejected_en = render_card_text(report, {"full_name": "Б", "status": "rejected", "lang": "en"})
    assert "🚫 Заявка отклонена" in rejected_en
    assert "🌐 Читает бота на английском" in rejected_en
    approved = render_card_text(report, {"full_name": "В", "status": "approved"})
    assert "✅ Заявка одобрена" in approved
    assert render_card_text(report, None).count("Заявка") == 0

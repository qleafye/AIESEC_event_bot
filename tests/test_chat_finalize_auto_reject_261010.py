"""Автоотказ при подаче анкеты в чате: после текста отказа бот не шлёт «Поздравляем, заявка
принята» и оффер реф-ссылки (приёмка 10.10 — делегат получал «отклонена» и следом «принята»)."""
import asyncio

from handlers import registration as reg


class _FMsg:
    def __init__(self, uid):
        self.from_user = type("U", (), {"id": uid, "username": "u"})()
        self.sent = []

    async def answer(self, text=None, *a, **k):
        self.sent.append(text)


class _FState:
    async def get_data(self):
        return {"full_name": "Тест Автоотказ", "participant_type": "full", "event_city": "msk"}

    async def clear(self):
        pass


def _run(monkeypatch, auto_rejected):
    async def fake_finalize(*a, **k):
        return {"mode": "new", "auto_rejected": auto_rejected}

    async def noop(*a, **k):
        return None

    offers = []

    async def fake_offer(message, uid, city):
        offers.append(uid)

    from handlers import reg_ambassador

    monkeypatch.setattr(reg, "finalize_data", fake_finalize)
    monkeypatch.setattr(reg, "post_finalize", noop)
    monkeypatch.setattr(reg, "get_main_menu_kb", noop)
    monkeypatch.setattr(reg_ambassador, "offer_ref_link", fake_offer)

    async def fake_setting(*a, **k):
        return "Поздравляем, твоя заявка принята!"

    monkeypatch.setattr(reg, "get_setting_for_city", fake_setting)
    msg = _FMsg(900100)
    asyncio.run(reg.finalize_registration(msg, _FState(), bot=None))
    return msg.sent, offers


def test_auto_rejected_gets_no_congrats_and_no_ref_offer(monkeypatch):
    sent, offers = _run(monkeypatch, auto_rejected=True)
    assert not any("Поздравляем" in (t or "") for t in sent)
    assert offers == []


def test_regular_submit_still_gets_congrats_and_ref_offer(monkeypatch):
    sent, offers = _run(monkeypatch, auto_rejected=False)
    assert any("Поздравляем" in (t or "") for t in sent)
    assert offers == [900100]

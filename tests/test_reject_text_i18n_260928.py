"""Жалоба делегата 28.09: отклонённый автоправилом («Курс из списка», текст про резерв) на любой
кнопке видел голое «заявка отклонена», а английский перевод этого текста был бессмысленным —
ручные переносы строк посреди фраз («о не⏎прохождении») переводились построчно и теряли
отрицание, «@youlead26» превращался в «@youlea d26».

pytest-asyncio нет — async через asyncio.run(), БД в tmp_path.
"""
import asyncio
import json

from config import config
from database import db
from handlers import user_actions as ua_mod
from services import i18n_worker
from services.i18n_glossary import apply, join_soft_wraps, protect
from tests._dbtpl import fast_init_db
from tests.test_delegate_texts_registry_260819 import FakeMessage

DELEGATE_ID = 941228

REJECT_RU = (
    "Мы внимательно изучили твою заявку, и, к\nсожалению, спешим сообщить о не\n"
    "прохождении на форум\n\nА пока оставайся с нами в канале и боте\n"
    "Здесь будут анонсы 👀\n@youlead26"
)


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_reject_text_i18n_260928.db")
    fast_init_db()


# ── склейка мягких переносов ─────────────────────────────────────────────────────────────

def test_soft_wraps_joined_inside_sentence_only():
    out = join_soft_wraps(REJECT_RU)
    assert "к сожалению, спешим сообщить о не прохождении на форум" in out
    # абзац, строка с заглавной и строка с упоминанием остаются на своих местах
    assert "форум\n\nА пока" in out
    assert "боте\nЗдесь" in out
    assert "👀\n@youlead26" in out


def test_list_lines_are_not_joined():
    text = "Возьми с собой:\n- паспорт\n- ноутбук"
    assert join_soft_wraps(text) == text


# ── защита упоминаний и ссылок ───────────────────────────────────────────────────────────

def test_mention_and_url_survive_translation():
    src = "Пиши в @youlead26 или на https://t.me/youlead26."
    protected, mapping = protect(src)
    assert "@youlead26" not in protected and "https://" not in protected
    # движок переводит только слова вокруг, сентинелы проходят как есть
    en = protected.replace("Пиши в", "Write to").replace("или на", "or at")
    assert apply(src, en, mapping) == "Write to @youlead26 or at https://t.me/youlead26."


def test_email_is_not_a_mention():
    protected, mapping = protect("Почта: team@aiesec.ru")
    assert mapping == {}


# ── воркер: в движок уходит склеенный текст, стухшие переводы ставятся заново ────────────

class _StubDriver:
    def __init__(self):
        self.batches = []

    def translate_batch(self, texts):
        self.batches.append(list(texts))
        return [t for t in texts]

    def unload(self):
        pass


def test_drain_sends_joined_text_and_keeps_mention(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    stub = _StubDriver()

    async def _fake_get_driver():
        return stub

    monkeypatch.setattr(i18n_worker, "get_driver", _fake_get_driver)
    from services.i18n import src_hash

    asyncio.run(db.enqueue_translation("en", src_hash(REJECT_RU), REJECT_RU, origin_key="reject_text"))
    asyncio.run(i18n_worker.drain())

    sent = stub.batches[0][0]
    assert "о не прохождении" in sent
    row = asyncio.run(db.get_translation("en", src_hash(REJECT_RU)))
    assert "@youlead26" in row["text"]
    assert row["src_text"] == REJECT_RU


def test_requeue_picks_only_stale_machine_translations(tmp_path):
    _db_ready(tmp_path)
    from services.i18n import src_hash

    stale_src = "Пиши в @youlead26"
    fresh_src = "Пиши в @youlead27"
    manual_src = "Пиши в @youlead28"
    wrapped_src = "спешим сообщить о не\nпрохождении"

    async def seed():
        await db.upsert_translation("en", src_hash(stale_src), stale_src, "Write to @youlea d26", manual=0)
        await db.upsert_translation("en", src_hash(fresh_src), fresh_src, "Write to @youlead27", manual=0)
        await db.upsert_translation("en", src_hash(manual_src), manual_src, "Write @youlea d28", manual=1)
        await db.upsert_translation("en", src_hash(wrapped_src), wrapped_src, "in a hurry\nadmission", manual=0)

    asyncio.run(seed())
    assert asyncio.run(i18n_worker.requeue_stale_machine_translations()) == 2
    pending = asyncio.run(db.list_pending_translations("en"))
    assert {r["src_text"] for r in pending} == {stale_src, wrapped_src}


# ── гейт отклонённого автоправилом ───────────────────────────────────────────────────────

def _seed_auto_rejected(rule_text):
    async def seed():
        await db.add_user({
            "telegram_id": DELEGATE_ID, "full_name": "Делегат", "registration_date": "2026-09-25",
        })
        await db.set_user_status(DELEGATE_ID, "rejected")
        rule_id = await db.create_reject_rule(
            name="Курс из списка", city="msk", tracks='["full"]',
            conditions='[[{"step": "course", "op": "in", "values": ["1", "2"]}]]',
            action="reject", reject_text=rule_text, enabled=1, created_by=None,
        )
        await db.update_user_answers(
            DELEGATE_ID, {"auto_reject_rule_ids": json.dumps([rule_id])},
            allowed_columns=["auto_reject_rule_ids"],
        )
        return rule_id

    return asyncio.run(seed())


def test_gate_repeats_rule_text_for_auto_rejected(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("reject_text", "Заявка отклонена."))
    _seed_auto_rejected("Ты в резерве — позовём, если освободится место.")

    message = FakeMessage(user_id=DELEGATE_ID)
    assert asyncio.run(ua_mod.ensure_registered(message)) is False
    assert message.answers_sent == [
        "Заявка отклонена.\n\nТы в резерве — позовём, если освободится место."
    ]


def test_gate_prefers_snapshot_from_journal(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("reject_text", "Заявка отклонена."))
    rule_id = _seed_auto_rejected("Текст правила сейчас")
    asyncio.run(db.upsert_auto_reject_log(
        DELEGATE_ID, json.dumps([rule_id]), json.dumps(["Текст на момент отказа"]),
        "2026-09-25 17:56:18",
    ))

    message = FakeMessage(user_id=DELEGATE_ID)
    asyncio.run(ua_mod.ensure_registered(message))
    assert message.answers_sent == ["Заявка отклонена.\n\nТекст на момент отказа"]


def test_gate_manual_reject_shows_general_text(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("reject_text", "Заявка отклонена."))
    asyncio.run(db.add_user({
        "telegram_id": DELEGATE_ID, "full_name": "Делегат", "registration_date": "2026-09-25",
    }))
    asyncio.run(db.set_user_status(DELEGATE_ID, "rejected"))

    message = FakeMessage(user_id=DELEGATE_ID)
    asyncio.run(ua_mod.ensure_registered(message))
    assert message.answers_sent == ["Заявка отклонена."]

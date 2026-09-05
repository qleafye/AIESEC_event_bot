"""Хотфикс 06.09 (production, ветка hotfix/finalize-draft-fields): с b460826 обёртка
`handlers/registration.py::finalize_registration` передаёт в `services.reg_finalize.
finalize_data` РЕАЛЬНЫЙ черновик `reg_drafts` (через `claim_reg_draft`), а не FSM-словарь.
`finalize_data` строила `users` только из `draft["answers"]` — а город/трек анкеты живут в
колонках черновика (`event_city`, `participant_type`), источник/реферер деп-линка — в
`draft["meta"]`. Итог на проде с 05.09 16:04 UTC: `event_city` NULL у всех новых
пользователей, деп-линковый `source` подменялся дефолтом «Самостоятельно», `referrer_id`
терялся. До b460826 всё это лежало прямо в FSM `data`, которую обёртка передавала как
"answers" — отсюда и работало.

pytest-asyncio недоступен — тот же приём, что в соседних `test_reg_finalize.py`
(`asyncio.run()` + временная БД на `tmp_path`)."""
import asyncio

from config import config
from database import db
from services import reg_finalize as rf

UID_A = 900800200
UID_B = 900800201
UID_C = 900800202


def _ready(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def test_finalize_data_new_pulls_city_track_source_referrer_from_draft(tmp_path, monkeypatch):
    """(a): черновиковые колонки (event_city/participant_type) и meta (source/referrer_id) —
    единственное место, где эти поля живы, когда делегат сам их не вводил (город/трек
    пришли из деп-линка ДО первого вопроса анкеты, «Источник» пропущен как раз потому, что
    он уже известен из тега)."""
    _ready(tmp_path, "hotfix_new.db")
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        draft = {
            "telegram_id": UID_A,
            "kind": "new",
            "event_city": "spb",
            "participant_type": "party",
            "meta": {"source": "website_1", "referrer_id": 123, "source_from_tag": True},
            # Реальные ответы делегата НЕ содержат ни город/трек (это колонки черновика, не
            # анкетный шаг), ни "source" (шаг пропущен) — только то, что он сам напечатал.
            "answers": {"full_name": "Анна Аннова"},
        }
        result = await rf.finalize_data(UID_A, "@anna", draft)
        user = await db.get_user(UID_A)
        return result, user

    result, user = asyncio.run(go())
    assert result["mode"] == "new"
    assert user is not None
    assert user["event_city"] == "spb"
    assert user["participant_type"] == "party"
    assert user["source"] == "website_1"
    assert user["referrer_id"] == 123


def test_finalize_data_new_real_answer_wins_over_meta_source(tmp_path, monkeypatch):
    """(b): делегат САМ ответил на «Источник» — его ответ обязан победить черновиковый
    deep-link tag, а не быть перезаписанным (порядок слияния: answers поверх draft_level)."""
    _ready(tmp_path, "hotfix_new_wins.db")
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        draft = {
            "telegram_id": UID_B,
            "kind": "new",
            "event_city": "msk",
            "participant_type": "full",
            "meta": {"source": "website_1"},
            "answers": {"full_name": "Борис Борисов", "source": "ВК"},
        }
        result = await rf.finalize_data(UID_B, "@boris", draft)
        user = await db.get_user(UID_B)
        return result, user

    _result, user = asyncio.run(go())
    assert user["source"] == "ВК"
    assert user["event_city"] == "msk"


def test_finalize_data_pseudo_draft_from_fsm_unaffected(tmp_path, monkeypatch):
    """(c): псевдо-черновик (fallback чата, когда `claim_reg_draft` не нашёл строку) — тот же
    словарь, что раньше собирала `finalize_registration` из FSM: `event_city`/`participant_type`/
    `source`/`referrer_id` уже лежат прямо в `answers`, верхнеуровневых `event_city`/`meta`
    у него нет вовсе. Слияние обязано остаться no-op — `draft.get(...)` на отсутствующих ключах
    даёт `None`/`{}`, реальные ответы никак не меняются."""
    _ready(tmp_path, "hotfix_pseudo.db")
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        # Точная форма из handlers/registration.py::finalize_registration: draft = {telegram_id,
        # kind, answers=dict(FSM data), updated_by} — без ключей "event_city"/"meta".
        draft = {
            "telegram_id": UID_C,
            "kind": "new",
            "answers": {
                "full_name": "Виктор Викторов",
                "event_city": "spb",
                "participant_type": "party",
                "source": "friend_tag",
                "referrer_id": 555,
            },
            "updated_by": "bot",
        }
        result = await rf.finalize_data(UID_C, "@viktor", draft)
        user = await db.get_user(UID_C)
        return result, user

    _result, user = asyncio.run(go())
    assert user["event_city"] == "spb"
    assert user["participant_type"] == "party"
    assert user["source"] == "friend_tag"
    assert user["referrer_id"] == 555

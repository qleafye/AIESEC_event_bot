"""Анкета из нескольких вопросов, сторона делегата: рассылка приглашения, закрытие, прохождение
всех типов вопросов кнопками, повторы и закрытая анкета.

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from config import config
from database import db, survey_db
from services.comms import polls as polls_svc
from tests._dbtpl import fast_init_db

ADMIN_ID = 920001
U1 = 920011
U2 = 920012


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "flow.db")
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


async def _add_user(tid, status="approved"):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, status, participant_type) "
            "VALUES (?, ?, ?, ?, 'full')", (tid, f"U{tid}", f"@u{tid}", status),
        )
        await conn.commit()


async def _survey(questions=(("single", {}),), intro="Привет, пройди опрос", outro=None, status="scheduled"):
    pid = await survey_db.create_survey_draft("Анкета", ADMIN_ID)
    await survey_db.update_survey_texts(pid, intro=intro, outro=outro)
    qids = []
    for qtype, kw in questions:
        kw = dict(kw)
        if qtype in ("single", "multi") and "options" not in kw:
            kw["options"] = [{"t": "Да", "more": None}, {"t": "Нет", "more": None}]
        if qtype == "table" and "rows" not in kw:
            kw["rows"] = ["Еда", "Зал"]
        qids.append(await survey_db.add_survey_question(pid, qtype, f"Вопрос {qtype}", **kw))
    await db.set_poll_status(pid, status)
    return pid, qids


class FakeBot:
    def __init__(self):
        self.messages = []
        self.polls = []
        self.stopped = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=500 + len(self.messages))

    async def send_poll(self, *a, **kw):
        self.polls.append((a, kw))

    async def stop_poll(self, chat_id, message_id):
        self.stopped.append((chat_id, message_id))


# ── рассылка и закрытие ──────────────────────────────────────────────────────────────────────

def test_deliver_survey_sends_invites_only(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        await _add_user(U2)
        pid, _ = await _survey()
        bot = FakeBot()
        stats = await polls_svc.deliver_poll(bot, pid)
        return pid, bot, stats

    pid, bot, stats = _run(go())
    assert stats["sent"] == 2
    assert bot.polls == []
    assert len(bot.messages) == 2
    for _, text, kb in bot.messages:
        assert "Привет" in text
        assert kb.inline_keyboard[0][0].callback_data == f"sv:s:{pid}"
    assert _run(db.get_poll(pid))["status"] == "open"
    assert len(_run(db.list_poll_messages(pid))) == 2


def test_redeliver_does_not_duplicate(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        pid, _ = await _survey()
        bot = FakeBot()
        await polls_svc.deliver_poll(bot, pid)
        await db.set_poll_status(pid, "scheduled")
        stats = await polls_svc.deliver_poll(bot, pid)
        return bot, stats

    bot, stats = _run(go())
    assert len(bot.messages) == 1 and stats["skipped"] == 1


def test_deliver_survey_quiet_hours_queues_but_checkpoints(tmp_path, monkeypatch):
    _ready(tmp_path)
    from services.comms import quiet_hours

    async def fake_due(now, user_id):
        return datetime(2030, 1, 1, 9, 0)

    monkeypatch.setattr(quiet_hours, "defer_until", fake_due)

    async def go():
        await _add_user(U1)
        pid, _ = await _survey()
        bot = FakeBot()
        stats = await polls_svc.deliver_poll(bot, pid)
        return pid, bot, stats

    pid, bot, stats = _run(go())
    assert bot.messages == [] and stats["queued"] == 1 and stats["sent"] == 0
    assert _run(survey_db.survey_recipient(pid, U1)) is True


def test_close_survey_does_not_stop_poll(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        pid, _ = await _survey()
        bot = FakeBot()
        await polls_svc.deliver_poll(bot, pid)
        res = await polls_svc.close_poll(bot, pid)
        return pid, bot, res

    pid, bot, res = _run(go())
    assert res == (0, 0) and bot.stopped == []
    assert _run(db.get_poll(pid))["status"] == "closed"


# ── прохождение: sv:* ────────────────────────────────────────────────────────────────────────

import handlers.user_actions  # noqa: E402,F401  -- порядок регистрации: user_actions первым
from handlers.comms import survey_delegate as sd  # noqa: E402
from handlers.access.admin_caps import required_capability  # noqa: E402


class FakeMsg:
    def __init__(self):
        self.edits = []
        self.answers = []
        self.markups = []

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.answers.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None):
        self.markups.append(reply_markup)


class FakeCb:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.message = FakeMsg()
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append((text, show_alert))


def _tap(handler, data, uid=U1):
    cb = FakeCb(data, uid)
    _run(handler(cb))
    return cb


def _last(cb):
    return (cb.message.edits + cb.message.answers)[-1][0]


async def _ready_survey(questions, **kw):
    await _add_user(U1)
    pid, qids = await _survey(questions, status="open", **kw)
    await db.record_poll_message(pid, U1, None, 1, True)
    return pid, qids


def test_start_non_recipient_and_recipient(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_survey([("single", {})]))
    cb = _tap(sd.sv_start, f"sv:s:{pid}", uid=U2)
    assert cb.alerts[0][0] == "Этот опрос отправлен не тебе"
    assert _run(survey_db.get_survey_progress(pid, U2)) is None
    cb = _tap(sd.sv_start, f"sv:s:{pid}")
    assert "Вопрос 1 из 1" in cb.message.answers[0][0]
    assert cb.message.markups[0].inline_keyboard[0][0].callback_data == f"sv:c:{pid}"
    assert _run(survey_db.get_survey_progress(pid, U1))["current_qid"] == qids[0]


def test_continue_after_restart_shows_same_question(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_survey([("single", {}), ("single", {})]))
    _tap(sd.sv_start, f"sv:s:{pid}")
    _tap(sd.sv_option, f"sv:o:{qids[0]}:0")
    cb = _tap(sd.sv_continue, f"sv:c:{pid}")  # каждый _run открывает новые соединения БД
    assert "Вопрос 2 из 2" in cb.message.answers[0][0]


def test_single_answer_advances_and_finishes_with_outro(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_survey([("single", {}), ("single", {})], outro="Пока!"))
    _tap(sd.sv_start, f"sv:s:{pid}")
    cb = _tap(sd.sv_option, f"sv:o:{qids[0]}:1")
    assert "Вопрос 2 из 2" in _last(cb)
    cb = _tap(sd.sv_option, f"sv:o:{qids[1]}:0")
    assert _last(cb) == "Пока!"
    assert _run(survey_db.get_survey_progress(pid, U1))["state"] == "done"
    answers = {(a["question_id"], a["sub"]): a["value"] for a in _run(survey_db.list_survey_answers(pid))}
    assert answers == {(qids[0], ""): 1, (qids[1], ""): 0}
    cb = _tap(sd.sv_option, f"sv:o:{qids[1]}:0")
    assert cb.alerts[0][0] == "Ты уже ответил(а), спасибо"


def test_default_outro(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_survey([("single", {})]))
    _tap(sd.sv_start, f"sv:s:{pid}")
    cb = _tap(sd.sv_option, f"sv:o:{qids[0]}:0")
    assert "Спасибо" in _last(cb)


def test_multi_toggle_limit_and_done(tmp_path):
    _ready(tmp_path)
    opts = [{"t": "A", "more": None}, {"t": "B", "more": None}, {"t": "C", "more": None}]
    pid, qids = _run(_ready_survey([("multi", {"options": opts, "max_choices": 2}), ("single", {})]))
    q = qids[0]
    _tap(sd.sv_start, f"sv:s:{pid}")
    cb = _tap(sd.sv_done, f"sv:d:{q}")
    assert cb.alerts[0][0] == "Выбери хотя бы один вариант"
    cb = _tap(sd.sv_option, f"sv:o:{q}:0")
    assert "✓ A" in str(cb.message.edits[0][1])
    _tap(sd.sv_option, f"sv:o:{q}:1")
    cb = _tap(sd.sv_option, f"sv:o:{q}:2")
    assert "не больше 2" in cb.alerts[0][0]
    cb = _tap(sd.sv_option, f"sv:o:{q}:0")  # снятие
    assert "✓ A" not in str(cb.message.edits[0][1])
    cb = _tap(sd.sv_done, f"sv:d:{q}")
    assert "Вопрос 2 из 2" in _last(cb)
    answers = {a["question_id"]: a["value"] for a in _run(survey_db.list_survey_answers(pid))}
    assert answers[q] == [1]


def test_more_clarification_queue_and_skip(tmp_path):
    _ready(tmp_path)
    opts = [{"t": "A", "more": "Что именно A?"}, {"t": "B", "more": "Что именно B?"}, {"t": "C", "more": None}]
    pid, qids = _run(_ready_survey([("multi", {"options": opts}), ("single", {})]))
    q = qids[0]
    _tap(sd.sv_start, f"sv:s:{pid}")
    _tap(sd.sv_option, f"sv:o:{q}:0")
    _tap(sd.sv_option, f"sv:o:{q}:1")
    cb = _tap(sd.sv_done, f"sv:d:{q}")
    assert "Что именно A?" in _last(cb)
    wait = _run(survey_db.get_survey_wait(U1))
    assert wait["await"] == {"qid": q, "sub": "m0", "queue": [1]}
    # устаревший тап варианта во время уточнения лишь перерисовывает уточнение
    cb = _tap(sd.sv_option, f"sv:o:{q}:2")
    assert "Что именно A?" in _last(cb)
    cb = _tap(sd.sv_skip, f"sv:k:{q}")
    assert "Что именно B?" in _last(cb)
    cb = _tap(sd.sv_skip, f"sv:k:{q}")
    assert "Вопрос 2 из 2" in _last(cb)


def test_scale_and_table(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_survey([("scale", {}), ("table", {"rows": ["Еда", "Зал"]})]))
    _tap(sd.sv_start, f"sv:s:{pid}")
    cb = _tap(sd.sv_scale, f"sv:n:{qids[0]}:4")
    assert "Строка 1 из 2" in _last(cb)
    cb = _tap(sd.sv_table, f"sv:t:{qids[1]}:3")
    assert "Строка 2 из 2" in _last(cb)
    _tap(sd.sv_table, f"sv:t:{qids[1]}:0")
    answers = {(a["question_id"], a["sub"]): a["value"] for a in _run(survey_db.list_survey_answers(pid))}
    assert answers == {(qids[0], ""): 4, (qids[1], "r0"): 3, (qids[1], "r1"): 0}
    assert _run(survey_db.get_survey_progress(pid, U1))["state"] == "done"


def test_scale_out_of_range_rejected(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_survey([("scale", {})]))
    _tap(sd.sv_start, f"sv:s:{pid}")
    cb = _tap(sd.sv_scale, f"sv:n:{qids[0]}:9")
    assert cb.alerts and _run(survey_db.list_survey_answers(pid)) == []


def test_text_question_sets_await(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_survey([("text", {})]))
    _tap(sd.sv_start, f"sv:s:{pid}")
    wait = _run(survey_db.get_survey_wait(U1))
    assert wait["current_qid"] == qids[0] and wait["await"]["sub"] == ""


def test_skip_required_forbidden_optional_ok(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_survey([("scale", {}), ("scale", {})]))

    async def make_optional():
        async with db._connect() as conn:
            await conn.execute("UPDATE survey_questions SET required = 0 WHERE id = ?", (qids[1],))
            await conn.commit()

    _tap(sd.sv_start, f"sv:s:{pid}")
    cb = _tap(sd.sv_skip, f"sv:k:{qids[0]}")
    assert cb.alerts[0][0] == "Этот вопрос обязательный"
    _tap(sd.sv_scale, f"sv:n:{qids[0]}:5")
    _run(make_optional())
    _tap(sd.sv_skip, f"sv:k:{qids[1]}")
    assert _run(survey_db.get_survey_progress(pid, U1))["state"] == "done"
    assert [a["question_id"] for a in _run(survey_db.list_survey_answers(pid))] == [qids[0]]


def test_closed_survey_alert(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_survey([("single", {})]))
    _tap(sd.sv_start, f"sv:s:{pid}")
    _run(db.set_poll_status(pid, "closed"))
    for h, d in ((sd.sv_option, f"sv:o:{qids[0]}:0"), (sd.sv_continue, f"sv:c:{pid}"),
                 (sd.sv_start, f"sv:s:{pid}")):
        assert _tap(h, d).alerts[0][0] == "Опрос закрыт"


def test_stale_question_redraws_current(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_survey([("single", {}), ("single", {})]))
    _tap(sd.sv_start, f"sv:s:{pid}")
    cb = _tap(sd.sv_option, f"sv:o:{qids[1]}:0")  # второй, а текущий первый
    assert "Вопрос 1 из 2" in _last(cb)
    assert _run(survey_db.list_survey_answers(pid)) == []


def test_foreign_question_and_bad_index_write_nothing(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_survey([("single", {})]))
    other, oq = _run(_survey([("single", {})], status="open"))
    _tap(sd.sv_start, f"sv:s:{pid}")
    cb = _tap(sd.sv_option, f"sv:o:{oq[0]}:0")
    assert cb.alerts  # по чужой анкете у делегата нет прогресса
    cb = _tap(sd.sv_option, f"sv:o:{qids[0]}:7")
    assert cb.alerts
    assert _run(survey_db.list_survey_answers(pid)) == []
    assert _run(survey_db.list_survey_answers(other)) == []


def test_double_tap_single_transition(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_survey([("single", {}), ("single", {})]))
    _tap(sd.sv_start, f"sv:s:{pid}")
    _tap(sd.sv_option, f"sv:o:{qids[0]}:0")
    cb = _tap(sd.sv_option, f"sv:o:{qids[0]}:1")  # второй тап по уже пройденному вопросу
    assert "Вопрос 2 из 2" in _last(cb)
    answers = [(a["question_id"], a["value"]) for a in _run(survey_db.list_survey_answers(pid))]
    assert answers == [(qids[0], 0)]


def test_sv_prefixes_not_capability_gated():
    for data in ("sv:s:1", "sv:c:1", "sv:o:1:0", "sv:d:1", "sv:k:1", "sv:n:1:3", "sv:t:1:0"):
        assert required_capability(callback_data=data) is None


def test_survey_link_opens_that_survey_and_qr_matches(tmp_path):
    """Ссылка конкретного опроса, старт по ней открывает его (черновик не мешает), QR — та же ссылка."""
    import io

    import segno
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from handlers.comms import admin_survey_builder as sb
    from handlers.comms.admin_survey_results import render_survey_card
    from handlers.forum import forum_deeplinks
    from handlers.states import SurveyBuild
    from services.comms import surveys

    _ready(tmp_path)
    scanner = 920099

    class _StartMsg:
        def __init__(self, uid):
            self.from_user = SimpleNamespace(id=uid)
            self.answers = []

        async def answer(self, text, reply_markup=None, parse_mode=None):
            self.answers.append((text, reply_markup))

    class _Bot:
        def __init__(self, username="YouLeadBot", boom=False):
            self.username = username
            self.boom = boom

        async def me(self):
            if self.boom:
                raise RuntimeError("down")
            return SimpleNamespace(username=self.username)

    class _PhotoMsg:
        def __init__(self):
            self.photos = []

        async def answer_photo(self, photo, caption=None, parse_mode=None):
            self.photos.append((photo, caption, parse_mode))

    class _AdminCb:
        def __init__(self, data):
            self.data = data
            self.from_user = SimpleNamespace(id=ADMIN_ID)
            self.message = _PhotoMsg()
            self.alerts = []

        async def answer(self, text=None, show_alert=False):
            self.alerts.append((text, show_alert))

    async def go():
        alpha = await survey_db.create_survey_draft("Первый день", ADMIN_ID)
        beta = await survey_db.create_survey_draft("Второй день", ADMIN_ID)
        empty = await survey_db.create_survey_draft("Пустой", ADMIN_ID)
        q_alpha = await survey_db.add_survey_question(
            alpha, "single", "Q-ALPHA-LINK",
            options=[{"t": "Да", "more": None}, {"t": "Нет", "more": None}],
        )
        await survey_db.add_survey_question(
            beta, "single", "Q-BETA-OTHER",
            options=[{"t": "Раз", "more": None}, {"t": "Два", "more": None}],
        )
        link = surveys.survey_link("YouLeadBot", alpha)
        assert link == f"https://t.me/YouLeadBot?start=survey_{alpha}"
        assert surveys.survey_link("@YouLeadBot", alpha) == link
        assert surveys.survey_link("", alpha) is None
        other = surveys.survey_link("YouLeadBot", beta)
        assert other != link and other.endswith(f"survey_{beta}")
        assert set(forum_deeplinks.DEEPLINKS) == {"sessions", "quiz"}
        assert surveys.parse_survey_start("sessions") is None
        assert surveys.parse_survey_start("quiz") is None
        assert surveys.parse_survey_start("survey_01") is None
        assert surveys.parse_survey_start(None) is None
        payload = link.split("start=", 1)[1]
        assert surveys.parse_survey_start(payload) == alpha

        state = FSMContext(
            storage=MemoryStorage(),
            key=StorageKey(bot_id=1, chat_id=scanner, user_id=scanner),
        )
        await state.set_state(SurveyBuild.intro)
        await state.update_data(pid=alpha)
        msg = _StartMsg(scanner)
        assert await forum_deeplinks.try_forum_deeplink(msg, state, payload) is True
        assert await state.get_state() is None
        shown, kb = msg.answers[-1]
        assert "Q-ALPHA-LINK" in shown and "Q-BETA-OTHER" not in shown
        assert f"sv:o:{q_alpha}:0" in [b.callback_data for row in kb.inline_keyboard for b in row]
        assert await db.get_user(scanner) is None
        assert (await survey_db.get_survey_progress(alpha, scanner))["current_qid"] == q_alpha
        assert await survey_db.get_survey_progress(beta, scanner) is None
        assert (await db.get_poll(alpha))["status"] == "draft"
        assert await db.list_poll_messages(alpha) == []

        tap = FakeCb(f"sv:o:{q_alpha}:0", scanner)
        await sd.sv_option(tap)
        assert not any(flag for _, flag in tap.alerts)
        assert (await survey_db.get_survey_progress(alpha, scanner))["state"] == "done"
        assert (await db.get_poll(alpha))["status"] == "draft"
        assert await db.list_poll_messages(alpha) == []

        missing = _StartMsg(scanner)
        assert await forum_deeplinks.try_forum_deeplink(missing, state, "survey_999999") is True
        assert "новую" in missing.answers[-1][0]
        blank = _StartMsg(scanner)
        assert await forum_deeplinks.try_forum_deeplink(blank, state, f"survey_{empty}") is True
        assert "вопрос" in blank.answers[-1][0].lower()
        assert "менеджер" in blank.answers[-1][0].lower()

        text, kb = await sb.render_survey_draft(alpha)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert surveys.SURVEY_LINK_BUTTON in labels
        assert surveys.SURVEY_LINK_BUTTON in text
        for label in labels:
            assert "psv_" not in label and "survey_" not in label
        _, card_kb = await render_survey_card(await db.get_poll(alpha))
        card_labels = [b.text for row in card_kb.inline_keyboard for b in row]
        assert surveys.SURVEY_LINK_BUTTON in card_labels
        assert required_capability(callback_data=f"psv_link:{alpha}") == "broadcast"

        admin_cb = _AdminCb(f"psv_link:{alpha}")
        await sb.survey_link_qr(admin_cb, _Bot())
        photo, caption, mode = admin_cb.message.photos[0]
        assert mode is None and link in caption and "psv_" not in caption
        buf = io.BytesIO()
        segno.make(link).save(buf, kind="png", scale=6, border=2)
        assert photo.data == buf.getvalue()
        assert (await db.get_poll(alpha))["status"] == "draft"
        assert await db.list_poll_messages(alpha) == []

        no_name = _AdminCb(f"psv_link:{alpha}")
        await sb.survey_link_qr(no_name, _Bot(username=None))
        assert no_name.message.photos == []
        assert no_name.alerts[-1][1] is True and "имя бота" in no_name.alerts[-1][0]
        assert "Ссылка и QR" in no_name.alerts[-1][0]
        boom = _AdminCb(f"psv_link:{alpha}")
        await sb.survey_link_qr(boom, _Bot(boom=True))
        assert boom.message.photos == [] and "имя бота" in boom.alerts[-1][0]
        no_q = _AdminCb(f"psv_link:{empty}")
        await sb.survey_link_qr(no_q, _Bot())
        assert no_q.message.photos == [] and "Добавить вопрос" in no_q.alerts[-1][0]

        await db.set_poll_status(alpha, "closed")
        closed = _StartMsg(scanner)
        assert await forum_deeplinks.try_forum_deeplink(closed, state, payload) is True
        assert "закрыт" in closed.answers[-1][0] and "менеджер" in closed.answers[-1][0].lower()
        assert await db.list_poll_messages(alpha) == []

    _run(go())

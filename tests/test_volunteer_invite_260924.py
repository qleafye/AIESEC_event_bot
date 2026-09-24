"""Идея №5 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): приглашение
волонтёров ссылкой вместо ручного сбора юзернеймов.

Покрытие: `database.db.claim_volunteer_invite` (жива/истекла/отозвана/лимит/двойной переход/
гонка на последний слот), тумблер `volunteer_invite_enabled` (выключен -> старые ссылки не
срабатывают), чужой город менеджера (`handlers/admin_volunteer_invite.py::_city_allowed`),
приём в `handlers/registration.py::cmd_start` (`vol_`-ветка deep-link, полностью отдельная от
анкеты делегата), «не понижает существующую роль».

pytest-asyncio недоступна — async через `asyncio.run()` (конвенция проекта)."""
from __future__ import annotations

import asyncio

import cities as cities_mod
from config import config
from database import db
from handlers import admin_volunteer_invite as avi  # noqa: F401 -- регистрирует volinvite_*/volinv_*
from handlers import registration as reg
from handlers.admin_caps import resolve_capabilities
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import _fresh_state, dispatch_callback, dispatch_message

ADMIN_ID = 924301
MANAGER_ID = 924310
VOLUNTEER_ID = 924320


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="volunteer_invite.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


# ══════════════════════════════════════════════════════════════════════════════════════════
# database/db.py: claim_volunteer_invite — жива/истекла/отозвана/исчерпана/двойной переход
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_claim_live_invite_ok_and_increments_used(tmp_path):
    _ready(tmp_path)
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, 30))
    outcome = _run(db.claim_volunteer_invite("c1", VOLUNTEER_ID))
    assert outcome == "ok"
    inv = _run(db.get_volunteer_invite("c1"))
    assert inv["used"] == 1
    uses = _run(db.list_volunteer_invite_uses("c1"))
    assert [u["telegram_id"] for u in uses] == [VOLUNTEER_ID]


def test_claim_not_found(tmp_path):
    _ready(tmp_path)
    assert _run(db.claim_volunteer_invite("nope", VOLUNTEER_ID)) == "not_found"


def test_claim_revoked(tmp_path):
    _ready(tmp_path)
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, None))
    _run(db.revoke_volunteer_invite("c1"))
    assert _run(db.claim_volunteer_invite("c1", VOLUNTEER_ID)) == "revoked"


def test_claim_link_expired(tmp_path):
    _ready(tmp_path)
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, "2000-01-01", None, None))
    assert _run(db.claim_volunteer_invite("c1", VOLUNTEER_ID)) == "link_expired"


def test_claim_link_not_yet_expired_today_inclusive(tmp_path):
    _ready(tmp_path)
    from services.staff_expiry import today_iso
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, today_iso(), None, None))
    assert _run(db.claim_volunteer_invite("c1", VOLUNTEER_ID)) == "ok"


def test_claim_exhausted(tmp_path):
    _ready(tmp_path)
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, 1))
    assert _run(db.claim_volunteer_invite("c1", VOLUNTEER_ID)) == "ok"
    other = VOLUNTEER_ID + 1
    assert _run(db.claim_volunteer_invite("c1", other)) == "exhausted"
    inv = _run(db.get_volunteer_invite("c1"))
    assert inv["used"] == 1  # второй провал не увеличил счётчик


def test_claim_double_tap_same_person_does_not_burn_second_slot(tmp_path):
    """«Двойной переход не жжёт второй слот» — та же ссылка, тот же человек, дважды."""
    _ready(tmp_path)
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, 1))
    first = _run(db.claim_volunteer_invite("c1", VOLUNTEER_ID))
    second = _run(db.claim_volunteer_invite("c1", VOLUNTEER_ID))
    assert first == "ok"
    assert second == "already_used"
    inv = _run(db.get_volunteer_invite("c1"))
    assert inv["used"] == 1
    uses = _run(db.list_volunteer_invite_uses("c1"))
    assert len(uses) == 1


def test_claim_race_for_last_slot_exactly_one_winner(tmp_path):
    """Гонка двух переходов на последний слот — две КОНКУРЕНТНЫЕ корутины, один max_uses."""
    _ready(tmp_path)
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, 1))

    async def race():
        a, b = VOLUNTEER_ID, VOLUNTEER_ID + 1
        return await asyncio.gather(
            db.claim_volunteer_invite("c1", a), db.claim_volunteer_invite("c1", b),
        )

    results = _run(race())
    assert sorted(results) == ["exhausted", "ok"]
    inv = _run(db.get_volunteer_invite("c1"))
    assert inv["used"] == 1
    uses = _run(db.list_volunteer_invite_uses("c1"))
    assert len(uses) == 1


def test_claim_unlimited_when_max_uses_none(tmp_path):
    _ready(tmp_path)
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, None))
    for i in range(5):
        assert _run(db.claim_volunteer_invite("c1", VOLUNTEER_ID + i)) == "ok"
    inv = _run(db.get_volunteer_invite("c1"))
    assert inv["used"] == 5


def test_revoke_volunteer_invite_idempotent(tmp_path):
    _ready(tmp_path)
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, None))
    assert _run(db.revoke_volunteer_invite("c1")) is True
    assert _run(db.revoke_volunteer_invite("c1")) is False  # уже отозвана


def test_revoke_does_not_remove_already_granted_roles(tmp_path):
    """«Ссылка перестанет работать, выданные права останутся»."""
    _ready(tmp_path)
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, None))
    _run(db.claim_volunteer_invite("c1", VOLUNTEER_ID))
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", None))
    _run(db.revoke_volunteer_invite("c1"))
    assert "checkin" in _run(resolve_capabilities(VOLUNTEER_ID))


def test_list_volunteer_invites_filters_by_city(tmp_path):
    _ready(tmp_path)
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, None))
    _run(db.create_volunteer_invite("c2", "msk", ADMIN_ID, None, None, None))
    spb_only = _run(db.list_volunteer_invites(city="spb"))
    assert [i["code"] for i in spb_only] == ["c1"]


# ══════════════════════════════════════════════════════════════════════════════════════════
# handlers/registration.py: приём vol_<код> в cmd_start — полностью отдельно от анкеты
# ══════════════════════════════════════════════════════════════════════════════════════════

class FakeBot:
    def __init__(self):
        self.sent = []  # [(chat_id, text)]

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    async def get_chat_member(self, *a, **k):
        raise RuntimeError("должно быть недостижимо для vol_-ветки")


class _FakeUser:
    def __init__(self, uid, username=None, full_name=None):
        self.id = uid
        self.username = username
        self.full_name = full_name


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeMessage:
    def __init__(self, uid, username=None, full_name=None):
        self.from_user = _FakeUser(uid, username, full_name)
        self.chat = _FakeChat(uid)
        self.answers = []

    async def answer(self, text, *a, **k):
        self.answers.append(text)

    async def answer_photo(self, *a, **k):
        self.answers.append("<photo>")


class _FakeCommand:
    def __init__(self, args):
        self.args = args


def _new_state(uid):
    return _fresh_state(uid)


def test_extract_volunteer_invite_code():
    assert reg._extract_volunteer_invite_code("vol_abc123") == "abc123"
    assert reg._extract_volunteer_invite_code("vol_") is None
    assert reg._extract_volunteer_invite_code(None) is None
    assert reg._extract_volunteer_invite_code("src_abc") is None
    assert reg._extract_volunteer_invite_code("123456") is None


def test_cmd_start_grants_volunteer_role_and_stops_before_registration_flow(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("volunteer_invite_enabled", "on"))
    _run(db.create_volunteer_invite("c1", None, ADMIN_ID, None, "2026-10-04", None))
    bot = FakeBot()
    msg = _FakeMessage(VOLUNTEER_ID, username="vol1", full_name="Волонтёр Один")
    _run(reg.cmd_start(msg, _new_state(VOLUNTEER_ID), bot=bot, command=_FakeCommand("vol_c1")))

    roles = _run(db.get_staff_roles(VOLUNTEER_ID))
    assert roles == ["volunteer"]
    staff = _run(db.list_staff())
    assert staff[0]["expires_at"] == "2026-10-04"
    # welcome text + шпаргалка волонтёра -- ДВА сообщения, ни одного текста анкеты/меню.
    assert any("волонт" in a.lower() or "welcome" in a.lower() or a for a in msg.answers)


def test_cmd_start_notifies_invite_creator(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("volunteer_invite_enabled", "on"))
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, 30))
    bot = FakeBot()
    msg = _FakeMessage(VOLUNTEER_ID, username="vol1", full_name="Волонтёр Один")
    _run(reg.cmd_start(msg, _new_state(VOLUNTEER_ID), bot=bot, command=_FakeCommand("vol_c1")))

    assert any(chat_id == ADMIN_ID for chat_id, _text in bot.sent)
    creator_msgs = [t for cid, t in bot.sent if cid == ADMIN_ID]
    assert any("1 из 30" in t for t in creator_msgs)


def test_cmd_start_toggle_off_dead_link_falls_through_to_normal_start(tmp_path):
    """«Выключен -> старые ссылки не срабатывают», и /start продолжает обычный путь."""
    _ready(tmp_path)
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, 30))
    # volunteer_invite_enabled НЕ включён (дефолт off).
    bot = FakeBot()
    msg = _FakeMessage(VOLUNTEER_ID)
    _run(reg.cmd_start(msg, _new_state(VOLUNTEER_ID), bot=bot, command=_FakeCommand("vol_c1")))

    assert _run(db.get_staff_roles(VOLUNTEER_ID)) == []
    inv = _run(db.get_volunteer_invite("c1"))
    assert inv["used"] == 0  # тумблер выключен -> слот не тронут
    # /start продолжает обычный путь -- делегат видит что-то (обычное приветствие), не падает.
    assert msg.answers


def test_cmd_start_dead_link_shows_expired_text_and_continues(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("volunteer_invite_enabled", "on"))
    _run(db.set_setting(
        "volunteer_invite_link_expired_text", "Ссылка устарела, попроси у организатора новую.",
    ))
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, "2000-01-01", None, None))
    bot = FakeBot()
    msg = _FakeMessage(VOLUNTEER_ID)
    _run(reg.cmd_start(msg, _new_state(VOLUNTEER_ID), bot=bot, command=_FakeCommand("vol_c1")))

    assert any("устарела" in a for a in msg.answers)
    assert _run(db.get_staff_roles(VOLUNTEER_ID)) == []
    assert len(msg.answers) > 1  # /start продолжает обычный путь дальше


def test_cmd_start_double_tap_grants_role_once_and_is_quiet_second_time(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("volunteer_invite_enabled", "on"))
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, None))
    bot = FakeBot()
    msg1 = _FakeMessage(VOLUNTEER_ID)
    _run(reg.cmd_start(msg1, _new_state(VOLUNTEER_ID), bot=bot, command=_FakeCommand("vol_c1")))
    first_count = len(msg1.answers)

    msg2 = _FakeMessage(VOLUNTEER_ID)
    _run(reg.cmd_start(msg2, _new_state(VOLUNTEER_ID), bot=bot, command=_FakeCommand("vol_c1")))

    assert _run(db.get_staff_roles(VOLUNTEER_ID)) == ["volunteer"]
    inv = _run(db.get_volunteer_invite("c1"))
    assert inv["used"] == 1
    assert first_count >= 1


def test_cmd_start_does_not_downgrade_existing_wider_access(tmp_path):
    """«Если у человека уже есть роль шире — не понижать (сообщить «у тебя уже есть доступ»)»."""
    _ready(tmp_path)
    _run(db.set_setting("volunteer_invite_enabled", "on"))
    _run(db.set_setting(
        "volunteer_invite_already_has_access_text", "У тебя уже есть доступ к боту как у менеджера.",
    ))
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    _run(db.create_volunteer_invite("c1", "spb", ADMIN_ID, None, None, None))
    bot = FakeBot()
    msg = _FakeMessage(MANAGER_ID)
    _run(reg.cmd_start(msg, _new_state(MANAGER_ID), bot=bot, command=_FakeCommand("vol_c1")))

    caps = _run(resolve_capabilities(MANAGER_ID))
    assert "moderate_reg" in caps  # не понижено
    assert "checkin" in caps  # дополнительно выдано (объединение ролей)
    assert any("уже есть доступ" in a for a in msg.answers)


# ══════════════════════════════════════════════════════════════════════════════════════════
# handlers/admin_volunteer_invite.py: экраны — чужой город менеджера, тумблер, мастер создания
# ══════════════════════════════════════════════════════════════════════════════════════════

def _two_cities():
    return [
        {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
        {"code": "spb", "label": "СПб", "tab_base": "", "enabled": 1, "sort_order": 1},
    ]


def test_manager_bound_to_city_cannot_open_other_citys_screen(tmp_path):
    _ready(tmp_path)
    saved = list(cities_mod.CITIES)
    try:
        cities_mod.set_cities_for_test(_two_cities())
        _run(db.set_setting("event_city_enabled", "on"))
        _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
        _run(db.set_staff_city(MANAGER_ID, "spb"))

        result, event = dispatch_callback("volinvite_cfg:msk", MANAGER_ID)
        assert event.answers  # denial toast ("Этот город правит суперадмин.")
        assert "правит суперадмин" in (event.answers[0][0] or "")
    finally:
        cities_mod.set_cities_for_test(saved)


def test_manager_bound_to_own_city_can_open_own_screen(tmp_path):
    _ready(tmp_path)
    saved = list(cities_mod.CITIES)
    try:
        cities_mod.set_cities_for_test(_two_cities())
        _run(db.set_setting("event_city_enabled", "on"))
        _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
        _run(db.set_staff_city(MANAGER_ID, "spb"))

        result, event = dispatch_callback("volinvite_cfg:spb", MANAGER_ID)
        text, _parse_mode, _kb = event.message.answers[-1]
        assert "СПб" in text
    finally:
        cities_mod.set_cities_for_test(saved)


def test_toggle_off_screen_has_no_create_button(tmp_path):
    _ready(tmp_path)
    result, event = dispatch_callback("volinvite_cfg:_all", ADMIN_ID)
    _text, _parse_mode, kb = event.message.answers[-1]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert not any(cb.startswith("volinvite_new:") for cb in cbs)


def test_toggle_on_screen_has_create_button(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("volunteer_invite_enabled", "on"))
    result, event = dispatch_callback("volinvite_cfg:_all", ADMIN_ID)
    _text, _parse_mode, kb = event.message.answers[-1]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(cb.startswith("volinvite_new:") for cb in cbs)


def test_volinvite_toggle_flips_setting(tmp_path):
    _ready(tmp_path)
    dispatch_callback("volinvite_toggle:_all", ADMIN_ID)
    from settings_schema import get_setting_typed
    assert _run(get_setting_typed("volunteer_invite_enabled")) == "on"
    dispatch_callback("volinvite_toggle:_all", ADMIN_ID)
    assert _run(get_setting_typed("volunteer_invite_enabled")) == "off"


# ── Мастер создания ссылки: три шага кнопками -> ссылка ────────────────────────────────────

def test_create_link_wizard_full_flow(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("volunteer_invite_enabled", "on"))
    state = _fresh_state(ADMIN_ID)

    dispatch_callback("volinvite_new:_all", ADMIN_ID, state=state)
    dispatch_callback("volinv_le:7", ADMIN_ID, state=state)
    dispatch_callback("volinv_re:none", ADMIN_ID, state=state)
    result, event = dispatch_callback("volinv_lim:30", ADMIN_ID, state=state)

    invites = _run(db.list_volunteer_invites())
    assert len(invites) == 1
    inv = invites[0]
    assert inv["max_uses"] == 30
    assert inv["rights_expires_at"] is None
    from services.staff_expiry import relative_days_iso
    assert inv["link_expires_at"] == relative_days_iso(7)
    # Ссылка показана менеджеру.
    assert any("?start=vol_" in a[0] for a in event.message.answers)


def test_create_link_wizard_custom_date_step(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("volunteer_invite_enabled", "on"))
    state = _fresh_state(ADMIN_ID)

    dispatch_callback("volinvite_new:_all", ADMIN_ID, state=state)
    dispatch_callback("volinv_le:custom", ADMIN_ID, state=state)
    dispatch_message("04.10.2026", ADMIN_ID, raw_state="VolunteerInviteWizard:waiting_link_date", state=state)
    dispatch_callback("volinv_re:forum", ADMIN_ID, state=state)  # без forum_date -> откажет
    dispatch_callback("volinv_lim:0", ADMIN_ID, state=state)

    invites = _run(db.list_volunteer_invites())
    assert invites[0]["link_expires_at"] == "2026-10-04"
    assert invites[0]["max_uses"] is None  # "без лимита"


def test_revoke_confirm_and_go(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("volunteer_invite_enabled", "on"))
    _run(db.create_volunteer_invite("c1", None, ADMIN_ID, None, None, None))

    dispatch_callback("volinv_revoke:c1", ADMIN_ID)
    dispatch_callback("volinv_revoke_go:c1", ADMIN_ID)

    inv = _run(db.get_volunteer_invite("c1"))
    assert inv["revoked"] == 1


def test_revoke_no_cancels_without_revoking(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("volunteer_invite_enabled", "on"))
    _run(db.create_volunteer_invite("c1", None, ADMIN_ID, None, None, None))

    dispatch_callback("volinv_revoke:c1", ADMIN_ID)
    dispatch_callback("volinv_revoke_no:c1", ADMIN_ID)

    inv = _run(db.get_volunteer_invite("c1"))
    assert inv["revoked"] == 0


def test_users_list_remove_button_removes_role(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("volunteer_invite_enabled", "on"))
    _run(db.create_volunteer_invite("c1", None, ADMIN_ID, None, None, None))
    _run(db.claim_volunteer_invite("c1", VOLUNTEER_ID))
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", None))

    result, event = dispatch_callback("volinv_users:c1", ADMIN_ID)
    text, _parse_mode, _kb = event.message.answers[-1]
    assert str(VOLUNTEER_ID) in text

    dispatch_callback(f"volinv_removeuser:c1:{VOLUNTEER_ID}", ADMIN_ID)
    assert _run(db.get_staff_roles(VOLUNTEER_ID)) == []


# ── Право экрана: держатель только checkin не может открыть приглашение ────────────────────

def test_volinvite_requires_moderate_reg_not_bare_checkin(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    result, event = dispatch_callback("volinvite_cfg:_all", VOLUNTEER_ID)
    assert event.answers  # denial toast, not the screen

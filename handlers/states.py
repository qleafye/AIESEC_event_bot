from aiogram.fsm.state import StatesGroup, State

class Registration(StatesGroup):
    full_name = State()
    age = State()
    email = State()
    phone = State()
    vk = State()                # ВК username (@...) — YL'26
    transport = State()         # трансфер до площадки / самостоятельно
    city = State()
    local_committee = State()
    position = State()
    informal_day = State()
    attendance_format = State()
    comments = State()
    source = State()
    education_status = State()
    university = State()
    course = State()
    specialty = State()
    work_status = State()
    work_sphere = State()
    missing_skills = State()
    expectations = State()
    department = State()
    aiesec_role = State()
    needs_certificate = State()
    alumni_status = State()     # аламни / айсекер / ни то, ни другое
    english_level = State()
    allergies = State()
    food_pref = State()
    arrival = State()
    housing = State()
    bed_sharing = State()       # готов делить двуспальную кровать (Да/Нет) — конфа
    bed_partner = State()       # с кем именно (условно, только при «Да»)
    cc_shop = State()
    exp_organizers = State()
    exp_content = State()
    volunteer = State()
    resume = State()
    confirm = State()
    # Phase 4 (CONS-02 / PAY-02/03): consent + payment steps
    consent_pending = State()   # waiting for «Принимаю» callback on a consent card
    # Phase 07.3 (04, RET-02): ждём тап ✅ Оставить / ✏️ Изменить на экране прошлого ответа.
    recall_pending = State()
    date_input = State()        # waiting for a ДД.ММ.ГГГГ date-type step answer
    receipt_upload = State()    # waiting for PDF document or photo receipt
    payment_option = State()    # IN-01: intentionally never set — process_payment_option runs
                                # without an FSMContext (see handlers/payment.py). Kept for clarity.
    # YL'26 reg-flow additions
    select_input = State()      # configurable single-select step (city / study_field / …)
    multi_input = State()       # configurable multi-select step (goal / formats)
    ambassador = State()        # ambassador yes/no question

class Approval(StatesGroup):
    reason = State()

class ReceiptReview(StatesGroup):
    reject_reason = State()   # admin types receipt rejection reason (mirrors Approval.reason)

class Question(StatesGroup):
    waiting_for_question = State()

class Broadcast(StatesGroup):
    target_selection = State()
    message = State()
    # Phase 3: scheduled broadcast (SCHED-01)
    schedule_when = State()
    schedule_message = State()
    # Phase 3: filtered broadcast builder (COMM-01/02/03)
    filter_field = State()
    filter_value = State()

class EditSetting(StatesGroup):
    waiting_for_value = State()
    waiting_for_photo = State()
    waiting_for_file = State()
    # Quick 260815-3hw (Task 3): confirm-gate before overwriting an EXISTING Google Sheets tab
    # (name collision on a tab the bot writes to) -- sheets_tab_confirm/sheets_tab_cancel.
    waiting_for_tab_confirm = State()
    # Quick 260822: «➕ Добавить пункт» списочной настройки -- одно сообщение = один пункт
    # (handlers/admin_settings_lists.py).
    waiting_for_list_item = State()

class StaffAdd(StatesGroup):
    # Phase 8 (ROLE-02, D-18): single-step wizard — one message resolves a person by
    # forwarded message / @username / numeric id, then role assignment is a callback.
    waiting_for_person = State()

class GameTaskCreate(StatesGroup):
    # Phase 9 (GAME-01/02/03, wave 2, 09-02): one State per task-creation wizard step.
    # category/proof_type are driven by inline buttons (not free text), but the state is
    # still set between steps — same «Отмена посреди визарда» guard every other wizard uses.
    # Quick 260819-gtl (CONTEXT.md decision 1/4): `title` is now the FIRST step (before
    # `text`); `photo` is a new optional step right after `text` ("⏭ Пропустить" skips it).
    title = State()
    text = State()
    photo = State()
    category = State()
    coins = State()
    proof_type = State()
    city = State()  # Phase 09.1 (B, GAME-06): "Кому задание?" — only when cities module is on
    deadline = State()
    confirm = State()

class GameTaskEdit(StatesGroup):
    # Quick 260819-gtl (CONTEXT.md decisions 1/4): point-edit of an EXISTING task's
    # title/photo — deliberately its own small StatesGroup, not GameTaskCreate reused, since
    # it edits ONE field at a time (no multi-step wizard) and needs its own «state:
    # GameTaskEdit:*» ADMIN_CAPS entry (moderate_game). Task id carried via state.get_data()
    # ("gte_task_id"), same idiom GameSubmit.proof uses for "gs_task_id".
    title = State()
    photo = State()
    # Phase 16 (16-03, GAME-UI-03): the remaining editable fields -- description / coins /
    # deadline, one field per state, same one-shot shape as title/photo above.
    text = State()
    coins = State()
    deadline = State()

class GameReview(StatesGroup):
    # Phase 9 (wave 4, 09-04): moderation — rejection reason (mirrors Approval.reason) and
    # an amount override on approve (A-04: «5 за каждый правильный ответ» tasks need this).
    reject_reason = State()
    approve_amount = State()

class GameSubmit(StatesGroup):
    # Phase 9 (wave 3, 09-03): delegate-facing submission wizard. Not under moderate_game —
    # CapabilityMiddleware only sits on admin.router, user_actions.router never sees it, so this
    # state was not part of 09-01's ADMIN_CAPS interface-first contract.
    proof = State()   # waiting for confirmation content; task_id carried via state.get_data()

class CoinsManual(StatesGroup):
    # Phase 14 (14-04, GAME-09): «🪙 Монеты вручную» button wizard — lives under moderate_game
    # (registered in handlers/admin_caps.py, "state:CoinsManual:*"), same as GameTaskCreate. The
    # confirm step is a callback (coinsman_confirm) reading state.get_data() directly, not a
    # fourth State — same shape GameTaskCreate.confirm's own confirm callback (gtconfirm) uses.
    person = State()  # waiting for a forwarded message / @username to resolve the recipient
    amount = State()  # waiting for the coin amount (sign already picked via coinsman_sign:*)
    reason = State()  # waiting for the mandatory reason text

class CityForm(StatesGroup):
    # Phase 14 (14-07, CITY-07): city registry wizard — lives under `settings`
    # (registered in handlers/admin_caps.py, "state:CityForm:*"). Add-wizard is two steps
    # (label -> tab base); the two edit flows are one field each. The city CODE is never a
    # state itself — on add it's generated server-side (cities.make_city_code); on edit it's
    # carried in state.get_data()["city_code"], set by the callback that opened the step.
    add_label = State()  # waiting for the new city's human-facing label
    add_tab = State()    # waiting for the new city's sheet-tab base name (or "—")
    edit_label = State()  # waiting for a replacement label for an existing city
    edit_tab = State()    # waiting for a replacement tab base for an existing city

class SeasonReset(StatesGroup):
    # Phase 07.3 (02, RET-01): «🔄 Новый сезон» wizard — lives under `settings`, but is
    # ADDITIONALLY gated to superadmin (`config.ADMIN_IDS`) inside every handler, same shape
    # as roles_city_start's re-check. Confirm-with-numbers stays a callback reading
    # state.get_data() (season_reset_go), not a third State — same CoinsManual precedent
    # CityForm above already documents.
    naming = State()      # waiting for the new season's name; also re-rendered after the
                           # numbers-confirm screen is shown (a tap, not text, advances further)
    passphrase = State()  # waiting for the typed confirmation phrase (old season name, or
                           # "НОВЫЙ СЕЗОН" literal if there was no old season)

class SeasonImport(StatesGroup):
    # Phase 07.3 (06, RET-04): «📥 Импорт прошлого события» wizard — lives under `settings`
    # (registered in handlers/admin_caps.py, "state:SeasonImport:*"). No third confirm State —
    # same CoinsManual/CityForm precedent: the confirm step is a callback reading
    # state.get_data() (season_import_go), not a State.
    waiting_file = State()  # waiting for a document (the foreign forum.db)
    naming = State()        # waiting for the season name to stamp imported rows with


class PollCreate(StatesGroup):
    # «📊 Опросы» → «➕ Новый опрос» (handlers/admin_poll_wizard.py), право `broadcast`
    # ("state:PollCreate:*" в handlers/admin_caps.py). Тумблеры/аудитория/подтверждение —
    # кнопки, но стейт между шагами стоит: тот же guard «Отмена посреди мастера».
    question = State()       # текст вопроса (≤300 символов)
    options = State()        # варианты по одному сообщением (или через «;»), 2–10, ≤100 символов
    settings = State()       # тумблеры «анонимный» / «несколько вариантов»
    audience = State()       # кому: все / одобренные / город / трек
    confirm = State()        # превью прислано, ждём «отправить сейчас» / «запланировать»
    schedule_when = State()  # дата-время ДД.ММ.ГГГГ ЧЧ:ММ

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

class StaffAdd(StatesGroup):
    # Phase 8 (ROLE-02, D-18): single-step wizard — one message resolves a person by
    # forwarded message / @username / numeric id, then role assignment is a callback.
    waiting_for_person = State()

class GameTaskCreate(StatesGroup):
    # Phase 9 (GAME-01/02/03, wave 2, 09-02): one State per task-creation wizard step.
    # category/proof_type are driven by inline buttons (not free text), but the state is
    # still set between steps — same «Отмена посреди визарда» guard every other wizard uses.
    text = State()
    category = State()
    coins = State()
    proof_type = State()
    city = State()  # Phase 09.1 (B, GAME-06): "Кому задание?" — only when cities module is on
    deadline = State()
    confirm = State()

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

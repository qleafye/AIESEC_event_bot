from aiogram.fsm.state import StatesGroup, State

class Registration(StatesGroup):
    admin_rereg = State()
    full_name = State()
    age = State()
    email = State()
    phone = State()
    vk = State()                # ВК username (@...) — YL'26
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
    english_level = State()
    allergies = State()
    food_pref = State()
    arrival = State()
    housing = State()
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
    payment_option = State()    # waiting for user to pick a payment option
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

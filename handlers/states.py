from aiogram.fsm.state import StatesGroup, State

class Registration(StatesGroup):
    admin_rereg = State()
    full_name = State()
    age = State()
    email = State()
    phone = State()
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

class Approval(StatesGroup):
    reason = State()

class Question(StatesGroup):
    waiting_for_question = State()

class Broadcast(StatesGroup):
    target_selection = State()
    message = State()

class EditSetting(StatesGroup):
    waiting_for_value = State()
    waiting_for_photo = State()
    waiting_for_file = State()

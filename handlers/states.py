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
    expectations_ar = State()
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
    resume = State()
    confirm = State()

class Question(StatesGroup):
    waiting_for_question = State()

class Broadcast(StatesGroup):
    target_selection = State()
    message = State()

class EditSetting(StatesGroup):
    waiting_for_value = State()
    waiting_for_photo = State()
    waiting_for_file = State()

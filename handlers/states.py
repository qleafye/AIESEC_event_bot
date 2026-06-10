from aiogram.fsm.state import StatesGroup, State

class Registration(StatesGroup):
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

class Question(StatesGroup):
    waiting_for_question = State()

class Broadcast(StatesGroup):
    target_selection = State()
    message = State()

class EditSetting(StatesGroup):
    waiting_for_value = State()
    waiting_for_photo = State()
    waiting_for_file = State()

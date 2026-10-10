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
    # Phase 28 (28-02, SU-01/SU-04, СкиллАп 5): пять новых текстовых шагов анкеты — свои
    # State нужны только им; stack/experience/readiness типа multi/select и обслуживаются
    # select_input/multi_input выше (RESEARCH Pattern 1, п.6). Показ и приём — в шве
    # handlers/reg/reg_extra_steps.py.
    resume_link = State()       # развилка резюме R2b: ссылка (SU-04); обработчик — план 28-04
    mini_projects = State()     # мини-профиль R2c, подшаг 1/3: над какими проектами работал(а)
    mini_portfolio = State()    # мини-профиль R2c, подшаг 2/3: портфолио/GitHub (можно пропустить)
    mini_direction = State()    # мини-профиль R2c, подшаг 3/3: желаемое направление развития
    case_optin = State()        # участие в кейс-чемпионате (Да/Нет), с пояснением менеджера
    # Phase 30 (30-06, A2-05): чат-проекция типа `repeatable` (handlers/reg/reg_types_repeatable.py)
    # — ОДНО состояние на весь цикл «Название? -> Опиши коротко -> Добавить ещё?», стадия блока
    # хранится в данных FSM (`_repeat_stage`), не отдельным State на под-вопрос (30-06-PLAN.md
    # <action>). Не переиспользует `Registration.mini_portfolio` — то состояние уже занято
    # приёмным хендлером `handlers/reg/reg_extra_steps.py::process_mini_portfolio`, зарегистрированным
    # РАНЬШЕ по порядку импорта хвоста `registration.py` (aiogram матчит по порядку регистрации).
    mini_portfolio_repeat = State()

class Approval(StatesGroup):
    reason = State()

class QuestionAnswer(StatesGroup):
    # Quick 260904-2cj: ответ на вопрос делегата прямо из экрана «❓ Вопросы делегатов»
    # (handlers/comms/admin_questions.py), право `moderate_reg` ("state:QuestionAnswer:*" в
    # handlers/access/admin_caps.py). Qid/user_id — в state.get_data() (aq_qid/aq_user_id), тот же
    # приём, что GameTaskEdit несёт task id.
    text = State()

class ReceiptReview(StatesGroup):
    reject_reason = State()   # admin types receipt rejection reason (mirrors Approval.reason)

class Question(StatesGroup):
    waiting_for_question = State()

class FaqItem(StatesGroup):
    # Quick 260906-8uq (FAQ-01..06): один текстовый шаг несёт ТРИ разных сценария — мастер
    # «➕ Добавить» (faq_mode="new", faq_step="question"/"answer", вопрос копится в
    # faq_question до второго сообщения), правку существующего пункта (faq_mode="edit",
    # faq_id/faq_field в state.get_data()) и (задача 4, FAQ-04) правку ЧЕРНОВИКА «❓ В FAQ» из
    # журнала вопросов (faq_mode="draft", faq_qid/faq_draft_q/faq_draft_a/faq_field —
    # faq_qid — id ИСХОДНОГО вопроса журнала, не пункта FAQ, отличает «draft» от «edit», у
    # которого вместо этого faq_id пункта) — форма QuestionAnswer, право `moderate_reg`
    # ("state:FaqItem:*" в handlers/access/admin_caps.py).
    text = State()

class LookupAdmin(StatesGroup):
    # Phase 30 (30-07, A2-03): поиск канoники при слиянии «Другое»/закреплении чипа на экране
    # «📚 Справочники» (handlers/admin_lookup.py) — заведён здесь (не локально в шве), т.к.
    # `tests/test_roles_phase8.py::_message_keys_from_line` резолвит "state:X:*" ТОЛЬКО для
    # групп, живущих в этом модуле (`hasattr(states_mod, group_name)`); "state:LookupAdmin:*"
    # в handlers/access/admin_caps.py.
    search = State()

class Broadcast(StatesGroup):
    target_selection = State()
    message = State()
    # Quick 260910-okb (BC-01/02/03): превью+подтверждение перед немедленной рассылкой —
    # process_broadcast больше не шлёт напрямую из Broadcast.message, а копит FSM и переводит
    # сюда; "state:Broadcast:*" в handlers/access/admin_caps.py уже покрывает новое состояние.
    confirm = State()
    # Phase 3: scheduled broadcast (SCHED-01)
    schedule_when = State()
    schedule_message = State()
    # Форум-ночь п.7: экран подтверждения (тумблер «❗ Важное») перед созданием отложенной
    # рассылки — "state:Broadcast:*" в handlers/access/admin_caps.py уже покрывает новое состояние.
    schedule_confirm = State()
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
    # Подтверждение «Пропала подстановка — сохранить всё равно?»: менеджер убрал из текста
    # скобки {…}, которые бот подставляет сам (callback'и phchk_*).
    waiting_for_placeholder_confirm = State()

class SettingsSearch(StatesGroup):
    # «🔎 Найти настройку» (handlers/admin_settings_search.py): менеджер пишет слово.
    waiting_query = State()

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
    # Phase 32 (32-12, D-12/D-28): «Волна» и «Аудитория» — оба шага кнопочные (как city выше),
    # это состояние только паркует визард между сообщением-подсказкой и тапом по кнопке.
    wave = State()
    audience = State()
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
    # (registered in handlers/access/admin_caps.py, "state:CoinsManual:*"), same as GameTaskCreate. The
    # confirm step is a callback (coinsman_confirm) reading state.get_data() directly, not a
    # fourth State — same shape GameTaskCreate.confirm's own confirm callback (gtconfirm) uses.
    person = State()  # waiting for a forwarded message / @username to resolve the recipient
    amount = State()  # waiting for the coin amount (sign already picked via coinsman_sign:*)
    reason = State()  # waiting for the mandatory reason text

class CityForm(StatesGroup):
    # Phase 14 (14-07, CITY-07): city registry wizard — lives under `settings`
    # (registered in handlers/access/admin_caps.py, "state:CityForm:*"). Add-wizard is two steps
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
    # (registered in handlers/access/admin_caps.py, "state:SeasonImport:*"). No third confirm State —
    # same CoinsManual/CityForm precedent: the confirm step is a callback reading
    # state.get_data() (season_import_go), not a State.
    waiting_file = State()  # waiting for a document (the foreign forum.db)
    naming = State()        # waiting for the season name to stamp imported rows with


class PollCreate(StatesGroup):
    # «📊 Опросы» → «➕ Новый опрос» (handlers/comms/admin_poll_wizard.py), право `broadcast`
    # ("state:PollCreate:*" в handlers/access/admin_caps.py). Тумблеры/аудитория/подтверждение —
    # кнопки, но стейт между шагами стоит: тот же guard «Отмена посреди мастера».
    question = State()       # текст вопроса (≤300 символов)
    options = State()        # варианты по одному сообщением (или через «;»), 2–10, ≤100 символов
    settings = State()       # тумблеры «анонимный» / «несколько вариантов»
    audience = State()       # кому: все / одобренные / город / трек
    confirm = State()        # превью прислано, ждём «отправить сейчас» / «запланировать»
    schedule_when = State()  # дата-время ДД.ММ.ГГГГ ЧЧ:ММ


class MiniAppTheme(StatesGroup):
    # Phase 19 (08, D-06) + Phase 19.1 (07, D-20): экраны «🎨 Оформление» / «🎭 Пресеты и ручки»
    # Mini App (handlers/admin_miniapp.py + handlers/admin_miniapp_theme.py), право `settings`
    # ("state:MiniAppTheme:*" в handlers/access/admin_caps.py). Своя маленькая группа, а не
    # переиспользование EditSetting -- та же причина, что у GameTaskEdit: у экранов свой экран
    # возврата, и каждое поле правится по одному за раз без общего wizard'а.
    logo = State()             # лого мероприятия (светлая тема), фото
    color = State()            # HEX одной из трёх цветовых ручек (handle -- в
                                # state.get_data()["miniapp_theme_color_handle"]: accent/secondary/bg)
    logo_dark = State()        # лого для тёмной темы, фото (необязательно)
    cover = State()            # обложка приложения, фото
    cover_dark = State()       # обложка для тёмной темы, фото (необязательно)
    sticker_empty = State()    # стикер «пусто», фото
    sticker_success = State()  # стикер «успех», фото
    sticker_error = State()    # стикер «ошибка», фото
    sticker_top1 = State()     # стикер «топ-1», фото
    coin_icon = State()        # своя иконка монеты, фото (необязательно)
    pattern = State()          # паттерн плиты, фото (необязательно)


class RejectRuleEdit(StatesGroup):
    # Phase 31 (31-08, D-09/D-21): экран карточки правила (handlers/applications/admin_reject_rules.py) —
    # правка имени и текста отказа. Правило id — в state.get_data() ("rre_rule_id"), тот же
    # приём, что FaqItem несёт item id. Право "settings" ("state:RejectRuleEdit:*" в
    # handlers/access/admin_caps.py).
    name = State()
    text = State()


class RejectCond(StatesGroup):
    # Phase 31 (31-10, D-01): конструктор условия правила (handlers/applications/admin_reject_cond.py) —
    # состояние ставится ТОЛЬКО на шаге ввода числа/даты текстом (arc_num); выбор вопроса/
    # оператора/значений — чистые callback'и без ожидания сообщения, id правила/группы/шаг/
    # оператор/отмеченные индексы живут в state.get_data() (arc_rule/arc_group/arc_step/
    # arc_op/arc_checked/arc_voff), тот же приём, что RejectRuleEdit несёт rre_rule_id. Право
    # "settings" (state:RejectCond:* в handlers/access/admin_caps.py).
    num = State()


class WaveCreate(StatesGroup):
    # Phase 32 (32-10, D-06/D-10/D-11): визард создания волны (handlers/game/admin_game_waves.py)
    # — даты (одной строкой через «;» или по одной), необязательный вводный текст, карточка
    # подтверждения. Право `moderate_game` ("state:WaveCreate:*" в handlers/access/admin_caps.py).
    # Тот же визард переиспользует «📋 Скопировать эту волну» (даты запрашиваются тем же
    # шагом, дальше идёт copy_wave вместо create_wave) — различает флаг wc_copy_src в
    # state.get_data().
    dates = State()
    intro = State()
    confirm = State()


class WaveEdit(StatesGroup):
    # Phase 32 (32-10): точечная правка ОДНОГО поля существующей волны с карточки — даты/
    # вводный текст/число призовых мест, одно поле за раз (тот же приём, что GameTaskEdit).
    # Волна id — в state.get_data() ("we_wave_id"), право `moderate_game`
    # ("state:WaveEdit:*" в handlers/access/admin_caps.py).
    dates = State()
    intro_text = State()
    prize_places = State()


class AdminI18nEdit(StatesGroup):
    # Phase 27 (27-06, LANG-05/LANG-09): экран «🌐 Английские тексты» (handlers/i18n/admin_i18n.py)
    # — ручная правка одного английского текста. Одно состояние: цель правки (какую строку
    # (src_hash/src_text/origin_key) и куда вернуться после сохранения) целиком живёт в
    # state.get_data() (i18n_hash/i18n_src_text/i18n_origin_key/i18n_return), как у CoinsManual.
    text = State()


class CheckinImport(StatesGroup):
    # Phase 12 (FORUM-CHECKIN.md, D-09/D-10): загрузка выгрузки офлайн-приложения-сканера
    # (handlers/forum/admin_checkin.py) — один шаг ожидания файла; выбор точки («🚪 Вход») — кнопка
    # без текстового ввода, второго State не заводим (то же решение, что у CoinsManual/
    # CityForm: подтверждение — callback, читающий state.get_data(), а не отдельный State).
    waiting_file = State()


class CheckinTestUpload(StatesGroup):
    # Форум-ночь B4 (идея №8): «🧪 Проверить приложение-сканер» — та же форма ожидания файла,
    # что CheckinImport, но СВОЁ состояние: этот путь НИЧЕГО не отмечает (только парсит и
    # отвечает читаемостью), путать его с настоящей загрузкой (CheckinImport.waiting_file,
    # которая ведёт к реальным отметкам) нельзя даже по ошибке одного и того же State.
    waiting_file = State()


class CheckinQrTimeEdit(StatesGroup):
    # Форум-ночь п.3 (D-03, идея №2): ввод «ЧЧ:ММ» для вечерней рассылки/утреннего повтора QR
    # (handlers/forum/admin_checkin.py) — какое именно время правим (checkin_qr_broadcast_time /
    # checkin_qr_morning_repeat_time) и для какого города живёт в state.get_data(), тот же
    # приём, что AdminI18nEdit/CoinsManual (одно состояние, цель правки в данных, не в State).
    waiting_value = State()


class CheckinVolGuideTimeEdit(StatesGroup):
    # D-36 (24.09, аудит форумных тумблеров): ввод «ЧЧ:ММ» для шпаргалки волонтёра накануне
    # форума (handlers/forum/admin_forum_functions.py, checkin_volunteer_guide_broadcast_time) — тот
    # же приём, что CheckinQrTimeEdit выше, но один временной слот, не два (город — в
    # state.get_data()).
    waiting_value = State()


class ForumDayMenuTimeEdit(StatesGroup):
    # Идея №1 бэклога чек-ина (режим «день форума»): ввод «ЧЧ:ММ» для времени начала режима
    # вечером накануне форума (handlers/forum/admin_forum_functions.py,
    # forum_day_menu_start_time) — тот же приём, что CheckinVolGuideTimeEdit выше, один
    # временной слот, город — в state.get_data().
    waiting_value = State()


class ForumDayReportTimeEdit(StatesGroup):
    # Идея №16 бэклога чек-ина (отчёт дня форума вечером): ввод «ЧЧ:ММ» для времени ежедневной
    # отправки (handlers/forum/admin_forum_functions.py, forum_day_report_time) — тот же приём, что
    # CheckinVolGuideTimeEdit/ForumDayMenuTimeEdit выше, один временной слот, город — в
    # state.get_data().
    waiting_value = State()


class ForumNoshowPollTimeEdit(StatesGroup):
    # Идея №23 бэклога чек-ина (опрос неявившихся): ввод «ЧЧ:ММ» для времени отправки на
    # следующий день после форума (handlers/forum/admin_forum_functions.py, forum_noshow_poll_time) —
    # тот же приём, что ForumDayReportTimeEdit выше.
    waiting_value = State()


class RegionalNoshowMoveTimeEdit(StatesGroup):
    # Трек «региональные форумы → Москва»: ввод «ЧЧ:ММ» для времени отправки предложения
    # переноса на следующий день после форума (handlers/forum/admin_forum_functions.py,
    # regional_noshow_offer_time) — тот же приём, что ForumNoshowPollTimeEdit выше.
    waiting_value = State()


class ForumNoshowPollOther(StatesGroup):
    # Идея №23 бэклога чек-ина: делегат нажал «Другое» на опросе неявившихся — ждём свободный
    # текст следующим сообщением (handlers/forum/forum_noshow_poll.py). Право не нужно (delegate-side,
    # вне CapabilityMiddleware — тот же прецедент, что SosReport/SessionFeedbackComment выше).
    # Отмена — следующий /start (cmd_start чистит FSM, см. докстринг SosReport выше), своего
    # Command("cancel")/«Отмена»-хендлера не заводим (тот же приём, что SessionFeedbackComment).
    waiting = State()


class ProgramSessionField(StatesGroup):
    # Форум-ночь п.4 (расписание форума в боте, handlers/forum/admin_program.py) — ввод ОДНОГО
    # текстового поля сессии программы: и мастер создания идёт по этим же состояниям шаг за
    # шагом, и точечная правка карточки существующей сессии заходит в нужное состояние
    # напрямую. Режим (создание/правка), город/день/id сессии/какое поле правится — целиком в
    # state.get_data() (тот же приём, что AdminI18nEdit/CoinsManual/RejectRuleEdit), выбор зала
    # и подтверждение конфликта — отдельные callback'и без ожидания текста, своего State не
    # заводят (решение, что и у CheckinImport про точку — см. её докстринг).
    time = State()
    title = State()
    speaker = State()
    description = State()


class ProgramHallName(StatesGroup):
    # Имя зала — и создание (на лету во время мастера сессии, и отдельно с экрана «Залы»), и
    # переименование существующего; контекст (город/id зала/куда вернуться) — в state.get_data().
    value = State()


class ProgramDayCustom(StatesGroup):
    # «📅 Другой день» — ввод даты текстом («31.10»/«31.10.2026»); город — в state.get_data().
    value = State()


class ProgramTrackEdit(StatesGroup):
    # Название трека сессий (создание и переименование); контекст — в state.get_data().
    name = State()


class ProgramCompetencyEdit(StatesGroup):
    # Название компетенции теста (создание и переименование); контекст — в state.get_data().
    name = State()


class ProgramEnrollLimit(StatesGroup):
    # Лимит мест на сессию текстом (число; «-» — без лимита); сессия — в state.get_data().
    value = State()


class QuizEdit(StatesGroup):
    # Правка текста вопроса/варианта/уровня теста; что именно правится — в state.get_data().
    value = State()


class QuizImport(StatesGroup):
    # Ожидание файла с вопросами теста (импорт из таблицы).
    waiting_file = State()


class SosReport(StatesGroup):
    # D-31 (24.09, «SOS без категорий»): «🆘 SOS» создаёт заявку и публикует карточку МГНОВЕННО
    # (handlers/forum/sos.py::sos_start), без вопроса «что случилось» — категорийный визард (details/
    # location/followup как отдельные шаги) снесён целиком. Единственное состояние —
    # `collecting` («дописываю SOS»): ЛЮБОЕ сообщение делегата (текст/фото/геопозиция) уходит в
    # тред карточки И дописывает саму карточку первым текстом/фото
    # (`database.db.add_sos_details`/`set_sos_location`). Живёт до «Готово»
    # (`i18n_ui_en.DONE_WORDS`), решения заявки оргом, таймаута
    # (`sos_collecting_timeout_minutes`, проверяется на каждом входящем сообщении) или /start.
    # Право не нужно (user_actions.router вне CapabilityMiddleware, тот же прецедент, что
    # GameSubmit.proof) — id заявки/город/момент входа несёт state.get_data()
    # (sos_collecting_report_id/sos_collecting_city/sos_collecting_started).
    collecting = State()


class SosChatBind(StatesGroup):
    # Экран менеджера «🆘 SOS» (handlers/forum/admin_sos.py), право `settings` ("state:SosChatBind:*"
    # в handlers/access/admin_caps.py — та же капа, что у остальной интеграционной привязки чата,
    # services/chat_tracking.py::is_bot_admin_user). Заявка (кто просил, для какого города)
    # живёт в `sos_chat_bind_pending` (services/sos.py), не в state.get_data() — вторая ветка
    # подтверждения (команда `/sos_id` в самой группе) физически не имеет доступа к этому FSM.
    waiting = State()


class SessionFeedbackComment(StatesGroup):
    # Форум-ночь п.9 (идея №15, D-24): «✍️ Написать» под приглашением оценить сессию
    # (handlers/forum/session_feedback.py) — ОДНО состояние ожидания текста, session_id несёт
    # state.get_data() (sfb_session_id). Право не нужно (delegate-side, вне
    # CapabilityMiddleware — тот же прецедент, что SosReport выше).
    waiting = State()


class VenueRevokeFind(StatesGroup):
    # Идея №32 (снятие отметки менеджером, handlers/forum/admin_venue.py): одно ожидание текста —
    # фамилия/@username делегата; дальше выбор человека и отметки идёт кнопками.
    waiting_query = State()


class RolesExpiryEdit(StatesGroup):
    # Идея №6 бэклога чек-ина (общая механика прав со сроком действия): «✏️ Ввести дату» на
    # экране «⏳ Срок действия роли» (handlers/access/admin_roles.py) — ОДНО состояние ожидания
    # «ДД.ММ.ГГГГ», (tid, role) несёт state.get_data() (rexp_tid/rexp_role). Право `settings`
    # (state:RolesExpiryEdit:* в handlers/access/admin_caps.py — тот же экран, что «👥 Роли и доступы»).
    waiting_date = State()


class VolunteerInviteWizard(StatesGroup):
    # Идея №5 бэклога чек-ина (приглашение волонтёров ссылкой) — два места, где мастер ждёт
    # свободный текст вместо кнопки-пресета: срок ССЫЛКИ (waiting_link_date) и срок ПРАВ
    # волонтёра (waiting_rights_date). Весь остальной прогресс мастера (город, уже выбранный
    # срок ссылки, лимит) несёт state.get_data() — тот же приём, что RolesExpiryEdit выше.
    waiting_link_date = State()
    waiting_rights_date = State()


class LostFoundNew(StatesGroup):
    # Идея №20 бэклога чек-ина (бюро находок) — три шага мастера «🧳 Нашли вещь»
    # (handlers/forum/admin_lost_found.py): фото → «где нашли/куда подойти» → предпросмотр
    # («📢 Опубликовать»/«✖️ Отмена»). Город и черновик (photo/where) несут
    # state.get_data() — тот же приём, что RolesExpiryEdit/VolunteerInviteWizard выше.
    waiting_photo = State()
    waiting_where = State()
    preview = State()


class ResumeReplace(StatesGroup):
    # Phase 33 (delegate-card admin actions, задача 3): «📎 Заменить резюме» — менеджер шлёт
    # файл в ответ на запрос бота (handlers/applications/admin_resume_replace.py); telegram_id делегата
    # несёт state.get_data() — тот же приём, что у StaffAdd выше.
    waiting_for_file = State()


class ChatRatingEdit(StatesGroup):
    # Квик 260927: экран «🏆 Рейтинг чата» (handlers/chat/admin_chat_rating.py) — менеджер вводит
    # число правила города (или название баллов); ключ (per-city композит или общий) и город
    # экрана несёт state.get_data(). Свой стейт, а не EditSetting: после сохранения менеджер
    # возвращается на этот экран, а не на общий редактор настройки.
    waiting_for_value = State()


class ChatRatingPostEdit(StatesGroup):
    # Квик 260927: экран «📣 Публикация рейтинга в чат» (handlers/chat/admin_chat_rating_post.py) —
    # время, число мест, заголовки и подпись поста; поле и город экрана несёт state.get_data().
    waiting_for_value = State()


class ChatCleanupEdit(StatesGroup):
    # Квик 260927: экран «🧹 Служебные сообщения в чате» (handlers/chat/admin_chat_cleanup.py) —
    # менеджер вводит задержку удаления в секундах; после сохранения — обратно на экран.
    waiting_for_value = State()


class AmbExclude(StatesGroup):
    # Ступени амбассадоров СкиллАп (handlers/amb/admin_amb_tiers.py): ручное исключение
    # приглашённого из зачёта — кого (пересылка / @username / id), причина свободным текстом,
    # подтверждение кнопкой. Данные человека и причина живут в state.get_data().
    waiting_for_person = State()
    waiting_for_reason = State()
    waiting_for_confirm = State()


class AmbAppoint(StatesGroup):
    # «🙋 Кандидаты и команда» → «➕ Назначить амбассадором» (handlers/amb/admin_amb_bulk.py):
    # менеджер присылает @ник, ссылку t.me, Telegram ID или пересылку сообщения делегата;
    # дальше — подтверждение кнопкой.
    waiting_for_person = State()


class AmbTierRevoke(StatesGroup):
    # Лестница ступеней (handlers/amb/admin_amb_tier_ladder.py) - «Снять ступень»: кто (пересылка /
    # @username / id), затем ступень и подтверждение кнопками (waiting_for_pick).
    waiting_for_person = State()
    waiting_for_pick = State()


class AmbPointsEdit(StatesGroup):
    # «Амбассадоры» - «Баллы и приватность» (handlers/amb/admin_amb_points.py): число баллов за
    # одобренного приглашённого вводом.
    waiting_for_value = State()


class AmbAttach(StatesGroup):
    # «Амбассадоры» - «Закрепить приглашённого» (handlers/amb/admin_amb_journal.py): кто пришёл,
    # кто привёл, заметка («скрины в чате»), подтверждение кнопкой.
    waiting_invitee = State()
    waiting_referrer = State()
    waiting_note = State()
    waiting_confirm = State()


class AmbSlotsEdit(StatesGroup):
    # Экран «🤝 Амбассадоры → 🚪 Вход и лимит» (handlers/amb/admin_amb_section.py): менеджер
    # вводит число мест в команде амбассадоров (0 — без лимита); после сохранения — на экран.
    waiting_for_limit = State()


class ProgramPhotoUpload(StatesGroup):
    # Фото программы ДЛЯ ОДНОГО ГОРОДА (или общее) — handlers/forum/admin_program_view.py; город и
    # экран возврата — в state.get_data().
    waiting = State()


class SourceLinkCreate(StatesGroup):
    # «🔗 Ссылки с метками» → «➕ Новая ссылка» (handlers/applications/admin_source_links.py): название метки.
    waiting_for_tag = State()


class ExtFormConnect(StatesGroup):
    # «📝 Внешние формы» → «➕ Подключить форму» (handlers/ext_forms/admin_ext_forms_connect.py):
    # менеджер присылает ссылку на форму.
    link = State()
    # Личная форма (без организации): ссылка принята, ждём название для карточки
    # («📮 Это личная форма — ответы пришлёт интеграция»).
    push_title = State()


class ExtFormImport(StatesGroup):
    # «📝 Внешние формы» → карточка личной формы → «📥 Загрузить старые ответы»
    # (handlers/ext_forms/admin_ext_forms_push.py): менеджер присылает файл выгрузки XLSX или CSV.
    waiting_file = State()


class ExtFormOAuth(StatesGroup):
    # «🔑 Войти через Яндекс»: код подтверждения из браузера, затем id организации.
    code = State()
    org_id = State()


class DelegationEdit(StatesGroup):
    # Экран «🏫 Делегации» (handlers/delegations/admin_delegations.py): ввод даты отсечки ЦА и текстов
    # делегату своими кнопками экрана под правом модерации заявок (не через общий редактор
    # настроек). Ключ редактируемого текста — в state.get_data()["dlg_text_key"].
    waiting_cutoff = State()
    waiting_text = State()


class DelegationLink(StatesGroup):
    # «🏫 Делегации → ⏳ Не зашли → 🔗 Привязать вручную» (handlers/delegations/admin_delegations_review.py):
    # менеджер присылает @ник / Telegram ID / пересланное сообщение делегата, затем подтверждает
    # привязку. В state.get_data(): dlg_link_row (id строки ответа), dlg_link_tid (кого нашли).
    waiting_person = State()
    waiting_confirm = State()


class ExtFormAppKeys(StatesGroup):
    # «🔑 Ключи приложения Яндекса»: идентификатор и секрет приложения.
    client_id = State()
    client_secret = State()


class CoinsTransfer(StatesGroup):
    # «📥 Перенос баллов из таблицы»: ждём ссылку на Google-таблицу.
    link = State()


class BotAvatar(StatesGroup):
    # «🖼 Аватар бота» (handlers/admin_bot_avatar.py): ждём фото, затем «Поставить?».
    photo = State()
    confirm = State()


# Состояния делегата: анкета (в т.ч. вложенные шаги типов вопросов и короткая анкета на
# месте) и прочие ответы делегата боту. Вход в админку их НЕ сбрасывает: менеджер, который
# сам заполняет анкету, не должен терять её, нажав /admin. Имена — строками, а не классами:
# `_CompositeChat`/`_LookupChat`/`OnsiteReg` живут в своих швах, импорт отсюда дал бы цикл
# (сторож имён — tests/test_admin_flow_state_reset_261009.py).
DELEGATE_STATE_GROUPS: frozenset[str] = frozenset({
    "Registration", "_CompositeChat", "_LookupChat", "OnsiteReg",
    "Question", "GameSubmit", "SosReport", "SessionFeedbackComment", "ForumNoshowPollOther",
})


async def clear_admin_flow_state(state) -> bool:
    """Вход в админку (/admin, «⬅ Панель») снимает любое незаконченное админское ожидание
    ввода — иначе оно переживает уход с экрана, и следующее сообщение (фото для рассылки,
    текст) молча уходит в брошенный мастер. Состояния делегата не трогает. True — сбросили."""
    current = await state.get_state()
    if not current or current.split(":", 1)[0] in DELEGATE_STATE_GROUPS:
        return False
    await state.clear()
    return True


class SheetTarget(StatesGroup):
    # «📊 Данные → 🔗 Какая таблица» (handlers/sheets/admin_sheet_target.py): ждём ссылку на таблицу.
    # Право `settings` + суперадмин (`config.ADMIN_IDS`), перепроверяется в каждом хендлере.
    waiting_ref = State()


class ChatExportImport(StatesGroup):
    # «📥 Загрузить историю чата» (handlers/chat/admin_chat_import.py): сначала ждём файл result.json из
    # Telegram Desktop, затем — подтверждение предпросмотра кнопкой. Выбранный чат и разобранный
    # файл несёт state.get_data(); текстового ввода нет.
    waiting_file = State()
    confirm = State()

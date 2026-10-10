"""Сторож путей джоб APScheduler: модули, на функции которых ссылаются сохранённые джобы.

`SQLAlchemyJobStore` (data/jobs.sqlite) хранит цель джобы строкой `модуль:функция`. Если
такой модуль переехать или функцию переименовать, после рестарта бот не загрузит
сохранённые джобы (отложенные рассылки, напоминания об оплате, волны, чек-ин…). Поэтому
эти модули лежат строго по своим путям в `services/`, а раскладка по доменам их обходит.

Второй тест держит список полным: любая новая `add_job(<функция модуля>, …)` в коде должна
попасть сюда, иначе её модуль могут однажды перенести.
"""
import ast

from tests._paths import REPO_ROOT

# модуль -> функции, которые бывают целями сохранённых джоб (включая старые цели, которые
# новые джобы уже не ставят, но которые могли остаться в jobs.sqlite на серверах).
JOB_ENTRY_MODULES = {
    "services.scheduler": {
        "send_scheduled_broadcast", "send_scheduled_poll", "send_payment_reminder",
        "send_wave_start", "send_task_deadline_reminder", "send_wave_end_ping",
        "send_wave_results", "sweep_payment_overdue", "miniapp_outbox_drain_job",
        "sheet_arrival_drain_job", "sheet_chat_drain_job", "ext_forms_pending_job",
        "ext_forms_reconcile_job", "ext_forms_google_job", "ext_forms_sheet_drain_job",
        "ext_forms_notify_job", "_amb_journal_reconcile_job", "translation_drain_job",
        "quiet_hours_flush_job", "chat_history_prune_job", "chat_cleanup_drain_job",
        "_reconcile_forum_report_and_poll_job", "nudge_incomplete_registrations",
        "allowlist_refresh_job",
        # Цели из таблицы `_setting_interval_jobs()` — передаются не по имени в add_job,
        # AST-проверка ниже их не видит, поэтому перечислены явно.
        "sync_incomplete_sheet_job", "sync_auto_reject_sheet_job", "resume_upload_retry_job",
        "chat_membership_refresh_job",
    },
    "services.daily_digest": {"daily_digest_job"},
    "services.checkin_broadcast": {"reconcile_forum_jobs", "_run_morning_job", "_run_evening_job"},
    "services.checkin_volunteer_broadcast": {"_run_job"},
    "services.chat_rating_post": {"run_job", "reconcile"},
    "services.chat_tracking": {"bind_reconcile_job"},
    "services.chat_cleanup": {"delete_service_message_job"},
    "services.regional_noshow_move": {"_run_job", "_notify_managers_job"},
    "services.forum_day_report": {"_run_job"},
    "services.forum_noshow_poll": {"_run_job"},
    "services.game_digest": {"send_game_digest"},
    "services.reg_digest": {"send_reg_digest"},
    "services.session_feedback": {"deliver_feedback_prompts"},
    "services.sos": {"delivery_retry_job", "escalation_job", "claimed_reminder_job"},
}

_SCHEDULE_CALLS = {"add_job", "_add_interval_job"}


def _module_path(module):
    return REPO_ROOT.joinpath(*module.split(".")).with_suffix(".py")


def _defined_functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_job_entry_modules_stay_in_place():
    problems = []
    for module, funcs in sorted(JOB_ENTRY_MODULES.items()):
        path = _module_path(module)
        if not path.exists():
            problems.append(f"{module}: файла {path.relative_to(REPO_ROOT).as_posix()} нет")
            continue
        missing = sorted(funcs - _defined_functions(path))
        if missing:
            problems.append(f"{module}: нет функций верхнего уровня {missing}")
    assert not problems, (
        "Сохранённые джобы APScheduler ссылаются на `модуль:функция` — переносить или "
        f"переименовывать их нельзя, иначе после рестарта джобы не загрузятся: {problems}"
    )


def test_every_scheduled_module_function_is_listed():
    unlisted = []
    for path in sorted(REPO_ROOT.glob("services/**/*.py")) + sorted(REPO_ROOT.glob("handlers/**/*.py")):
        rel = path.relative_to(REPO_ROOT).with_suffix("")
        module = ".".join(rel.parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top_funcs = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in _SCHEDULE_CALLS:
                continue
            target = node.args[0]
            if isinstance(target, ast.Name) and target.id in top_funcs:
                if target.id not in JOB_ENTRY_MODULES.get(module, set()):
                    unlisted.append(f"{module}:{target.id}")
    assert not unlisted, (
        "Новая цель джобы APScheduler — допишите её в JOB_ENTRY_MODULES, чтобы модуль не "
        f"перенесли: {sorted(set(unlisted))}"
    )

"""Движок теста компетенций. Общий: не знает конкретных вопросов — весь контент (вопросы,
варианты, баллы по компетенциям, уровни) лежит в БД (`database.quiz_db`). Вопрос — один выбор
из вариантов. Модуль не зависит от aiogram: UI (админка, делегатский поток) строится поверх.

Формат итога (JSON в quiz_attempts.scores_json):
`{"<competency_id>": {"points": int, "max": int, "level_id": int | None}}`.
"""
from __future__ import annotations

from database import quiz_db, session_enroll_db

# Тексты теста, которые менеджер правит в разделе настроек (без quiz_points_max — это число).
QUIZ_TEXT_KEYS = (
    "quiz_menu_label",
    "quiz_start_button",
    "quiz_question_header",
    "quiz_resume_text",
    "quiz_restarted_text",
    "quiz_result_header",
    "quiz_result_line",
    "quiz_no_level_label",
    "quiz_choose_sessions_button",
    "quiz_my_result_button",
    "quiz_retake_button",
    "quiz_disabled_text",
    "quiz_not_approved_text",
    "quiz_no_result_text",
)


# ── Чистый подсчёт ───────────────────────────────────────────────────────────────────────────

def max_points_by_competency(options_by_question: dict[int, list[dict]]) -> dict[int, int]:
    """Максимум по компетенции: сумма по вопросам наибольших баллов среди вариантов.
    Компетенции с нулевым максимумом в результат не попадают."""
    total: dict[int, int] = {}
    for options in options_by_question.values():
        best: dict[int, int] = {}
        for opt in options:
            for cid, pts in (opt.get("points") or {}).items():
                best[int(cid)] = max(best.get(int(cid), 0), int(pts))
        for cid, pts in best.items():
            total[cid] = total.get(cid, 0) + pts
    return {cid: pts for cid, pts in total.items() if pts > 0}


def chosen_points(answers: dict[int, int], options_by_question: dict[int, list[dict]]) -> dict[int, int]:
    """Сумма баллов выбранных вариантов по компетенциям. Ответ на вариант, которого нет
    (удалён или чужой), пропускается."""
    total: dict[int, int] = {}
    for qid, oid in answers.items():
        for opt in options_by_question.get(int(qid), []):
            if opt["id"] == int(oid):
                for cid, pts in (opt.get("points") or {}).items():
                    total[int(cid)] = total.get(int(cid), 0) + int(pts)
                break
    return total


def level_for(chosen: int, max_: int, levels: list[dict], mode: str) -> dict | None:
    """Уровень с наибольшим порогом, который набран. percent: chosen*100 >= threshold*max
    (целые числа, без деления); points: chosen >= threshold. Нет максимума или ниже всех
    порогов — без уровня."""
    if max_ <= 0:
        return None
    best = None
    for level in levels:
        threshold = int(level["threshold"])
        if mode == "points":
            reached = chosen >= threshold
        else:
            reached = chosen * 100 >= threshold * max_
        if reached and (best is None or threshold > int(best["threshold"])):
            best = level
    return best


def build_scores(
    answers: dict[int, int],
    options_by_question: dict[int, list[dict]],
    levels: list[dict],
    mode: str,
    competency_ids: set[int] | None = None,
) -> dict[str, dict]:
    maxes = max_points_by_competency(options_by_question)
    chosen = chosen_points(answers, options_by_question)
    scores: dict[str, dict] = {}
    for cid, max_ in maxes.items():
        if competency_ids is not None and cid not in competency_ids:
            continue
        points = chosen.get(cid, 0)
        level = level_for(points, max_, levels, mode)
        scores[str(cid)] = {
            "points": points, "max": max_, "level_id": level["id"] if level else None,
        }
    return scores


# ── Попытки ──────────────────────────────────────────────────────────────────────────────────

async def start_or_resume(telegram_id: int, quiz: dict) -> tuple[dict | None, str]:
    """Статусы: "resumed" (продолжение с места), "restarted" (тест обновили — незавершённая
    попытка сброшена), "finished" (уже пройден, пересдача выключена), "new"."""
    quiz_id, version = quiz["id"], quiz["content_version"]
    open_attempt = await quiz_db.get_open_attempt(telegram_id, quiz_id)
    if open_attempt:
        if open_attempt["content_version"] == version:
            return open_attempt, "resumed"
        await quiz_db.delete_open_attempts(telegram_id, quiz_id)
        attempt_id = await quiz_db.create_attempt(telegram_id, quiz_id, version)
        return await quiz_db.get_attempt(attempt_id), "restarted"
    if not quiz.get("allow_retake"):
        finished = await quiz_db.get_last_finished_attempt(telegram_id, quiz_id)
        if finished:
            return finished, "finished"
    attempt_id = await quiz_db.create_attempt(telegram_id, quiz_id, version)
    return await quiz_db.get_attempt(attempt_id), "new"


async def current_question(attempt: dict) -> tuple[dict, list[dict], int, int] | None:
    """(вопрос, варианты, номер, всего) — первый по порядку без ответа; None — все отвечены."""
    questions = await quiz_db.list_questions(attempt["quiz_id"])
    answers = attempt.get("answers") or {}
    for index, question in enumerate(questions, start=1):
        if question["id"] in answers:
            continue
        options = await quiz_db.list_options(question["id"])
        if options:  # вопрос без вариантов не показываем: ответить на него нельзя
            return question, options, index, len(questions)
    return None


async def answer(telegram_id: int, attempt_id: int, question_id: int, option_id: int) -> str:
    """"ok" | "done" | "not_owner" | "finished" | "bad_option" | "stale"."""
    attempt = await quiz_db.get_attempt(attempt_id)
    if not attempt or attempt["telegram_id"] != int(telegram_id):
        return "not_owner"
    if attempt["finished_at"]:
        return "finished"
    quiz_id = attempt["quiz_id"]
    current_quiz = await quiz_db.get_quiz(quiz_id)
    if not current_quiz or attempt["content_version"] != current_quiz["content_version"]:
        return "restarted"  # вопросы или баллы поменяли — попытка по смеси версий недопустима
    options_by_question = await quiz_db.list_options_for_quiz(quiz_id)
    if not any(o["id"] == int(option_id) for o in options_by_question.get(int(question_id), [])):
        return "bad_option"
    current = await current_question(attempt)
    if current is None or current[0]["id"] != int(question_id):
        return "stale"
    if not await quiz_db.record_answer(attempt_id, question_id, option_id):
        return "finished"
    attempt = await quiz_db.get_attempt(attempt_id)
    if await current_question(attempt) is not None:
        return "ok"
    quiz = await quiz_db.get_quiz(quiz_id)
    competencies = await session_enroll_db.list_competencies(quiz["city"])
    scores = build_scores(
        attempt["answers"], options_by_question, await quiz_db.list_levels(quiz_id),
        quiz["score_mode"], {c["id"] for c in competencies},
    )
    await quiz_db.finish_attempt(attempt_id, scores)
    return "done"


def _current_level(item: dict, levels: list[dict], quiz: dict) -> dict | None:
    """Уровень по СОХРАНЁННЫМ баллам и максимуму, но по ТЕКУЩИМ порогам и режиму теста: правка
    порогов менеджером применяется и к уже прошедшим. Нет сохранённых баллов — старый level_id."""
    if "points" in item and "max" in item:
        return level_for(int(item["points"]), int(item["max"]), levels, quiz["score_mode"])
    return next((lv for lv in levels if lv["id"] == item.get("level_id")), None)


async def result_lines(telegram_id: int, quiz: dict) -> list[dict] | None:
    """[{competency, level_name, level_description}] по последней законченной попытке; None —
    результата нет."""
    attempt = await quiz_db.get_last_finished_attempt(telegram_id, quiz["id"])
    if not attempt:
        return None
    scores = attempt.get("scores") or {}
    level_list = await quiz_db.list_levels(quiz["id"])
    lines = []
    for comp in await session_enroll_db.list_competencies(quiz["city"]):
        item = scores.get(comp["id"])
        if not isinstance(item, dict):
            continue
        level = _current_level(item, level_list, quiz)
        lines.append({
            "competency": comp["name"],
            "level_name": level["name"] if level else None,
            "level_description": (level.get("description") or "") if level else "",
        })
    return lines


async def retake(telegram_id: int, quiz: dict) -> dict | None:
    """Новая попытка после законченной — только если пересдача разрешена."""
    if not quiz.get("allow_retake"):
        return None
    await quiz_db.delete_open_attempts(telegram_id, quiz["id"])
    attempt_id = await quiz_db.create_attempt(telegram_id, quiz["id"], quiz["content_version"])
    return await quiz_db.get_attempt(attempt_id)


async def stats(quiz: dict) -> dict:
    """{started, finished, by_competency: {название: {уровень | None: людей}}}."""
    counts = await quiz_db.attempt_counts(quiz["id"])
    level_list = await quiz_db.list_levels(quiz["id"])
    names = {c["id"]: c["name"] for c in await session_enroll_db.list_competencies(quiz["city"])}
    by_comp: dict[str, dict] = {}
    for row in await quiz_db.list_finished_scores(quiz["id"]):
        for cid, item in row["scores"].items():
            if cid not in names or not isinstance(item, dict):
                continue
            bucket = by_comp.setdefault(names[cid], {})
            level = _current_level(item, level_list, quiz)
            level_name = level["name"] if level else None
            bucket[level_name] = bucket.get(level_name, 0) + 1
    return {"started": counts["started"], "finished": counts["finished"], "by_competency": by_comp}

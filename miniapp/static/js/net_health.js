// Индикатор связи сканера (идея №11): по времени ответа отметок решает, показывать ли
// волонтёру полосу «сеть медленная — переходите на приложение-сканер». Чистая логика без DOM —
// поведенческий тест гоняет её в node (tests/test_miniapp_net_health_js_260925.py).
//
// Плохой запрос — дольше SLOW_MS, ошибка сети (fetch упал до HTTP-статуса) или 5xx. Хороший —
// успешный быстрее FAST_MS. Промежуточный (2–5 с) обрывает обе серии: полоса не мигает на
// пограничной сети. STREAK плохих подряд — полоса видна; STREAK хороших подряд — скрыта.

export const SLOW_MS = 5000;
export const FAST_MS = 2000;
export const STREAK = 3;

export function isBadStatus(err) {
  if (!err) return false;
  if (typeof err.status !== "number") return true; // сеть не ответила вовсе
  return err.status >= 500;
}

export function createNetHealth({ slowMs = SLOW_MS, fastMs = FAST_MS, streak = STREAK } = {}) {
  let bad = 0;
  let good = 0;
  let degraded = false;

  function note(kind) {
    if (kind === "bad") { bad += 1; good = 0; }
    else if (kind === "good") { good += 1; bad = 0; }
    else { bad = 0; good = 0; }
    if (!degraded && bad >= streak) degraded = true;
    else if (degraded && good >= streak) degraded = false;
    return degraded;
  }

  // ms — время ответа; err — ошибка запроса (null — успех; 4xx — сервер ответил, судим по
  // времени). Возвращает «сеть плохая?».
  function record(ms, err = null) {
    if (err && isBadStatus(err)) return note("bad");
    if (ms > slowMs) return note("bad");
    if (ms < fastMs) return note("good");
    return note("mid");
  }

  return { record, isDegraded: () => degraded };
}

// Замер одного запроса: запрос, висящий дольше slowMs, засчитывается плохим сразу (на мёртвой
// сети fetch может не вернуться десятки секунд), а его поздний ответ уже не считается второй раз.
export async function timed(health, fn, onChange, { slowMs = SLOW_MS, now = () => Date.now() } = {}) {
  const started = now();
  let counted = false;
  const timer = setTimeout(() => {
    counted = true;
    onChange(health.record(slowMs + 1));
  }, slowMs + 1);
  try {
    const res = await fn();
    clearTimeout(timer);
    if (!counted) onChange(health.record(now() - started));
    return res;
  } catch (err) {
    clearTimeout(timer);
    if (!counted) onChange(health.record(now() - started, err));
    throw err;
  }
}

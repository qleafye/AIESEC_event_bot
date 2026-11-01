// Когда камера остаётся открытой после скана. Чистая функция — тест гоняет её в node
// (tests/test_scanner_continuous_261101.py).
//
// Попап Telegram закрывает экран, а не сам колбэк. Закрывать его на каждом «уже был»
// и на чужой картинке заставляло заново жать «Сканировать». Камера остаётся, пока
// волонтёру не нужно что-то нажать на плашке: запись на сессию, пропуск на месте,
// отметка вопреки дню, настоящий отказ. Обратная связь без закрытия — вибрация.

const CONTINUE_STATUSES = new Set(["new", "moved", "duplicate", "foreign_event", "shop"]);

export function scanContinues(res, { training = false, offersEnroll } = {}) {
  if (!res || res.status === "error") return false;
  if (typeof offersEnroll === "function" && offersEnroll(res, { training })) return false;
  if (res.onsite_approve || res.onsite_override || res.onsite_register || res.day_override) return false;
  return CONTINUE_STATUSES.has(res.status);
}

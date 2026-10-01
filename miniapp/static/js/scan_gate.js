// Приёмник текстов QR для непрерывного сканера. Чистая логика без DOM — поведенческий тест
// гоняет её в node (tests/test_miniapp_scan_gate_js_261001.py).
//
// Камера в непрерывном режиме присылает один и тот же текст много раз за секунду, а следующий
// делегат может оказаться в кадре, пока ответ на предыдущий скан ещё в пути. Поэтому:
// - повтор ТОГО ЖЕ текста глушится на guardMs (считая от конца его отправки — медленный ответ
//   не превращает повтор в «уже был»);
// - пока идёт отправка, ДРУГОЙ текст не выбрасывается, а встаёт в короткую очередь (без
//   повторов) и уходит сразу после ответа. Занятость проверяется ДО запоминания текста:
//   иначе выброшенный текст попадал под глушилку и делегат проходил неотмеченным;
// - submit(text) должен завершаться по ответу на сам скан — счётчики грузятся без ожидания.

export const RESCAN_GUARD_MS = 3000;
export const QUEUE_LIMIT = 5;

export function createScanGate({ submit, guardMs = RESCAN_GUARD_MS, queueLimit = QUEUE_LIMIT, now = () => Date.now() }) {
  let inFlight = null; // текст, который сейчас отправляется
  const queue = [];
  let lastText = null;
  let lastAt = 0;

  function recentlyDone(text) {
    return text === lastText && now() - lastAt < guardMs;
  }

  async function run(text) {
    inFlight = text;
    lastText = text;
    lastAt = now();
    try {
      await submit(text);
    } catch (err) {
      // ошибку показывает сам submit; очередь не должна вставать
    } finally {
      lastText = text;
      lastAt = now();
      inFlight = null;
    }
    while (queue.length) {
      const next = queue.shift();
      if (!recentlyDone(next)) { run(next); return; }
    }
  }

  // Колбэк камеры. Возвращает false — попап закрывает сам экран, когда придёт ответ.
  function onText(text) {
    if (!text) return false;
    if (text === inFlight || queue.includes(text)) return false;
    if (inFlight !== null) {
      if (queue.length < queueLimit) queue.push(text);
      return false;
    }
    if (recentlyDone(text)) return false;
    run(text);
    return false;
  }

  return { onText, isBusy: () => inFlight !== null, pending: () => queue.slice() };
}

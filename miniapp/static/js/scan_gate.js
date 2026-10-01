// Приёмник текстов QR для непрерывного сканера. Чистая логика без DOM — поведенческий тест
// гоняет её в node (tests/test_miniapp_scan_gate_js_261001.py).
//
// Камера в непрерывном режиме присылает один и тот же текст много раз за секунду, а следующий
// делегат может оказаться в кадре, пока ответ на предыдущий скан ещё в пути. Поэтому:
// - каждый отправленный текст глушится на guardMs (считая от конца его отправки — медленный
//   ответ не превращает повтор в «уже был»). Помним ВСЕ недавние тексты, а не только последний:
//   два QR, чередующиеся в кадре, иначе уходили бы по второму разу и давали «Уже был»;
// - пока идёт отправка, ДРУГОЙ текст не выбрасывается, а встаёт в короткую очередь (без
//   повторов) и уходит сразу после ответа. Занятость проверяется ДО запоминания текста:
//   иначе выброшенный текст попадал под глушилку и делегат проходил неотмеченным;
// - submit(text) должен завершаться по ответу на сам скан — счётчики грузятся без ожидания;
// - submit вернул HOLD (🔴/🟡/таймаут — волонтёр должен увидеть отказ) — очередь встаёт на
//   паузу до resume() (явное «Сканировать дальше»/«Сканировать»). Иначе зелёная плашка
//   следующего из очереди затирала красную через доли секунды, и отказанного пропускали.

export const RESCAN_GUARD_MS = 3000;
export const QUEUE_LIMIT = 5;
export const HOLD = "hold";

export function createScanGate({ submit, guardMs = RESCAN_GUARD_MS, queueLimit = QUEUE_LIMIT, now = () => Date.now() }) {
  let inFlight = null; // текст, который сейчас отправляется
  let held = false; // пауза после не-🟢 исхода — до resume()
  const queue = [];
  const recent = new Map(); // текст → момент, до которого его повтор глушится

  function remember(text) {
    const t = now();
    for (const [k, until] of recent) if (until <= t) recent.delete(k);
    recent.set(text, t + guardMs);
  }

  function recentlyDone(text) {
    const until = recent.get(text);
    return until !== undefined && now() < until;
  }

  function drain() {
    while (!held && inFlight === null && queue.length) {
      const next = queue.shift();
      if (!recentlyDone(next)) { run(next); return; }
    }
  }

  async function run(text) {
    inFlight = text;
    remember(text);
    let outcome;
    try {
      outcome = await submit(text);
    } catch (err) {
      // ошибку показывает сам submit; очередь не должна вставать
    } finally {
      remember(text);
      inFlight = null;
    }
    if (outcome === HOLD) held = true;
    drain();
  }

  // Колбэк камеры. Возвращает false — попап закрывает сам экран, когда придёт ответ.
  function onText(text) {
    if (!text) return false;
    if (text === inFlight || queue.includes(text)) return false;
    if (recentlyDone(text)) return false;
    if (inFlight !== null || held) {
      if (queue.length < queueLimit) queue.push(text);
      return false;
    }
    run(text);
    return false;
  }

  // Волонтёр увидел отказ и сам продолжил — очередь уходит дальше.
  function resume() {
    held = false;
    drain();
  }

  return {
    onText,
    resume,
    isBusy: () => inFlight !== null,
    isHeld: () => held,
    pending: () => queue.slice(),
  };
}

// Кнопка «Записать / Перезаписать на эту» на плашке сканера (точка-сессия). Зависимости
// передаются из screens/scanner.js — модуль гоняется в node без DOM (tests/test_miniapp_scanner_enroll_js_38.py).
// Сервер сам решает, показывать ли кнопку: в ответе скана есть `enroll` только когда запись
// модуля включена и делегат стоит не на этой сессии.

export function enrollButton(res, { h, api, say, failureText, refreshCounters, timeoutMs }) {
  const btn = h("button", { class: "btn secondary", type: "button", text: res.enroll.label });
  btn.addEventListener("click", async () => {
    if (btn.hasAttribute("disabled")) return;
    btn.setAttribute("disabled", "");
    const fallback = res.enroll.error || ""; // текст приходит с сервера (реестр настроек)
    try {
      const out = await api("/checkin/enroll", {
        method: "POST", timeoutMs,
        body: { telegram_id: res.telegram_id, session_id: res.enroll.session_id },
      });
      if (out && out.status === "ok") {
        say(out.message, "ok");
        refreshCounters();
        return;
      }
      btn.removeAttribute("disabled");
      say((out && out.message) || fallback, "warn");
    } catch (err) {
      btn.removeAttribute("disabled");
      say(failureText(err, fallback), "warn");
    }
  });
  return btn;
}

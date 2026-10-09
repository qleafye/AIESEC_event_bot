// Слово валюты после числа в согласии с ним: подпись «баллов» из настроек → «1 балл», «2 балла».
// Узнаёт три русских слова (балл, монета, коин) и английские points/coins; своё слово менеджера
// («лайков») возвращается как есть — склонять незнакомое слово наугад нельзя. Отдельный модуль,
// а не ui.js: в ui.js русских литералов нет (сторож tests/test_miniapp_error_states_js_260911.py).
// Поведенческий тест — tests/test_ui_unit_for_js_261010.py.
const RU_FORMS = [
  ["балл", "балла", "баллов"], ["монета", "монеты", "монет"], ["коин", "коина", "коинов"],
];
const EN_FORMS = { point: ["point", "points"], points: ["point", "points"], coin: ["coin", "coins"], coins: ["coin", "coins"] };

function ruForm(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

export function unitFor(n, unit) {
  const word = String(unit || "");
  const num = Math.abs(Number(n) || 0);
  const forms = RU_FORMS.find((f) => f.includes(word.toLowerCase()));
  if (forms) return ruForm(Math.trunc(num), ...forms);
  const en = EN_FORMS[word.toLowerCase()];
  if (en) return num === 1 ? en[0] : en[1];
  return word;
}

# -*- coding: utf-8 -*-
"""Generate application-flow-bpmn.svg (README «Путь заявки») — vertical swimlanes, hand-placed layout.

Run: python docs/diagrams/gen_application_flow_bpmn.py
Mermaid cannot draw real BPMN lanes (subgraph `direction` is ignored once edges cross lanes),
so the diagram is plain SVG with deterministic coordinates. Edit rows/columns below and re-run.
"""
import html, os
W, H = 900, 1780
LANES = [("Делегат", 40, 300), ("Бот", 300, 680), ("Менеджер", 680, 900)]
FONT = "font-family='-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif'"
out = []
A = out.append
A(f"<svg xmlns='http://www.w3.org/2000/svg' width='{W}' height='{H}' viewBox='0 0 {W} {H}' {FONT} font-size='12'>")
A("""<defs>
<marker id='arr' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='8' markerHeight='8' orient='auto-start-reverse'><path d='M0,0 L10,5 L0,10 z' fill='#374151'/></marker>
<style>
 .lane{fill:#f9fafb;stroke:#9ca3af;stroke-width:1}
 .lane2{fill:#f3f4f6}
 .hdr{fill:#e5e7eb;stroke:#9ca3af}
 .task{fill:#ffffff;stroke:#374151;stroke-width:1.2}
 .bot{fill:#eef2ff;stroke:#4338ca;stroke-width:1.2}
 .gw{fill:#ffffff;stroke:#b45309;stroke-width:1.4}
 .ev{fill:#ffffff;stroke:#111827;stroke-width:1.4}
 .end{fill:#ffffff;stroke:#111827;stroke-width:3}
 .db{fill:#eef2ff;stroke:#4338ca;stroke-width:1.2}
 .bg{fill:#f5f3ff;stroke:#7c3aed;stroke-width:1.2;stroke-dasharray:5 3}
 .e{fill:none;stroke:#374151;stroke-width:1.3;marker-end:url(#arr)}
 .ed{fill:none;stroke:#7c3aed;stroke-width:1.3;stroke-dasharray:5 3;marker-end:url(#arr)}
 .t{fill:#111827;text-anchor:middle;dominant-baseline:middle}
 .h{fill:#111827;text-anchor:middle;dominant-baseline:middle;font-weight:600;font-size:14px}
 .lbl{fill:#374151;font-size:11px;text-anchor:middle;dominant-baseline:middle}
 .gwt{fill:#111827;font-size:10px;text-anchor:middle;dominant-baseline:middle}
 .lblbg{fill:#ffffff;stroke:none}
 .note{fill:#4b5563;font-size:11px}
</style></defs>""")
A(f"<rect x='0' y='0' width='{W}' height='{H}' fill='#ffffff'/>")
for i, (name, x0, x1) in enumerate(LANES):
    A(f"<rect class='lane{' lane2' if i % 2 else ''}' x='{x0}' y='40' width='{x1-x0}' height='{H-40}'/>")
    A(f"<rect class='hdr' x='{x0}' y='0' width='{x1-x0}' height='40'/>")
    A(f"<text class='h' x='{(x0+x1)/2}' y='20'>{name}</text>")
A(f"<rect class='hdr' x='0' y='0' width='40' height='{H}'/>")
A(f"<text class='h' transform='translate(20,{H/2}) rotate(-90)'>Путь заявки: от /start до подтверждённой оплаты</text>")


def text(x, y, lines, cls="t"):
    n = len(lines); y0 = y - (n - 1) * 7.5
    for k, l in enumerate(lines):
        A(f"<text class='{cls}' x='{x}' y='{y0 + k*15}'>{html.escape(l)}</text>")


def box(x, y, lines, cls="task", w=170, h=44, rx=6):
    A(f"<rect class='{cls}' x='{x-w/2}' y='{y-h/2}' width='{w}' height='{h}' rx='{rx}'/>"); text(x, y, lines)


def stadium(x, y, lines, cls="ev", w=170, h=40):
    box(x, y, lines, cls, w, h, rx=20)


def gw(x, y, lines, s=None):
    # Подпись живёт внутри ромба: никаких белых подложек, иначе они стирают его грани.
    # Отсюда и ограничение на текст — две строки по 9 символов при GS=42.
    s = s or GS
    A(f"<polygon class='gw' points='{x},{y-s} {x+s},{y} {x},{y+s} {x-s},{y}'/>")
    text(x, y, lines, cls="gwt")


def db(x, y, lines, w=170, h=48):
    A(f"<path class='db' d='M{x-w/2},{y-h/2+7} a{w/2},7 0 0 1 {w},0 v{h-14} a{w/2},7 0 0 1 -{w},0 z'/>"
      f"<ellipse class='db' cx='{x}' cy='{y-h/2+7}' rx='{w/2}' ry='7'/>"); text(x, y + 6, lines)


def edge(pts, label=None, cls="e", lx=None, ly=None, lw=None):
    d = "M" + " L".join(f"{p[0]},{p[1]}" for p in pts); A(f"<path class='{cls}' d='{d}'/>")
    if label:
        if lx is None:
            (x0, y0), (x1, y1) = pts[0], pts[1]; lx, ly = (x0 + x1) / 2, (y0 + y1) / 2
        lw = lw or (len(label) * 6.5 + 8)
        A(f"<rect class='lblbg' x='{lx-lw/2}' y='{ly-8}' width='{lw}' height='16'/>")
        A(f"<text class='lbl' x='{lx}' y='{ly}'>{html.escape(label)}</text>")


D, B, M = 170, 430, 790   # column centers
S = 580                    # bot side column
G = 60                     # left gutter for bypass edges
GS = 42                    # gateway half-size
r = [70 + 95 * i for i in range(18)]   # row centers

# --- nodes ---
stadium(D, r[0], ["/start по deep-link:", "город · метка кампании · реферал"], w=200, h=44)
gw(B, r[1], ["Предотбор", "включён?"])
gw(B, r[2], ["@username", "в списке?"])
box(B, r[3], ["Собирает шаги REG_FLOW из", "включённых вопросов и трека"], cls="bot", w=200)
box(D, r[4], ["Отвечает на вопросы анкеты", "трек · город · согласия · резюме"], w=200)
box(D, r[5], ["Подтверждает сводку ответов"], w=200)
db(B, r[5], ["SQLite", "users · reg_started · consents"], w=200)
gw(B, r[6], ["Модерация", "вручную?"])
box(S, r[6], ["Фон: строка → Google Sheets", "3 повтора 5/15/30 с, алерт"], cls="bg", w=170, h=44)
box(S, r[7], ["Уведомляет менеджеров", "с правом moderate_reg"], cls="bot", w=170)
gw(M, r[8], ["Карточка", "заявки"])
stadium(D, r[8], ["Отказ / отклонение", "с причиной"], cls="end", w=180)
box(B, r[9], ["status = approved", "+ статус в таблице (фон)"], cls="bot", w=200)
gw(B, r[10], ["Оплата", "включена?"])
box(D, r[11], ["Выбирает тариф"], w=200)
box(B, r[12], ["Реквизиты + напоминания", "T-3 / T-1 (APScheduler)"], cls="bot", w=200)
box(D, r[13], ["Загружает чек (PDF / фото)"], w=200)
box(B, r[14], ["payment_status = receipt_sent", "пинг менеджерам moderate_receipts"], cls="bot", w=220)
gw(M, r[14], ["Карточка", "чека"])
box(B, r[16], ["payment_status = paid", "напоминания T-3/T-1 отменены"], cls="bot", w=200)
stadium(D, r[17], ["Участник: меню, коины,", "рейтинг, вопросы организаторам"], cls="end", w=200, h=44)

# --- edges ---
edge([(D + 100, r[0]), (B, r[0]), (B, r[1] - GS)])
edge([(B, r[1] + GS), (B, r[2] - GS)], "да", lx=B + 16, ly=(r[1] + r[2]) / 2)
edge([(B - GS, r[1]), (B - 110, r[1]), (B - 110, r[3]), (B - 100, r[3])], "нет", lx=B - 110, ly=r[2] - 48)
edge([(B, r[2] + GS), (B, r[3] - 22)], "да")
edge([(B - GS, r[2]), (G, r[2]), (G, r[8]), (D - 90, r[8])], "нет", lx=G + 55, ly=r[2] - 10)
edge([(B, r[3] + 22), (B, r[4]), (D + 100, r[4])])
edge([(D, r[4] + 22), (D, r[5] - 22)])
edge([(D + 100, r[5]), (B - 100, r[5])])
edge([(B, r[5] + 24), (B, r[6] - GS)])
edge([(B + 100, r[5]), (S, r[5]), (S, r[6] - 22)], cls="ed")
edge([(B + GS, r[6]), (S - 100, r[6]), (S - 100, r[7]), (S - 85, r[7])], "да", lx=S - 100, ly=(r[6] + r[7]) / 2)
edge([(S + 85, r[7]), (M, r[7]), (M, r[8] - GS)])
edge([(B, r[6] + GS), (B, r[9] - 22)], "авто", lx=B + 18, ly=r[7])
edge([(M - GS, r[8]), (D + 90, r[8])], "отклонить", lx=560, ly=r[8] - 12)
edge([(M, r[8] + GS), (M, r[9]), (B + 100, r[9])], "одобрить", lx=M + 40, ly=r[8] + 60)
edge([(B, r[9] + 22), (B, r[10] - GS)])
edge([(B - GS, r[10]), (G, r[10]), (G, r[17]), (D - 100, r[17])], "нет", lx=G + 55, ly=r[10] - 10)
edge([(B, r[10] + GS), (B, r[11]), (D + 100, r[11])], "да", lx=B + 18, ly=r[10] + 58)
edge([(D, r[11] + 22), (D, r[12]), (B - 100, r[12])])
edge([(B, r[12] + 22), (B, r[13]), (D + 100, r[13])])
edge([(D, r[13] + 22), (D, r[14]), (B - 110, r[14])])
edge([(B + 110, r[14]), (M - GS, r[14])])
edge([(M, r[14] + GS), (M, r[15]), (G + 15, r[15]), (G + 15, r[13]), (D - 100, r[13])],
     "отклонён — загрузить снова", lx=M - 160, ly=r[15] - 12, lw=170)
edge([(M + GS, r[14]), (M + 75, r[14]), (M + 75, r[16]), (B + 100, r[16])], "подтвердить", lx=M + 30, ly=r[16] - 12)
edge([(B, r[16] + 22), (B, r[17]), (D + 100, r[17])])

# legend
A(f"<text class='note' x='310' y='{H-40}'>Ромб — развилка (гейт по настройке или решение менеджера); синие — автоматика бота;</text>")
A(f"<text class='note' x='310' y='{H-24}'>пунктир — фоновая задача, делегат её не ждёт; жирный контур — конец процесса.</text>")
A("</svg>")
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "application-flow-bpmn.svg")
open(p, "w", encoding="utf-8").write("\n".join(out))
print("written", p)

"""Diagnostic: why does the Google sheet show fewer columns than enabled questions?

Run on the SERVER (same .env / DB as the bot):

    python -m scripts.diag_sheet_columns

Prints, purely from the live DB + code:
  1) every registration question, whether it's ON, and whether it maps to a sheet column
  2) the exact header list active_sheet_headers() would write right now
  3) (if reachable) the header row currently in the Google sheet
No writes — read-only.
"""
import asyncio

from database.db import get_setting
from handlers.registration import (
    REG_FLOW,
    REG_DEFAULTS,
    SHEET_COLUMNS,
    _is_step_enabled,
    active_sheet_headers,
)


async def main():
    # Map gate setting_key -> sheet header(s), so we can see which enabled questions
    # actually produce a column.
    gate_to_headers: dict[str, list[str]] = {}
    for header, gate, _fn in SHEET_COLUMNS:
        if gate is not None:
            gate_to_headers.setdefault(gate, []).append(header)

    print("=== QUESTIONS (REG_FLOW) ===")
    print(f"{'setting_key':28} {'ON?':4} {'raw':6} {'-> sheet column(s)'}")
    on_count = 0
    on_without_column = []
    for _step, gate, *_ in REG_FLOW:
        raw = await get_setting(gate)
        on = await _is_step_enabled(gate)
        if on:
            on_count += 1
        cols = gate_to_headers.get(gate, [])
        if on and not cols:
            on_without_column.append(gate)
        raw_disp = raw if raw is not None else f"({REG_DEFAULTS.get(gate,'on')})"
        print(f"{gate:28} {'ON' if on else 'off':4} {raw_disp:6} {', '.join(cols) or '— NO COLUMN'}")

    headers = await active_sheet_headers()
    print()
    print(f"Questions ON: {on_count}")
    print(f"ON but NO sheet column: {on_without_column or 'none'}")
    print()
    print(f"=== active_sheet_headers() -> {len(headers)} columns ===")
    for h in headers:
        print(f"  {h}")

    # Compare with the live sheet header, if Google is reachable.
    try:
        from services.sheets import _get_sheet
        live = [c.strip() for c in await asyncio.to_thread(lambda: _get_sheet().row_values(1))]
        print()
        print(f"=== live sheet header row -> {len(live)} columns ===")
        for h in live:
            print(f"  {h}")
        missing = [h for h in headers if h not in live]
        extra = [h for h in live if h not in headers]
        print()
        print(f"In active_headers but MISSING from sheet: {missing or 'none'}")
        print(f"In sheet but not in active_headers:       {extra or 'none'}")
    except Exception as e:
        print(f"\n(could not read live sheet header: {e})")


if __name__ == "__main__":
    asyncio.run(main())

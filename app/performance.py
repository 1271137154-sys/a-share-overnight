"""Next-day performance calculation. 9:35 requires minute bars and is intentionally None for daily-only phase one."""
import pandas as pd

def calculate_next_day_performance(selection_close: float, history: pd.DataFrame, selection_date: str) -> tuple[str, dict] | None:
    if history.empty or "date" not in history:
        return None
    rows = history.copy()
    rows["date"] = rows["date"].astype(str).str[:10]
    after = rows[rows["date"] > selection_date]
    if after.empty:
        return None
    next_day = after.iloc[0]
    return str(next_day["date"]), {
        "open_return": (float(next_day["open"]) / selection_close - 1) * 100,
        "high_return": (float(next_day["high"]) / selection_close - 1) * 100,
        "low_return": (float(next_day["low"]) / selection_close - 1) * 100,
        "close_return": (float(next_day["close"]) / selection_close - 1) * 100,
        "nine_thirty_five_return": None,
    }

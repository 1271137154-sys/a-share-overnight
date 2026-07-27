"""Transparent wide-screen diagnostics.  Missing data is never converted to zero."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import math
import pandas as pd

CURRENT_FIELDS = ("close", "high", "low", "volume", "amount", "turnover", "volume_ratio")


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _mainboard(row):
    return str(row.get("code", "")).zfill(6).startswith(("60", "00"))


def _current_non_st(row):
    return "ST" not in str(row.get("name", "")).upper()


def _history_before(history: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    if history is None or history.empty or "date" not in history:
        return pd.DataFrame()
    frame = history.copy()
    frame["date"] = frame["date"].astype(str).str[:10]
    return frame[frame["date"] < trade_date].sort_values("date")


def _limit_up_days(history: pd.DataFrame, trade_date: str):
    """Approximate normal-mainboard limits with unadjusted close prices.

    Historical ST status is not available from the free source, so this routine
    explicitly reports that limitation instead of pretending it has ST history.
    """
    prior = _history_before(history, trade_date)
    if len(prior) < 2 or "close" not in prior:
        return None, [], "historical_st_status_unavailable"
    closes = pd.to_numeric(prior["close"], errors="coerce").tolist()
    dates = prior["date"].tolist()
    hits = []
    for index in range(1, len(closes)):
        preclose, close = _number(closes[index - 1]), _number(closes[index])
        if preclose is None or close is None:
            continue
        limit_price = round(preclose * 1.10 + 1e-9, 2)
        if close >= limit_price - 0.011:
            hits.append({"date": dates[index], "preclose": preclose, "limit_price": limit_price, "close": close})
    recent = [item for item in hits if item["date"] in dates[-10:]]
    days_ago = None
    if recent:
        latest = recent[-1]["date"]
        days_ago = len(prior[prior["date"] >= latest])
    return days_ago, recent, "historical_st_status_unavailable"


def metrics_for(row: pd.Series, history: pd.DataFrame, trade_date: str) -> tuple[dict, list[str]]:
    missing = [field for field in CURRENT_FIELDS if _number(row.get(field)) is None]
    prior = _history_before(history, trade_date)
    if len(prior) < 10:
        missing.append("history_10_complete_trading_days")
    closes = pd.to_numeric(prior.get("close", pd.Series(dtype=float)), errors="coerce")
    volumes = pd.to_numeric(prior.get("volume", pd.Series(dtype=float)), errors="coerce")
    close = _number(row.get("close")); high = _number(row.get("high")); low = _number(row.get("low")); volume = _number(row.get("volume"))
    ma5 = ((sum(closes.tail(4)) + close) / 5) if close is not None and len(closes) >= 4 and closes.tail(4).notna().all() else None
    previous_ma5 = closes.tail(5).mean() if len(closes) >= 5 and closes.tail(5).notna().all() else None
    ma10 = ((sum(closes.tail(9)) + close) / 10) if close is not None and len(closes) >= 9 and closes.tail(9).notna().all() else None
    prior_volume_ma5 = volumes.tail(5).mean() if len(volumes) >= 5 and volumes.tail(5).notna().all() else None
    limit_days, limits, st_limit = _limit_up_days(history, trade_date)
    values = {
        "close": close, "high": high, "low": low, "volume": volume,
        "amount": _number(row.get("amount")), "turnover": _number(row.get("turnover")),
        "volume_ratio": _number(row.get("volume_ratio")), "pct_change": _number(row.get("pct_change")),
        "ma5": ma5, "ma10": ma10, "ma5_slope": (ma5 - previous_ma5) if ma5 is not None and previous_ma5 is not None else None,
        "close_high_ratio": (close / high) if close is not None and high not in (None, 0) else None,
        "return_5d": (close / closes.iloc[-5] - 1) * 100 if close is not None and len(closes) >= 5 and _number(closes.iloc[-5]) else None,
        "return_10d": (close / closes.iloc[-10] - 1) * 100 if close is not None and len(closes) >= 10 and _number(closes.iloc[-10]) else None,
        "prior5_average_volume": _number(prior_volume_ma5),
        "volume_ma5_multiple": (volume / prior_volume_ma5) if volume is not None and prior_volume_ma5 not in (None, 0) else None,
        "limit_up_days_ago": limit_days, "limit_ups": limits, "st_limit_status": st_limit,
        "history_last_date": prior["date"].iloc[-1] if len(prior) else None,
    }
    return values, sorted(set(missing))


def _condition(name, value, minimum=None, maximum=None):
    if value is None:
        return {"name": name, "status": "missing", "actual": None, "threshold": f"{minimum} 至 {maximum}"}
    passed = (minimum is None or value >= minimum) and (maximum is None or value <= maximum)
    return {"name": name, "status": "pass" if passed else "fail", "actual": value, "threshold": f"{minimum} 至 {maximum}"}


def conditions(strategy_id: str, row: pd.Series, m: dict, missing: list[str]):
    base = []
    if not _mainboard(row): base.append({"name":"沪深主板","status":"fail","actual":row.get("code"),"threshold":"60、00 开头"})
    else: base.append({"name":"沪深主板","status":"pass","actual":row.get("code"),"threshold":"60、00 开头"})
    base.append({"name":"非ST（当前）","status":"pass" if _current_non_st(row) else "fail","actual":row.get("name"),"threshold":"非ST"})
    base.append({"name":"完整行情","status":"missing" if missing else "pass","actual":", ".join(missing) if missing else "完整","threshold":"必需字段完整"})
    if strategy_id == "strong_close_momentum":
        base += [
            _condition("MA5向上", m["ma5_slope"], 0.0000001),
            _condition("收盘价大于MA5", None if m["close"] is None or m["ma5"] is None else m["close"]-m["ma5"], 0.0000001),
            _condition("今日涨幅", m["pct_change"], 3, 8), _condition("量比", m["volume_ratio"], 1, 4),
            _condition("换手率", m["turnover"], 3, 22), _condition("收盘价/最高价", m["close_high_ratio"], .97, None),
            _condition("近10日涨幅", m["return_10d"], -15, 40),
        ]
    elif strategy_id == "recent_limit_up_trend":
        base += [
            _condition("MA5大于MA10", None if m["ma5"] is None or m["ma10"] is None else m["ma5"]-m["ma10"], 0.0000001),
            _condition("MA5向上", m["ma5_slope"], 0.0000001), _condition("收盘价大于MA5", None if m["close"] is None or m["ma5"] is None else m["close"]-m["ma5"], 0.0000001),
            _condition("距今2至7日有涨停", m["limit_up_days_ago"], 2, 7), _condition("近5日涨幅", m["return_5d"], 0, 25),
            _condition("今日涨幅", m["pct_change"], 1.5, 7), _condition("量比", m["volume_ratio"], .8, 4), _condition("收盘价/最高价", m["close_high_ratio"], .97, None),
        ]
    else:
        base += [
            _condition("MA5大于MA10", None if m["ma5"] is None or m["ma10"] is None else m["ma5"]-m["ma10"], 0.0000001),
            _condition("MA5向上", m["ma5_slope"], 0.0000001), _condition("收盘价大于MA5", None if m["close"] is None or m["ma5"] is None else m["close"]-m["ma5"], 0.0000001),
            _condition("距今3至10日有涨停", m["limit_up_days_ago"], 3, 10), _condition("今日涨幅", m["pct_change"], 1, 7),
            _condition("成交量/前5日平均成交量", m["volume_ma5_multiple"], .8, 3), _condition("换手率", m["turnover"], 2, 18), _condition("收盘价/最高价", m["close_high_ratio"], .97, None),
        ]
    return base


def build_report(spot: pd.DataFrame, histories: dict[str, pd.DataFrame], trade_date: str, source_updated_at=None):
    strategy_ids = ["strong_close_momentum", "recent_limit_up_trend", "limit_up_reacceleration"]
    per_strategy = {key: {"funnel": [], "single_condition": {}, "real_failures": 0, "data_missing": 0, "matches": []} for key in strategy_ids}
    limits, stock_rows = [], {}
    baseline = {"all": len(spot), "mainboard": 0, "non_st": 0, "complete": 0}
    for _, row in spot.iterrows():
        code = str(row.get("code", "")).zfill(6); history = histories.get(code, pd.DataFrame())
        m, missing = metrics_for(row, history, trade_date); stock_rows[code] = {"code":code,"name":row.get("name"),"metrics":m,"missing":missing}
        if _mainboard(row): baseline["mainboard"] += 1
        else: continue
        if _current_non_st(row): baseline["non_st"] += 1
        else: continue
        if not missing: baseline["complete"] += 1
        limits.extend([dict(item, code=code, name=row.get("name")) for item in m["limit_ups"]])
        for strategy_id in strategy_ids:
            checks = conditions(strategy_id, row, m, missing)
            stock_rows[code].setdefault("strategies", {})[strategy_id] = checks
            if any(c["status"] == "missing" for c in checks): per_strategy[strategy_id]["data_missing"] += 1
            elif any(c["status"] == "fail" for c in checks): per_strategy[strategy_id]["real_failures"] += 1
            else: per_strategy[strategy_id]["matches"].append(code)
    for strategy_id, result in per_strategy.items():
        active = [item for item in stock_rows.values() if _mainboard(pd.Series({"code":item["code"]})) and _current_non_st(pd.Series({"name":item["name"]})) and not item["missing"]]
        count = len(active); result["funnel"] = [{"condition":"全部沪深A股","remaining":baseline["all"]},{"condition":"主板","remaining":baseline["mainboard"]},{"condition":"非ST","remaining":baseline["non_st"]},{"condition":"完整行情","remaining":baseline["complete"]}]
        for label_index, label in enumerate([c["name"] for c in conditions(strategy_id, pd.Series({"code":"600000","name":"示例"}), {k:None for k in ["ma5_slope","close","ma5","pct_change","volume_ratio","turnover","close_high_ratio","return_10d","ma10","limit_up_days_ago","return_5d","volume_ma5_multiple"]}, [])][3:]):
            passed = [item for item in active if all(c["status"] == "pass" for c in item["strategies"][strategy_id][3:3+label_index+1])]
            single = sum(1 for item in active if item["strategies"][strategy_id][3+label_index]["status"] == "pass")
            result["funnel"].append({"condition":label,"remaining":len(passed)})
            result["single_condition"][label] = single
    daily = Counter(item["date"] for item in limits)
    comparison_codes = ["603629", "605196", "600988"]
    return {"trade_date":trade_date,"run_at":datetime.now().astimezone().isoformat(timespec="seconds"),"source_updated_at":source_updated_at,"latest_history_date":max((x["metrics"]["history_last_date"] or "" for x in stock_rows.values()), default=None),"recent_trading_days":sorted({x["metrics"]["history_last_date"] for x in stock_rows.values() if x["metrics"]["history_last_date"]}, reverse=True)[:10],"baseline":baseline,"strategies":per_strategy,"limit_up":{"total":len(limits),"per_date":dict(daily),"sample":limits[:20],"limitation":"历史ST状态免费数据源不可得；已对当前非ST主板按普通10%涨停价近似识别，未静默当作精确ST历史。"},"comparison":{code:stock_rows.get(code,{"code":code,"missing":["not_found_in_spot"]}) for code in comparison_codes},"stocks":stock_rows}

import json
from pathlib import Path
import pandas as pd

def load_strategies(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["strategies"]

def _check(value, minimum=None, maximum=None):
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)

def calculate_metrics(spot: pd.Series, history: pd.DataFrame) -> dict:
    closes = pd.to_numeric(history.get("close", pd.Series(dtype=float)), errors="coerce")
    volumes = pd.to_numeric(history.get("volume", pd.Series(dtype=float)), errors="coerce")
    ma5, ma10, ma20 = closes.tail(5).mean(), closes.tail(10).mean(), closes.tail(20).mean()
    close, high, low = float(spot["close"]), float(spot["high"]), float(spot["low"])
    latest_volume = float(spot["volume"])
    limit_dates, limit_up_days_ago = [], None
    if len(history) >= 2:
        pct = closes.pct_change() * 100
        limit_dates = history.loc[pct >= 9.5, "date"].astype(str).tolist()
        if limit_dates:
            last_index = history.index[history["date"].astype(str) == limit_dates[-1]][-1]
            limit_up_days_ago = len(history.loc[last_index:]) - 1
    return {"close": close, "pct_change": float(spot["pct_change"]), "high": high, "low": low, "volume": latest_volume,
            "amount": float(spot["amount"]), "turnover": float(spot["turnover"]), "volume_ratio": float(spot["volume_ratio"]),
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma5_distance": (close / ma5 - 1) * 100 if ma5 else None,
            "close_high_ratio": close / high if high else None, "amplitude": (high / low - 1) * 100 if low else None,
            "return_3d": (close / closes.iloc[-4] - 1) * 100 if len(closes) >= 4 else None,
            "return_5d": (close / closes.iloc[-6] - 1) * 100 if len(closes) >= 6 else None,
            "return_10d": (close / closes.iloc[-11] - 1) * 100 if len(closes) >= 11 else None,
            "volume_ma5_multiple": latest_volume / volumes.tail(5).mean() if len(volumes) >= 5 and volumes.tail(5).mean() else None,
            "ma5_slope": ma5 - closes.iloc[-6:-1].mean() if len(closes) >= 10 else None,
            "ma10_slope": ma10 - closes.iloc[-11:-1].mean() if len(closes) >= 20 else None,
            "limit_dates": limit_dates, "limit_up_days_ago": limit_up_days_ago}

def evaluate(metrics: dict, strategy: dict) -> list[str]:
    r, failures = strategy["rules"], []
    def range_rule(key, label):
        if key in r and (metrics.get(key) is None or not _check(metrics[key], r.get(f"{key}_min"), r.get(f"{key}_max"))): failures.append(label)
    for key, label in [("pct_change","今日涨幅"),("turnover","换手率"),("volume_ratio","量比"),("amplitude","振幅"),("ma5_distance","距离5日线"),("close_high_ratio","收盘/最高"),("return_3d","近3日涨幅"),("return_5d","近5日涨幅"),("return_10d","近10日涨幅"),("volume_ma5_multiple","成交量/5日均量"),("amount","成交额")]:
        min_key, max_key = f"{key}_min", f"{key}_max"
        if min_key in r or max_key in r:
            if metrics.get(key) is None or not _check(metrics[key], r.get(min_key), r.get(max_key)): failures.append(label)
    if r.get("close_above_ma5") and not metrics["close"] > metrics["ma5"]: failures.append("收盘价未站上5日线")
    if r.get("ma5_above_ma10") and not metrics["ma5"] > metrics["ma10"]: failures.append("5日线未高于10日线")
    if r.get("ma5_slope_positive") and not (metrics.get("ma5_slope") or 0) > 0: failures.append("5日线未上行")
    if r.get("ma10_slope_positive") and not (metrics.get("ma10_slope") or 0) > 0: failures.append("10日线未上行")
    if "limit_up_days_min" in r:
        days = metrics.get("limit_up_days_ago")
        if days is None: failures.append("近期无涨停")
        elif not _check(days, r["limit_up_days_min"], r.get("limit_up_days_max")):
            failures.append("最近涨停不在要求区间")
    return failures

def strong_close_momentum(metrics: dict, strategy: dict) -> list[str]:
    return evaluate(metrics, strategy)

def recent_limit_up_trend(metrics: dict, strategy: dict) -> list[str]:
    return evaluate(metrics, strategy)

def limit_up_reacceleration(metrics: dict, strategy: dict) -> list[str]:
    return evaluate(metrics, strategy)

STRATEGY_FUNCTIONS = {
    "strong_close_momentum": strong_close_momentum,
    "recent_limit_up_trend": recent_limit_up_trend,
    "limit_up_reacceleration": limit_up_reacceleration,
}

def structure_score(metrics: dict) -> int:
    """Transparent 0-10 score: a review aid, not a prediction or trade signal."""
    points = 0
    points += 2 if metrics.get("ma5_distance") is not None and 0 <= metrics["ma5_distance"] <= 5 else 0
    points += 2 if (metrics.get("close_high_ratio") or 0) >= 0.98 else 0
    points += 1 if (metrics.get("volume_ratio") or 0) >= 1.2 else 0
    points += 1 if (metrics.get("amount") or 0) >= 300_000_000 else 0
    points += 1 if (metrics.get("ma5_slope") or 0) > 0 else 0
    points += 1 if metrics.get("ma5", 0) > metrics.get("ma10", 0) else 0
    points += 2 if metrics.get("limit_up_days_ago") is not None and metrics["limit_up_days_ago"] <= 7 else 0
    return points

def risk_notes(metrics: dict) -> list[str]:
    notes = []
    if (metrics.get("volume_ratio") or 0) < 1.2: notes.append("量比偏低")
    if (metrics.get("ma5_distance") or 0) > 7: notes.append("偏离5日线过远")
    if (metrics.get("close_high_ratio") or 0) < 0.98: notes.append("收盘未贴近全天高点")
    if (metrics.get("amount") or 0) < 300_000_000: notes.append("成交额偏低")
    if metrics.get("limit_up_days_ago") is None: notes.append("近期未识别涨停")
    return notes or ["未发现规则化风险提示"]

def explain_rejection(metrics: dict, failures: dict, strategies: list[dict]) -> str:
    """Explain the closest missed strategy in human-readable terms."""
    closest_id = min(failures, key=lambda key: len(failures[key]))
    labels = failures[closest_id]
    if "距离5日线" in labels:
        limit = next(s["rules"].get("ma5_distance_max") for s in strategies if s["id"] == closest_id)
        return f"距离5日线 {metrics['ma5_distance']:.2f}%，超过 {limit:.0f}%"
    if "今日涨幅" in labels:
        return f"今日涨幅 {metrics['pct_change']:.2f}% 不在该策略范围"
    if "收盘/最高" in labels:
        return f"收盘/最高 {metrics['close_high_ratio'] * 100:.2f}% 未达到要求"
    return "、".join(labels[:3]) if labels else "未命中策略"

def analyze_universe(spot: pd.DataFrame, histories: dict[str, pd.DataFrame], strategies: list[dict]) -> tuple[list[dict], list[dict]]:
    matches, rejected = [], []
    for _, row in spot.iterrows():
        metrics = calculate_metrics(row, histories.get(row["code"], pd.DataFrame()))
        failures = {s["id"]: STRATEGY_FUNCTIONS[s["id"]](metrics, s) for s in strategies}
        matched = [s["id"] for s in strategies if not failures[s["id"]]]
        item = {"code": row["code"], "name": row["name"], "metrics": metrics, "failures": failures}
        if matched:
            metrics["structure_score"] = structure_score(metrics)
            metrics["risk_notes"] = risk_notes(metrics)
            item.update({"strategy_ids": matched})
            matches.append(item)
        else:
            item["reason"] = explain_rejection(metrics, failures, strategies)
            rejected.append(item)
    return matches, rejected

def run_strategies(spot: pd.DataFrame, histories: dict[str, pd.DataFrame], strategies: list[dict]) -> list[dict]:
    return analyze_universe(spot, histories, strategies)[0]

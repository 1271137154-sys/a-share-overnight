"""Create a date-validated formal wide-screen result.

Historical daily bars are cached locally.  On every run the current full-market
snapshot is fetched fresh and is appended/replaced as the market-date bar.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CN_TZ = ZoneInfo("Asia/Shanghai")
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.data_source import AkshareDataSource
from app.database import Database
from app.diagnostics import build_report

CACHE = ROOT / "data" / "history_cache"
SITE = ROOT / "site" / "data"
GIT = shutil.which("git") or str(Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "native" / "git" / "cmd" / "git.exe")


def jsonable(value):
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


def read_cache(code: str):
    path = CACHE / f"{code}.json"
    if not path.exists():
        return None
    try:
        return pd.read_json(path)
    except Exception:
        return None


def write_cache(code: str, frame: pd.DataFrame):
    CACHE.mkdir(parents=True, exist_ok=True)
    frame.to_json(CACHE / f"{code}.json", orient="records", force_ascii=False)


def load_company_profiles() -> dict:
    """Read cached public company profiles for industry-board matching.

    The profile industry is the bridge from a stock code to Sina's industry
    board quote.  Missing profiles remain explicitly unconfirmed.
    """
    path = ROOT / "data" / "company_profiles.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_progress(success, total, failed, current, started_at, status="running", failed_details=None):
    SITE.mkdir(parents=True, exist_ok=True)
    elapsed = max((datetime.now().astimezone() - started_at).total_seconds(), 1)
    rate = success / (elapsed / 60)
    remaining = max(total - success - len(failed), 0)
    payload = {"status": status, "success": success, "total": total, "coverage": success / total if total else 0,
               "failed": len(failed), "current_code": current, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "rate_per_minute": rate, "estimated_remaining_minutes": (remaining / rate) if rate else None,
               "failed_codes": failed, "failed_details": failed_details or {}}
    (SITE / "progress.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def publish_progress():
    """Publish only the progress data; failures are non-fatal to downloading."""
    try:
        subprocess.run([GIT, "add", "site/data/progress.json"], cwd=ROOT, check=True, capture_output=True)
        changed = subprocess.run([GIT, "diff", "--cached", "--quiet"], cwd=ROOT).returncode != 0
        if changed:
            subprocess.run([GIT, "commit", "-m", "chore: update history-cache progress"], cwd=ROOT, check=True, capture_output=True)
            subprocess.run([GIT, "push", "origin", "main"], cwd=ROOT, check=True, capture_output=True)
    except Exception:
        pass


def fetch_history(source, code: str):
    cached = read_cache(code)
    if cached is not None and len(cached) >= 60:
        return cached, "cache"
    last_error = None
    for attempt in range(3):
        try:
            frame = source.fetch_history(code, days=80)
            if frame is not None and len(frame) >= 60:
                write_cache(code, frame)
                return frame, "network"
        except Exception as exc:
            last_error = exc
        time.sleep(attempt + 1)
    return None, f"failed:{last_error}" if last_error else "failed:insufficient_history"


def composite(history: pd.DataFrame, spot: pd.Series, market_date: str):
    """Use the fresh same-day snapshot as the final unadjusted daily bar."""
    frame = history.copy()
    # Tencent and Eastmoney history endpoints may report volume in different
    # units (lots versus shares).  Amount / close / volume exposes the unit:
    # around 100 means historical volume is in lots, so normalize to shares
    # before comparing it with the fresh spot snapshot.
    if {"amount", "close", "volume"}.issubset(frame.columns):
        amounts = pd.to_numeric(frame["amount"], errors="coerce")
        closes = pd.to_numeric(frame["close"], errors="coerce")
        volumes = pd.to_numeric(frame["volume"], errors="coerce")
        ratio = (amounts / (closes * volumes)).replace([float("inf"), -float("inf")], pd.NA).dropna().median()
        if ratio is not None and 50 <= float(ratio) <= 150:
            frame["volume"] = volumes * 100
    frame["date"] = frame["date"].astype(str).str[:10]
    row = {"date": market_date, "open": spot.get("open"), "high": spot.get("high"), "low": spot.get("low"), "close": spot.get("close"), "volume": spot.get("volume"), "amount": spot.get("amount"), "turnover": spot.get("turnover"), "pct_change": spot.get("pct_change")}
    frame = frame[frame["date"] != market_date]
    return pd.concat([frame, pd.DataFrame([row])], ignore_index=True).sort_values("date").tail(81)


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def score_candidate(metrics: dict, history: pd.DataFrame, market_date: str, strategy_id: str, board: dict | None = None):
    """Second-layer review score.  This never changes a strategy match.

    It only ranks stocks that already passed at least one of the three fixed
    wide-screen strategies.  Missing board-strength and intraday data stay
    explicitly pending for manual review.
    """
    score, plus, minus = 60, [], []
    amount = _as_float(metrics.get("amount")) or 0
    ma5_distance = _as_float(metrics.get("ma5_distance"))
    close_high = _as_float(metrics.get("close_high_ratio"))
    ma10_slope = _as_float(metrics.get("ma10_slope"))
    ma20_slope = _as_float(metrics.get("ma20_slope"))
    details = {
        "amount": amount, "ma5_distance": ma5_distance,
        "ma10_trend": "up" if ma10_slope is not None and ma10_slope > 0 else "not_up_or_missing",
        "ma20_trend": "up" if ma20_slope is not None and ma20_slope > 0 else "not_up_or_missing",
        "close_high_ratio": close_high,
    }
    if amount >= 500_000_000:
        score += 10; plus.append("成交额≥5亿元，流动性较好")
    elif amount >= 300_000_000:
        score += 5; plus.append("成交额≥3亿元")
    else:
        minus.append("成交额低于3亿元")
    if ma5_distance is not None:
        if 0 < ma5_distance <= 5:
            score += 8; plus.append("收盘贴近MA5（偏离≤5%）")
        elif ma5_distance <= 7:
            score += 4; plus.append("收盘仍在MA5附近")
        else:
            score -= 8; minus.append("距离MA5偏离超过7%")
    if ma10_slope is not None and ma10_slope > 0:
        score += 5; plus.append("MA10向上")
    if ma20_slope is not None and ma20_slope > 0:
        score += 3; plus.append("MA20向上")
    if close_high is not None:
        if close_high >= .985:
            score += 7; plus.append("收盘接近全天最高价")
        elif close_high >= .98:
            score += 4; plus.append("收盘强度良好")
        else:
            score -= 5; minus.append("收盘距离最高价偏远")

    frame = history.copy()
    frame["date"] = frame["date"].astype(str).str[:10]
    for field in ("open", "high", "low", "close", "volume"):
        frame[field] = pd.to_numeric(frame.get(field), errors="coerce")
    frame = frame[frame["date"] <= market_date].sort_values("date")
    limit_ups = metrics.get("limit_ups") or []
    latest_limit_date = limit_ups[-1]["date"] if limit_ups else None
    details["latest_limit_up_date"] = latest_limit_date
    details["limit_up_days_ago"] = metrics.get("limit_up_days_ago")
    max_drawdown = None
    if latest_limit_date:
        post = frame[frame["date"] >= latest_limit_date]
        start = post.iloc[0] if not post.empty else None
        start_close = _as_float(start.get("close")) if start is not None else None
        low = _as_float(post["low"].min()) if not post.empty else None
        if start_close not in (None, 0) and low is not None:
            max_drawdown = (low / start_close - 1) * 100
            if max_drawdown >= -5:
                score += 5; plus.append("涨停后最大回撤≤5%")
            elif max_drawdown >= -8:
                score += 2; plus.append("涨停后回撤可控")
            elif max_drawdown < -10:
                score -= 6; minus.append("涨停后最大回撤超过10%")
        days = metrics.get("limit_up_days_ago")
        if days is not None and 2 <= days <= 5:
            score += 5; plus.append("涨停时间窗口较合适")
        elif days is not None and 6 <= days <= 10:
            score += 3; plus.append("近期有有效涨停")
    details["max_drawdown_after_limit_up"] = max_drawdown

    post = frame[frame["date"] >= latest_limit_date] if latest_limit_date else frame.tail(10)
    long_bear = False
    reversals = 0
    if len(post) >= 2:
        vols = post["volume"].rolling(5, min_periods=1).mean().shift(1)
        previous_close = post["close"].shift(1)
        decline = post["close"] / previous_close - 1
        long_bear = bool(((post["close"] < post["open"]) & (decline <= -.03) & (post["volume"] > vols * 1.2)).fillna(False).any())
        high_range = (post["high"] - post["low"]).replace(0, pd.NA)
        upper_shadow = (post["high"] - post[["open", "close"]].max(axis=1)) / high_range
        reversals = int(((upper_shadow >= .35) & (post["close"] / post["high"] < .98)).fillna(False).sum())
    details["volume_expansion_long_bear"] = long_bear
    details["repeated_rally_pullbacks"] = reversals >= 2
    if long_bear:
        score -= 8; minus.append("出现放量长阴")
    else:
        score += 3; plus.append("未见放量长阴")
    if reversals >= 2:
        score -= 7; minus.append("连续冲高回落")
    else:
        score += 3; plus.append("未见连续冲高回落")
    board = board or {}
    board_pct = _as_float(board.get("board_pct_change"))
    details["board_name"] = board.get("board_name")
    details["board_pct_change"] = board_pct
    details["board_company_count"] = _as_float(board.get("board_company_count"))
    if board_pct is None:
        minus.append("板块强度数据未取得，不能作为隔夜执行依据")
    elif board_pct >= 2:
        score += 8; plus.append(f"所属行业板块当日上涨 {board_pct:.2f}%")
    elif board_pct >= 0:
        score += 2; plus.append(f"所属行业板块维持红盘（{board_pct:.2f}%）")
    else:
        score -= 8; minus.append(f"所属行业板块走弱（{board_pct:.2f}%）")

    # Each strategy has a different next-day premium source.  These are
    # strategy-specific ranking adjustments, never a cross-strategy bonus.
    pct = _as_float(metrics.get("pct_change"))
    volume_ratio = _as_float(metrics.get("volume_ratio"))
    volume_multiple = _as_float(metrics.get("volume_ma5_multiple"))
    if strategy_id == "strong_close_momentum":
        if volume_ratio is not None and 1.2 <= volume_ratio <= 2.8:
            score += 5; plus.append("策略1：量比适中，盘中增量资金较健康")
        elif volume_ratio is not None and volume_ratio > 3.2:
            score -= 5; minus.append("策略1：量比过高，次日兑现压力增加")
        if pct is not None and pct >= 7:
            score -= 4; minus.append("策略1：当日涨幅接近上限，隔夜预期偏满")
    elif strategy_id == "recent_limit_up_trend":
        if details.get("limit_up_days_ago") is not None and 2 <= details["limit_up_days_ago"] <= 4:
            score += 4; plus.append("策略2：涨停后处于较短的趋势延续窗口")
        if _as_float(metrics.get("return_5d")) is not None and _as_float(metrics.get("return_5d")) > 18:
            score -= 5; minus.append("策略2：近5日累计涨幅偏高，趋势续强可能转为兑现")
    elif strategy_id == "limit_up_reacceleration":
        if volume_multiple is not None and 1.0 <= volume_multiple <= 1.8:
            score += 5; plus.append("策略3：再转强日成交量温和放大")
        elif volume_multiple is not None and volume_multiple > 2.5:
            score -= 5; minus.append("策略3：再转强日放量过猛，需防高位分歧")
    raw_score = score
    score = max(0, min(100, score))
    category = "隔夜候选" if score >= 80 and board_pct is not None and board_pct >= 0 else "观察" if score >= 65 else "回避"
    return {"score": score, "priority_score": raw_score, "category": category, "plus": plus, "minus": minus,
            "pending_manual": ["尾盘分时结构需确认", "次日竞价强弱需确认"], "details": details}


STRICT_RULES = {
    "common": [
        "成交额不低于3亿元", "收盘/最高价不低于98%", "MA5偏离不超过5%",
        "所属行业板块当日不弱于+1%", "未出现放量长阴", "未出现连续冲高回落",
    ],
    "strong_close_momentum": ["涨幅4%至7.5%", "量比1.2至3", "换手率4%至18%"],
    "recent_limit_up_trend": ["涨幅2.5%至6%", "量比1.2至3", "换手率4%至14%", "涨停距今2至5个交易日", "近5日涨幅不超过20%"],
    "limit_up_reacceleration": ["涨幅2%至6.5%", "成交量/前5日均量1.1至2.5", "换手率4%至15%", "涨停距今3至7个交易日"],
}


def strict_overnight_gate(metrics: dict, review: dict, strategy_id: str) -> tuple[bool, list[str]]:
    """Hard gate for the small, actionable next-day-premium list.

    The wide strategies remain unchanged.  This gate is deliberately stricter
    and is evaluated *inside each strategy* before any daily ranking.  A stock
    matching multiple strategies neither receives a bonus nor bypasses a
    failed hard gate.
    """
    details = review.get("details", {})
    amount = _as_float(metrics.get("amount"))
    close_high = _as_float(metrics.get("close_high_ratio"))
    ma5_distance = _as_float(metrics.get("ma5_distance"))
    board_pct = _as_float(details.get("board_pct_change"))
    pct = _as_float(metrics.get("pct_change"))
    turnover = _as_float(metrics.get("turnover"))
    volume_ratio = _as_float(metrics.get("volume_ratio"))
    volume_multiple = _as_float(metrics.get("volume_ma5_multiple"))
    days = _as_float(details.get("limit_up_days_ago"))
    return_5d = _as_float(metrics.get("return_5d"))
    failures: list[str] = []

    if amount is None or amount < 300_000_000: failures.append("成交额低于3亿元")
    if close_high is None or close_high < .98: failures.append("收盘强度不足（收盘/最高<98%）")
    if ma5_distance is None or not (0 <= ma5_distance <= 5): failures.append("距离MA5超过5%")
    if board_pct is None or board_pct < 1: failures.append("所属行业板块当日未达到+1%")
    if details.get("volume_expansion_long_bear"): failures.append("出现放量长阴")
    if details.get("repeated_rally_pullbacks"): failures.append("连续冲高回落")

    if strategy_id == "strong_close_momentum":
        if pct is None or not (4 <= pct <= 7.5): failures.append("涨幅不在4%至7.5%")
        if volume_ratio is None or not (1.2 <= volume_ratio <= 3): failures.append("量比不在1.2至3")
        if turnover is None or not (4 <= turnover <= 18): failures.append("换手率不在4%至18%")
    elif strategy_id == "recent_limit_up_trend":
        if pct is None or not (2.5 <= pct <= 6): failures.append("涨幅不在2.5%至6%")
        if volume_ratio is None or not (1.2 <= volume_ratio <= 3): failures.append("量比不在1.2至3")
        if turnover is None or not (4 <= turnover <= 14): failures.append("换手率不在4%至14%")
        if days is None or not (2 <= days <= 5): failures.append("涨停时间窗口不在2至5日")
        if return_5d is None or return_5d > 20: failures.append("近5日涨幅超过20%")
    elif strategy_id == "limit_up_reacceleration":
        if pct is None or not (2 <= pct <= 6.5): failures.append("涨幅不在2%至6.5%")
        if volume_multiple is None or not (1.1 <= volume_multiple <= 2.5): failures.append("成交量倍数不在1.1至2.5")
        if turnover is None or not (4 <= turnover <= 15): failures.append("换手率不在4%至15%")
        if days is None or not (3 <= days <= 7): failures.append("涨停时间窗口不在3至7日")
    return not failures, failures


def choose_final_candidates(selected: list[dict]) -> tuple[list[dict], dict]:
    """Return at most two daily ideas, at most one from each strategy."""
    strategy_ids = ("strong_close_momentum", "recent_limit_up_trend", "limit_up_reacceleration")
    per_strategy: list[dict] = []
    summary: dict[str, dict] = {}
    for strategy_id in strategy_ids:
        eligible = []
        for item in selected:
            review = item.get("strategy_reviews", {}).get(strategy_id)
            if not review:
                continue
            passed, failures = strict_overnight_gate(item["metrics"], review, strategy_id)
            review["strict_overnight"] = {"passed": passed, "failures": failures, "rules": STRICT_RULES["common"] + STRICT_RULES[strategy_id]}
            if passed:
                eligible.append(item)
        eligible.sort(key=lambda item: item["strategy_reviews"][strategy_id]["priority_score"], reverse=True)
        summary[strategy_id] = {"strict_eligible": len(eligible), "selected": 0}
        if eligible:
            item = eligible[0]
            per_strategy.append({"code": item["code"], "name": item["name"], "strategy_id": strategy_id,
                                 "score": item["strategy_reviews"][strategy_id]["score"],
                                 "priority_score": item["strategy_reviews"][strategy_id]["priority_score"]})
    # A duplicate is kept only under its best individual strategy.  No overlap
    # bonus is used, and the final daily list is deliberately capped at two.
    per_strategy.sort(key=lambda item: item["priority_score"], reverse=True)
    final, seen = [], set()
    for item in per_strategy:
        if item["code"] in seen:
            continue
        final.append({**item, "rank": len(final) + 1})
        seen.add(item["code"])
        if len(final) == 2:
            break
    for item in final:
        summary[item["strategy_id"]]["selected"] += 1
    return final, summary


def update_overnight_performance(market_date: str, fresh: pd.DataFrame) -> dict:
    """Measure prior final-list entries once the next complete daily bar exists.

    "success" is deliberately defined as next-day *high* reaching +1% from
    the selection close.  It is an observable potential exit window, not a
    claim that every trader could fill at that price.  Open/close returns are
    shown alongside it so the page does not hide execution risk.
    """
    path = SITE / "overnight-performance.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        stored = {}
    entries = stored.get("entries", [])
    known = {f"{x.get('selection_date')}:{x.get('code')}:{x.get('strategy_id')}" for x in entries}
    prior_files = []
    for candidate_path in SITE.glob("????-??-??.json"):
        try:
            payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("formal") and payload.get("market_date", "") < market_date:
            prior_files.append(payload)
    if prior_files:
        prior = max(prior_files, key=lambda payload: payload.get("market_date", ""))
        record_by_code = {item.get("code"): item for item in prior.get("records", [])}
        spot_by_code = {str(row["code"]).zfill(6): row for _, row in fresh.iterrows()}
        for candidate in prior.get("final_overnight_candidates", []):
            code, strategy_id = candidate.get("code"), candidate.get("strategy_id")
            key = f"{prior.get('market_date')}:{code}:{strategy_id}"
            record, row = record_by_code.get(code), spot_by_code.get(str(code).zfill(6))
            if key in known or not record or row is None:
                continue
            base = _as_float(record.get("metrics", {}).get("close"))
            opening, high, close = (_as_float(row.get(name)) for name in ("open", "high", "close"))
            if base in (None, 0) or None in (opening, high, close):
                continue
            entries.append({
                "selection_date": prior["market_date"], "measured_date": market_date,
                "code": code, "name": candidate.get("name"), "strategy_id": strategy_id,
                "selection_close": base,
                "open_return": round((opening / base - 1) * 100, 3),
                "high_return": round((high / base - 1) * 100, 3),
                "close_return": round((close / base - 1) * 100, 3),
                "success_high_ge_1pct": high / base - 1 >= .01,
            })
    entries.sort(key=lambda item: (item["selection_date"], item["code"], item["strategy_id"]), reverse=True)
    def aggregate(items):
        total = len(items)
        return {"trades": total,
                "open_positive_rate": round(sum(x["open_return"] > 0 for x in items) / total * 100, 1) if total else None,
                "high_ge_1pct_rate": round(sum(x["success_high_ge_1pct"] for x in items) / total * 100, 1) if total else None,
                "avg_open_return": round(sum(x["open_return"] for x in items) / total, 3) if total else None,
                "avg_high_return": round(sum(x["high_return"] for x in items) / total, 3) if total else None,
                "avg_close_return": round(sum(x["close_return"] for x in items) / total, 3) if total else None}
    summary = {"all": aggregate(entries), "by_strategy": {}}
    for strategy_id in ("strong_close_momentum", "recent_limit_up_trend", "limit_up_reacceleration"):
        summary["by_strategy"][strategy_id] = aggregate([x for x in entries if x["strategy_id"] == strategy_id])
    payload = {"updated_for_market_date": market_date, "definition": "成功率=次日最高价相对尾盘筛选收盘价达到或超过+1%，仅代表历史上存在理论可兑现窗口，不代表实际成交结果。", "entries": entries, "summary": summary}
    SITE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main(market_date: str | None = None, use_stored_snapshot: bool = False):
    market_date = market_date or date.today().isoformat()
    now = datetime.now(CN_TZ)
    if now.date().isoformat() != market_date or now.hour < 15:
        raise RuntimeError(f"{market_date}行情未确认收盘，正式筛选未生成。")
    db = Database(settings.database_path)
    source = AkshareDataSource(settings.request_retries, settings.request_timeout_seconds)
    spot = db.load_snapshot(market_date) if use_stored_snapshot else source.fetch_spot()
    if spot is None or spot.empty:
        raise RuntimeError(f"{market_date}行情获取失败，今日正式筛选未生成。")
    fresh = spot[spot["code"].astype(str).str.zfill(6).str.startswith(("60", "00")) & ~spot["name"].astype(str).str.contains("ST", na=False)].copy()
    if fresh.empty:
        raise RuntimeError(f"{market_date}行情获取失败，今日正式筛选未生成。")
    fresh["code"] = fresh["code"].astype(str).str.zfill(6)
    histories, failed, failed_details, sources = {}, [], {}, {}
    started_at = datetime.now().astimezone(); total = len(fresh); workers = 4
    remaining_codes = list(fresh["code"]); start = 0
    while start < total:
        batch = remaining_codes[start:start + workers]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            jobs = {executor.submit(fetch_history, source, code): code for code in batch}
            batch_failures = 0
            for job in as_completed(jobs):
                code = jobs[job]; history, origin = job.result()
                if history is None:
                    failed.append(code); failed_details[code] = origin; batch_failures += 1
                else:
                    histories[code] = composite(history, fresh.loc[fresh["code"] == code].iloc[0], market_date)
                    sources[code] = origin
        start += len(batch); processed = start
        if processed % 100 < len(batch) or processed == total:
            write_progress(len(histories), total, failed, batch[-1], started_at, failed_details=failed_details)
            publish_progress()
        if batch_failures and workers > 1:
            workers -= 1
            time.sleep(min(30, 2 ** min(batch_failures, 5)))
    coverage = len(histories) / len(fresh)
    raw_last = max((pd.Series(frame["date"]).astype(str).str[:10].iloc[-2] for frame in histories.values() if len(frame) >= 2), default=None)
    if coverage < .95:
        raise RuntimeError(f"历史数据覆盖率 {coverage:.2%} 低于95%，今日正式筛选未生成。失败代码：{','.join(failed[:50])}")
    report = build_report(fresh, histories, market_date, source_updated_at=now.isoformat(timespec="seconds"))
    board_strength = source.fetch_industry_strength()
    company_profiles = load_company_profiles()
    selected = []
    for code, item in report["stocks"].items():
        ids = [sid for sid, checks in item.get("strategies", {}).items() if all(c["status"] == "pass" for c in checks)]
        if ids:
            profile = company_profiles.get(code, {})
            industry = profile.get("industry")
            board = board_strength.get(industry) if industry else None
            reviews = {
                sid: score_candidate(item["metrics"], histories[code], market_date, sid, board)
                for sid in ids
            }
            for review in reviews.values():
                review["details"]["company_industry"] = industry
                review["details"]["board_match_method"] = "CNINFO industry exact match" if board else "unavailable"
            selected.append({"code": code, "name": item["name"], "strategy_ids": ids, "metrics": item["metrics"],
                             "strategy_reviews": reviews,
                             "failures": {sid: [c["name"] for c in checks if c["status"] != "pass"] for sid, checks in item["strategies"].items()}})
    build_id = hashlib.sha256(f"{market_date}:{now.isoformat()}:{len(selected)}".encode()).hexdigest()[:12]
    metadata = {"build_id":build_id,"data_version":"formal-wide-v1","market_date":market_date,"snapshot_date":market_date,"snapshot_source":"stored_2026-07-27_fresh_snapshot" if use_stored_snapshot else "fresh_full_market_snapshot","historical_last_date":raw_last,"used_snapshot_composite":True,"final_calculation_date":market_date,"run_at":now.isoformat(timespec="seconds"),"coverage":coverage,"history_success":len(histories),"history_failed":len(failed),"failed_codes":failed,"formal":True}
    if not use_stored_snapshot:
        db.save_snapshot(market_date, fresh, "fresh-full-market-snapshot")
    db.save_selections(market_date, selected); db.save_strategy_matches(market_date, selected)
    names = {"strong_close_momentum":"策略1：强势收盘隔夜动量（宽筛）","recent_limit_up_trend":"策略2：近期涨停后的温和趋势（宽筛）","limit_up_reacceleration":"策略3：涨停后整理再转强（宽筛）"}
    for item in selected: item["strategy_names"] = [names[key] for key in item["strategy_ids"]]
    # A union is only a de-duplicated browsing list.  It never changes a
    # stock's rank or decision; each strategy is ranked independently in UI.
    selected.sort(key=lambda item: item["code"])
    # Keep the three fixed wide-screen formulas intact.  This separate,
    # strategy-local hard gate is only for the small next-day-premium list.
    final_candidates, strict_summary = choose_final_candidates(selected)
    overnight_performance = update_overnight_performance(market_date, fresh)
    score_counts = {sid: {"overnight_candidate": sum(review["category"] == "隔夜候选" for item in selected for key, review in item["strategy_reviews"].items() if key == sid),
                          "observe": sum(review["category"] == "观察" for item in selected for key, review in item["strategy_reviews"].items() if key == sid),
                          "avoid": sum(review["category"] == "回避" for item in selected for key, review in item["strategy_reviews"].items() if key == sid)}
                    for sid in names}
    payload = {**metadata,"data_version":"formal-wide-v2-strict-final","records":jsonable(selected),"strategy_counts":db.strategy_counts(market_date),
               "union_count":len(selected),"multi_strategy_count":sum(len(item["strategy_ids"]) > 1 for item in selected),
               "score_counts":score_counts,"board_strength_source":"Sina industry board spot matched to CNINFO company industry","excluded_history_insufficient":failed,"rejections":[],"history_sources":sources,
               "final_overnight_candidates":jsonable(final_candidates),"final_candidate_count":len(final_candidates),
               "strict_summary":jsonable(strict_summary),"overnight_performance":jsonable(overnight_performance.get("summary", {}))}
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / f"{market_date}.json").write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    (SITE / "formal-latest.json").write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    manifest_path = SITE / "manifest.json"
    existing = json.loads(manifest_path.read_text(encoding="utf-8")).get("dates", []) if manifest_path.exists() else []
    dates = [market_date] + [item for item in existing if item != market_date]
    manifest_path.write_text(json.dumps({"dates": dates[:90], "latest": market_date}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_progress(len(histories), total, failed, "", started_at, status="completed", failed_details=failed_details)
    print(json.dumps({**metadata,"counts":payload["strategy_counts"],"union":len(selected),"multi":sum(len(x["strategy_ids"])>1 for x in selected)}, ensure_ascii=False))


if __name__ == "__main__":
    main(use_stored_snapshot="--stored-snapshot" in sys.argv)

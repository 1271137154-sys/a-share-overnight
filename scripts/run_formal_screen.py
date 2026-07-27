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

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
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
    frame["date"] = frame["date"].astype(str).str[:10]
    row = {"date": market_date, "open": spot.get("open"), "high": spot.get("high"), "low": spot.get("low"), "close": spot.get("close"), "volume": spot.get("volume"), "amount": spot.get("amount"), "turnover": spot.get("turnover"), "pct_change": spot.get("pct_change")}
    frame = frame[frame["date"] != market_date]
    return pd.concat([frame, pd.DataFrame([row])], ignore_index=True).sort_values("date").tail(81)


def main(market_date: str | None = None):
    market_date = market_date or date.today().isoformat()
    now = datetime.now().astimezone()
    if now.date().isoformat() != market_date or now.hour < 15:
        raise RuntimeError(f"{market_date}行情未确认收盘，正式筛选未生成。")
    source = AkshareDataSource(settings.request_retries, settings.request_timeout_seconds)
    spot = source.fetch_spot()
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
    selected = []
    for code, item in report["stocks"].items():
        ids = [sid for sid, checks in item.get("strategies", {}).items() if all(c["status"] == "pass" for c in checks)]
        if ids:
            selected.append({"code": code, "name": item["name"], "strategy_ids": ids, "metrics": item["metrics"], "failures": {sid: [c["name"] for c in checks if c["status"] != "pass"] for sid, checks in item["strategies"].items()}})
    build_id = hashlib.sha256(f"{market_date}:{now.isoformat()}:{len(selected)}".encode()).hexdigest()[:12]
    metadata = {"build_id":build_id,"data_version":"formal-wide-v1","market_date":market_date,"snapshot_date":market_date,"historical_last_date":raw_last,"used_snapshot_composite":True,"final_calculation_date":market_date,"run_at":now.isoformat(timespec="seconds"),"coverage":coverage,"history_success":len(histories),"history_failed":len(failed),"failed_codes":failed,"formal":True}
    db = Database(settings.database_path); db.save_snapshot(market_date, fresh, "fresh-full-market-snapshot")
    db.save_selections(market_date, selected); db.save_strategy_matches(market_date, selected)
    names = {"strong_close_momentum":"策略1：强势收盘隔夜动量（宽筛）","recent_limit_up_trend":"策略2：近期涨停后的温和趋势（宽筛）","limit_up_reacceleration":"策略3：涨停后整理再转强（宽筛）"}
    for item in selected: item["strategy_names"] = [names[key] for key in item["strategy_ids"]]
    payload = {**metadata,"records":jsonable(selected),"strategy_counts":db.strategy_counts(market_date),"rejections":[],"history_sources":sources}
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
    main()

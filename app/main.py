import csv, io, logging
from logging.handlers import RotatingFileHandler
from datetime import date
from pathlib import Path
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from starlette.middleware.sessions import SessionMiddleware
from .config import ROOT, settings
from .database import Database
from .data_source import AkshareDataSource
from .strategies import load_strategies, run_strategies
from .performance import calculate_next_day_performance
from .security import authenticate, is_admin, require_admin
from .scheduler import start_scheduler
from .tasks import TASK_NAME, run_daily_screen

settings.log_dir.mkdir(parents=True, exist_ok=True)
log_handler = RotatingFileHandler(settings.log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=[log_handler])
app = FastAPI(title=settings.app_name, description="Research-only stock screening service")
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, https_only=False, same_site="lax")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")
db, source = Database(settings.database_path), AkshareDataSource(settings.request_retries, settings.request_timeout_seconds)
strategies = load_strategies(ROOT / "config" / "strategies.json")
scheduler = None

@app.on_event("startup")
def startup_scheduler():
    global scheduler
    scheduler = start_scheduler(db, source, strategies)

@app.on_event("shutdown")
def shutdown_scheduler():
    if scheduler:
        scheduler.shutdown(wait=False)

def selection_context(trade_date: str):
    records = db.load_selections(trade_date)
    performance = db.performance_for_date(trade_date)
    names = {s["id"]: s["name"] for s in strategies}
    for r in records:
        r["strategy_names"] = [names[x] for x in r["strategy_ids"]]
        r["performance"] = performance.get(r["code"])
    groups = {s["id"]: [r for r in records if s["id"] in r["strategy_ids"]] for s in strategies}
    multi = [r for r in records if len(r["strategy_ids"]) > 1]
    return records, groups, multi

@app.get("/", response_class=HTMLResponse)
def home(request: Request, trade_date: str | None = None):
    dates = db.dates(); selected = trade_date or (dates[0] if dates else date.today().isoformat())
    records, groups, multi = selection_context(selected)
    return templates.TemplateResponse(request, "index.html", {"records": records, "groups": groups, "multi": multi, "strategies": strategies, "dates": dates, "trade_date": selected, "admin": is_admin(request)})

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})

@app.post("/login")
def login(request: Request, username: str = Form(), password: str = Form()):
    if not authenticate(username, password):
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid credentials"}, status_code=401)
    request.session["admin"] = True
    return RedirectResponse(url="/", status_code=303)

@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)

@app.post("/refresh")
def refresh(request: Request):
    require_admin(request)
    return run_daily_screen(db, source, strategies)

@app.get("/status", response_class=HTMLResponse)
def task_status(request: Request):
    return templates.TemplateResponse(request, "status.html", {"task": db.latest_task(TASK_NAME), "admin": is_admin(request), "scheduler_enabled": settings.enable_scheduler, "schedule": f"交易日 {settings.scheduler_hour:02d}:{settings.scheduler_minute:02d}（上海时间）"})

@app.get("/rejections", response_class=HTMLResponse)
def rejections(request: Request, trade_date: str | None = None):
    dates = db.dates(); selected = trade_date or (dates[0] if dates else date.today().isoformat())
    items = db.load_rejections(selected)
    return templates.TemplateResponse(request, "rejections.html", {"items": items, "dates": dates, "trade_date": selected})

@app.post("/performance/{selection_date}")
def update_performance(selection_date: str, request: Request):
    require_admin(request)
    records = db.load_selections(selection_date)
    updated = 0
    for record in records:
        history = source.fetch_history(record["code"], days=20)
        result = calculate_next_day_performance(record["metrics"]["close"], history, selection_date)
        if result:
            measured_date, values = result
            db.save_performance(selection_date, record["code"], measured_date, values)
            updated += 1
    return {"selection_date": selection_date, "updated": updated, "note": "9:35 收益需要分钟数据，将在第二阶段接入。"}

@app.get("/export.csv")
def export_csv(trade_date: str):
    records, _, _ = selection_context(trade_date)
    stream = io.StringIO(); writer = csv.writer(stream)
    writer.writerow(["日期","代码","名称","命中策略","现价","涨幅%","量比","换手%","成交额","5日线","10日线","20日线","距5日线%","收盘/最高","最近涨停"])
    for r in records:
        m = r["metrics"]; writer.writerow([trade_date,r["code"],r["name"]," / ".join(r["strategy_names"]),m.get("close"),m.get("pct_change"),m.get("volume_ratio"),m.get("turnover"),m.get("amount"),m.get("ma5"),m.get("ma10"),m.get("ma20"),m.get("ma5_distance"),m.get("close_high_ratio"), ";".join(m.get("limit_dates", []))])
    return StreamingResponse(iter(["\ufeff" + stream.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=selections-{trade_date}.csv"})

@app.get("/health")
def health(): return {"status":"ok", "database": str(settings.database_path), "strategies": len(strategies), "mode": "read-only-by-default"}

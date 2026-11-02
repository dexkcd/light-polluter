"""
Light sensor rig: the web app.

Log in with the seeded username and password, watch the sensor live, start and stop
sessions on the Pi, and download every run's files in the same format logger.py writes.

Settings (environment variables):
    APP_USERNAME, APP_PASSWORD   the login
    SESSION_SECRET               signs the login cookie (any long random string)
    MQTT_URL                     how the app reaches the broker, e.g. wss://broker-xxxx.up.railway.app
    MQTT_APP_PASSWORD            the broker's "app" user's password
    PUBLIC_BROKER_URL            the broker address for the Pi (default: MQTT_URL)
    MQTT_PI_PASSWORD             optional: shown on the Setup page, for the Pi's rig.env
    MQTT_LAPTOP_PASSWORD         optional: shown on the Setup page, for logger.py on a laptop
    MQTT_MOCK_PASSWORD           optional: turns on the mock rig, a simulated Pi run by the app itself
                                 on the broker's mock/... topics, with its data in DATA_DIR/mock
    DATA_DIR                     where the data goes (default: ./data; /data on Railway)
    INSECURE_COOKIES=1           only for running it locally over plain http

Run locally from the repo folder:
    PYTHONPATH=.:cloud uvicorn app.main:app --port 8000
"""

import asyncio
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import check_session
import session as plans

AGENT_SCRIPT = Path(plans.__file__).resolve().with_name("pi_agent.py")

from .bridge import Bridge
from .store import SESSION_ID, Store, session_time

HERE = Path(__file__).resolve().parent
CHECK_SCRIPT = Path(check_session.__file__).resolve()

USERNAME = os.environ.get("APP_USERNAME", "")
PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)
MQTT_URL = os.environ.get("MQTT_URL", "ws://localhost:8080/")
PUBLIC_BROKER_URL = os.environ.get("PUBLIC_BROKER_URL") or MQTT_URL
SECURE_COOKIES = os.environ.get("INSECURE_COOKIES") != "1"
if not USERNAME or not PASSWORD:
    raise SystemExit("Set APP_USERNAME and APP_PASSWORD.")
if "SESSION_SECRET" not in os.environ:
    print("SESSION_SECRET isn't set, so everyone is logged out whenever the app restarts.")

PLAN_INFO = {
    "main": "Sessions 1 and 2: baselines, then steps and ramps, clean and with pollution (runs 1–10).",
    "small-steps": "Sessions 3 and 4: small steps between 1.0 and 0.85 or 0.95, clean and with pollution (runs 11–18).",
    "pollution-levels": "The pollution-levels block: clean runs, then steps and ramps at pollution duty 0.25, 0.5 and 1.0 (runs 21–43).",
    "pollution-levels-test": "The check before the pollution-levels block: dark, warm-up, then the pollution LED at each duty.",
    "pollution-check": "A 2-minute check that switching the pollution LED changes the reading.",
}
TOP_OPTIONS = ("seed", "time_scale", "only", "skip")  # shown in the start form's first section

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
MOCK_PASSWORD = os.environ.get("MQTT_MOCK_PASSWORD")
APP_MQTT = (os.environ.get("MQTT_USERNAME", "app"), os.environ.get("MQTT_APP_PASSWORD"))


class Rig:
    """The real rig, or the mock one: each has its own data folder and broker topics."""

    def __init__(self, mock):
        self.mock = mock
        self.store = Store(DATA_DIR / "mock" if mock else DATA_DIR)
        self.bridge = Bridge(self.store, MQTT_URL, *APP_MQTT, client_id="light-rig-app-mock" if mock else "light-rig-app",
                             prefix="mock" if mock else "rig")


REAL = Rig(mock=False)
MOCK = Rig(mock=True) if MOCK_PASSWORD else None


def rig_for(request):
    return MOCK if MOCK and request.session.get("mock") else REAL


class MockAgent:
    """The mock rig's Pi: pi_agent.py with simulated hardware (mock_rig/), run by the app."""

    def __init__(self):
        self.proc = None

    def start(self):
        secrets_ = ("APP_PASSWORD", "SESSION_SECRET", "MQTT_APP_PASSWORD", "MQTT_PI_PASSWORD", "MQTT_LAPTOP_PASSWORD")
        env = {k: v for k, v in os.environ.items() if k not in secrets_}
        env.update(RIG_BROKER=MQTT_URL, RIG_MQTT_USERNAME="mock", RIG_MQTT_PASSWORD=MOCK_PASSWORD,
                   RIG_TOPIC_PREFIX="mock", RIG_MOCK="1", RIG_DATA_DIR=str(DATA_DIR / "mock" / "pi"),
                   RIG_ENV_FILE="/nonexistent")
        self.proc = subprocess.Popen([sys.executable, "-u", str(AGENT_SCRIPT)], env=env, cwd=AGENT_SCRIPT.parent)

    async def watch(self):
        while True:
            await asyncio.sleep(15)
            if self.proc and self.proc.poll() is not None:
                print(f"The mock rig's agent stopped (exit code {self.proc.returncode}); starting it again.")
                self.start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


mock_agent = MockAgent() if MOCK else None


@asynccontextmanager
async def lifespan(app):
    loop = asyncio.get_running_loop()
    for rig in (REAL, MOCK):
        if rig:
            rig.bridge.start(loop)
    watcher = None
    if mock_agent:
        mock_agent.start()
        watcher = asyncio.create_task(mock_agent.watch())
    yield
    if watcher:
        watcher.cancel()
        mock_agent.stop()
    for rig in (REAL, MOCK):
        if rig:
            rig.bridge.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="rig_session",
                   max_age=30 * 24 * 3600, same_site="lax", https_only=SECURE_COOKIES)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["minutes"] = lambda m: f"{m:.0f} min" if m and m >= 1 else f"{(m or 0) * 60:.0f} s"
templates.env.filters["size"] = lambda b: f"{b / 1e6:.1f} MB" if b >= 1e5 else f"{b / 1e3:.0f} kB"


# ---- login ----
class NeedLogin(Exception):
    pass


@app.exception_handler(NeedLogin)
async def need_login(request, exc):
    if request.url.path.startswith("/api/") or request.method != "GET":
        return JSONResponse({"ok": False, "error": "You're logged out. Reload the page to log in."}, status_code=401)
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


@app.middleware("http")
async def same_origin_only(request, call_next):
    """Refuse changes sent from other sites (the login cookie is SameSite=Lax too)."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"ok": False, "error": "Cross-site request refused."}, status_code=403)
    return await call_next(request)


def user(request):
    if request.session.get("user") != USERNAME:
        raise NeedLogin()
    return USERNAME


FAILED_LOGINS = {}


def client_ip(request):
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


def page_context(request, name):
    rig = rig_for(request)
    return {"status": rig.bridge.public_status(), "flash": request.session.pop("flash", None),
            "page": name.split(".")[0], "mock": rig.mock, "mock_available": MOCK is not None,
            "path": request.url.path}


def render(request, name, **context):
    return templates.TemplateResponse(request, name, {**page_context(request, name), **context})


@app.post("/mode")
def switch_mode(request: Request, mock: str = Form("0"), next: str = Form("/")):
    user(request)
    request.session["mock"] = mock == "1" and MOCK is not None
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    if target.startswith("/sessions/"):
        target = "/sessions"  # that session belongs to the other rig
    return RedirectResponse(target, status_code=303)


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": None, "page": "login"})


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    ip = client_ip(request)
    now = time.time()
    recent = [t for t in FAILED_LOGINS.get(ip, []) if now - t < 900]
    if len(recent) >= 10:
        error = "Too many wrong tries. Wait 15 minutes, then try again."
    elif hmac.compare_digest(username.encode(), USERNAME.encode()) & hmac.compare_digest(password.encode(), PASSWORD.encode()):
        FAILED_LOGINS.pop(ip, None)
        request.session.clear()
        request.session["user"] = USERNAME
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        return RedirectResponse(target, status_code=303)
    else:
        FAILED_LOGINS[ip] = recent + [now]
        await asyncio.sleep(0.5)
        error = "That username and password don't match."
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": error, "page": "login"},
                                      status_code=401)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---- pages ----
def plan_catalog():
    """Everything the start form needs, taken from session.py itself so it never drifts."""
    parser = plans.build_parser()
    groups, top = [], {}
    for group in parser._action_groups:
        fields = []
        for action in group._group_actions:
            if not action.option_strings or action.dest in plans.NOT_FROM_APP:
                continue
            default = action.default
            field = {"name": action.dest, "flag": max(action.option_strings, key=len),
                     "help": (action.help or "").replace("%%", "%"), "choices": action.choices,
                     "nargs": action.nargs, "number": action.type in (int, float),
                     "default": " ".join(map(str, default)) if isinstance(default, list) else default}
            if action.dest in TOP_OPTIONS:
                top[action.dest] = field
            else:
                fields.append(field)
        if fields:
            title = group.title if group.title not in ("options", "optional arguments") else "General"
            groups.append({"title": title[0].upper() + title[1:], "fields": fields})
    return {"plans": [{"name": name, "info": PLAN_INFO.get(name, ""),
                       "runs": [{"number": r[0], "name": r[1]} for r in rows]}
                      for name, rows in plans.PLANS.items()],
            "groups": groups, "top": top}


@app.get("/")
def dashboard(request: Request):
    user(request)
    return render(request, "dashboard.html", catalog=plan_catalog())


@app.get("/sessions")
def sessions_page(request: Request):
    user(request)
    rig = rig_for(request)
    store, bridge = rig.store, rig.bridge
    return render(request, "sessions.html", sessions=store.sessions(), tests=store.single_tests())


@app.get("/sessions/{sid}")
def session_page(request: Request, sid: str):
    user(request)
    rig = rig_for(request)
    store, bridge = rig.store, rig.bridge
    detail = store.session_detail(sid) if SESSION_ID.match(sid) else None
    if not detail:
        raise HTTPException(404, "No such session")
    check = store.checks / f"{sid}.txt"
    return render(request, "session.html", s=detail, log=store.session_log(sid),
                  check=check.read_text() if check.exists() else None,
                  check_time=time.strftime("%d %b %Y, %H:%M", time.localtime(check.stat().st_mtime)) if check.exists() else None,
                  backup=bridge.backup_results.get(sid))


@app.get("/setup")
def setup_page(request: Request):
    user(request)
    broker = PUBLIC_BROKER_URL
    pi_env = f"RIG_BROKER={broker}\nRIG_MQTT_USERNAME=pi\nRIG_MQTT_PASSWORD={os.environ.get('MQTT_PI_PASSWORD', '')}\n"
    laptop = os.environ.get("MQTT_LAPTOP_PASSWORD")
    laptop_env = f"RIG_BROKER={broker}\nRIG_MQTT_USERNAME=laptop\nRIG_MQTT_PASSWORD={laptop}\n" if laptop else None
    return render(request, "setup.html", broker=broker, pi_env=pi_env, laptop_env=laptop_env,
                  has_pi_password=bool(os.environ.get("MQTT_PI_PASSWORD")))


# ---- files ----
@app.get("/files/{name}")
def download(request: Request, name: str):
    user(request)
    rig = rig_for(request)
    store, bridge = rig.store, rig.bridge
    path = store.file_path(name)
    if not path:
        raise HTTPException(404, "No such file")
    if name.endswith(".png"):
        return FileResponse(path, media_type="image/png")
    media = "text/csv" if name.endswith(".csv") else "application/x-ndjson"
    return FileResponse(path, media_type=media, filename=name)


def zip_response(rig, prefix, filename):
    if rig.mock:
        filename = filename.replace("light-rig_", "light-rig-MOCK_")
    tmp = tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024)
    rig.store.zip_to(tmp, prefix)
    size = tmp.tell()
    tmp.seek(0)

    def chunks():
        try:
            while block := tmp.read(1024 * 1024):
                yield block
        finally:
            tmp.close()
    return StreamingResponse(chunks(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "Content-Length": str(size)})


@app.get("/sessions/{sid}/download")
def download_session(request: Request, sid: str):
    user(request)
    if not SESSION_ID.match(sid):
        raise HTTPException(404, "No such session")
    return zip_response(rig_for(request), sid, f"light-rig_{sid}.zip")


@app.get("/download/all")
def download_all(request: Request):
    user(request)
    return zip_response(rig_for(request), None, f"light-rig_all_{time.strftime('%Y%m%d-%H%M')}.zip")


@app.post("/upload")
async def upload(request: Request, files: list[UploadFile] = File(...)):
    user(request)
    rig = rig_for(request)
    store, bridge = rig.store, rig.bridge
    added, skipped = [], []
    for f in files:
        data = await f.read()
        a, s = await asyncio.to_thread(store.add_upload, f.filename, data)
        added += a
        skipped += s
    note = f"Added {len(added)} file{'s' if len(added) != 1 else ''}."
    if skipped:
        note += f" Skipped {len(skipped)}: " + "; ".join(skipped[:8]) + (" …" if len(skipped) > 8 else "")
    request.session["flash"] = note
    return RedirectResponse("/sessions", status_code=303)


@app.post("/sessions/{sid}/check")
def run_check(request: Request, sid: str):
    user(request)
    rig = rig_for(request)
    store, bridge = rig.store, rig.bridge
    if not SESSION_ID.match(sid) or not store.session_detail(sid):
        raise HTTPException(404, "No such session")
    try:
        result = subprocess.run([sys.executable, str(CHECK_SCRIPT), sid, "--data-dir", store.runs.name, "--plot"],
                                capture_output=True, text=True, timeout=300,
                                env={**os.environ, "MPLBACKEND": "Agg"}, cwd=store.root)
        output = result.stdout + (("\n" + result.stderr) if result.stderr.strip() else "")
    except subprocess.TimeoutExpired:
        output = "The checks took longer than 5 minutes and were stopped."
    (store.checks / f"{sid}.txt").write_text(output)
    plots = sorted(p.name for p in store.runs.glob(f"{sid}_*_plot.png"))
    return {"ok": True, "output": output, "plots": plots}


# ---- live data and commands ----
def sse(event):
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


@app.get("/api/events")
async def events(request: Request):
    user(request)
    rig = rig_for(request)
    store, bridge = rig.store, rig.bridge
    queue = bridge.subscribe()

    async def stream():
        try:
            yield sse(bridge.snapshot())
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), 15)
                except asyncio.TimeoutError:
                    yield ": still here\n\n"
                    continue
                yield sse(event)
        finally:
            bridge.unsubscribe(queue)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def run_kind(row):
    if row["change"] == "dark":
        return "dark"
    if row["name"].startswith(("warmup", "baseline")):
        return "settle"
    return {"low": "low", "high": "high", "check": "check"}.get(row["pollution_level"], "clean")


def preview(plan, options):
    argv = plans.argv_from_options(plan, options)
    a = plans.parse_args(argv, raise_errors=True)
    order_known = a.seed is not None or plan != "pollution-levels"
    a.pollution_order = plans.pollution_order(a) if a.seed is not None else (
        list(plans.POLLUTION_DUTIES) if plan == "pollution-levels" else None)
    rows = plans.build_plan(a)
    if not rows:
        raise plans.OptionError("Nothing to run: every run is left out.")
    return {"ok": True, "plan": plan, "order_known": order_known, "pollution_order": a.pollution_order,
            "total_minutes": sum(r["minutes"] for r in rows),
            "rows": [{"number": r["number"], "name": r["name"], "minutes": r["minutes"], "kind": run_kind(r),
                      "test_led": plans.describe_levels(a, r), "pollution": plans.describe_pollution(a, r),
                      "purpose": r["purpose"]} for r in rows]}


async def body(request):
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise HTTPException(400, "Send a JSON object.")
    return data


@app.post("/api/preview")
async def api_preview(request: Request):
    user(request)
    data = await body(request)
    try:
        return preview(data.get("plan"), data.get("options") or {})
    except plans.OptionError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/command")
async def api_command(request: Request):
    user(request)
    rig = rig_for(request)
    store, bridge = rig.store, rig.bridge
    data = await body(request)
    action = data.get("action")
    if action == "start":
        try:
            preview(data.get("plan"), data.get("options") or {})
        except plans.OptionError as e:
            return {"ok": False, "error": str(e)}
        return await bridge.command("start", plan=data["plan"], options=data.get("options") or {})
    if action == "stop":
        return await bridge.command("stop")
    if action == "backup":
        sid = str(data.get("session") or "")
        if not SESSION_ID.match(sid):
            return {"ok": False, "error": "That isn't a session id."}
        bridge.backup_results.pop(sid, None)
        return await bridge.command("backup", timeout=30, session=sid)
    return {"ok": False, "error": f"Unknown action '{action}'."}


@app.get("/api/backup/{sid}")
def api_backup(request: Request, sid: str):
    user(request)
    rig = rig_for(request)
    store, bridge = rig.store, rig.bridge
    return {"result": bridge.backup_results.get(sid)}


@app.get("/healthz")
def healthz():
    return {"ok": True, "broker": REAL.bridge.connected,
            "mock_agent": None if not mock_agent else mock_agent.proc is not None and mock_agent.proc.poll() is None}


@app.exception_handler(404)
async def not_found(request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    context = page_context(request, "error.html") if request.session.get("user") == USERNAME else {
        "page": "error", "status": {"state": "unknown"}, "flash": None, "mock": False, "mock_available": False}
    return templates.TemplateResponse(request, "error.html", {**context, "message": getattr(exc, "detail", "Not found")},
                                      status_code=404)


templates.env.globals["session_time"] = session_time

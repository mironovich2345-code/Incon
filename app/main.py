"""Instagram Content Intelligence — веб-интерфейс.

Запуск: uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import json
import secrets
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import analytics, claude_ai, classify, importers, planner, publishing, references, reports
from .config import BASE_DIR, settings
from .db import connect, get_or_create_account, init_db, kv_get, kv_set, normalize_username, own_account, row, rows
from .metrics import CATALOG, SCOPE_LABELS, STATUS_LABELS, by_scope
from .security import TokenStoreError, delete_tokens, token_status

app = FastAPI(title="Instagram Content Intelligence", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
_basic = HTTPBasic(auto_error=False)

conn = connect()
init_db(conn)


# ---------------- Шаблонные фильтры ----------------
def _num(v, digits: int = 0, suffix: str = ""):
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:,.{digits}f}".replace(",", " ") + suffix
    return v


templates.env.filters["num"] = _num
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []
templates.env.globals.update(
    TOPICS=classify.TOPIC_LIST, FORMATS=classify.FORMAT_LIST, HOOKS=classify.HOOK_LIST,
    DRAFT_STATUS=publishing.STATUS_LABELS, settings=settings,
)


def auth(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """Если задан APP_PASSWORD — интерфейс закрыт Basic-авторизацией."""
    if not settings.app_password:
        return
    ok = credentials is not None and secrets.compare_digest(credentials.password.encode(), settings.app_password.encode())
    if not ok:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("flash", request.query_params.get("msg"))
    ctx.setdefault("error", request.query_params.get("err"))
    ctx["own"] = own_account(conn)
    ctx["reauth"] = kv_get(conn, "reauth_required") if settings.meta_api_enabled else None
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    from urllib.parse import urlencode
    q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    return RedirectResponse(url + (("&" if "?" in url else "?") + urlencode(q) if q else ""), status_code=303)


D = [Depends(auth)]


# ---------------- Обзор ----------------
@app.get("/", response_class=HTMLResponse, dependencies=D)
def overview(request: Request):
    acc = own_account(conn)
    summary = analytics.account_summary(conn, acc["id"]) if acc else None
    reels = analytics.own_reels(conn)
    return render(request, "overview.html", summary=summary, top=reels[:5],
                  by_format=analytics.compare_by(reels, "format"),
                  plan_today=rows(conn, "SELECT * FROM content_plan WHERE day=? ORDER BY slot", (date.today().isoformat(),)),
                  pending=rows(conn, "SELECT * FROM publication_drafts WHERE status='pending_approval'"))


@app.post("/own", dependencies=D)
def set_own(username: str = Form(...), followers: str = Form("")):
    acc_id = get_or_create_account(conn, username, is_own=True)
    conn.execute("UPDATE accounts SET is_own=0 WHERE id!=? AND is_own=1", (acc_id,))
    conn.commit()
    if followers.strip():
        importers.record_followers(conn, acc_id, int(followers), "manual")
    return back("/", "Аккаунт сохранён")


# ---------------- Мои Reels ----------------
@app.get("/reels", response_class=HTMLResponse, dependencies=D)
def reels_page(request: Request, sort: str = "views", group: str = "format"):
    reels = analytics.own_reels(conn, sort)
    group = group if group in {"format", "topic", "hook_type", "published_at"} else "format"
    return render(request, "reels.html", reels=reels, groups=analytics.compare_by(reels, group), group=group, sort=sort)


@app.post("/media/{media_id}/edit", dependencies=D)
def edit_media(media_id: int, request: Request, format: str = Form(""), topic: str = Form(""), hook_type: str = Form(""),
               hook_text: str = Form(""), leads: str = Form(""), duration_sec: str = Form(""), next: str = Form("/reels")):
    conn.execute(
        """UPDATE media SET format=?, topic=?, hook_type=?, hook_text=?, leads=?, duration_sec=COALESCE(?, duration_sec),
           classification_source='manual' WHERE id=?""",
        (format or None, topic or None, hook_type or None, hook_text or None,
         int(leads) if leads.strip() else None, float(duration_sec) if duration_sec.strip() else None, media_id),
    )
    conn.commit()
    return back(next if next.startswith("/") else "/reels", "Сохранено")


# ---------------- Конкуренты ----------------
@app.get("/competitors", response_class=HTMLResponse, dependencies=D)
def competitors_page(request: Request):
    return render(request, "competitors.html", comps=analytics.competitors_table(conn))


@app.post("/competitors/add", dependencies=D)
def competitors_add(username: str = Form(...)):
    u = normalize_username(username)
    if not u:
        return back("/competitors", err="Пустой username")
    get_or_create_account(conn, u)
    return back("/competitors", f"@{u} добавлен")


@app.post("/competitors/{acc_id}/toggle", dependencies=D)
def competitors_toggle(acc_id: int):
    conn.execute("UPDATE accounts SET tracked = 1 - tracked WHERE id=? AND is_own=0", (acc_id,))
    conn.commit()
    return back("/competitors")


@app.get("/competitors/{acc_id}", response_class=HTMLResponse, dependencies=D)
def competitor_detail(request: Request, acc_id: int):
    acc = row(conn, "SELECT * FROM accounts WHERE id=?", (acc_id,))
    if not acc:
        raise HTTPException(404)
    items = analytics.competitor_media(conn, acc_id)
    stats = analytics.score_outliers(items)
    history = rows(conn, "SELECT * FROM account_snapshots WHERE account_id=? ORDER BY captured_on", (acc_id,))
    return render(request, "competitor.html", acc=acc, items=items, stats=stats,
                  patterns=analytics.pattern_summary(items), history=history)


@app.get("/top", response_class=HTMLResponse, dependencies=D)
def top_page(request: Request):
    top = analytics.top_reels_all(conn, 100)
    return render(request, "top.html", top=top, patterns=analytics.pattern_summary([dict(t, is_outlier=t["outlier_ratio"] >= settings.outlier_ratio) for t in top]))


# ---------------- Импорт ----------------
@app.get("/import", response_class=HTMLResponse, dependencies=D)
def import_page(request: Request):
    return render(request, "import.html")


@app.post("/import/{kind}", dependencies=D)
async def import_file(kind: str, file: UploadFile = File(...), username: str = Form("")):
    content = await file.read()
    acc = own_account(conn)
    try:
        if kind == "own_reels":
            if not (username or acc):
                return back("/import", err="Укажите ваш username")
            res = importers.import_own_reels(conn, content, username or acc["username"])
        elif kind == "account_daily":
            if not (username or acc):
                return back("/import", err="Укажите ваш username")
            res = importers.import_account_daily(conn, content, username or acc["username"])
        elif kind == "competitor_posts":
            res = importers.import_competitor_posts(conn, content, username or None)
        else:
            raise HTTPException(404)
    except importers.ImportErrorWithRow as exc:
        return back("/import", err=str(exc))
    msg = f"Импортировано строк: {res['imported']}. Распознаны колонки: {', '.join(res['columns'])}"
    err = "; ".join(res["errors"][:10]) if res["errors"] else None
    return back("/import", msg, err)


# ---------------- Метрики ----------------
@app.get("/metrics", response_class=HTMLResponse, dependencies=D)
def metrics_page(request: Request):
    checks = {(r["metric"], r["scope"]): r for r in rows(conn, "SELECT * FROM metric_availability")}
    return render(request, "metrics.html", groups=by_scope(), scope_labels=SCOPE_LABELS,
                  status_labels=STATUS_LABELS, checks=checks)


# ---------------- Референсы ----------------
@app.get("/references", response_class=HTMLResponse, dependencies=D)
def references_page(request: Request):
    return render(request, "references.html", refs=rows(conn, "SELECT * FROM references_lib ORDER BY id DESC"),
                  ffmpeg=references.ffmpeg_available(), whisper=references.whisper_available())


@app.post("/references/upload", dependencies=D)
async def references_upload(title: str = Form(...), author: str = Form(""), source_url: str = Form(""),
                            rights_note: str = Form(""), tags: str = Form(""),
                            video: UploadFile = File(...), subtitles: UploadFile | None = File(None)):
    if not (video.filename or "").lower().endswith((".mp4", ".mov", ".m4v")):
        return back("/references", err="Нужен файл MP4/MOV")
    if not references.ffmpeg_available():
        return back("/references", err="ffmpeg не установлен на сервере")
    cur = conn.execute("INSERT INTO references_lib(title, author, source_url, rights_note, tags) VALUES (?,?,?,?,?)",
                       (title, author or None, source_url or None, rights_note or None, tags or None))
    conn.commit()
    ref_id = cur.lastrowid
    workdir = settings.references_dir / str(ref_id)
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "video.mp4"
    with path.open("wb") as fh:
        shutil.copyfileobj(video.file, fh)
    subs = None
    if subtitles is not None and subtitles.filename:
        subs = (await subtitles.read()).decode("utf-8", errors="replace")
    try:
        res = references.process_video(path, workdir, transcript_upload=subs)
    except references.MediaToolError as exc:
        return back(f"/references/{ref_id}", err=str(exc))
    conn.execute(
        """UPDATE references_lib SET video_path=?, duration_sec=?, width=?, height=?, transcript=?, transcript_segments=?,
           transcript_source=?, keyframes=?, cuts=?, structure=? WHERE id=?""",
        (str(path), res["duration_sec"], res["width"], res["height"], res["transcript"],
         json.dumps(res["transcript_segments"], ensure_ascii=False), res["transcript_source"],
         json.dumps(res["keyframes"], ensure_ascii=False), json.dumps(res["cuts"]),
         json.dumps(res["structure"], ensure_ascii=False), ref_id),
    )
    conn.commit()
    return back(f"/references/{ref_id}", "Референс обработан", "; ".join(res["warnings"]) or None)


@app.get("/references/{ref_id}", response_class=HTMLResponse, dependencies=D)
def reference_detail(request: Request, ref_id: int):
    ref = row(conn, "SELECT * FROM references_lib WHERE id=?", (ref_id,))
    if not ref:
        raise HTTPException(404)
    prompt = claude_ai.reference_analysis_prompt(ref) if ref.get("structure") else None
    return render(request, "reference.html", ref=ref, prompt=prompt, structure=json.loads(ref.get("structure") or "{}"),
                  claude_ok=claude_ai.claude_available())


@app.post("/references/{ref_id}/analyze", dependencies=D)
def reference_analyze(ref_id: int):
    ref = row(conn, "SELECT * FROM references_lib WHERE id=?", (ref_id,))
    if not ref:
        raise HTTPException(404)
    frames = [k["path"] for k in json.loads(ref.get("keyframes") or "[]")]
    res = claude_ai.ask(claude_ai.reference_analysis_prompt(ref), images=frames)
    if not res["ok"]:
        return back(f"/references/{ref_id}", err=res["error"])
    conn.execute("UPDATE references_lib SET analysis_md=? WHERE id=?", (res["text"], ref_id))
    conn.commit()
    return back(f"/references/{ref_id}", "Разбор Claude сохранён")


@app.post("/references/{ref_id}/analysis", dependencies=D)
def reference_save_analysis(ref_id: int, analysis_md: str = Form(...)):
    conn.execute("UPDATE references_lib SET analysis_md=? WHERE id=?", (analysis_md, ref_id))
    conn.commit()
    return back(f"/references/{ref_id}", "Разбор сохранён")


@app.get("/frames/{ref_id}/{name}", dependencies=D)
def frame(ref_id: int, name: str):
    base = (settings.references_dir / str(ref_id) / "frames").resolve()
    p = (base / name).resolve()
    if base not in p.parents or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# ---------------- Сценарии ----------------
def _insights_text() -> str:
    reels = analytics.own_reels(conn)
    if not reels:
        return "Данных по моим роликам пока нет."
    lines = []
    for field, label in (("format", "формат"), ("topic", "тема"), ("hook_type", "хук")):
        for g in analytics.compare_by(reels, field)[:3]:
            lines.append(f"- {label} «{g['group']}»: медиана просмотров {_num(g['views'])} (n={g['n_views']}), "
                         f"ср. просмотр {_num(g['avg_watch_sec'], 1)} с (n={g['n_avg_watch_sec']}), роликов {g['count']}")
    return "\n".join(lines)


@app.get("/scripts", response_class=HTMLResponse, dependencies=D)
def scripts_page(request: Request):
    return render(request, "scripts.html", scripts=rows(conn, "SELECT * FROM scripts ORDER BY id DESC"),
                  refs=rows(conn, "SELECT id, title FROM references_lib ORDER BY id DESC"),
                  plan=rows(conn, "SELECT * FROM content_plan WHERE script_id IS NULL ORDER BY day, slot"),
                  claude_ok=claude_ai.claude_available())


@app.post("/scripts/generate", dependencies=D)
async def scripts_generate(request: Request, topic: str = Form(...), format: str = Form(...), hook_type: str = Form(...),
                           duration: int = Form(40), mode: str = Form("template"), plan_id: str = Form("")):
    form = await request.form()
    ref_ids = [int(x) for x in form.getlist("ref_ids")]
    refs = [r for r in (row(conn, "SELECT * FROM references_lib WHERE id=?", (i,)) for i in ref_ids) if r]
    title = f"{topic}: {format}"
    if mode == "claude":
        prompt = claude_ai.script_prompt(topic, format, hook_type, refs, _insights_text(), duration)
        res = claude_ai.ask(prompt)
        if not res["ok"]:
            body, storyboard, gen = f"> {res['error']}\n\nПромпт для ручного запуска:\n\n```\n{prompt}\n```", [], "prompt"
        else:
            body, storyboard, gen = res["text"], claude_ai.extract_storyboard(res["text"]) or [], "claude"
    else:
        t = claude_ai.template_script(topic, format, hook_type, duration)
        body, storyboard, gen = t["body_md"], t["storyboard"], "template"
    cur = conn.execute(
        "INSERT INTO scripts(title, topic, format, hook_type, reference_ids, body_md, storyboard, generator) VALUES (?,?,?,?,?,?,?,?)",
        (title, topic, format, hook_type, json.dumps(ref_ids), body, json.dumps(storyboard, ensure_ascii=False), gen))
    sid = cur.lastrowid
    if plan_id.strip():
        conn.execute("UPDATE content_plan SET script_id=?, status='scripted' WHERE id=?", (sid, int(plan_id)))
    conn.commit()
    return back(f"/scripts/{sid}", "Сценарий создан")


@app.get("/scripts/{sid}", response_class=HTMLResponse, dependencies=D)
def script_detail(request: Request, sid: int):
    s = row(conn, "SELECT * FROM scripts WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404)
    return render(request, "script.html", s=s, storyboard=json.loads(s.get("storyboard") or "[]"))


@app.post("/scripts/{sid}", dependencies=D)
def script_save(sid: int, body_md: str = Form(...), status: str = Form("draft")):
    conn.execute("UPDATE scripts SET body_md=?, status=? WHERE id=?", (body_md, status, sid))
    conn.commit()
    return back(f"/scripts/{sid}", "Сохранено")


# ---------------- Контент-план ----------------
@app.get("/plan", response_class=HTMLResponse, dependencies=D)
def plan_page(request: Request):
    items = rows(conn, """SELECT p.*, s.title AS script_title, m.views AS views, m.permalink AS permalink
                          FROM content_plan p LEFT JOIN scripts s ON s.id=p.script_id LEFT JOIN media m ON m.id=p.media_id
                          ORDER BY p.day, p.slot""")
    days: dict[str, list] = {}
    for it in items:
        days.setdefault(it["day"], []).append(it)
    own_media = rows(conn, "SELECT m.id, m.permalink, m.published_at FROM media m JOIN accounts a ON a.id=m.account_id WHERE a.is_own=1 ORDER BY published_at DESC")
    return render(request, "plan.html", days=days, own_media=own_media,
                  default_start=(date.today() + timedelta(days=1)).isoformat())


@app.post("/plan/create", dependencies=D)
def plan_create(start: str = Form(...), replace: str = Form("")):
    n = planner.create_plan(conn, date.fromisoformat(start), replace=bool(replace))
    return back("/plan", f"Добавлено слотов: {n}")


@app.post("/plan/{pid}", dependencies=D)
def plan_update(pid: int, title: str = Form(""), status: str = Form("planned"), notes: str = Form(""), media_id: str = Form("")):
    conn.execute("UPDATE content_plan SET title=?, status=?, notes=?, media_id=? WHERE id=?",
                 (title or None, status, notes or None, int(media_id) if media_id.strip() else None, pid))
    if media_id.strip():
        p = row(conn, "SELECT * FROM content_plan WHERE id=?", (pid,))
        # Связываем факт с планом: формат/тема/хук ролика берутся из плана эксперимента.
        conn.execute("UPDATE media SET format=?, topic=?, hook_type=?, classification_source='manual' WHERE id=?",
                     (p["format"], p["topic"], p["hook_type"], int(media_id)))
    conn.commit()
    return back("/plan", "Слот обновлён")


# ---------------- Публикации на утверждение ----------------
@app.get("/drafts", response_class=HTMLResponse, dependencies=D)
def drafts_page(request: Request):
    return render(request, "drafts.html",
                  drafts=rows(conn, "SELECT d.*, p.day, p.slot FROM publication_drafts d LEFT JOIN content_plan p ON p.id=d.plan_id ORDER BY d.id DESC"),
                  plan=rows(conn, "SELECT * FROM content_plan ORDER BY day, slot"),
                  scripts=rows(conn, "SELECT id, title FROM scripts ORDER BY id DESC"))


@app.post("/drafts/new", dependencies=D)
async def drafts_new(caption: str = Form(""), cover_note: str = Form(""), scheduled_for: str = Form(""),
                     plan_id: str = Form(""), script_id: str = Form(""), video: UploadFile | None = File(None)):
    cur = conn.execute("INSERT INTO publication_drafts(plan_id, script_id, caption, cover_note, scheduled_for) VALUES (?,?,?,?,?)",
                       (int(plan_id) if plan_id else None, int(script_id) if script_id else None, caption, cover_note or None, scheduled_for or None))
    did = cur.lastrowid
    if video is not None and video.filename:
        d = settings.drafts_dir / str(did)
        d.mkdir(parents=True, exist_ok=True)
        p = d / Path(video.filename).name
        with p.open("wb") as fh:
            shutil.copyfileobj(video.file, fh)
        conn.execute("UPDATE publication_drafts SET video_path=? WHERE id=?", (str(p), did))
    conn.execute("INSERT INTO draft_events(draft_id, from_status, to_status) VALUES (?, NULL, 'draft')", (did,))
    conn.commit()
    return back(f"/drafts/{did}", "Черновик создан")


@app.get("/drafts/{did}", response_class=HTMLResponse, dependencies=D)
def draft_detail(request: Request, did: int):
    d = row(conn, "SELECT * FROM publication_drafts WHERE id=?", (did,))
    if not d:
        raise HTTPException(404)
    return render(request, "draft.html", d=d, problems=publishing.validate(d),
                  events=rows(conn, "SELECT * FROM draft_events WHERE draft_id=? ORDER BY id", (did,)),
                  allowed=sorted(publishing.TRANSITIONS.get(d["status"], set())))


@app.post("/drafts/{did}/edit", dependencies=D)
def draft_edit(did: int, caption: str = Form(""), cover_note: str = Form(""), scheduled_for: str = Form("")):
    d = row(conn, "SELECT status FROM publication_drafts WHERE id=?", (did,))
    if d and d["status"] not in {"draft", "changes_requested", "rejected"}:
        return back(f"/drafts/{did}", err="Редактировать можно только черновик или ролик, возвращённый на правки")
    conn.execute("UPDATE publication_drafts SET caption=?, cover_note=?, scheduled_for=?, updated_at=datetime('now') WHERE id=?",
                 (caption, cover_note or None, scheduled_for or None, did))
    conn.commit()
    return back(f"/drafts/{did}", "Сохранено")


@app.post("/drafts/{did}/transition", dependencies=D)
def draft_transition(did: int, to_status: str = Form(...), comment: str = Form("")):
    try:
        publishing.transition(conn, did, to_status, comment or None)
    except publishing.TransitionError as exc:
        return back(f"/drafts/{did}", err=str(exc))
    return back(f"/drafts/{did}", f"Статус: {publishing.STATUS_LABELS[to_status]}")


# ---------------- Отчёты ----------------
@app.get("/reports", response_class=HTMLResponse, dependencies=D)
def reports_page(request: Request):
    return render(request, "reports.html", reports=rows(conn, "SELECT id, kind, title, created_at FROM reports ORDER BY id DESC"),
                  claude_ok=claude_ai.claude_available())


@app.post("/reports/generate", dependencies=D)
def reports_generate(kind: str = Form("rules")):
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    if kind == "claude":
        prompt = claude_ai.report_prompt(reports.build_context(conn))
        res = claude_ai.ask(prompt)
        body = res["text"] if res["ok"] else f"> {res['error']}\n\nПромпт для ручного запуска:\n\n```\n{prompt}\n```"
        title = f"Рекомендации Claude — {stamp}" if res["ok"] else f"Промпт для Claude — {stamp}"
    else:
        body, title = reports.rules_report(conn), f"Автоотчёт — {stamp}"
    cur = conn.execute("INSERT INTO reports(kind, title, body_md) VALUES (?,?,?)", (kind, title, body))
    conn.commit()
    return back(f"/reports/{cur.lastrowid}")


@app.get("/reports/{rid}", response_class=HTMLResponse, dependencies=D)
def report_detail(request: Request, rid: int):
    r = row(conn, "SELECT * FROM reports WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404)
    return render(request, "report.html", r=r)


# ---------------- Подключение Meta API ----------------
def _secure_cookie() -> bool:
    return settings.meta_redirect_uri.startswith("https://")


@app.get("/connect", response_class=HTMLResponse, dependencies=D)
def connect_page(request: Request):
    from .meta.diagnostics import last_report
    return render(request, "connect.html", tokens=token_status(conn), problems=settings.meta_config_problems(),
                  kv={k: kv_get(conn, k) for k in ("ig_user_id", "ig_username", "page_id", "token_type", "account_source",
                                                  "connected_at", "last_sync", "reauth_required")},
                  report=last_report(conn))


@app.get("/meta/login", dependencies=D)
def meta_login():
    from .meta.client import GraphError, oauth_dialog_url
    from .meta.oauth import STATE_COOKIE, STATE_TTL, new_state
    if not settings.meta_api_enabled:
        return back("/connect", err="META_API_ENABLED=false — включите API в .env")
    problems = settings.meta_config_problems()
    if problems:
        return back("/connect", err="Настройка не завершена: " + "; ".join(problems))
    state = new_state(conn)
    try:
        url = oauth_dialog_url(state)
    except GraphError as exc:
        return back("/connect", err=str(exc))
    resp = RedirectResponse(url, status_code=303)
    # SameSite=Lax: cookie приходит при возврате с facebook.com (top-level GET-редирект).
    resp.set_cookie(STATE_COOKIE, state, max_age=STATE_TTL, httponly=True, secure=_secure_cookie(),
                    samesite="lax", path="/meta")
    return resp


@app.get("/meta/callback", dependencies=D)
def meta_callback(request: Request, code: str = "", state: str = "", error: str = "", error_reason: str = "",
                  error_description: str = ""):
    from .meta.client import GraphError
    from .meta.oauth import STATE_COOKIE, OAuthError, check_state, complete_login

    def done(resp: RedirectResponse) -> RedirectResponse:
        resp.delete_cookie(STATE_COOKIE, path="/meta")
        return resp

    try:
        check_state(conn, state, request.cookies.get(STATE_COOKIE))
    except OAuthError as exc:
        return done(back("/connect", err=str(exc)))
    if error:
        reason = "вы отменили вход или не выдали разрешения" if error_reason == "user_denied" else (error_description or error)
        return done(back("/connect", err=f"Meta: {reason}"))
    if not code:
        return done(back("/connect", err="Meta не вернула code"))
    try:
        res = complete_login(conn, code)
    except (OAuthError, GraphError, TokenStoreError) as exc:
        return done(back("/connect", err=f"Ошибка подключения: {exc}"))
    msg = f"Подключено: @{res['ig_username'] or res['ig_user_id']} (токен {res['token_type']}, аккаунт найден через {res['account_source']})"
    warn = []
    if res["missing_scopes"]:
        warn.append("не выданы разрешения: " + ", ".join(res["missing_scopes"]))
    if res["unneeded_scopes"]:
        warn.append("выданы лишние разрешения (уберите из конфигурации): " + ", ".join(res["unneeded_scopes"]))
    return done(back("/connect", msg, "; ".join(warn) or None))


def _graph_failure(exc) -> RedirectResponse:
    from .meta.oauth import mark_reauth
    if getattr(exc, "kind", "") == "token":
        mark_reauth(conn, str(exc))
        return back("/connect", err="Токен Meta недействителен или истёк — войдите заново. " + str(exc))
    return back("/connect", err=str(exc))


@app.post("/meta/sync", dependencies=D)
def meta_sync(scope: str = Form("all")):
    if not settings.meta_api_enabled:
        return back("/connect", err="META_API_ENABLED=false — используется резервный ручной импорт")
    from .meta.client import GraphError
    from .meta.sync import client_from_store, sync_competitor, sync_own
    try:
        client, ig_user_id = client_from_store(conn)
    except (GraphError, TokenStoreError) as exc:
        return back("/connect", err=str(exc))
    msgs = []
    try:
        if scope in {"all", "own"}:
            r = sync_own(conn, client, ig_user_id)
            msgs.append(f"Мои Reels: {r['reels']}; недоступных метрик: {len(r['unavailable'])}")
        if scope in {"all", "competitors"}:
            for c in rows(conn, "SELECT username FROM accounts WHERE is_own=0 AND tracked=1"):
                r = sync_competitor(conn, client, ig_user_id, c["username"])
                msgs.append(f"@{c['username']}: " + (f"{r['media']} публикаций" if r["ok"] else f"недоступен — {r['error']}"))
    except GraphError as exc:
        return _graph_failure(exc)
    finally:
        client.close()
    kv_set(conn, "last_sync", datetime.now().isoformat(timespec="minutes"))
    return back("/connect", "; ".join(msgs))


@app.post("/meta/diagnostics", dependencies=D)
def meta_diagnostics_run():
    from .meta import diagnostics
    if not settings.meta_api_enabled:
        return back("/connect", err="META_API_ENABLED=false")
    try:
        rep = diagnostics.run(conn)
    except TokenStoreError as exc:
        return back("/connect", err=str(exc))
    return back("/connect", "Диагностика завершена" + ("" if rep["ok"] else ": есть проблемы — см. таблицу"))


@app.get("/meta/diagnostics.json", dependencies=D)
def meta_diagnostics_json():
    from fastapi.responses import JSONResponse
    from .meta.diagnostics import last_report
    return JSONResponse(last_report(conn) or {"status": "not_run"})


@app.get("/meta/health", dependencies=D)
def meta_health():
    from fastapi.responses import JSONResponse
    from .meta.oauth import token_health
    if not (settings.meta_app_id and settings.meta_app_secret):
        return JSONResponse({"status": "not_configured"})
    try:
        return JSONResponse(token_health(conn))
    except TokenStoreError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


@app.post("/meta/disconnect", dependencies=D)
def meta_disconnect():
    delete_tokens(conn)
    for k in ("ig_user_id", "ig_username", "page_id", "token_type", "account_source", "auth_provider", "reauth_required"):
        kv_set(conn, k, "")
    return back("/connect", "Токены удалены с сервера. Доступ приложения также можно отозвать в настройках Facebook → Бизнес-интеграции.")

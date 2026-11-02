"""OAuth-поток, ошибки API, отсутствующие метрики, диагностика и безопасность секретов."""
import io
import json
import logging
import re
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from app import importers, security
from app.config import settings
from app.db import kv_get, kv_set
from app.meta import client as mc
from app.meta import diagnostics, oauth
from app.meta.client import GraphClient, GraphError

ROOT = Path(__file__).resolve().parent.parent
USER_SHORT, USER_LONG, PAGE_TOKEN, SYS_TOKEN = "EAAshortUSERtoken111", "EAAlongUSERtoken222", "EAApageTOKEN333", "EAAsystemTOKEN444"
APP_SECRET = "appsecretVALUE999"


@pytest.fixture()
def meta_env(monkeypatch):
    key = security.generate_key()
    for k, v in {"meta_app_id": "123", "meta_app_secret": APP_SECRET, "meta_login_config_id": "1313415880861047",
                 "token_encryption_key": key, "token_encryption_keys_old": [], "app_password": "pw",
                 "meta_redirect_uri": "https://incon-production.up.railway.app/meta/callback", "meta_ig_username": "",
                 "meta_api_enabled": True, "railway_environment": "", "railway_public_domain": "", "railway_volume_mount_path": "", "allow_no_password": False}.items():
        monkeypatch.setattr(settings, k, v)
    return key


def router(token_type="USER", pages=True, granular=None, scopes=None, fail=None, calls=None):
    """Мок Graph API. fail: {path_suffix: (status, code, message)}."""
    scopes = scopes or ["instagram_basic", "instagram_manage_insights", "pages_show_list", "pages_read_engagement"]

    def handler(req: httpx.Request):
        path, q = req.url.path, dict(req.url.params)
        if calls is not None:
            calls.append((path, {k: v for k, v in q.items() if k not in {"access_token", "appsecret_proof"}}))
        for suffix, (status, code, msg) in (fail or {}).items():
            if path.endswith(suffix) and (suffix != "/insights" or "metric" in q):
                if suffix == "/insights" and q.get("metric") not in {"reels_skip_rate", "follows_and_unfollows"}:
                    break
                return httpx.Response(status, json={"error": {"message": msg, "code": code, "type": "OAuthException", "fbtrace_id": "TRACE1"}})
        if path.endswith("/oauth/access_token"):
            if q.get("grant_type") == "fb_exchange_token":
                return httpx.Response(200, json={"access_token": USER_LONG, "expires_in": 5184000})
            return httpx.Response(200, json={"access_token": SYS_TOKEN if token_type == "SYSTEM_USER" else USER_SHORT, "expires_in": 3600})
        if path.endswith("/debug_token"):
            gs = [{"scope": "instagram_basic", "target_ids": granular}] if granular else []
            exp = 0 if token_type == "SYSTEM_USER" else int(time.time()) + 50 * 86400
            return httpx.Response(200, json={"data": {"is_valid": True, "type": token_type, "user_id": "777", "scopes": scopes,
                                                      "expires_at": exp, "data_access_expires_at": int(time.time()) + 80 * 86400,
                                                      "granular_scopes": gs}})
        if path.endswith("/me/accounts"):
            data = [{"id": "PAGE1", "name": "Page", "access_token": PAGE_TOKEN,
                     "instagram_business_account": {"id": "IG1", "username": "my_account"}}] if pages else []
            return httpx.Response(200, json={"data": data})
        if path.endswith("/me"):
            return httpx.Response(200, json={"id": "777", "name": "Me"})
        if path.endswith("/IG1") and "business_discovery" in q.get("fields", ""):
            u = re.search(r"username\(([^)]+)\)", q["fields"]).group(1)
            if u == "di.sukharev":
                return httpx.Response(400, json={"error": {"message": "Invalid user id", "code": 110}})
            return httpx.Response(200, json={"business_discovery": {"username": u, "followers_count": 1000, "media": {"data": [
                {"id": "x1", "like_count": 5, "comments_count": 1, "view_count": 300, "media_product_type": "REELS"}]}}})
        if path.endswith("/IG1"):
            return httpx.Response(200, json={"id": "IG1", "username": "my_account", "followers_count": 208, "media_count": 3})
        if path.endswith("/IG1/media"):
            return httpx.Response(200, json={"data": [{"id": "R1", "media_product_type": "REELS", "media_type": "VIDEO"},
                                                      {"id": "P1", "media_product_type": "FEED", "media_type": "IMAGE"}]})
        if path.endswith("/insights"):
            m = q["metric"]
            return httpx.Response(200, json={"data": [{"name": n, "total_value": {"value": 7}} for n in m.split(",")]})
        return httpx.Response(404, json={"error": {"message": f"unknown {path}", "code": 803}})

    return httpx.MockTransport(handler)


# ---------------- OAuth URL и конфигурация ----------------

def test_dialog_url_uses_config_id_not_scope(meta_env):
    url = mc.oauth_dialog_url("STATE")
    q = dict(httpx.URL(url).params)
    assert url.startswith("https://www.facebook.com/")
    assert q["config_id"] == "1313415880861047" and q["response_type"] == "code" and q["override_default_response_type"] == "true"
    assert "scope" not in q and q["redirect_uri"] == "https://incon-production.up.railway.app/meta/callback"
    assert APP_SECRET not in url


def test_dialog_url_requires_config_id(meta_env, monkeypatch):
    monkeypatch.setattr(settings, "meta_login_config_id", "")
    with pytest.raises(GraphError):
        mc.oauth_dialog_url("S")


def test_config_problems(meta_env, monkeypatch):
    assert settings.meta_config_problems() == []
    monkeypatch.setattr(settings, "meta_redirect_uri", "http://example.com/cb?x=1")
    monkeypatch.setattr(settings, "app_password", "")
    p = " ".join(settings.meta_config_problems())
    assert "https" in p and "/meta/callback" in p and "APP_PASSWORD" in p
    assert APP_SECRET not in p


# ---------------- state ----------------

def test_state_valid_and_single_use(conn):
    s = oauth.new_state(conn)
    oauth.check_state(conn, s, s)
    with pytest.raises(oauth.OAuthError):
        oauth.check_state(conn, s, s)  # повторно нельзя


@pytest.mark.parametrize("url_state,cookie", [("other", None), (None, None), ("X", "Y")])
def test_state_mismatch(conn, url_state, cookie):
    s = oauth.new_state(conn)
    with pytest.raises(oauth.OAuthError):
        oauth.check_state(conn, url_state or None, cookie if cookie else (None if url_state is None else s))


def test_state_expired(conn):
    s = oauth.new_state(conn)
    kv_set(conn, "oauth_state", f"{s}:{int(time.time()) - 3600}")
    with pytest.raises(oauth.OAuthError, match="истёк"):
        oauth.check_state(conn, s, s)


# ---------------- complete_login ----------------

def test_login_user_token_page_flow(conn, meta_env, monkeypatch):
    calls = []
    t = router(scopes=["instagram_basic", "instagram_manage_insights", "pages_show_list", "pages_read_engagement",
                       "instagram_manage_messages"], calls=calls)
    monkeypatch.setattr(mc.settings, "meta_app_secret", APP_SECRET)
    res = oauth.complete_login(conn, "CODE", transport=t)
    assert res["ig_user_id"] == "IG1" and res["account_source"] == "me/accounts" and res["exchanged_long_lived"]
    assert res["unneeded_scopes"] == ["instagram_manage_messages"] and res["missing_scopes"] == []
    assert any(p.endswith("/oauth/access_token") and q.get("grant_type") == "fb_exchange_token" for p, q in calls)
    assert security.load_token(conn, "meta_user", key=meta_env) == USER_LONG
    assert security.load_token(conn, "meta_page", "PAGE1", key=meta_env) == PAGE_TOKEN
    assert oauth.active_token(conn) == (PAGE_TOKEN, "meta_page")
    raw = b"".join(r[0] for r in conn.execute("SELECT token_encrypted FROM oauth_tokens"))
    assert USER_LONG.encode() not in raw and PAGE_TOKEN.encode() not in raw
    assert kv_get(conn, "token_type") == "USER" and kv_get(conn, "reauth_required") == ""


def test_login_system_user_without_pages_uses_granular_ids(conn, meta_env):
    calls = []
    res = oauth.complete_login(conn, "CODE", transport=router("SYSTEM_USER", pages=False, granular=["IG1"], calls=calls))
    assert res["account_source"] == "debug_token.granular_scopes" and res["ig_user_id"] == "IG1"
    assert not res["exchanged_long_lived"]
    assert not any(q.get("grant_type") == "fb_exchange_token" for _, q in calls)
    assert oauth.active_token(conn) == (SYS_TOKEN, "meta_system_user")


def test_login_no_instagram_keeps_old_tokens(conn, meta_env):
    security.save_token(conn, "meta_user", "old", "OLDTOKEN", key=meta_env)
    with pytest.raises(oauth.LoginFailure) as e:
        oauth.complete_login(conn, "CODE", transport=router(pages=False, granular=None))
    assert e.value.reason == "no_instagram_found"
    assert security.load_token(conn, "meta_user", "old", key=meta_env) == "OLDTOKEN"


def test_login_wrong_username_selection(conn, meta_env, monkeypatch):
    monkeypatch.setattr(settings, "meta_ig_username", "someone_else")
    with pytest.raises(oauth.LoginFailure) as e:
        oauth.complete_login(conn, "CODE", transport=router())
    assert e.value.reason == "username_mismatch" and "someone_else" not in str(e.value)


def test_exchange_error_scrubs_secrets(meta_env):
    def handler(req):
        return httpx.Response(400, json={"error": {"message": f"Invalid code CODE123 secret {APP_SECRET}", "code": 100}})
    with pytest.raises(GraphError) as e:
        mc.exchange_code("CODE123", transport=httpx.MockTransport(handler))
    assert "CODE123" not in str(e.value) and APP_SECRET not in str(e.value)


def test_exchange_network_error(meta_env):
    def boom(req):
        raise httpx.ConnectTimeout("timeout")
    with pytest.raises(GraphError, match="Сетевая"):
        mc.exchange_code("C", transport=httpx.MockTransport(boom))


# ---------------- токены: ротация и здоровье ----------------

def test_replace_tokens_is_atomic(conn, meta_env):
    security.save_token(conn, "meta_user", "a", "T1", key=meta_env)
    with pytest.raises(Exception):
        security.replace_tokens(conn, [{"provider": "meta_user", "subject": "b", "token": "T2"}, {"provider": "meta_page"}], key=meta_env)
    assert security.load_token(conn, "meta_user", "a", key=meta_env) == "T1"


def test_encryption_key_rotation(conn, monkeypatch):
    old, new = security.generate_key(), security.generate_key()
    monkeypatch.setattr(settings, "token_encryption_key", old)
    monkeypatch.setattr(settings, "token_encryption_keys_old", [])
    security.save_token(conn, "meta_user", "me", "TOKEN")
    monkeypatch.setattr(settings, "token_encryption_key", new)
    monkeypatch.setattr(settings, "token_encryption_keys_old", [old])
    assert security.load_token(conn, "meta_user", "me") == "TOKEN"  # читается старым ключом
    assert security.rotate_encryption(conn) == 1
    monkeypatch.setattr(settings, "token_encryption_keys_old", [])
    assert security.load_token(conn, "meta_user", "me") == "TOKEN"  # теперь только новым


def test_token_health_flags_invalid(conn, meta_env):
    security.save_token(conn, "meta_user", "me", "TK", key=meta_env)

    def handler(req):
        return httpx.Response(200, json={"data": {"is_valid": False, "type": "USER", "scopes": []}})
    h = oauth.token_health(conn, transport=httpx.MockTransport(handler))
    assert h["status"] == "invalid" and kv_get(conn, "reauth_required")


def test_token_health_expiring(conn, meta_env):
    security.save_token(conn, "meta_user", "me", "TK", key=meta_env)

    def handler(req):
        return httpx.Response(200, json={"data": {"is_valid": True, "type": "USER", "scopes": ["instagram_basic"],
                                                  "expires_at": int(time.time()) + 3 * 86400}})
    h = oauth.token_health(conn, transport=httpx.MockTransport(handler))
    assert h["status"] == "expiring" and "instagram_manage_insights" in h["missing_scopes"]


# ---------------- ошибки API ----------------

def test_network_error_does_not_raise_from_call_and_hides_token():
    def boom(req):
        raise httpx.ConnectError(f"cannot connect {req.url}")
    c = GraphClient("SECRETTOKEN42", "v24.0", transport=httpx.MockTransport(boom), app_secret="")
    r = c.call("/me")
    assert r.http_status is None and not r.ok and r.error["kind"] == "network"
    assert "SECRETTOKEN42" not in json.dumps(r.public())
    with pytest.raises(GraphError) as e:
        c.get("/me")
    assert e.value.kind == "network"


def test_appsecret_proof_sent():
    seen = {}

    def handler(req):
        seen.update(req.url.params)
        return httpx.Response(200, json={})
    GraphClient("TOK", "v24.0", transport=httpx.MockTransport(handler), app_secret="S").get("/me")
    assert seen["appsecret_proof"] == mc.appsecret_proof("TOK", "S")


def test_httpx_logs_do_not_leak_token():
    buf = io.StringIO()
    root = logging.getLogger()
    h = logging.StreamHandler(buf)
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    try:
        GraphClient("LEAKYTOKEN77", "v24.0", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), app_secret="").get("/me")
    finally:
        root.removeHandler(h)
        root.setLevel(logging.WARNING)
    assert "LEAKYTOKEN77" not in buf.getvalue()


def test_token_error_in_insights_is_not_treated_as_missing_metric():
    def handler(req):
        return httpx.Response(400, json={"error": {"message": "Error validating access token", "code": 190}})
    c = GraphClient("T", "v24.0", transport=httpx.MockTransport(handler), app_secret="")
    with pytest.raises(GraphError) as e:
        c.media_insights("R1", ["views", "reach"])
    assert e.value.kind == "token"


def test_missing_metric_reported_not_invented():
    def handler(req):
        m = req.url.params["metric"]
        if "," in m or m == "reels_skip_rate":
            return httpx.Response(400, json={"error": {"message": "(#100) metric not supported", "code": 100}})
        return httpx.Response(200, json={"data": [{"name": m, "values": [{"value": 12}]}]})
    c = GraphClient("T", "v24.0", transport=httpx.MockTransport(handler), app_secret="")
    values, unavailable = c.media_insights("R1", ["views", "reels_skip_rate"])
    assert values == {"views": 12} and "reels_skip_rate" in unavailable
    assert "reels_skip_rate" not in values


def test_rate_limit_propagates():
    def handler(req):
        return httpx.Response(400, json={"error": {"message": "Application request limit reached", "code": 4}})
    c = GraphClient("T", "v24.0", transport=httpx.MockTransport(handler), app_secret="")
    with pytest.raises(GraphError) as e:
        c.media_insights("R1", ["views"])
    assert e.value.kind == "rate_limit"


def test_business_discovery_rejects_bad_username():
    c = GraphClient("T", "v24.0", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), app_secret="")
    with pytest.raises(GraphError):
        c.business_discovery("IG1", "bad){evil}")


# ---------------- диагностика ----------------

def test_diagnostics_records_statuses_without_secrets(conn, meta_env, monkeypatch):
    oauth.complete_login(conn, "CODE", transport=router())
    t = router(fail={"/insights": (400, 100, "metric not supported for this media")})
    real = GraphClient.__init__

    def patched(self, token, version=None, transport=None, app_secret=None):
        real(self, token, version, t, app_secret)
    monkeypatch.setattr(GraphClient, "__init__", patched)
    rep = diagnostics.run(conn, transport=t)
    by = {s["step"]: s for s in rep["steps"]}
    assert by["Профиль Instagram"]["http_status"] == 200
    assert by["Insights Reels: views"]["ok"] and not by["Insights Reels: reels_skip_rate"]["ok"]
    assert by["Insights Reels: reels_skip_rate"]["http_status"] == 400
    assert by["Insights аккаунта: follows_and_unfollows"]["error"]["code"] == 100
    assert by["Business Discovery: свой аккаунт (проверка метода)"]["ok"]
    assert by["Business Discovery: @aironzak"]["ok"]
    assert not by["Business Discovery: @di.sukharev"]["ok"] and by["Business Discovery: @di.sukharev"]["http_status"] == 400
    stored = conn.execute("SELECT report_json FROM diagnostic_runs").fetchone()[0]
    for secret in (USER_SHORT, USER_LONG, PAGE_TOKEN, APP_SECRET, meta_env, "access_token="):
        assert secret not in stored
    av = {(r["metric"], r["scope"]): r["status"] for r in conn.execute("SELECT * FROM metric_availability")}
    assert av[("reels_skip_rate", "own_reel")] == "unavailable" and av[("views", "own_reel")] == "available"
    assert av[("view_count", "competitor_media")] in {"available", "unavailable"}


def test_diagnostics_without_connection(conn, meta_env):
    rep = diagnostics.run(conn)
    assert not rep["ok"] and any(s["step"] == "Токен" for s in rep["steps"])


# ---------------- CSV как резервный источник ----------------

def test_csv_then_api_same_reel_no_crash(conn):
    importers.import_own_reels(conn, "permalink,views,leads\nhttps://i/x,100,3\n", "me")
    acc = conn.execute("SELECT id FROM accounts WHERE username='me'").fetchone()[0]
    mid = importers.upsert_media(conn, acc, {"ig_media_id": "111", "permalink": "https://i/x", "views": 150}, "api")
    r = conn.execute("SELECT ig_media_id, views, leads, metrics_source FROM media WHERE id=?", (mid,)).fetchone()
    assert tuple(r) == ("111", 150, 3, "api")
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1


def test_manual_import_does_not_override_api(conn):
    acc = conn.execute("INSERT INTO accounts(username, is_own) VALUES ('me', 1)").lastrowid
    importers.upsert_media(conn, acc, {"ig_media_id": "1", "permalink": "https://i/y", "views": 500}, "api")
    importers.import_own_reels(conn, "permalink,views,leads\nhttps://i/y,10,2\n", "me")
    r = conn.execute("SELECT views, leads, metrics_source FROM media").fetchone()
    assert tuple(r) == (500, 2, "api")  # просмотры из API сохранены, заявки дополнены вручную


# ---------------- веб-маршруты ----------------

@pytest.fixture()
def web(meta_env, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import main
    from app.db import connect, init_db
    c = connect(tmp_path / "w.sqlite3")
    init_db(c)
    monkeypatch.setattr(main, "conn", c)
    client = TestClient(main.app, base_url="https://incon-production.up.railway.app")
    client.auth = ("u", "pw")
    return client, c, main


def test_web_requires_password(web):
    client, _, _ = web
    assert client.get("/connect", auth=("u", "wrong")).status_code == 401


def test_web_login_sets_state_cookie_and_redirects(web):
    client, c, _ = web
    r = client.get("/meta/login", follow_redirects=False)
    assert r.status_code == 303 and "config_id=1313415880861047" in r.headers["location"]
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()


def test_web_callback_rejects_bad_state(web, monkeypatch):
    client, c, main = web
    called = []
    monkeypatch.setattr(oauth, "complete_login", lambda *a, **k: called.append(1))
    client.get("/meta/login", follow_redirects=False)
    r = client.get("/meta/callback?code=C&state=forged")
    assert "state" in r.text and not called


def test_web_callback_user_denied(web):
    client, c, _ = web
    r = client.get("/meta/login", follow_redirects=False)
    state = dict(httpx.URL(r.headers["location"]).params)["state"]
    r = client.get(f"/meta/callback?state={state}&error=access_denied&error_reason=user_denied")
    assert "Вход отменён" in r.text


def test_web_callback_success(web, monkeypatch):
    client, c, main = web
    monkeypatch.setattr(oauth, "complete_login", lambda conn, code: {
        "ig_username": "my_account", "ig_user_id": "IG1", "token_type": "USER", "account_source": "me/accounts",
        "missing_scopes": [], "unneeded_scopes": ["instagram_content_publish"]})
    r = client.get("/meta/login", follow_redirects=False)
    state = dict(httpx.URL(r.headers["location"]).params)["state"]
    r = client.get(f"/meta/callback?code=C&state={state}")
    assert "Подключено: @my_account" in r.text and "instagram_content_publish" in r.text


def test_connect_page_never_renders_tokens(web):
    client, c, _ = web
    security.save_token(c, "meta_user", "me", "VISIBLETOKEN123")
    html = client.get("/connect").text
    assert "VISIBLETOKEN123" not in html and settings.meta_redirect_uri in html and APP_SECRET not in html


def test_sync_token_error_marks_reauth(web, monkeypatch):
    client, c, main = web
    kv_set(c, "ig_user_id", "IG1")
    security.save_token(c, "meta_user", "me", "TK")
    from app.meta import sync

    def bad(*a, **k):
        raise GraphError("Error validating access token", code=190, status=400)
    monkeypatch.setattr(sync, "sync_own", bad)
    r = client.post("/meta/sync", data={"scope": "own"})
    assert "войдите заново" in r.text and kv_get(c, "reauth_required")


# ---------------- секреты в репозитории ----------------

def test_no_secrets_tracked_in_git():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert ".env" not in files and not any(f.startswith("data/") for f in files)
    patterns = [re.compile(p) for p in (r"EAA[A-Za-z0-9]{40,}", r"IGQV[A-Za-z0-9_-]{40,}", r"sk-ant-[A-Za-z0-9_-]{20,}",
                                        r"(?m)^\s*(META_APP_SECRET|ANTHROPIC_API_KEY|TOKEN_ENCRYPTION_KEY)=\S+")]
    for f in files:
        p = ROOT / f
        if not p.exists() or p.suffix in {".png", ".jpg", ".mp4"}:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for pat in patterns:
            assert not pat.search(text), f"похоже на секрет в {f}"

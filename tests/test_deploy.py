"""Production / Railway: запуск, прокси, хранилище, redirect_uri, безопасность, SUAT."""
import logging
import time
from pathlib import Path

import httpx
import pytest

from app import logsafe, security, serve
from app.config import Settings, settings
from app.meta import client as mc
from app.meta import diagnostics, oauth

ROOT = Path(__file__).resolve().parent.parent
REDIRECT = "https://incon-production.up.railway.app/meta/callback"
CONFIG_ID = "1313415880861047"
SECRET = "appSECRETvalue-777"


# ---------------- запуск ----------------

def test_serve_options_railway():
    o = serve.build_options({"PORT": "4321", "RAILWAY_ENVIRONMENT": "production"})
    assert o["port"] == 4321 and o["host"] == "0.0.0.0" and o["proxy_headers"] is True
    assert o["forwarded_allow_ips"] == "*" and o["workers"] == 1
    assert all("scrub" in h["filters"] for h in o["log_config"]["handlers"].values())


def test_serve_options_local_defaults():
    o = serve.build_options({})
    assert o["port"] == 8000 and o["host"] == "127.0.0.1" and o["forwarded_allow_ips"] == "127.0.0.1"


def test_serve_rejects_bad_port():
    with pytest.raises(SystemExit):
        serve.build_options({"PORT": "abc"})


def test_dockerfile_and_dockerignore():
    df = (ROOT / "Dockerfile").read_text()
    assert 'CMD ["python", "-m", "app.serve"]' in df and "ffmpeg" in df and "ENV DATA_DIR=/data" in df
    assert "COPY .env" not in df and "COPY . " not in df
    ignore = (ROOT / ".dockerignore").read_text().split()
    assert ".env" in ignore and "data/" in ignore and ".git/" in ignore


# ---------------- логи ----------------

def test_access_log_scrubbed():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/meta/callback?code=AQBsecretCODE&state=STATEval", "1.1", 303), None)
    logsafe.ScrubFilter().filter(rec)
    msg = rec.getMessage()
    assert "AQBsecretCODE" not in msg and "STATEval" not in msg and "code=***" in msg


def test_scrub_text_tokens():
    t = logsafe.scrub_text("Authorization: Bearer abcdefghijkl access_token=EAAB123 EAABsometokenvalue1234567890123456789")
    assert "abcdefghijkl" not in t and "EAAB123" not in t and "EAABsometoken" not in t


# ---------------- конфигурация ----------------

def test_data_dir_falls_back_to_railway_volume(monkeypatch, tmp_path):
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    assert Settings().data_dir == tmp_path


def test_redirect_default_from_railway_domain(monkeypatch):
    monkeypatch.delenv("META_REDIRECT_URI", raising=False)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "incon-production.up.railway.app")
    assert Settings().meta_redirect_uri == REDIRECT


def _hosted(monkeypatch, **env):
    base = {"RAILWAY_ENVIRONMENT": "production", "RAILWAY_PUBLIC_DOMAIN": "incon-production.up.railway.app",
            "RAILWAY_VOLUME_MOUNT_PATH": "/data", "DATA_DIR": "/data", "APP_PASSWORD": "pw", "META_APP_ID": "123",
            "META_APP_SECRET": SECRET, "META_LOGIN_CONFIG_ID": CONFIG_ID, "META_REDIRECT_URI": REDIRECT,
            "TOKEN_ENCRYPTION_KEY": security.generate_key(), "ALLOW_NO_PASSWORD": "false"}
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    return Settings()


def test_production_config_ok(monkeypatch):
    s = _hosted(monkeypatch)
    assert s.meta_config_problems() == [] and s.storage_persistent is True and s.is_hosted


def test_redirect_host_mismatch_detected(monkeypatch):
    s = _hosted(monkeypatch, META_REDIRECT_URI="https://other.up.railway.app/meta/callback")
    assert any("не совпадает" in p for p in s.meta_config_problems())


@pytest.mark.parametrize("uri", [REDIRECT + "/", REDIRECT.replace("https", "http"), REDIRECT + "?x=1"])
def test_redirect_uri_must_be_exact(monkeypatch, uri):
    assert _hosted(monkeypatch, META_REDIRECT_URI=uri).meta_config_problems()


def test_hosted_ignores_allow_no_password(monkeypatch):
    s = _hosted(monkeypatch, APP_PASSWORD="", ALLOW_NO_PASSWORD="true")
    assert s.password_required and any("APP_PASSWORD" in p for p in s.meta_config_problems())


def test_storage_not_on_volume(monkeypatch):
    s = _hosted(monkeypatch, DATA_DIR="/srv/data")
    assert s.storage_persistent is False and any("Volume" in p for p in s.deployment_problems())


def test_non_numeric_ids_rejected(monkeypatch):
    p = " ".join(_hosted(monkeypatch, META_APP_ID="my-app", META_LOGIN_CONFIG_ID="cfg").meta_config_problems())
    assert "META_APP_ID" in p and "META_LOGIN_CONFIG_ID" in p


# ---------------- redirect_uri: точное совпадение в диалоге и при обмене code ----------------

@pytest.fixture()
def prod(monkeypatch):
    key = security.generate_key()
    for k, v in {"meta_app_id": "123", "meta_app_secret": SECRET, "meta_login_config_id": CONFIG_ID,
                 "meta_redirect_uri": REDIRECT, "token_encryption_key": key, "token_encryption_keys_old": [],
                 "app_password": "pw", "meta_api_enabled": True, "allow_no_password": False, "meta_ig_username": "",
                 "railway_environment": "production", "railway_public_domain": "incon-production.up.railway.app",
                 "railway_volume_mount_path": str(settings.data_dir), }.items():
        monkeypatch.setattr(settings, k, v)
    return key


def test_redirect_uri_identical_in_dialog_and_exchange(prod):
    dialog = dict(httpx.URL(mc.oauth_dialog_url("S")).params)
    seen = {}

    def handler(req):
        seen.update(req.url.params)
        return httpx.Response(200, json={"access_token": "T"})
    mc.exchange_code("C", transport=httpx.MockTransport(handler))
    assert dialog["redirect_uri"] == seen["redirect_uri"] == REDIRECT
    assert dialog["config_id"] == CONFIG_ID and dialog["client_id"] == "123"


# ---------------- веб: production ----------------

@pytest.fixture()
def web(prod, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import main
    from app.db import connect, init_db
    c = connect(tmp_path / "p.sqlite3")
    init_db(c)
    monkeypatch.setattr(main, "conn", c)
    main._auth_failures.clear()
    client = TestClient(main.app, base_url="https://incon-production.up.railway.app")
    return client, c, main


def test_healthz_public_and_minimal(web):
    client, _, _ = web
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_hosted_without_password_closed(web, monkeypatch):
    client, _, _ = web
    monkeypatch.setattr(settings, "app_password", "")
    monkeypatch.setattr(settings, "allow_no_password", True)
    monkeypatch.setattr(settings, "meta_api_enabled", False)  # проверяем именно правило «Railway без пароля»
    for path in ("/", "/reels", "/connect", "/meta/health", "/meta/diagnostics.json"):
        assert client.get(path).status_code == 503, path
    assert client.get("/healthz").status_code == 200


def test_security_headers(web):
    client, _, _ = web
    r = client.get("/connect", auth=("u", "pw"))
    for h in ("x-frame-options", "x-content-type-options", "referrer-policy", "strict-transport-security"):
        assert h in r.headers
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"


def test_bruteforce_lockout(web):
    client, _, main = web
    for _ in range(main.AUTH_MAX_FAILURES):
        assert client.get("/", auth=("u", "bad")).status_code == 401
    assert client.get("/", auth=("u", "bad")).status_code == 429
    assert client.get("/", auth=("u", "pw")).status_code == 429  # блокировка по IP на 15 минут


def test_no_openapi_or_docs(web):
    client, _, _ = web
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(p).status_code == 404


def test_pages_never_contain_secrets(web):
    client, c, main = web
    security.save_token(c, "meta_user", "me", "EAAvisibleTOKEN0001")
    for p in ("/", "/connect", "/metrics", "/import", "/meta/health", "/meta/diagnostics.json"):
        body = client.get(p, auth=("u", "pw")).text
        for s in ("EAAvisibleTOKEN0001", SECRET, settings.token_encryption_key):
            assert s not in body, p


def test_template_settings_proxy_has_no_secrets():
    from app import main
    pub = main.templates.env.globals["settings"]
    for attr in ("meta_app_secret", "token_encryption_key", "app_password"):
        assert not hasattr(pub, attr)


def test_app_check_route(web, monkeypatch):
    client, c, main = web
    monkeypatch.setattr(mc, "check_app_credentials", lambda: {"ok": True, "id": "123", "name": "Content Intelligence", "http_status": 200})
    r = client.post("/meta/app-check", auth=("u", "pw"))
    assert "App ID и App Secret верны" in r.text and "Content Intelligence" in r.text
    monkeypatch.setattr(mc, "check_app_credentials", lambda: {"ok": False, "http_status": 400, "code": 101, "error": "Error validating client secret"})
    r = client.post("/meta/app-check", auth=("u", "pw"))
    assert "не прошла" in r.text and SECRET not in r.text


def test_check_app_credentials(prod):
    def ok(req):
        assert req.url.path.endswith("/123")
        return httpx.Response(200, json={"id": "123", "name": "Content Intelligence"})
    res = mc.check_app_credentials(transport=httpx.MockTransport(ok))
    assert res["ok"] and res["name"] == "Content Intelligence"

    def bad(req):
        return httpx.Response(400, json={"error": {"message": f"Error validating client secret 123|{SECRET}", "code": 101}})
    res = mc.check_app_credentials(transport=httpx.MockTransport(bad))
    assert not res["ok"] and res["code"] == 101 and SECRET not in str(res)


# ---------------- SUAT ----------------

def _suat_router(me_accounts_error=True, debug_ok=True, token_type="SYSTEM_USER", granular=("IG9",)):
    def handler(req):
        p, q = req.url.path, dict(req.url.params)
        if p.endswith("/oauth/access_token"):
            assert q.get("grant_type") != "fb_exchange_token", "SUAT не должен обмениваться на долгоживущий"
            return httpx.Response(200, json={"access_token": "EAAsystemUSERtoken000"})
        if p.endswith("/debug_token"):
            if not debug_ok:
                return httpx.Response(400, json={"error": {"message": "Invalid OAuth access token", "code": 190}})
            return httpx.Response(200, json={"data": {"is_valid": True, "type": token_type, "user_id": "SU1", "expires_at": 0,
                                                      "scopes": ["instagram_basic", "instagram_manage_insights"],
                                                      "granular_scopes": [{"scope": "instagram_basic", "target_ids": list(granular)}]}})
        if p.endswith("/me/accounts"):
            if me_accounts_error:
                return httpx.Response(400, json={"error": {"message": "(#100) Tried accessing nonexisting field (accounts)", "code": 100}})
            return httpx.Response(200, json={"data": []})
        if p.endswith("/IG9"):
            return httpx.Response(200, json={"id": "IG9", "username": "my_account"})
        return httpx.Response(404, json={"error": {"message": "nope", "code": 803}})
    return httpx.MockTransport(handler)


def test_suat_login_when_me_accounts_unsupported(conn, prod):
    res = oauth.complete_login(conn, "CODE", transport=_suat_router())
    assert res["token_type"] == "SYSTEM_USER" and res["account_source"] == "debug_token.granular_scopes"
    assert res["ig_user_id"] == "IG9" and res["page_id"] is None and not res["exchanged_long_lived"]
    assert oauth.active_token(conn)[1] == "meta_system_user"
    row = conn.execute("SELECT expires_at FROM oauth_tokens WHERE provider='meta_system_user'").fetchone()
    assert row["expires_at"] is None  # «без срока» не превращается в выдуманную дату


def test_login_fails_if_token_type_unknown(conn, prod):
    with pytest.raises(oauth.OAuthError, match="debug_token"):
        oauth.complete_login(conn, "CODE", transport=_suat_router(debug_ok=False))
    assert conn.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 0


def test_login_rejects_page_token_type(conn, prod):
    with pytest.raises(oauth.LoginFailure) as e:
        oauth.complete_login(conn, "CODE", transport=_suat_router(token_type="PAGE"))
    assert e.value.reason == "unsupported_token_type"


def test_capabilities_summary():
    steps = [{"step": "Страницы с Instagram (/me/accounts)", "ok": False, "http_status": 400},
             {"step": "Профиль Instagram", "ok": True, "http_status": 200},
             {"step": "Insights Reels: views", "ok": True, "http_status": 200},
             {"step": "Insights Reels: reels_skip_rate", "ok": False, "http_status": 400},
             {"step": "Business Discovery: свой аккаунт (проверка метода)", "ok": True, "http_status": 200}]
    caps = diagnostics.capabilities(steps)
    assert caps["me_accounts"] is False and caps["profile"] is True and caps["reel_insights"] == "partial"
    assert caps["business_discovery_self"] is True and caps["business_discovery_competitors"] is None

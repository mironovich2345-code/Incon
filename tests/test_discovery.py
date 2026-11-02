"""Поиск Instagram-аккаунта после Unified Login: USER / SYSTEM_USER, пустой /me/accounts,
отсутствующие target_ids, обезличенный отчёт о попытке входа."""
import json
import time

import httpx
import pytest

from app import security
from app.config import settings
from app.meta import diagnostics, discovery, oauth
from app.meta.client import GraphClient

REDIRECT = "https://incon-production.up.railway.app/meta/callback"
IG_ID, PAGE_ID, BIZ_ID = "17841400000000001", "100000000000001", "200000000000002"
SECRET, CODE = "appSECRET-discovery", "AQDsecretOAUTHcode123"
TOKENS = {"USER": "EAAuserSHORTtoken0000000001", "SYSTEM_USER": "EAAsystemTOKEN00000000002"}
LONG = "EAAuserLONGtoken000000000003"


@pytest.fixture()
def meta(monkeypatch):
    key = security.generate_key()
    for k, v in {"meta_app_id": "123", "meta_app_secret": SECRET, "meta_login_config_id": "1313415880861047",
                 "meta_redirect_uri": REDIRECT, "token_encryption_key": key, "token_encryption_keys_old": [],
                 "app_password": "pw", "meta_api_enabled": True, "meta_ig_username": "", "allow_no_password": False,
                 "railway_environment": "", "railway_public_domain": "", "railway_volume_mount_path": ""}.items():
        monkeypatch.setattr(settings, k, v)
    return key


SU_ID = "400000000000004"
MISSING = "(#100) Missing Permission"


def make_router(token_type="SYSTEM_USER", granular=(), scopes=None, me_accounts=None, page_lookup=None,
                businesses=None, business_edges=None, profile_ok=True, calls=None, me_info=None, assigned=None):
    """me_accounts: None → ошибка 100 (ребро недоступно), [] → пусто, [page…] → страницы.
    businesses: None → «Missing Permission». business_edges: {edge: [items]}, остальные → «Missing Permission».
    me_info: ответ /me (по умолчанию {"id": SU_ID}). assigned: None → «Missing Permission», [] / [items]."""
    me_info = {"id": SU_ID} if me_info is None else me_info
    scopes = scopes if scopes is not None else ["instagram_basic", "instagram_manage_insights"]
    page_lookup, business_edges = page_lookup or {}, business_edges or {}

    def err(code=100, msg="(#100) Tried accessing nonexisting field", status=400):
        return httpx.Response(status, json={"error": {"message": msg, "code": code, "type": "OAuthException"}})

    def handler(req):
        p, q = req.url.path, dict(req.url.params)
        if calls is not None:
            calls.append(p)
        if p.endswith("/oauth/access_token"):
            if q.get("grant_type") == "fb_exchange_token":
                return httpx.Response(200, json={"access_token": LONG, "expires_in": 5184000})
            return httpx.Response(200, json={"access_token": TOKENS[token_type], "expires_in": 3600})
        if p.endswith("/debug_token"):
            return httpx.Response(200, json={"data": {
                "is_valid": True, "type": token_type, "user_id": "300000000000003", "scopes": scopes,
                "expires_at": 0 if token_type == "SYSTEM_USER" else int(time.time()) + 50 * 86400,
                "granular_scopes": [dict(g) for g in granular]}})
        if p.endswith("/me/accounts"):
            return err() if me_accounts is None else httpx.Response(200, json={"data": me_accounts})
        if p.endswith("/me/businesses"):
            return err(100, MISSING) if businesses is None else httpx.Response(200, json={"data": [{"id": b} for b in businesses]})
        if p.endswith("/assigned_instagram_accounts"):
            assert p.split("/")[-2] == SU_ID
            return err(100, MISSING) if assigned is None else httpx.Response(200, json={"data": assigned})
        for edge in ("owned_instagram_accounts", "instagram_business_accounts", "instagram_accounts"):
            if p.endswith(f"/{edge}"):
                return httpx.Response(200, json={"data": business_edges[edge]}) if edge in business_edges else err(100, MISSING)
        if p.endswith("/me"):
            return httpx.Response(200, json=me_info)
        if "instagram_business_account" in q.get("fields", "") and p.split("/")[-1] in page_lookup:
            return httpx.Response(200, json={"id": p.split("/")[-1], "instagram_business_account": page_lookup[p.split("/")[-1]]})
        if p.endswith(f"/{IG_ID}"):
            if not profile_ok:
                return err(10, "(#10) Application does not have permission for this action", 403)
            return httpx.Response(200, json={"id": IG_ID, "username": "mironovich.pro"})
        return err(803, "unknown path", 404)
    return httpx.MockTransport(handler)


def assert_anonymous(report: dict, key: str):
    text = json.dumps(report, ensure_ascii=False)
    for secret in (*TOKENS.values(), LONG, CODE, SECRET, key, IG_ID, PAGE_ID, BIZ_ID, "mironovich.pro", "300000000000003"):
        assert secret not in text, f"в отчёте найдено: {secret}"


# ---------------- успешный поиск ----------------

def test_system_user_without_target_ids_found_via_business(conn, meta):
    """Типичный Unified Login: SUAT, страницы нет, доступ «ко всем» (target_ids нет), аккаунт — в портфолио."""
    t = make_router("SYSTEM_USER", granular=[{"scope": "instagram_basic"}, {"scope": "instagram_manage_insights"}],
                    me_accounts=None, businesses=[BIZ_ID],
                    business_edges={"instagram_business_accounts": [{"id": IG_ID, "username": "mironovich.pro"}]})
    res = oauth.complete_login(conn, CODE, transport=t)
    assert res["ig_user_id"] == IG_ID and res["account_source"] == "business.instagram_business_accounts"
    assert res["token_type"] == "SYSTEM_USER" and res["page_id"] is None
    assert oauth.active_token(conn) == (TOKENS["SYSTEM_USER"], "meta_system_user")
    rep = oauth.last_login_attempt(conn)
    assert rep["reason"] == "ok" and rep["granular_scopes"] == [{"scope": "instagram_basic", "target_ids": 0},
                                                                {"scope": "instagram_manage_insights", "target_ids": 0}]
    assert_anonymous(rep, meta)


def test_system_user_target_ids_under_business_scope_name(conn, meta):
    t = make_router("SYSTEM_USER", granular=[{"scope": "instagram_business_basic", "target_ids": [IG_ID]}],
                    scopes=["instagram_business_basic"])
    res = oauth.complete_login(conn, CODE, transport=t)
    assert res["account_source"] == "debug_token.granular_scopes" and res["ig_user_id"] == IG_ID


def test_user_token_empty_me_accounts_page_from_granular(conn, meta):
    t = make_router("USER", granular=[{"scope": "pages_show_list", "target_ids": [PAGE_ID]}, {"scope": "instagram_basic"}],
                    scopes=["instagram_basic", "pages_show_list"], me_accounts=[],
                    page_lookup={PAGE_ID: {"id": IG_ID, "username": "mironovich.pro"}})
    res = oauth.complete_login(conn, CODE, transport=t)
    assert res["account_source"] == "page.instagram_business_account" and res["exchanged_long_lived"]
    assert res["page_id"] is None  # токена страницы нет — используется токен входа
    assert oauth.active_token(conn) == (LONG, "meta_user")


def test_user_token_me_accounts_page_token_preferred(conn, meta):
    page = {"id": PAGE_ID, "access_token": "EAApageTOKEN000000000009",
            "instagram_business_account": {"id": IG_ID, "username": "mironovich.pro"}}
    t = make_router("USER", granular=[{"scope": "instagram_basic", "target_ids": [IG_ID]}],
                    scopes=["instagram_basic", "pages_show_list"], me_accounts=[page])
    res = oauth.complete_login(conn, CODE, transport=t)
    assert res["page_id"] == PAGE_ID and oauth.active_token(conn) == ("EAApageTOKEN000000000009", "meta_page")


def test_target_ids_not_required_when_me_accounts_works(conn, meta):
    page = {"id": PAGE_ID, "access_token": "EAApageTOKEN000000000009",
            "instagram_business_account": {"id": IG_ID, "username": "mironovich.pro"}}
    t = make_router("USER", granular=[], me_accounts=[page])
    assert oauth.complete_login(conn, CODE, transport=t)["account_source"] == "me/accounts"


def test_username_selection_among_several(conn, meta, monkeypatch):
    other = "17841400000000099"
    monkeypatch.setattr(settings, "meta_ig_username", "mironovich.pro")
    t = make_router("SYSTEM_USER", granular=[{"scope": "instagram_basic", "target_ids": [other, IG_ID]}])
    # other не читается (404 в моке) → не подтверждён; выбирается подтверждённый IG_ID
    assert oauth.complete_login(conn, CODE, transport=t)["ig_user_id"] == IG_ID


def test_system_user_assigned_instagram_accounts(conn, meta):
    """После добавления business_management: системному пользователю назначен аккаунт."""
    calls = []
    t = make_router("SYSTEM_USER", granular=[{"scope": "instagram_basic"}, {"scope": "business_management"}],
                    scopes=["instagram_basic", "instagram_manage_insights", "business_management"],
                    me_accounts=[], businesses=None, assigned=[{"id": IG_ID, "username": "mironovich.pro"}], calls=calls)
    res = oauth.complete_login(conn, CODE, transport=t)
    assert res["account_source"] == "system_user.assigned_instagram_accounts" and res["ig_user_id"] == IG_ID
    assert any(c.endswith(f"/{SU_ID}/assigned_instagram_accounts") for c in calls)


def test_client_business_id_owned_instagram_accounts(conn, meta):
    t = make_router("SYSTEM_USER", granular=[{"scope": "instagram_basic"}],
                    scopes=["instagram_basic", "business_management"], me_accounts=[], businesses=None,
                    me_info={"id": SU_ID, "client_business_id": BIZ_ID}, assigned=[],
                    business_edges={"owned_instagram_accounts": [{"id": IG_ID, "username": "mironovich.pro"}]})
    res = oauth.complete_login(conn, CODE, transport=t)
    assert res["account_source"] == "business.owned_instagram_accounts"
    rep = oauth.last_login_attempt(conn)
    probe = next(p for p in rep["probes"] if p["method"] == "Системный пользователь и бизнес клиента")
    assert probe["note"] == "client_business_id получен" and probe["found"] == 1
    assert_anonymous(rep, meta)


def test_user_token_skips_system_user_endpoints(conn, meta):
    calls = []
    page = {"id": PAGE_ID, "access_token": "EAApageTOKEN000000000009",
            "instagram_business_account": {"id": IG_ID, "username": "mironovich.pro"}}
    t = make_router("USER", granular=[], me_accounts=[page], calls=calls)
    oauth.complete_login(conn, CODE, transport=t)
    assert not any(c.endswith("/assigned_instagram_accounts") for c in calls)
    assert not any(c.endswith("/me") for c in calls)


def test_all_probe_endpoints_documented(conn, meta):
    t = make_router("SYSTEM_USER", granular=[{"scope": "instagram_basic"}, {"scope": "pages_show_list", "target_ids": [PAGE_ID]}],
                    me_accounts=[], businesses=[BIZ_ID], me_info={"id": SU_ID, "client_business_id": BIZ_ID}, assigned=[])
    with pytest.raises(oauth.LoginFailure):
        oauth.complete_login(conn, CODE, transport=t)
    for p in oauth.last_login_attempt(conn)["probes"]:
        assert p["doc"], f"endpoint без ссылки на документацию: {p['endpoint']}"


# ---------------- понятные причины неудачи ----------------

PROD_SCOPES = ["instagram_basic", "instagram_manage_comments", "instagram_manage_insights",
               "instagram_content_publish", "instagram_manage_messages", "public_profile"]


def test_production_scenario_business_management_missing(conn, meta):
    """Реальная диагностика Railway: SYSTEM_USER, instagram-разрешения без target_ids,
    /me/accounts пуст, /me/businesses → 400 code 100 Missing Permission."""
    t = make_router("SYSTEM_USER", granular=[{"scope": s} for s in PROD_SCOPES], scopes=PROD_SCOPES,
                    me_accounts=[], businesses=None, me_info={"id": SU_ID}, assigned=[])
    with pytest.raises(oauth.LoginFailure) as e:
        oauth.complete_login(conn, CODE, transport=t)
    assert e.value.reason == "business_management_missing" and "business_management" in str(e.value)
    rep = oauth.last_login_attempt(conn)
    assert "GET /me/businesses" in rep["missing_permission_endpoints"]
    methods = {p["method"]: p for p in rep["probes"]}
    assert methods["Instagram-аккаунты, назначенные системному пользователю"]["note"].startswith("пустой список")
    assert methods["Бизнес-портфолио пользователя"]["doc"] == "Graph API · User · businesses"
    assert methods["Страницы пользователя"]["note"] == "страниц: 0"
    assert_anonymous(rep, meta)
    assert conn.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 0


def test_nothing_found_reason_and_report(conn, meta):
    t = make_router("SYSTEM_USER", granular=[{"scope": "instagram_basic"}, {"scope": "business_management"}],
                    scopes=["instagram_basic", "business_management"], me_accounts=None, businesses=None)
    with pytest.raises(oauth.LoginFailure) as e:
        oauth.complete_login(conn, CODE, transport=t)
    assert e.value.reason == "no_instagram_found"
    rep = oauth.last_login_attempt(conn)
    methods = {p["method"]: p for p in rep["probes"]}
    assert methods["granular_scopes (instagram_*)"]["note"].startswith("instagram-разрешения выданы без target_ids")
    assert methods["Страницы пользователя"]["http_status"] == 400 and methods["Страницы пользователя"]["error_code"] == 100
    assert rep["candidates"] == 0 and rep["token_type"] == "SYSTEM_USER"
    assert_anonymous(rep, meta)
    assert conn.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 0  # ничего не сохранено


def test_user_empty_me_accounts_no_targets(conn, meta):
    t = make_router("USER", granular=[], me_accounts=[], businesses=[])
    with pytest.raises(oauth.LoginFailure) as e:
        oauth.complete_login(conn, CODE, transport=t)
    assert e.value.reason == "no_instagram_found"
    probe = next(p for p in oauth.last_login_attempt(conn)["probes"] if p["method"] == "Страницы пользователя")
    assert probe["ok"] and probe["found"] == 0 and probe["note"] == "страниц: 0"


def test_no_instagram_scopes_reason(conn, meta):
    t = make_router("USER", granular=[{"scope": "public_profile"}], scopes=["public_profile"], me_accounts=[], businesses=[])
    with pytest.raises(oauth.LoginFailure) as e:
        oauth.complete_login(conn, CODE, transport=t)
    assert e.value.reason == "no_instagram_scopes" and "public_profile" in str(e.value)


def test_profile_unreadable_reason(conn, meta):
    t = make_router("SYSTEM_USER", granular=[{"scope": "instagram_basic", "target_ids": [IG_ID]}], profile_ok=False)
    with pytest.raises(oauth.LoginFailure) as e:
        oauth.complete_login(conn, CODE, transport=t)
    assert e.value.reason == "profile_unreadable" and "10" in str(e.value)
    assert_anonymous(oauth.last_login_attempt(conn), meta)


def test_exchange_failure_reported_without_code(conn, meta):
    def handler(req):
        return httpx.Response(400, json={"error": {"message": f"Invalid verification code {CODE}", "code": 100}})
    with pytest.raises(oauth.LoginFailure) as e:
        oauth.complete_login(conn, CODE, transport=httpx.MockTransport(handler))
    assert e.value.reason == "exchange_failed"
    rep = oauth.last_login_attempt(conn)
    assert rep["http_status"] == 400 and CODE not in json.dumps(rep) and CODE not in str(e.value)


def test_debug_token_failure_reason(conn, meta):
    def handler(req):
        if req.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": TOKENS["USER"]})
        return httpx.Response(400, json={"error": {"message": "Invalid OAuth access token signature", "code": 190}})
    with pytest.raises(oauth.LoginFailure) as e:
        oauth.complete_login(conn, CODE, transport=httpx.MockTransport(handler))
    assert e.value.reason == "debug_token_failed" and oauth.last_login_attempt(conn)["error_code"] == 190


def test_attempts_are_capped(conn, meta):
    for _ in range(25):
        oauth.record_failure(conn, "user_denied", "dialog")
    assert conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 20


def test_anonymize():
    t = discovery.anonymize(f"Object with ID '{IG_ID}' @mironovich.pro access_token={TOKENS['USER']}")
    assert IG_ID not in t and "mironovich.pro" not in t and TOKENS["USER"] not in t


# ---------------- диагностика ----------------

def test_diagnostics_includes_discovery_probes(conn, meta, monkeypatch):
    t = make_router("SYSTEM_USER", granular=[{"scope": "instagram_basic"}], me_accounts=None, businesses=[BIZ_ID],
                    business_edges={"instagram_business_accounts": [{"id": IG_ID}]})
    oauth.complete_login(conn, CODE, transport=t)
    real = GraphClient.__init__

    def patched(self, token, version=None, transport=None, app_secret=None):
        real(self, token, version, t, app_secret)
    monkeypatch.setattr(GraphClient, "__init__", patched)
    rep = diagnostics.run(conn, transport=t)
    names = [s["step"] for s in rep["steps"]]
    assert "Поиск аккаунта: Страницы пользователя" in names
    assert any(n.startswith("Поиск аккаунта: Instagram-аккаунты бизнес-портфолио") for n in names)
    assert rep["capabilities"]["account_discovery"] is True
    step = next(s for s in rep["steps"] if s["step"] == "Поиск аккаунта: Страницы пользователя")
    assert step["http_status"] == 400 and step["error"]["code"] == 100
    stored = conn.execute("SELECT report_json FROM diagnostic_runs").fetchone()[0]
    assert TOKENS["SYSTEM_USER"] not in stored and SECRET not in stored


# ---------------- веб ----------------

@pytest.fixture()
def web(meta, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import main
    from app.db import connect, init_db
    c = connect(tmp_path / "w.sqlite3")
    init_db(c)
    monkeypatch.setattr(main, "conn", c)
    main._auth_failures.clear()
    return TestClient(main.app, base_url="https://incon-production.up.railway.app"), c


def _login_state(client):
    r = client.get("/meta/login", follow_redirects=False, auth=("u", "pw"))
    return dict(httpx.URL(r.headers["location"]).params)["state"]


def test_web_failure_shows_reason_and_report(web, monkeypatch):
    client, c = web
    from app.meta import client as mc
    t = make_router("SYSTEM_USER", granular=[{"scope": "instagram_basic"}], scopes=PROD_SCOPES,
                    me_accounts=[], businesses=None, assigned=[])
    monkeypatch.setattr(mc, "exchange_code", lambda code, transport=None: {"access_token": TOKENS["SYSTEM_USER"]})
    monkeypatch.setattr(oauth, "exchange_code", lambda code, transport=None: {"access_token": TOKENS["SYSTEM_USER"]})
    monkeypatch.setattr(oauth, "debug_token", lambda tok, transport=None: mc.debug_token(tok, transport=t))
    real = GraphClient.__init__
    monkeypatch.setattr(GraphClient, "__init__", lambda self, token, version=None, transport=None, app_secret=None:
                        real(self, token, version, t, app_secret))
    state = _login_state(client)
    r = client.get(f"/meta/callback?code={CODE}&state={state}", auth=("u", "pw"))
    assert "Подключение не выполнено" in r.text and "нет разрешения business_management" in r.text
    assert "Последняя попытка подключения" in r.text and "без target_ids" in r.text
    for s in (CODE, TOKENS["SYSTEM_USER"], SECRET):
        assert s not in r.text
    js = client.get("/meta/login-attempts.json", auth=("u", "pw")).json()
    assert js[0]["reason"] == "business_management_missing" and CODE not in json.dumps(js)


def test_web_state_failure_recorded(web):
    client, c = web
    _login_state(client)
    client.get("/meta/callback?code=X&state=forged", auth=("u", "pw"))
    assert oauth.last_login_attempt(c)["reason"] == "state_invalid"


def test_login_attempts_json_requires_auth(web):
    client, _ = web
    assert client.get("/meta/login-attempts.json").status_code == 401

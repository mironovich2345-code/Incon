import json

import httpx
import pytest

from app import security
from app.meta.client import GraphClient, GraphError
from app.meta.sync import sync_competitor


def test_token_encrypted_at_rest(conn):
    key = security.generate_key()
    security.save_token(conn, "meta_user", "me", "SECRET-TOKEN", key=key)
    raw = conn.execute("SELECT token_encrypted FROM oauth_tokens").fetchone()[0]
    assert b"SECRET-TOKEN" not in raw
    assert security.load_token(conn, "meta_user", "me", key=key) == "SECRET-TOKEN"
    with pytest.raises(security.TokenStoreError):
        security.load_token(conn, "meta_user", "me", key=security.generate_key())
    assert "token" not in json.dumps(security.token_status(conn)).replace("token_status", "")


def _transport(handler):
    return httpx.MockTransport(handler)


def test_media_insights_isolates_unavailable_metrics():
    def handler(req: httpx.Request):
        metric = req.url.params["metric"]
        if "," in metric or metric == "reels_skip_rate":
            return httpx.Response(400, json={"error": {"message": f"(#100) metric invalid access_token=abc", "code": 100}})
        return httpx.Response(200, json={"data": [{"name": metric, "values": [{"value": 42}]}]})

    c = GraphClient("tok", "v24.0", transport=_transport(handler))
    values, unavailable = c.media_insights("1", ["views", "reels_skip_rate"])
    assert values == {"views": 42}
    assert "reels_skip_rate" in unavailable and "abc" not in unavailable["reels_skip_rate"]


def test_business_discovery_sync(conn):
    def handler(req: httpx.Request):
        assert "business_discovery.username(aironzak)" in req.url.params["fields"]
        return httpx.Response(200, json={"business_discovery": {
            "username": "aironzak", "followers_count": 1000, "media_count": 2,
            "media": {"data": [
                {"id": "a", "permalink": "https://i/a", "timestamp": "2026-10-01T10:00:00+0000", "like_count": 10, "comments_count": 1, "media_product_type": "REELS", "view_count": 500},
                {"id": "b", "permalink": "https://i/b", "timestamp": "2026-10-02T10:00:00+0000", "like_count": 30, "comments_count": 3, "media_product_type": "REELS", "view_count": 5000},
            ], "paging": {"cursors": {}}}}})

    c = GraphClient("tok", "v24.0", transport=_transport(handler))
    res = sync_competitor(conn, c, "999", "aironzak")
    assert res == {"ok": True, "media": 2}
    rows = conn.execute("SELECT views FROM media m JOIN accounts a ON a.id=m.account_id WHERE a.username='aironzak' ORDER BY views").fetchall()
    assert [r[0] for r in rows] == [500, 5000]
    assert conn.execute("SELECT status FROM metric_availability WHERE metric='view_count'").fetchone()[0] == "available"


def test_business_discovery_non_professional(conn):
    def handler(req):
        return httpx.Response(400, json={"error": {"message": "Invalid user id", "code": 110}})

    c = GraphClient("tok", "v24.0", transport=_transport(handler))
    res = sync_competitor(conn, c, "999", "di.sukharev")
    assert not res["ok"]
    st = conn.execute("SELECT discovery_status FROM accounts WHERE username='di.sukharev'").fetchone()[0]
    assert st == "not_professional"


def test_no_publish_endpoints_in_client():
    import inspect
    import app.meta.client as mc
    src = inspect.getsource(mc)
    assert "media_publish" not in src.replace("/media_publish не реализованы", "")
    assert "instagram_content_publish" not in mc.REQUIRED_SCOPES

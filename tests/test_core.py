from datetime import date
from pathlib import Path

import pytest

from app import analytics, importers, planner, publishing, reports
from app.classify import classify_text
from app.db import get_or_create_account, normalize_username

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def test_default_competitors_seeded(conn):
    names = {r["username"] for r in conn.execute("SELECT username FROM accounts")}
    assert {"aironzak", "di.sukharev"} <= names


def test_normalize_username():
    assert normalize_username("@Di.Sukharev") == "di.sukharev"
    assert normalize_username("https://www.instagram.com/aironzak/") == "aironzak"


def test_import_own_reels_keeps_missing_as_null(conn):
    res = importers.import_own_reels(conn, (SAMPLES / "own_reels.example.csv").read_bytes(), "me")
    assert res["imported"] == 3 and not res["errors"]
    r3 = conn.execute("SELECT * FROM media WHERE permalink LIKE '%EXAMPLE3%'").fetchone()
    assert r3["views"] is None and r3["reach"] is None  # пусто -> NULL, не 0
    assert r3["likes"] == 31
    assert r3["classification_source"] == "manual"
    r1 = conn.execute("SELECT * FROM media WHERE permalink LIKE '%EXAMPLE1%'").fetchone()
    assert r1["avg_watch_time_ms"] == pytest.approx(11400)
    assert r1["classification_source"] == "rules" and r1["topic"] in {"Сотрудники", "Продажи"}


def test_reimport_does_not_erase_known_values(conn):
    importers.import_own_reels(conn, (SAMPLES / "own_reels.example.csv").read_bytes(), "me")
    importers.import_own_reels(conn, "permalink,likes\nhttps://www.instagram.com/reel/EXAMPLE1/,60\n", "me")
    r = conn.execute("SELECT views, likes FROM media WHERE permalink LIKE '%EXAMPLE1%'").fetchone()
    assert r["views"] == 1250 and r["likes"] == 60


def test_compare_by_reports_sample_sizes(conn):
    importers.import_own_reels(conn, (SAMPLES / "own_reels.example.csv").read_bytes(), "me")
    groups = analytics.compare_by(analytics.own_reels(conn), "topic")
    total = sum(g["count"] for g in groups)
    assert total == 3
    for g in groups:
        assert g["n_views"] <= g["count"]


def test_derived_metrics_not_invented():
    r = analytics.enrich({"views": None, "follows": 5, "likes": 3, "comments": None})
    assert r["follows_per_1k"] is None and r["engagement"] is None


def test_competitor_outliers(conn):
    importers.import_competitor_posts(conn, (SAMPLES / "competitor_posts.example.csv").read_bytes())
    acc = conn.execute("SELECT id FROM accounts WHERE username='example_competitor'").fetchone()
    items = analytics.competitor_media(conn, acc["id"])
    stats = analytics.score_outliers(items, ratio=2.0)
    assert stats["basis"] == "views" and stats["median"] == 13500
    outliers = [i for i in items if i["is_outlier"]]
    assert len(outliers) == 1 and "DEMO2" in outliers[0]["permalink"]


def test_outliers_fallback_to_engagement_proxy():
    items = [analytics.enrich({"views": None, "likes": l, "comments": 0}) for l in (10, 12, 11, 60)]
    stats = analytics.score_outliers(items, ratio=2.0)
    assert stats["basis"].startswith("likes+comments")
    assert sum(i["is_outlier"] for i in items) == 1


def test_follower_growth(conn):
    importers.import_account_daily(conn, (SAMPLES / "account_daily.example.csv").read_bytes(), "me")
    acc = get_or_create_account(conn, "me")
    g = analytics.follower_growth(conn, acc)
    assert [x["followers"] for x in g] == [208, 214] and g[1]["delta"] == 6 and g[1]["follows"] == 8
    d = conn.execute("SELECT reach FROM account_daily WHERE day='2026-11-02'").fetchone()
    assert d["reach"] is None


def test_latin_square_plan_balanced():
    plan = planner.latin_plan(date(2026, 11, 3))
    assert len(plan) == 25
    for key in ("format", "topic", "hook_type"):
        for day in {p["day"] for p in plan}:
            assert len({p[key] for p in plan if p["day"] == day}) == 5
        for slot in range(1, 6):
            assert len({p[key] for p in plan if p["slot"] == slot}) == 5
    for a, b in (("format", "topic"), ("format", "hook_type"), ("topic", "hook_type")):
        assert len({(p[a], p[b]) for p in plan}) == 25  # попарная ортогональность


def test_classifier():
    c = classify_text("Как внедрить ИИ в отдел продаж за 7 дней? Показываю")
    assert c.hook_type == "Вопрос" and c.format == "Инструкция / демонстрация"
    assert classify_text("Работа с командой").topic == "Сотрудники"  # «бот» внутри «работа» не считается


def test_publishing_workflow(conn):
    cur = conn.execute("INSERT INTO publication_drafts(caption) VALUES ('')")
    did = cur.lastrowid
    with pytest.raises(publishing.TransitionError):
        publishing.transition(conn, did, "pending_approval")  # пустая подпись и нет видео
    conn.execute("UPDATE publication_drafts SET caption='Текст', video_path='/tmp/x.mp4' WHERE id=?", (did,))
    publishing.transition(conn, did, "pending_approval")
    with pytest.raises(publishing.TransitionError):
        publishing.transition(conn, did, "pending_approval")
    publishing.transition(conn, did, "approved", "ок")
    st = conn.execute("SELECT status, decided_at FROM publication_drafts WHERE id=?", (did,)).fetchone()
    assert st["status"] == "approved" and st["decided_at"]
    assert not hasattr(publishing, "publish")


def test_rules_report_runs(conn):
    importers.import_own_reels(conn, (SAMPLES / "own_reels.example.csv").read_bytes(), "me")
    conn.execute("UPDATE accounts SET is_own=1 WHERE username='me'")
    text = reports.rules_report(conn)
    assert "Ограничения данных" in text and "нет данных" not in text.split("## Мой аккаунт")[0]

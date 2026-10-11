"""Sponsored cards: admin management, topic matching, pacing, placement rules, hide / report / click."""
import io
import uuid

import pytest
from fastapi.testclient import TestClient

import app.rag.chat as chat
from app.core.config import get_settings
from app.main import app
from app.services import accounts, ads
from tests.test_chat_stream import DOCS, FakeLLM, _events, _kb


def _client(admin=False):
    c = TestClient(app)
    r = c.post("/api/auth/register", json={"email": f"ad-{uuid.uuid4().hex[:8]}@example.com", "name": "A",
                                           "password": "correct-horse"})
    u = r.json()["user"]
    if admin:
        accounts.update_user(u["id"], -1, role="admin")
    return c, u


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    with ads.closing(ads._conn()) as c:
        c.executescript("DELETE FROM ads; DELETE FROM ad_events; DELETE FROM ad_hidden; DELETE FROM ad_pacing;")
        c.commit()
    monkeypatch.setattr(get_settings(), "ads_every_n_answers", 1)
    yield
    with ads.closing(ads._conn()) as c:                                  # leave no ads for other tests' answers
        c.executescript("DELETE FROM ads; DELETE FROM ad_events; DELETE FROM ad_hidden; DELETE FROM ad_pacing;")
        c.commit()


AD = {"advertiser": "Axis Bank", "title": "Axis home loans from 8.5%", "body": "Fast sanction for salaried buyers.",
      "link": "https://www.axisbank.com/home-loan", "cta": "Learn more", "banks": ["Axis Bank"], "products": ["Home Loan"]}


def test_admin_manages_ads_and_users_cannot():
    admin, _ = _client(admin=True)
    user, _ = _client()
    assert user.post("/api/admin/ads", json=AD).status_code == 403
    ad = admin.post("/api/admin/ads", json=AD).json()
    assert ad["banks"] == ["Axis Bank"] and ad["active"] and ad["stats"]["impressions"] == 0
    assert admin.post("/api/admin/ads", json={**AD, "link": "javascript:alert(1)"}).status_code == 400

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (80, 60), "red").save(buf, "JPEG")
    img = admin.post(f"/api/admin/ads/{ad['id']}/image", files={"file": ("a.jpg", buf.getvalue())}).json()
    assert img["image_url"] == f"/api/ads/{ad['id']}/image"
    assert user.get(img["image_url"]).headers["content-type"] == "image/png"
    assert admin.post(f"/api/admin/ads/{ad['id']}/image", files={"file": ("x.jpg", b"not an image")}).status_code == 400

    assert admin.patch(f"/api/admin/ads/{ad['id']}", json={"active": False}).json()["active"] is False
    assert admin.delete(f"/api/admin/ads/{ad['id']}").status_code == 200
    assert admin.get("/api/admin/ads").json()["ads"] == []


def test_targeting_and_pacing():
    ads.create(AD)
    ads.create({**AD, "advertiser": "Generic", "title": "Urban Money app", "banks": [], "products": [], "priority": 0})
    _, u = _client()
    hit = ads.pick(u["id"], "What is the Axis HL DSA code?")
    assert hit["advertiser"] == "Axis Bank" and "Axis Bank" in hit["why"] and hit["domain"] == "axisbank.com"
    other = ads.pick(u["id"], "Axis personal loan ROI?")                 # Axis but not home loan: generic ad
    assert other["advertiser"] == "Generic" and "not chosen from your conversation" in other["why"]

    get_settings().ads_every_n_answers = 3
    shown = [ads.pick(u["id"], "Axis HL code?") is not None for _ in range(6)]
    assert shown == [False, False, True, False, False, True]


def test_ads_follow_kb_answers_only_and_respect_hide_and_report(monkeypatch):
    ad = ads.create(AD)
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": FakeLLM())
    _kb(monkeypatch, DOCS)
    c, u = _client()
    ev = _events(c.post("/api/chat/stream", json={"mode": "kb", "question": "Axis home loan code?"}).text)
    kinds = [k for k, _ in ev]
    assert "ad" in kinds and kinds.index("ad") > kinds.index("sources") and kinds[-1] == "done"
    card = dict(ev)["ad"]
    assert card["title"] == AD["title"] and "link" not in card            # the destination goes through the click redirect

    ask = _events(c.post("/api/chat/stream", json={"mode": "ask", "question": "Why Axis for this home loan?"}).text)
    assert "ad" not in [k for k, _ in ask]                                  # never next to a recommendation
    _kb(monkeypatch, [])
    nf = _events(c.post("/api/chat/stream", json={"mode": "kb", "question": "Axis home loan code?"}).text)
    assert "ad" not in [k for k, _ in nf]                                   # never on "not found"

    r = c.get(card["click_url"], follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == AD["link"]
    assert c.post(f"/api/ads/{ad['id']}/report", json={"reason": "misleading", "note": "rate is wrong"}).status_code == 200
    assert ads.pick(u["id"], "Axis home loan code?") is None                # reported -> hidden for this user
    other, ou = _client()
    assert ads.pick(ou["id"], "Axis home loan code?") is not None           # but still shown to others
    st = ads.get(ad["id"])["stats"]
    assert st["impressions"] == 2 and st["clicks"] == 1 and st["reports"] == 1 and st["ctr"] == 50.0
    assert ads.reports(ad["id"])[0]["detail"] == "misleading: rate is wrong"


def test_scheduled_and_paused_ads_do_not_run():
    ads.create({**AD, "starts": "2999-01-01"})
    ads.create({**AD, "ends": "2000-01-01"})
    ads.create({**AD, "active": False})
    _, u = _client()
    assert ads.pick(u["id"], "Axis home loan code?") is None

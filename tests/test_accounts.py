"""Accounts: registration, login, private conversations and the admin console."""
import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.services import accounts


def _user(name="Asha", password="correct-horse"):
    """A fresh client (its own cookie jar) logged in as a new user."""
    c = TestClient(app)
    email = f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com"
    r = c.post("/api/auth/register", json={"email": email, "name": name, "password": password})
    assert r.status_code == 200, r.text
    return c, r.json()["user"]


def _admin():
    c, u = _user("Admin")
    accounts.update_user(u["id"], -1, role="admin")
    return c, u


def test_register_login_logout():
    c, u = _user()
    assert c.get("/api/auth/me").json()["user"]["email"] == u["email"]
    assert c.post("/api/auth/logout").json()["ok"]
    assert c.get("/api/auth/me").status_code == 401

    bad = TestClient(app).post("/api/auth/login", json={"email": u["email"], "password": "wrong-password"})
    assert bad.status_code == 401 and "incorrect" in bad.json()["detail"]
    unknown = TestClient(app).post("/api/auth/login", json={"email": "nobody@example.com", "password": "whatever1"})
    assert unknown.status_code == 401 and unknown.json()["detail"] == bad.json()["detail"]

    ok = c.post("/api/auth/login", json={"email": u["email"].upper(), "password": "correct-horse"})
    assert ok.status_code == 200 and c.get("/api/auth/me").status_code == 200


def test_session_cookie_is_httponly_and_password_is_hashed():
    c = TestClient(app)
    r = c.post("/api/auth/register", json={"email": f"h-{uuid.uuid4().hex[:6]}@example.com", "name": "H",
                                           "password": "secret-pass"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    with accounts._conn() as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE id=?", (r.json()["user"]["id"],)).fetchone()[0]
    assert stored.startswith("scrypt$") and "secret-pass" not in stored


def test_registration_validation():
    c = TestClient(app)
    assert c.post("/api/auth/register", json={"email": "not-an-email", "name": "X", "password": "longenough"}).status_code == 422
    assert c.post("/api/auth/register", json={"email": "a@b.co", "name": "X", "password": "short"}).status_code == 422
    _, u = _user()
    dup = c.post("/api/auth/register", json={"email": u["email"], "name": "Y", "password": "longenough"})
    assert dup.status_code == 422 and "already exists" in dup.json()["detail"]


def test_conversations_need_login():
    c = TestClient(app)
    assert c.get("/api/conversations").status_code == 401
    assert c.post("/api/conversations", json={"title": "x"}).status_code == 401


def test_conversation_crud_and_search():
    c, _ = _user()
    data = {"case": {"loan_type": "Home Loan"}, "messages": [{"role": "user", "text": "Salaried, Pune resale flat"}]}
    conv = c.post("/api/conversations", json={"title": "Pune flat", "data": data}).json()
    other = c.post("/api/conversations", json={"title": "Car loan", "data": {}}).json()

    assert c.get(f"/api/conversations/{conv['id']}").json()["data"] == data
    titles = [x["title"] for x in c.get("/api/conversations").json()["conversations"]]
    assert set(titles) == {"Pune flat", "Car loan"}
    found = c.get("/api/conversations", params={"q": "RESALE"}).json()["conversations"]
    assert [x["id"] for x in found] == [conv["id"]]                     # message text is searchable
    assert c.get("/api/conversations", params={"q": "%"}).json()["conversations"] == []   # LIKE wildcards escaped

    data["messages"].append({"role": "assistant", "text": "Next question"})
    assert c.patch(f"/api/conversations/{conv['id']}", json={"data": data}).status_code == 200
    assert c.patch(f"/api/conversations/{conv['id']}", json={"title": "Renamed"}).json()["title"] == "Renamed"
    got = c.get(f"/api/conversations/{conv['id']}").json()
    assert got["title"] == "Renamed" and len(got["data"]["messages"]) == 2

    assert c.delete(f"/api/conversations/{other['id']}").status_code == 200
    assert c.get(f"/api/conversations/{other['id']}").status_code == 404


def test_users_only_see_their_own_conversations():
    alice, _ = _user("Alice")
    bob, _ = _user("Bob")
    conv = alice.post("/api/conversations", json={"title": "Alice private", "data": {"x": 1}}).json()
    assert bob.get("/api/conversations").json()["conversations"] == []
    assert bob.get(f"/api/conversations/{conv['id']}").status_code == 404
    assert bob.patch(f"/api/conversations/{conv['id']}", json={"title": "hacked"}).status_code == 404
    assert bob.delete(f"/api/conversations/{conv['id']}").status_code == 404
    assert alice.get(f"/api/conversations/{conv['id']}").json()["title"] == "Alice private"


def test_admin_console_requires_admin_role():
    user, _ = _user()
    assert user.get("/api/admin/users").status_code == 403
    assert TestClient(app).get("/api/admin/users").status_code == 401
    admin, _ = _admin()
    assert admin.get("/api/admin/stats").json()["users"] >= 2
    # an admin session also opens the staff endpoints that used to need the admin key
    assert admin.get("/api/audit").status_code == 200
    assert user.get("/api/audit").status_code == 401


def test_admin_manages_users_and_sees_activity():
    admin, me = _admin()
    target, t = _user("Target")
    target.post("/api/conversations", json={"title": "t", "data": {}})

    row = next(u for u in admin.get("/api/admin/users").json()["users"] if u["id"] == t["id"])
    assert row["conversations"] == 1

    assert admin.patch(f"/api/admin/users/{t['id']}", json={"active": False}).json()["user"]["active"] is False
    assert target.get("/api/auth/me").status_code == 401                 # disabling ends their sessions
    login = TestClient(app).post("/api/auth/login", json={"email": t["email"], "password": "correct-horse"})
    assert login.status_code == 401 and "disabled" in login.json()["detail"]

    assert admin.patch(f"/api/admin/users/{t['id']}", json={"active": True, "password": "new-password"}).status_code == 200
    assert TestClient(app).post("/api/auth/login", json={"email": t["email"], "password": "new-password"}).status_code == 200

    assert admin.patch(f"/api/admin/users/{me['id']}", json={"role": "user"}).status_code == 422   # no self-demotion
    assert admin.delete(f"/api/admin/users/{me['id']}").status_code == 422

    actions = {a["action"] for a in admin.get("/api/admin/activity").json()["activity"]}
    assert {"register", "login", "conversation_created", "admin_updated_user"} <= actions

    assert admin.delete(f"/api/admin/users/{t['id']}").status_code == 200
    assert accounts.list_conversations(t["id"]) == []                  # their chats go with them


def test_recommendation_is_logged_for_the_user(monkeypatch):
    import app.rag.workflow as wf
    wf.build_graph.cache_clear()
    monkeypatch.setattr(wf, "search", lambda *a, **k: [])
    c, u = _user()
    case = {"loan_type": "Home Loan", "loan_amount": "40 lakh", "age": "32", "profile": "Salaried", "cibil": "760",
            "overdue": "No", "settled": "No", "existing_emi": "0", "pin": "411001", "hl_purpose": "Resale",
            "agreement_value": "60 lakh", "prop_pin": "411038", "possession": "Ready Possession", "legal_cat": "NA",
            "net_salary": "85000", "current_job_years": "3"}
    r = c.post("/api/recommend/stream", json={"case": case, "case_id": "conv1"})
    assert "event: result" in r.text
    mine = accounts.recent_activity(user_id=u["id"])
    assert any(a["action"] == "recommendation" and a["detail"].startswith("Home Loan") for a in mine)

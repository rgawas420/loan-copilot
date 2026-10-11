"""Google Sign-In: Google's ID token is verified (signature, audience, issuer, verified email), then mapped onto the
existing accounts and sessions: a new account when sign-up is open, the same account (password, chats and role
kept) when the email already exists, and never a second, separate login system."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services import accounts, google_auth

PASSWORD = "correct-horse"


def _email():
    return f"g-{uuid.uuid4().hex[:8]}@gmail.com"


@pytest.fixture
def google(monkeypatch):
    """Google sign-in set up; the token "verifies" to the identity the test puts in it."""
    monkeypatch.setattr(get_settings(), "google_client_id", "test-client.apps.googleusercontent.com")
    tokens = {}

    def verify(credential):
        if credential not in tokens:
            raise google_auth.GoogleAuthError("Google sign-in could not be verified. Try again.")
        return tokens[credential]

    def token(email, sub=None, name="Ravi Kumar"):
        t = "tok-" + uuid.uuid4().hex
        tokens[t] = google_auth.GoogleIdentity(sub=sub or uuid.uuid4().hex, email=email, name=name)
        return t
    monkeypatch.setattr(google_auth, "verify", verify)
    return token


def _login(c, credential):
    return c.post("/api/auth/google", json={"credential": credential})


def test_without_a_client_id_there_is_no_google_sign_in():
    c = TestClient(app)
    assert c.get("/api/auth/config").json()["google_client_id"] is None
    assert _login(c, "x" * 40).status_code == 404


def test_a_new_google_user_gets_an_ordinary_account_and_session(google):
    c = TestClient(app)
    assert c.get("/api/auth/config").json()["google_client_id"] == "test-client.apps.googleusercontent.com"
    TestClient(app).post("/api/auth/register", json={"email": _email(), "name": "First", "password": PASSWORD})
    email = _email()                                      # not the first account, so not made admin
    r = _login(c, google(email, name="Ravi Kumar"))
    assert r.status_code == 200 and r.json()["created"] is True
    user = r.json()["user"]
    assert user["email"] == email and user["name"] == "Ravi Kumar" and user["role"] == "user"
    assert user["google"] is True and user["has_password"] is False
    assert c.get("/api/auth/me").json()["user"]["id"] == user["id"]                 # the usual session cookie
    assert c.post("/api/conversations", json={"title": "t", "data": {}}).status_code == 200
    # no password exists, so nobody can log in to it with one
    assert TestClient(app).post("/api/auth/login", json={"email": email, "password": accounts.NO_PASSWORD}).status_code == 401


def test_an_existing_account_is_linked_and_keeps_its_password_chats_and_role(google):
    c = TestClient(app)
    email = _email()
    uid = c.post("/api/auth/register", json={"email": email, "name": "Asha", "password": PASSWORD}).json()["user"]["id"]
    conv = c.post("/api/conversations", json={"title": "Pune flat", "data": {"x": 1}}).json()["id"]
    g = TestClient(app)
    sub = uuid.uuid4().hex
    r = _login(g, google(email.upper(), sub=sub))
    assert r.status_code == 200 and r.json()["created"] is False
    assert r.json()["user"]["id"] == uid and r.json()["user"]["name"] == "Asha" and r.json()["user"]["google"]
    assert g.get(f"/api/conversations/{conv}").json()["data"] == {"x": 1}          # same chats
    assert TestClient(app).post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    # later logins find the account by its Google id, even if the Google email changes
    assert _login(TestClient(app), google(_email(), sub=sub)).json()["user"]["id"] == uid


def test_an_email_linked_to_another_google_account_is_refused(google):
    email = _email()
    _login(TestClient(app), google(email, sub="first-google-id"))
    r = _login(TestClient(app), google(email, sub="other-google-id"))
    assert r.status_code == 403 and "different Google account" in r.json()["detail"]


def test_closed_sign_up_still_lets_existing_users_in(google, monkeypatch):
    c = TestClient(app)
    email = _email()
    c.post("/api/auth/register", json={"email": email, "name": "Asha", "password": PASSWORD})
    monkeypatch.setattr(get_settings(), "allow_registration", False)
    assert _login(TestClient(app), google(email)).status_code == 200
    r = _login(TestClient(app), google(_email()))
    assert r.status_code == 403 and "Sign-up is closed" in r.json()["detail"]


def test_a_disabled_account_cannot_sign_in_with_google(google):
    email = _email()
    uid = _login(TestClient(app), google(email, sub="dis")).json()["user"]["id"]
    with accounts.closing(accounts._conn()) as c:
        c.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
        c.commit()
    r = _login(TestClient(app), google(email, sub="dis"))
    assert r.status_code == 403 and "disabled" in r.json()["detail"]


def test_an_invalid_token_is_refused(google):
    r = _login(TestClient(app), "not-a-google-token-" + "x" * 20)
    assert r.status_code == 401 and "could not be verified" in r.json()["detail"]


def test_a_google_only_account_is_told_it_has_no_password(google):
    c = TestClient(app)
    _login(c, google(_email()))
    r = c.post("/api/auth/password", json={"current": "anything", "new": "new-password-1"})
    assert r.status_code == 422 and "signs in with Google" in r.json()["detail"]


# ------------------------------------------------------------------ the token checks themselves
@pytest.fixture
def token_says(monkeypatch):
    """What Google's library returns for the token (the library checks the signature, audience and expiry)."""
    monkeypatch.setattr(get_settings(), "google_client_id", "cid")
    from google.oauth2 import id_token
    seen = {}

    def fake(credential, request, audience, clock_skew_in_seconds=0):
        seen["audience"] = audience
        if isinstance(said["info"], Exception):
            raise said["info"]
        return said["info"]
    said = {"info": {}}
    monkeypatch.setattr(id_token, "verify_oauth2_token", fake)
    return said, seen


def test_the_token_must_be_for_this_app_from_google_with_a_verified_email(token_says):
    said, seen = token_says
    good = {"iss": "https://accounts.google.com", "sub": "123", "email": "A@Gmail.com", "email_verified": True,
            "name": "A B"}
    said["info"] = good
    g = google_auth.verify("t")
    assert (g.sub, g.email, g.name) == ("123", "a@gmail.com", "A B") and seen["audience"] == "cid"
    for bad in ({**good, "email_verified": False}, {**good, "iss": "evil.example.com"}, {**good, "email": ""},
                ValueError("Token expired"), ValueError("Wrong recipient")):
        said["info"] = bad
        with pytest.raises(google_auth.GoogleAuthError):
            google_auth.verify("t")

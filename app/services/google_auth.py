"""Google Sign-In (OpenID Connect): verify the ID token Google's sign-in button gives the browser.

The browser never sends a password or an access token here, only Google's signed ID token. It is accepted only
when Google's own library confirms the signature (Google's published keys), that it was issued for this app's
client id (GOOGLE_CLIENT_ID), by accounts.google.com, has not expired, and that Google has verified the email.
The account it maps to is an ordinary account (app/services/accounts.py: google_login), with the same session
cookie as an email / password login.
"""
from dataclasses import dataclass

from app.core.config import get_settings

ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
CLOCK_SKEW_S = 10


class GoogleAuthError(Exception):
    pass


@dataclass
class GoogleIdentity:
    sub: str            # Google's stable account id: the link to our user
    email: str
    name: str


def enabled() -> bool:
    return bool(get_settings().google_client_id)


def verify(credential: str) -> GoogleIdentity:
    """The identity in a Google ID token, or GoogleAuthError when it is not a valid token for this app."""
    client_id = get_settings().google_client_id
    if not client_id:
        raise GoogleAuthError("Google sign-in is not set up.")
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token
    try:
        info = id_token.verify_oauth2_token(credential, google_requests.Request(), client_id,
                                            clock_skew_in_seconds=CLOCK_SKEW_S)
    except Exception as e:                      # bad signature, wrong audience, expired, malformed
        raise GoogleAuthError("Google sign-in could not be verified. Try again.") from e
    if info.get("iss") not in ISSUERS:
        raise GoogleAuthError("Google sign-in could not be verified. Try again.")
    if not info.get("sub") or not info.get("email"):
        raise GoogleAuthError("Google did not share an email address for this account.")
    if info.get("email_verified") is not True:
        raise GoogleAuthError("This Google account's email address is not verified.")
    email = str(info["email"]).strip().lower()
    name = str(info.get("name") or info.get("given_name") or email.split("@")[0]).strip()[:80]
    return GoogleIdentity(sub=str(info["sub"]), email=email, name=name)

"""Spotify OAuth 2.0 Authorization Code flow."""
import os
import secrets
import base64
import hashlib
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse

from app.db import save_token, get_token

router = APIRouter(prefix="/auth")

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

SCOPES = " ".join([
    "playlist-read-private",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-read-private",
    "user-top-read",
    "user-read-recently-played",
])


def _pkce_pair() -> tuple[str, str]:
    """Luo PKCE code_verifier ja code_challenge."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


@router.get("/login")
async def login(request: Request):
    """Ohjaa käyttäjä Spotifyn kirjautumissivulle."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    request.session["pkce_verifier"] = verifier
    request.session["oauth_state"] = state

    params = {
        "client_id": os.environ["SPOTIFY_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": os.environ["SPOTIFY_REDIRECT_URI"],
        "scope": SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    return RedirectResponse(f"{SPOTIFY_AUTH_URL}?{urlencode(params)}")


@router.get("/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """Spotify kutsuu tätä kirjautumisen jälkeen."""
    if error:
        raise HTTPException(400, f"Spotify kirjautumisvirhe: {error}")

    if state != request.session.get("oauth_state"):
        raise HTTPException(400, "Virheellinen state — mahdollinen CSRF")

    verifier = request.session.pop("pkce_verifier", None)
    if not verifier:
        raise HTTPException(400, "PKCE verifier puuttuu")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": os.environ["SPOTIFY_REDIRECT_URI"],
                "client_id": os.environ["SPOTIFY_CLIENT_ID"],
                "code_verifier": verifier,
            },
        )
        resp.raise_for_status()
        token_data = resp.json()

    # Hae käyttäjän Spotify ID
    me_resp = await _get_me(token_data["access_token"])
    user_id = me_resp["id"]
    display_name = me_resp.get("display_name", user_id)

    request.session["user_id"] = user_id
    request.session["display_name"] = display_name

    await save_token(user_id, token_data)
    return RedirectResponse("/")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


async def _get_me(access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_valid_token(user_id: str) -> str:
    """Palauta voimassa oleva access_token, refreshaa tarvittaessa."""
    import time
    token_data = await get_token(user_id)
    if not token_data:
        raise HTTPException(401, "Ei kirjautunut")

    if token_data["expires_at"] - time.time() < 60:
        token_data = await _refresh_token(user_id, token_data["refresh_token"])

    return token_data["access_token"]


async def _refresh_token(user_id: str, refresh_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": os.environ["SPOTIFY_CLIENT_ID"],
            },
        )
        resp.raise_for_status()
        token_data = resp.json()

    await save_token(user_id, token_data)
    return token_data

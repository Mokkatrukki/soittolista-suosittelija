"""Luo soittolista — Spotify-playlist-luonti.

Kappaleet tulevat Claude Code + MCP-palvelimista.
Tämä reitti hoitaa vain soittolistan luonnin Spotifyhin.
"""
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_valid_token
from app.db import save_playlist

router = APIRouter(prefix="/create")
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

SPOTIFY_API = "https://api.spotify.com/v1"


def _require_user(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(401, "Kirjaudu sisään ensin")
    return user_id


@router.get("", response_class=HTMLResponse)
async def create_page(request: Request):
    user_id = request.session.get("user_id")
    display_name = request.session.get("display_name")
    return templates.TemplateResponse(
        "create.html",
        {"request": request, "user_id": user_id, "display_name": display_name},
    )


@router.post("/playlist", response_class=HTMLResponse)
async def create_playlist(request: Request):
    """Luo Spotify-soittolista valituista kappaleista.

    Odottaa form-dataa:
      uri[]              — Spotify track URI:t (spotify:track:xxx)
      playlist_name      — soittolistan nimi
      playlist_description — kuvaus (valinnainen)
    """
    user_id = _require_user(request)
    token = await get_valid_token(user_id)

    form = await request.form()
    selected_uris: list[str] = form.getlist("uri")
    playlist_name: str = form.get("playlist_name", "Uusi soittolista")
    playlist_description: str = form.get("playlist_description", "")

    if not selected_uris:
        return HTMLResponse('<p class="create-error">Valitse ainakin yksi kappale.</p>')

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            resp = await client.post(
                f"{SPOTIFY_API}/me/playlists",
                json={
                    "name": playlist_name,
                    "description": playlist_description,
                    "public": False,
                },
            )
            resp.raise_for_status()
            playlist_id = resp.json()["id"]
            playlist_url = resp.json().get("external_urls", {}).get("spotify", "")

            for i in range(0, len(selected_uris), 100):
                await client.post(
                    f"{SPOTIFY_API}/playlists/{playlist_id}/items",
                    json={"uris": selected_uris[i : i + 100]},
                )

        await save_playlist(
            user_id=user_id,
            name=playlist_name,
            description=playlist_description,
            source="create",
            tracks=[{"uri": u} for u in selected_uris],
            spotify_id=playlist_id,
        )

    except httpx.HTTPStatusError as e:
        logger.error("Spotify API error: %s", e)
        return HTMLResponse(
            f'<p class="create-error">Spotify-virhe: {e.response.status_code}. Yritä uudelleen.</p>'
        )
    except Exception as e:
        logger.error("create_playlist error: %s", e)
        return HTMLResponse('<p class="create-error">Soittolistan luonti epäonnistui.</p>')

    return templates.TemplateResponse(
        "create_success.html",
        {
            "request": request,
            "playlist_name": playlist_name,
            "track_count": len(selected_uris),
            "playlist_url": playlist_url,
        },
    )

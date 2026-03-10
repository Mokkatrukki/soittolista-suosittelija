"""Discovery-sivu: Claude pushaa kappaleet, käyttäjä arvioi selaimessa."""
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_valid_token
from app.db import (
    save_discovery_tracks,
    get_discovery_tracks,
    save_discovery_rating,
    get_discovery_ratings,
    clear_discovery_ratings,
)
from app.spotify_player import get_devices, play_track, pause_playback

router = APIRouter(prefix="/discover")
templates = Jinja2Templates(directory="app/templates")


def _require_user(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(401, "Kirjaudu sisään ensin")
    return user_id


@router.get("", response_class=HTMLResponse)
async def discover_page(request: Request):
    user_id = request.session.get("user_id")
    display_name = request.session.get("display_name")
    if not user_id:
        return templates.TemplateResponse(
            "discover.html",
            {"request": request, "user_id": None, "display_name": None},
        )
    return templates.TemplateResponse(
        "discover.html",
        {"request": request, "user_id": user_id, "display_name": display_name},
    )


@router.get("/current", response_class=HTMLResponse)
async def discover_current(request: Request):
    """HTMX-fragmentti: track-kortit. Pollataan 3s välein."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    tracks = await get_discovery_tracks()
    ratings = await get_discovery_ratings()

    return templates.TemplateResponse(
        "discover_cards.html",
        {"request": request, "tracks": tracks, "ratings": ratings},
    )


@router.get("/ratings")
async def discover_ratings(request: Request):
    """JSON-endpoint: Claude lukee arviot tästä."""
    ratings = await get_discovery_ratings()
    return JSONResponse(ratings)


@router.put("/tracks")
async def push_tracks(request: Request):
    """Claude pushaa kappaleet tänne. Body: [{artist, title, uri, reason, image_url}, ...]"""
    try:
        tracks = await request.json()
    except Exception:
        raise HTTPException(400, "Virheellinen JSON")

    if not isinstance(tracks, list):
        raise HTTPException(400, "Odotetaan JSON-listaa")

    await save_discovery_tracks(tracks)
    await clear_discovery_ratings()
    return {"ok": True, "count": len(tracks)}


@router.post("/rate", response_class=HTMLResponse)
async def rate_track(
    request: Request,
    uri: str = Form(...),
    rating: int = Form(...),
):
    """Käyttäjä antaa peukun. Palauttaa päivitetyn kortin."""
    _require_user(request)
    await save_discovery_rating(uri, rating)

    tracks = await get_discovery_tracks()
    ratings = await get_discovery_ratings()
    track = next((t for t in tracks if t.get("uri") == uri), None)
    if not track:
        return HTMLResponse("")

    return templates.TemplateResponse(
        "discover_card_single.html",
        {"request": request, "track": track, "ratings": ratings},
    )


@router.post("/player/play")
async def player_play(
    request: Request,
    uri: str = Form(...),
    device_id: str = Form(""),
):
    user_id = _require_user(request)
    try:
        token = await get_valid_token(user_id)
    except HTTPException:
        raise HTTPException(401, "Kirjaudu uudelleen")

    ok = await play_track(token, uri, device_id or None)
    if not ok:
        raise HTTPException(503, "Toisto epäonnistui — onko Spotify auki ja Premium-tili?")
    return {"ok": True}


@router.post("/player/pause")
async def player_pause(
    request: Request,
    device_id: str = Form(""),
):
    user_id = _require_user(request)
    token = await get_valid_token(user_id)
    await pause_playback(token, device_id or None)
    return {"ok": True}


@router.get("/player/devices", response_class=HTMLResponse)
async def player_devices(request: Request):
    """HTMX-fragmentti: laitevalitsin."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")
    try:
        token = await get_valid_token(user_id)
        devices = await get_devices(token)
    except Exception:
        devices = []

    return templates.TemplateResponse(
        "discover_devices.html",
        {"request": request, "devices": devices},
    )

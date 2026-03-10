"""Artistiverkosto-explorer — navigoi artistiverkostossa, kerää soittolista."""
import asyncio
import html as _html
import json
import logging
import os
from collections import defaultdict

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ai.resolver import resolve_uris
from app.auth import get_valid_token
from app.db import save_playlist

router = APIRouter(prefix="/explore")
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
SPOTIFY_API = "https://api.spotify.com/v1"

# Artisti-infocache: nimi (lowercase) → {"tags": [...], "label": "...", "country": "..."}
_artist_cache: dict[str, dict] = {}

_NON_GENRE_TAGS = {
    "seen live", "favorites", "favourite", "my favorites", "favourite songs",
    "awesome", "love", "cool", "good", "great", "best", "amazing",
    "check it out", "must hear", "all time favorites", "i love", "essential",
    "vinyl", "spotify", "artists i've seen live", "under 2000 listeners",
    "beautiful", "classic", "epic", "legendary",
}


def _require_user(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(401, "Kirjaudu sisään ensin")
    return user_id


async def _lastfm_get(method: str, **params) -> dict:
    all_params = {
        "method": method,
        "api_key": os.getenv("LASTFM_API_KEY", ""),
        "format": "json",
        **params,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(LASTFM_BASE, params=all_params)
        resp.raise_for_status()
        return resp.json()


async def _get_artist_tags(artist: str) -> list[str]:
    try:
        data = await _lastfm_get("artist.getTopTags", artist=artist, autocorrect=1)
        tags = data.get("toptags", {}).get("tag", [])
        result = []
        for t in tags[:12]:
            name = t.get("name", "").lower().strip()
            if name and name not in _NON_GENRE_TAGS and len(name) > 1:
                result.append(name)
        return result[:5]
    except Exception as e:
        logger.debug("Failed to get tags for %s: %s", artist, e)
        return []


async def _get_similar_artists(artist: str, limit: int = 15) -> list[str]:
    try:
        data = await _lastfm_get("artist.getSimilar", artist=artist, limit=limit, autocorrect=1)
        similar = data.get("similarartists", {}).get("artist", [])
        return [a["name"] for a in similar if a.get("name")]
    except Exception as e:
        logger.debug("Failed to get similar for %s: %s", artist, e)
        return []


async def _get_top_tracks(artist: str, limit: int = 10) -> list[dict]:
    try:
        data = await _lastfm_get("artist.getTopTracks", artist=artist, limit=limit, autocorrect=1)
        tracks = data.get("toptracks", {}).get("track", [])
        return [{"artist": artist, "title": t["name"]} for t in tracks if t.get("name")]
    except Exception as e:
        logger.debug("Failed to get top tracks for %s: %s", artist, e)
        return []


async def _get_artist_info(artist: str) -> dict:
    """Hae tagit. Välimuisti."""
    key = artist.lower()
    if key in _artist_cache and "tags" in _artist_cache[key]:
        return _artist_cache[key]
    tags = await _get_artist_tags(artist)
    _artist_cache.setdefault(key, {})["tags"] = tags
    return _artist_cache[key]


async def _get_discogs_info(artist: str) -> dict:
    """Hae Discogs-info: label, country. Välimuisti."""
    key = artist.lower()
    if key in _artist_cache and "label" in _artist_cache[key]:
        return _artist_cache[key]

    token = os.getenv("DISCOGS_TOKEN", "")
    if not token:
        return {}

    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": "SoittolistaSuosittelija/1.0 +https://github.com/example",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            search_resp = await client.get(
                "https://api.discogs.com/database/search",
                params={"q": artist, "type": "artist", "per_page": 1},
            )
            if search_resp.status_code != 200:
                return {}
            results = search_resp.json().get("results", [])
            if not results:
                return {}
            artist_id = results[0]["id"]

            releases_resp = await client.get(
                f"https://api.discogs.com/artists/{artist_id}/releases",
                params={"sort": "year", "sort_order": "desc", "per_page": 3, "page": 1},
            )
            if releases_resp.status_code != 200:
                return {}
            releases = releases_resp.json().get("releases", [])

            label, country = "", ""
            for rel in releases:
                if not label and rel.get("label"):
                    label = rel["label"]
                if not country and rel.get("country"):
                    country = rel["country"]
                if label and country:
                    break

        info = {"label": label, "country": country}
        _artist_cache.setdefault(key, {}).update(info)
        return info
    except Exception as e:
        logger.debug("Discogs info failed for %s: %s", artist, e)
        return {}


async def _fetch_neighbors(focus_set: list[str], path_names: set[str]) -> list[dict]:
    """Hae naapurit kaikille fokussolmuille rinnakkain."""
    if not focus_set:
        return []

    similar_lists = await asyncio.gather(
        *[_get_similar_artists(a, limit=10) for a in focus_set],
        return_exceptions=True,
    )

    artist_via: dict[str, list[str]] = defaultdict(list)
    for focus, similars in zip(focus_set, similar_lists):
        if isinstance(similars, Exception):
            continue
        for name in similars:
            artist_via[name].append(focus)

    path_lower = {n.lower() for n in path_names}
    artist_via = {
        name: via for name, via in artist_via.items()
        if name.lower() not in path_lower
    }

    sorted_artists = sorted(
        artist_via.items(),
        key=lambda x: (-len(x[1]), x[0].lower()),
    )[:20]

    names = [name for name, _ in sorted_artists]

    tag_results, discogs_results = await asyncio.gather(
        asyncio.gather(*[_get_artist_info(n) for n in names], return_exceptions=True),
        asyncio.gather(*[_get_discogs_info(n) for n in names], return_exceptions=True),
    )

    neighbors = []
    for (name, via), tag_res, disc_res in zip(sorted_artists, tag_results, discogs_results):
        tags = tag_res.get("tags", []) if isinstance(tag_res, dict) else []
        label = disc_res.get("label", "") if isinstance(disc_res, dict) else ""
        country = disc_res.get("country", "") if isinstance(disc_res, dict) else ""
        neighbors.append({
            "name": name,
            "tags": tags,
            "label": label,
            "country": country,
            "via": via,
            "via_count": len(via),
        })

    return neighbors


def _focus_title(focus_set: list[str]) -> str:
    if not focus_set:
        return "Ei fokusta"
    if len(focus_set) == 1:
        return f"{focus_set[0]}:n suositukset"
    if len(focus_set) == 2:
        return f"{focus_set[0]} + {focus_set[1]}:n suositukset"
    rest = ", ".join(focus_set[:-1])
    return f"{rest} + {focus_set[-1]}:n suositukset"


def _render_oob_response(
    path: list[dict],
    focus_set: list[str],
    selected: list[str],
    neighbors: list[dict],
    path_json: str,
    focus_json: str,
    selected_json: str,
    neighbors_json: str,
) -> HTMLResponse:
    """Rakenna täydellinen HTMX-vastaus: next-level (main) + OOB-päivitykset."""
    next_html = templates.env.get_template("explore_next.html").render(
        neighbors=neighbors,
        focus_set=focus_set,
        selected=selected,
        focus_title=_focus_title(focus_set),
        path_json=path_json,
        focus_json=focus_json,
        selected_json=selected_json,
        neighbors_json=neighbors_json,
    )

    path_html = templates.env.get_template("explore_path.html").render(
        path=path,
        focus_set=focus_set,
        selected=selected,
        path_json=path_json,
        focus_json=focus_json,
        selected_json=selected_json,
        neighbors_json=neighbors_json,
    )

    pills_html = templates.env.get_template("explore_pills.html").render(
        selected=selected,
        path_json=path_json,
        focus_json=focus_json,
        selected_json=selected_json,
        neighbors_json=neighbors_json,
    )

    def esc(s: str) -> str:
        return _html.escape(s, quote=True)

    state_inputs = (
        f'<input type="hidden" name="path_json" value="{esc(path_json)}">'
        f'<input type="hidden" name="focus_json" value="{esc(focus_json)}">'
        f'<input type="hidden" name="selected_json" value="{esc(selected_json)}">'
        f'<input type="hidden" name="neighbors_json" value="{esc(neighbors_json)}">'
    )

    return HTMLResponse(
        next_html
        + f'\n<div id="path-column" class="explore-path-column" hx-swap-oob="true">{path_html}</div>'
        + f'\n<div id="explore-state" style="display:none" hx-swap-oob="true">{state_inputs}</div>'
        + f'\n<div id="selected-pills" class="selected-pills-area" hx-swap-oob="true">{pills_html}</div>'
    )


# ---------------------------------------------------------------------------
# Reitit
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def explore_page(request: Request):
    user_id = request.session.get("user_id")
    display_name = request.session.get("display_name")
    return templates.TemplateResponse(
        "explore.html",
        {"request": request, "user_id": user_id, "display_name": display_name},
    )


@router.post("/start", response_class=HTMLResponse)
async def explore_start(request: Request, seed: str = Form(...)):
    user_id = request.session.get("user_id")
    display_name = request.session.get("display_name")

    seed = seed.strip()
    if not seed:
        return HTMLResponse('<p class="explore-error">Kirjoita artistin nimi.</p>')

    tags, _ = await asyncio.gather(
        _get_artist_tags(seed),
        _get_similar_artists(seed, limit=1),  # warm up cache
    )
    _artist_cache.setdefault(seed.lower(), {})["tags"] = tags

    path = [{"name": seed, "tags": tags, "focus": True, "parent": None, "depth": 0}]
    focus_set = [seed]
    selected: list[str] = []

    neighbors = await _fetch_neighbors(focus_set, {seed})

    path_json = json.dumps(path)
    focus_json = json.dumps(focus_set)
    selected_json = json.dumps(selected)
    neighbors_json = json.dumps(neighbors)

    def esc(s: str) -> str:
        return _html.escape(s, quote=True)

    return templates.TemplateResponse(
        "explore_layout.html",
        {
            "request": request,
            "user_id": user_id,
            "display_name": display_name,
            "path": path,
            "focus_set": focus_set,
            "selected": selected,
            "neighbors": neighbors,
            "focus_title": _focus_title(focus_set),
            "path_json": path_json,
            "focus_json": focus_json,
            "selected_json": selected_json,
            "neighbors_json": neighbors_json,
        },
    )


@router.post("/traverse", response_class=HTMLResponse)
async def explore_traverse(
    request: Request,
    artist: str = Form(...),
    path_json: str = Form("[]"),
    focus_json: str = Form("[]"),
    selected_json: str = Form("[]"),
    neighbors_json: str = Form("[]"),
):
    try:
        path: list[dict] = json.loads(path_json)
        focus_set: list[str] = json.loads(focus_json)
        selected: list[str] = json.loads(selected_json)
    except json.JSONDecodeError:
        raise HTTPException(400, "Virheellinen state JSON")

    artist = artist.strip()

    # Etsi parent: viimeisin fokussolmu
    parent = focus_set[-1] if focus_set else None
    depth = 0
    if parent:
        parent_node = next((n for n in path if n["name"] == parent), None)
        if parent_node:
            depth = parent_node["depth"] + 1

    path_names_lower = {n["name"].lower() for n in path}
    if artist.lower() not in path_names_lower:
        info = await _get_artist_info(artist)
        tags = info.get("tags", [])
        path.append({
            "name": artist,
            "tags": tags,
            "focus": True,
            "parent": parent,
            "depth": depth,
        })

    # Lisää fokuslistaan
    if artist not in focus_set:
        focus_set.append(artist)
    # Aseta fokus = True polkusolmussa
    for node in path:
        if node["name"] == artist:
            node["focus"] = True

    all_path_names = {n["name"] for n in path}
    neighbors = await _fetch_neighbors(focus_set, all_path_names)

    new_path_json = json.dumps(path)
    new_focus_json = json.dumps(focus_set)
    new_selected_json = json.dumps(selected)
    new_neighbors_json = json.dumps(neighbors)

    return _render_oob_response(
        path, focus_set, selected, neighbors,
        new_path_json, new_focus_json, new_selected_json, new_neighbors_json,
    )


@router.post("/focus-toggle", response_class=HTMLResponse)
async def explore_focus_toggle(
    request: Request,
    artist: str = Form(...),
    path_json: str = Form("[]"),
    focus_json: str = Form("[]"),
    selected_json: str = Form("[]"),
    neighbors_json: str = Form("[]"),
):
    try:
        path: list[dict] = json.loads(path_json)
        focus_set: list[str] = json.loads(focus_json)
        selected: list[str] = json.loads(selected_json)
    except json.JSONDecodeError:
        raise HTTPException(400, "Virheellinen state JSON")

    if artist in focus_set:
        if len(focus_set) > 1:
            focus_set.remove(artist)
            for node in path:
                if node["name"] == artist:
                    node["focus"] = False
    else:
        focus_set.append(artist)
        for node in path:
            if node["name"] == artist:
                node["focus"] = True

    all_path_names = {n["name"] for n in path}
    neighbors = await _fetch_neighbors(focus_set, all_path_names)

    new_path_json = json.dumps(path)
    new_focus_json = json.dumps(focus_set)
    new_selected_json = json.dumps(selected)
    new_neighbors_json = json.dumps(neighbors)

    return _render_oob_response(
        path, focus_set, selected, neighbors,
        new_path_json, new_focus_json, new_selected_json, new_neighbors_json,
    )


@router.post("/select-toggle", response_class=HTMLResponse)
async def explore_select_toggle(
    request: Request,
    artist: str = Form(...),
    path_json: str = Form("[]"),
    focus_json: str = Form("[]"),
    selected_json: str = Form("[]"),
    neighbors_json: str = Form("[]"),
):
    try:
        path: list[dict] = json.loads(path_json)
        focus_set: list[str] = json.loads(focus_json)
        selected: list[str] = json.loads(selected_json)
        neighbors: list[dict] = json.loads(neighbors_json)
    except json.JSONDecodeError:
        raise HTTPException(400, "Virheellinen state JSON")

    if artist in selected:
        selected.remove(artist)
    else:
        selected.append(artist)

    new_selected_json = json.dumps(selected)

    return _render_oob_response(
        path, focus_set, selected, neighbors,
        path_json, focus_json, new_selected_json, neighbors_json,
    )


@router.post("/create", response_class=HTMLResponse)
async def explore_create(
    request: Request,
    selected_json: str = Form("[]"),
    path_json: str = Form("[]"),
    mode: str = Form("mix"),
    count: int = Form(20),
):
    user_id = _require_user(request)

    try:
        selected: list[str] = json.loads(selected_json)
        path: list[dict] = json.loads(path_json)
    except json.JSONDecodeError:
        raise HTTPException(400, "Virheellinen state JSON")

    if not selected:
        return HTMLResponse('<p class="explore-error">Valitse ainakin yksi artisti.</p>')

    try:
        token = await get_valid_token(user_id)
    except HTTPException:
        return HTMLResponse('<p class="explore-error">Kirjaudu sisään ensin.</p>')

    per_artist = max(4, count // max(len(selected), 1) + 2)

    track_lists = await asyncio.gather(
        *[_get_top_tracks(a, limit=per_artist) for a in selected],
        return_exceptions=True,
    )

    all_tracks: list[dict] = []
    for artist, tlist in zip(selected, track_lists):
        if isinstance(tlist, Exception) or not tlist:
            continue
        if mode == "popular":
            all_tracks.extend(tlist[:max(2, count // max(len(selected), 1))])
        elif mode == "discovery":
            # Hyppää top-3, ota sen jälkeen
            deep = tlist[3:]
            if deep:
                all_tracks.extend(deep[:max(2, count // max(len(selected), 1))])
            else:
                all_tracks.extend(tlist[:2])
        else:  # mix
            all_tracks.extend(tlist[:2])
            if len(tlist) > 3:
                all_tracks.extend(tlist[3:5])

    all_tracks = all_tracks[:count]

    if not all_tracks:
        return HTMLResponse('<p class="explore-error">Kappaleiden haku epäonnistui.</p>')

    all_tracks = await resolve_uris(all_tracks, token)

    if not all_tracks:
        return HTMLResponse('<p class="explore-error">Spotify-haku epäonnistui.</p>')

    seed_name = path[0]["name"] if path else selected[0]
    if len(selected) == 1:
        playlist_name = f"{selected[0]} — verkosto"
    else:
        playlist_name = f"{seed_name}:n verkosto"

    description = f"Artistiverkosto: {', '.join(selected[:5])}"
    if len(selected) > 5:
        description += f" + {len(selected) - 5} muuta"

    uris = [t["uri"] for t in all_tracks if t.get("uri")]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            resp = await client.post(
                f"{SPOTIFY_API}/me/playlists",
                json={"name": playlist_name, "description": description, "public": False},
            )
            resp.raise_for_status()
            playlist_id = resp.json()["id"]
            playlist_url = resp.json().get("external_urls", {}).get("spotify", "")

            for i in range(0, len(uris), 100):
                await client.post(
                    f"{SPOTIFY_API}/playlists/{playlist_id}/items",
                    json={"uris": uris[i : i + 100]},
                )

        await save_playlist(
            user_id=user_id,
            name=playlist_name,
            description=description,
            source="explore",
            tracks=all_tracks,
            spotify_id=playlist_id,
        )
    except httpx.HTTPStatusError as e:
        logger.error("Spotify API error: %s", e)
        return HTMLResponse(
            f'<p class="explore-error">Spotify-virhe ({e.response.status_code}). Yritä uudelleen.</p>'
        )
    except Exception as e:
        logger.error("explore_create error: %s", e)
        return HTMLResponse('<p class="explore-error">Soittolistan luonti epäonnistui.</p>')

    return templates.TemplateResponse(
        "explore_result.html",
        {
            "request": request,
            "playlist_name": playlist_name,
            "track_count": len(uris),
            "playlist_url": playlist_url,
            "selected": selected,
        },
    )

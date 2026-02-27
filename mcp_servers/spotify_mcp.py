"""Spotify MCP-palvelin kehityskäyttöön.

Lukee OAuth-tokenin suoraan SQLite-tietokannasta (sama kuin web-sovellus).
Käytä web-sovelluksen kautta ensin kirjautuaksesi sisään.
"""
import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

import aiosqlite

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")
SPOTIFY_BASE = "https://api.spotify.com/v1"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

mcp = FastMCP("spotify")


async def _get_token() -> str:
    """Hae viimeisin voimassa oleva token tietokannasta."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tokens ORDER BY updated_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()

    if not row:
        raise RuntimeError("Ei kirjautuneita käyttäjiä. Kirjaudu ensin web-sovelluksessa: http://127.0.0.1:8000")

    token = dict(row)

    if token["expires_at"] - time.time() < 60:
        token = await _refresh(token["user_id"], token["refresh_token"])

    return token["access_token"]


async def _refresh(user_id: str, refresh_token: str) -> dict:
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
        data = resp.json()

    expires_at = time.time() + data.get("expires_in", 3600)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE tokens SET access_token=?, expires_at=?, updated_at=?
               WHERE user_id=?""",
            (data["access_token"], expires_at, time.time(), user_id),
        )
        await db.commit()

    data["expires_at"] = expires_at
    return data


async def _get(path: str, params: dict = None) -> dict:
    token = await _get_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SPOTIFY_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, json: dict = None) -> dict:
    token = await _get_token()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SPOTIFY_BASE}{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=json or {},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def search_track(artist: str, title: str) -> dict:
    """Hae tietty kappale Spotifysta. Palauttaa id:n, uri:n ja metatiedot."""
    q = f"track:{title} artist:{artist}"
    data = await _get("/search", {"q": q, "type": "track", "limit": 1})
    tracks = data.get("tracks", {}).get("items", [])
    if not tracks:
        return {"found": False, "artist": artist, "title": title}
    t = tracks[0]
    return {
        "found": True,
        "uri": t["uri"],
        "id": t["id"],
        "name": t["name"],
        "artist": t["artists"][0]["name"],
        "album": t["album"]["name"],
        "duration_ms": t["duration_ms"],
    }


@mcp.tool()
async def search_tracks_batch(tracks: list[dict]) -> list[dict]:
    """Hae useita kappaleita rinnakkain. tracks = [{artist, title}, ...]. Max 20 kerralla."""
    async def _one(t):
        return await search_track(t["artist"], t["title"])
    return await asyncio.gather(*[_one(t) for t in tracks[:20]])


@mcp.tool()
async def get_my_profile() -> dict:
    """Hae kirjautuneen käyttäjän Spotify-profiili."""
    data = await _get("/me")
    return {"id": data["id"], "display_name": data.get("display_name"), "uri": data["uri"]}


@mcp.tool()
async def get_top_tracks(time_range: str = "medium_term", limit: int = 20) -> list[dict]:
    """Hae käyttäjän top-kappaleet.
    time_range: short_term (4vk) | medium_term (6kk) | long_term (vuosia)
    """
    data = await _get("/me/top/tracks", {"time_range": time_range, "limit": min(limit, 50)})
    return [
        {"uri": t["uri"], "name": t["name"], "artist": t["artists"][0]["name"], "album": t["album"]["name"]}
        for t in data.get("items", [])
    ]


@mcp.tool()
async def get_top_artists(time_range: str = "medium_term", limit: int = 20) -> list[dict]:
    """Hae käyttäjän top-artistit.
    time_range: short_term (4vk) | medium_term (6kk) | long_term (vuosia)
    """
    data = await _get("/me/top/artists", {"time_range": time_range, "limit": min(limit, 50)})
    return [
        {"id": a["id"], "name": a["name"], "genres": a.get("genres", [])}
        for a in data.get("items", [])
    ]


@mcp.tool()
async def get_recently_played(limit: int = 20) -> list[dict]:
    """Hae käyttäjän viimeksi kuunnellut kappaleet."""
    data = await _get("/me/player/recently-played", {"limit": min(limit, 50)})
    return [
        {
            "uri": item["track"]["uri"],
            "name": item["track"]["name"],
            "artist": item["track"]["artists"][0]["name"],
            "played_at": item["played_at"],
        }
        for item in data.get("items", [])
    ]


@mcp.tool()
async def get_my_playlists(limit: int = 20) -> list[dict]:
    """Hae kirjautuneen käyttäjän soittolistat."""
    data = await _get("/me/playlists", {"limit": min(limit, 50)})
    return [
        {"id": p["id"], "name": p["name"], "uri": p["uri"]}
        for p in data.get("items", [])
    ]


@mcp.tool()
async def create_playlist(name: str, description: str = "", public: bool = False) -> dict:
    """Luo uusi soittolista kirjautuneelle käyttäjälle."""
    data = await _post("/me/playlists", {
        "name": name,
        "description": description,
        "public": public,
    })
    return {"id": data["id"], "uri": data["uri"], "name": data["name"]}


@mcp.tool()
async def add_tracks_to_playlist(playlist_id: str, uris: list[str]) -> dict:
    """Lisää kappaleet soittolistaan. uris = ['spotify:track:xxx', ...]. Max 100 kerralla."""
    data = await _post(f"/playlists/{playlist_id}/items", {"uris": uris[:100]})
    return {"snapshot_id": data.get("snapshot_id"), "added": len(uris[:100])}


@mcp.tool()
async def get_playlist_items(playlist_id: str, limit: int = 20) -> list[dict]:
    """Hae soittolistan kappaleet."""
    data = await _get(f"/playlists/{playlist_id}/items", {"limit": min(limit, 50)})
    result = []
    for item in data.get("items", []):
        track = item.get("item") or item.get("track")
        if track and track.get("type") == "track":
            result.append({
                "uri": track["uri"],
                "name": track["name"],
                "artist": track["artists"][0]["name"],
                "added_at": item.get("added_at"),
            })
    return result


@mcp.tool()
async def get_artist_albums(artist_id: str, limit: int = 10) -> list[dict]:
    """Hae artistin albumit julkaisupäivän mukaan. artist_id = Spotify artist ID."""
    data = await _get(f"/artists/{artist_id}/albums", {"limit": min(limit, 50), "include_groups": "album,single"})
    return [
        {
            "id": a["id"],
            "name": a["name"],
            "release_date": a.get("release_date"),
            "type": a.get("album_type"),
            "uri": a["uri"],
        }
        for a in data.get("items", [])
    ]


if __name__ == "__main__":
    mcp.run()

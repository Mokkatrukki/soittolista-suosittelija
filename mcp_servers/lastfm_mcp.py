"""Last.fm MCP-palvelin kehityskäyttöön.

Ei vaadi käyttäjän kirjautumista — pelkkä API-avain riittää lukuoperaatioihin.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("lastfm")

LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"


async def _call(method: str, params: dict = None) -> dict:
    api_key = os.environ.get("LASTFM_API_KEY")
    if not api_key or api_key == "your_lastfm_api_key_here":
        raise RuntimeError("LASTFM_API_KEY puuttuu .env-tiedostosta. Hanki osoitteesta: https://www.last.fm/api/account/create")

    p = {"method": method, "api_key": api_key, "format": "json", "autocorrect": 1, **(params or {})}
    async with httpx.AsyncClient() as client:
        resp = await client.get(LASTFM_BASE, params=p)
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise RuntimeError(f"Last.fm virhe {data['error']}: {data.get('message')}")
    return data


# --- CHART ---

@mcp.tool()
async def chart_top_tracks(limit: int = 30) -> list[dict]:
    """Hae globaalit top-kappaleet Last.fm:stä tällä hetkellä."""
    data = await _call("chart.getTopTracks", {"limit": min(limit, 100)})
    return [
        {
            "artist": t["artist"]["name"],
            "title": t["name"],
            "listeners": int(t.get("listeners", 0)),
            "playcount": int(t.get("playcount", 0)),
        }
        for t in data.get("tracks", {}).get("track", [])
    ]


@mcp.tool()
async def chart_top_artists(limit: int = 20) -> list[dict]:
    """Hae globaalit top-artistit Last.fm:stä tällä hetkellä."""
    data = await _call("chart.getTopArtists", {"limit": min(limit, 100)})
    return [
        {
            "name": a["name"],
            "listeners": int(a.get("listeners", 0)),
            "playcount": int(a.get("playcount", 0)),
        }
        for a in data.get("artists", {}).get("artist", [])
    ]


# --- GEO (maakohtaiset top-listat) ---

@mcp.tool()
async def geo_top_tracks(country: str = "Finland", limit: int = 30) -> list[dict]:
    """Hae maan top-kappaleet. Esim: country='Finland', 'Sweden', 'Germany'.
    Hyvä 'suosittua nyt Suomessa' -featureen."""
    data = await _call("geo.getTopTracks", {"country": country, "limit": min(limit, 100)})
    return [
        {
            "artist": t["artist"]["name"],
            "title": t["name"],
            "listeners": int(t.get("listeners", 0)),
            "rank": int(t.get("@attr", {}).get("rank", 0)),
        }
        for t in data.get("tracks", {}).get("track", [])
    ]


@mcp.tool()
async def geo_top_artists(country: str = "Finland", limit: int = 20) -> list[dict]:
    """Hae maan top-artistit. Esim: country='Finland'."""
    data = await _call("geo.getTopArtists", {"country": country, "limit": min(limit, 100)})
    return [
        {
            "name": a["name"],
            "listeners": int(a.get("listeners", 0)),
        }
        for a in data.get("topartists", {}).get("artist", [])
    ]


# --- ARTIST ---

@mcp.tool()
async def artist_top_tracks(artist: str, limit: int = 20) -> list[dict]:
    """Hae artistin suosituimmat kappaleet. Korvaa Spotifyn poistetun top-tracks endpointin."""
    data = await _call("artist.getTopTracks", {"artist": artist, "limit": min(limit, 50)})
    return [
        {
            "artist": artist,
            "title": t["name"],
            "playcount": int(t.get("playcount", 0)),
            "listeners": int(t.get("listeners", 0)),
        }
        for t in data.get("toptracks", {}).get("track", [])
    ]


@mcp.tool()
async def artist_top_tags(artist: str) -> list[dict]:
    """Hae artistin top-tagit (genret/tyylit) suosituimmuusjärjestyksessä."""
    data = await _call("artist.getTopTags", {"artist": artist})
    return [
        {
            "tag": t["name"],
            "count": int(t.get("count", 0)),
            "url": t.get("url", ""),
        }
        for t in data.get("toptags", {}).get("tag", [])
    ]


@mcp.tool()
async def artist_similar(artist: str, limit: int = 10) -> list[dict]:
    """Hae samanlaiset artistit."""
    data = await _call("artist.getSimilar", {"artist": artist, "limit": min(limit, 30)})
    return [
        {
            "name": a["name"],
            "match": float(a.get("match", 0)),
        }
        for a in data.get("similarartists", {}).get("artist", [])
    ]


@mcp.tool()
async def artist_info(artist: str) -> dict:
    """Hae artistin tiedot, kuuntelijamäärät ja tagit."""
    data = await _call("artist.getInfo", {"artist": artist})
    info = data.get("artist", {})
    tags = [t["name"] for t in info.get("tags", {}).get("tag", [])]
    return {
        "name": info.get("name"),
        "listeners": int(info.get("stats", {}).get("listeners", 0)),
        "playcount": int(info.get("stats", {}).get("playcount", 0)),
        "tags": tags,
        "bio_summary": info.get("bio", {}).get("summary", "")[:300],
    }


@mcp.tool()
async def artist_search(artist: str, limit: int = 5) -> list[dict]:
    """Hae artisteja nimellä — hyvä kirjoitusvirheiden korjaukseen."""
    data = await _call("artist.search", {"artist": artist, "limit": min(limit, 10)})
    return [
        {
            "name": a["name"],
            "listeners": int(a.get("listeners", 0)),
            "mbid": a.get("mbid", ""),
        }
        for a in data.get("results", {}).get("artistmatches", {}).get("artist", [])
    ]


# --- TAG ---

@mcp.tool()
async def tag_top_tracks(tag: str, limit: int = 30) -> list[dict]:
    """Hae genren / tagin top-kappaleet. Esim: tag='jazz', 'metal', 'indie', 'finnish'."""
    data = await _call("tag.getTopTracks", {"tag": tag, "limit": min(limit, 100)})
    return [
        {
            "artist": t["artist"]["name"],
            "title": t["name"],
            "rank": int(t.get("@attr", {}).get("rank", 0)),
        }
        for t in data.get("tracks", {}).get("track", [])
    ]


@mcp.tool()
async def tag_top_artists(tag: str, limit: int = 20) -> list[dict]:
    """Hae genren top-artistit. Esim: tag='ambient', 'punk', 'suomi-pop'."""
    data = await _call("tag.getTopArtists", {"tag": tag, "limit": min(limit, 50)})
    return [
        {
            "name": a["name"],
            "rank": int(a.get("@attr", {}).get("rank", 0)),
        }
        for a in data.get("topartists", {}).get("artist", [])
    ]


@mcp.tool()
async def tag_info(tag: str) -> dict:
    """Hae tagin metadata: käyttäjämäärä (reach), käyttökerrat (taggings) ja lyhyt kuvaus.
    Hyödyllinen tagin laadun arviointiin — korkea taggings = vakiintunut genre.
    """
    data = await _call("tag.getInfo", {"tag": tag})
    t = data.get("tag", {})
    return {
        "name": t.get("name"),
        "reach": int(t.get("reach", 0)),
        "taggings": int(t.get("taggings", 0)),
        "streamable": t.get("streamable") == "1",
        "summary": (t.get("wiki", {}).get("summary", "") or "")[:300],
    }


@mcp.tool()
async def tag_similar(tag: str) -> list[dict]:
    """Hae samanlaiset tagit/genret. Hyvä genre-laajennukseen."""
    data = await _call("tag.getSimilar", {"tag": tag})
    return [
        {"tag": t["name"], "url": t.get("url", "")}
        for t in data.get("similartags", {}).get("tag", [])
    ]


# --- TRACK ---

@mcp.tool()
async def track_similar(artist: str, title: str, limit: int = 20) -> list[dict]:
    """Hae samanlaiset kappaleet kappaleen perusteella. Hyvä soittolistan laajennukseen."""
    data = await _call("track.getSimilar", {"artist": artist, "track": title, "limit": min(limit, 50)})
    return [
        {
            "artist": t["artist"]["name"],
            "title": t["name"],
            "match": float(t.get("match", 0)),
            "playcount": int(t.get("playcount", 0)),
        }
        for t in data.get("similartracks", {}).get("track", [])
    ]


@mcp.tool()
async def track_info(artist: str, title: str) -> dict:
    """Hae kappaleen tiedot, tagit ja kuuntelijamäärät."""
    data = await _call("track.getInfo", {"artist": artist, "track": title})
    info = data.get("track", {})
    tags = [t["name"] for t in info.get("toptags", {}).get("tag", [])]
    return {
        "title": info.get("name"),
        "artist": info.get("artist", {}).get("name"),
        "duration_ms": int(info.get("duration", 0)),
        "listeners": int(info.get("listeners", 0)),
        "playcount": int(info.get("playcount", 0)),
        "tags": tags,
    }


@mcp.tool()
async def track_search(title: str, limit: int = 10) -> list[dict]:
    """Hae kappaleita nimellä."""
    data = await _call("track.search", {"track": title, "limit": min(limit, 30)})
    return [
        {
            "artist": t["artist"],
            "title": t["name"],
            "listeners": int(t.get("listeners", 0)),
        }
        for t in data.get("results", {}).get("trackmatches", {}).get("track", [])
    ]


if __name__ == "__main__":
    mcp.run()

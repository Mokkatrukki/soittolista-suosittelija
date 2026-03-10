"""MusicBrainz + ListenBrainz MCP-palvelin.

MusicBrainz (mb_*): artistisuhteet (influenced-by, member-of), tagit, MBID-haku
ListenBrainz (lb_*): similar artists kuunteludatan perusteella, suosiotilastot

Rate limitit:
  MusicBrainz : max 1 pyyntö/sekunti — _mb_get() serialisoi automaattisesti
  ListenBrainz: ~60 req/min ilman tokenia (riittää normaalikäyttöön)

Ei vaadi API-avaimia — User-Agent riittää molemmille.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("musicbrainz")

MB_BASE = "https://musicbrainz.org/ws/2"
LB_BASE = "https://api.listenbrainz.org"
USER_AGENT = "soittolista-suosittelija/0.1 (dev)"

# MusicBrainz: max 1 req/s — semafori serialisoi kutsut
_mb_sem = asyncio.Semaphore(1)
_mb_last: float = 0.0


async def _mb_get(path: str, params: dict | None = None) -> dict:
    """MusicBrainz GET — serialisoitu, 1 req/s välein."""
    global _mb_last
    async with _mb_sem:
        gap = 1.1 - (time.monotonic() - _mb_last)
        if gap > 0:
            await asyncio.sleep(gap)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{MB_BASE}{path}",
                params={"fmt": "json", **(params or {})},
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            data = resp.json()
        _mb_last = time.monotonic()
    return data


def _lb_headers() -> dict:
    headers = {"User-Agent": USER_AGENT}
    token = os.environ.get("LB_TOKEN")
    if token:
        headers["Authorization"] = f"Token {token}"
    return headers


async def _lb_get(path: str, params: dict | None = None) -> dict:
    """ListenBrainz GET. Käyttää LB_TOKEN:ia jos asetettu (.env)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{LB_BASE}{path}",
            params=params or {},
            headers=_lb_headers(),
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# MusicBrainz-työkalut
# ---------------------------------------------------------------------------

@mcp.tool()
async def mb_artist_search(artist: str, limit: int = 5) -> list[dict]:
    """Hae artisti MusicBrainzista nimellä — palauttaa MBID:n ja perustiedot.

    MBID tarvitaan mb_artist_relations, mb_artist_tags ja lb_* -työkaluihin.
    Jos hakutulos sisältää useita, valitse korkein score tai oikea disambiguation.
    """
    data = await _mb_get("/artist", {"query": f'artist:"{artist}"', "limit": min(limit, 10)})
    return [
        {
            "mbid": a.get("id"),
            "name": a.get("name"),
            "country": a.get("country", ""),
            "disambiguation": a.get("disambiguation", ""),
            "score": int(a.get("score", 0)),
        }
        for a in data.get("artists", [])
    ]


@mcp.tool()
async def mb_artist_relations(mbid: str) -> dict:
    """Hae artistin MusicBrainz-suhteet: influenced-by, member-of, tribute jne.

    'influenced_by' paljastaa musiikilliset juuret — erinomainen surprising-tier
    suositteluun. 'members' kertoo bändin jäsenet, 'member_of' henkilön bändit.
    """
    data = await _mb_get(f"/artist/{mbid}", {"inc": "artist-rels"})

    influenced_by: list[dict] = []
    has_influenced: list[dict] = []
    members: list[dict] = []
    member_of: list[dict] = []
    other: list[dict] = []

    for rel in data.get("relations", []):
        rel_type = rel.get("type", "")
        direction = rel.get("direction", "")
        target = rel.get("artist", {})
        if not target:
            continue
        entry = {
            "name": target.get("name"),
            "mbid": target.get("id"),
            "disambiguation": target.get("disambiguation", ""),
        }
        if rel_type == "influenced by":
            if direction == "backward":
                influenced_by.append(entry)   # tämä artisti ← vaikutus
            else:
                has_influenced.append(entry)  # tämä artisti → vaikutti
        elif rel_type == "member of band":
            if direction == "forward":
                member_of.append(entry)   # henkilö → bändi
            else:
                members.append(entry)     # bändi → jäsen
        else:
            other.append({**entry, "type": rel_type, "direction": direction})

    return {
        "name": data.get("name"),
        "mbid": mbid,
        "type": data.get("type", ""),
        "influenced_by": influenced_by,
        "has_influenced": has_influenced,
        "members": members,
        "member_of": member_of,
        "other_relations": other[:10],
    }


@mcp.tool()
async def mb_artist_tags(mbid: str) -> list[dict]:
    """Hae artistin MusicBrainz-tagit (yhteisön äänestämät genret/tyylit).

    Eri data kuin Last.fm — perustuu MusicBrainz-yhteisön ääniin, ei scrobbleihin.
    """
    data = await _mb_get(f"/artist/{mbid}", {"inc": "tags"})
    tags = sorted(data.get("tags", []), key=lambda t: int(t.get("count", 0)), reverse=True)
    return [
        {"tag": t["name"], "votes": int(t.get("count", 0))}
        for t in tags
    ]


@mcp.tool()
async def mb_release_search(artist: str, limit: int = 10) -> list[dict]:
    """Hae artistin julkaisut MusicBrainzista (albumit, EP:t, singlet).

    Hyödyllinen uusimpien julkaisujen löytämiseen tai diskografian selailuun.
    Huom: GET /browse/new-releases on poistettu Spotifysta — tämä korvaa sen.
    """
    data = await _mb_get("/release-group", {
        "artist": artist,
        "type": "album|ep|single",
        "limit": min(limit, 25),
    })
    return [
        {
            "title": r.get("title"),
            "type": r.get("primary-type", ""),
            "date": r.get("first-release-date", ""),
            "mbid": r.get("id"),
        }
        for r in data.get("release-groups", [])
    ]


# ---------------------------------------------------------------------------
# ListenBrainz-työkalut
# ---------------------------------------------------------------------------

@mcp.tool()
async def lb_similar_artists(mbid: str, count: int = 20) -> list[dict]:
    """Hae samankaltaiset artistit ListenBrainzin lb-radio/artist-endpointin kautta.

    Eri signaali kuin Last.fm artist.getSimilar — perustuu kuuntelusessioihin.
    Palauttaa artistit listen_count-järjestyksessä. Vaatii MBID:n (hae mb_artist_search).
    """
    data = await _lb_get(f"/1/lb-radio/artist/{mbid}", {
        "mode": "easy",
        "max_similar_artists": min(count, 50),
        "max_recordings_per_artist": 1,
        "pop_begin": 0,
        "pop_end": 100,
    })
    results = []
    for artist_mbid, recordings in data.items():
        if artist_mbid == mbid or not recordings:
            continue
        rec = recordings[0]
        results.append({
            "name": rec.get("similar_artist_name", ""),
            "mbid": rec.get("similar_artist_mbid", artist_mbid),
            "listen_count": rec.get("total_listen_count", 0),
        })
    return sorted(results, key=lambda x: x["listen_count"], reverse=True)


@mcp.tool()
async def lb_artist_popularity(mbids: list[str]) -> list[dict]:
    """Hae artistien suosio ListenBrainzista (listener_count, listen_count).

    Voidaan hakea useammalle MBID:lle kerralla (max 25).
    Käyttö: vertaa kuuntelijamääriä, löydä piilohelmia.
    """
    if not mbids:
        return []
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{LB_BASE}/1/popularity/artist/",
            json=mbids[:25],
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
    results = data if isinstance(data, list) else data.get("payload", [])
    return [
        {
            "mbid": r.get("artist_mbid"),
            "listeners": r.get("listener_count", 0),
            "listen_count": r.get("total_listen_count", 0),
        }
        for r in results
    ]


if __name__ == "__main__":
    mcp.run()

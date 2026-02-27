"""Music Discovery MCP — yhdistää Yle Areena, MusicBrainz ja Apple Music RSS.

Ei vaadi API-avaimia (Areena käyttää julkista app_id:tä, muut ovat avoimia).
"""
import re
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("music_discovery")

AREENA_API = "https://areena.api.yle.fi/v1/ui"
AREENA_PARAMS = {
    "language": "fi",
    "v": "10",
    "client": "yle-areena-web",
    "app_id": "areena-web-items",
    "app_key": "wlTs5D9OjIdeS9krPzRQR4I1PYVzoazN",
}

# Tunnetut musiikkiohjelmat (sarja-ID → nimi)
MUSIC_SHOWS = {
    "1-3201240": "Pekka Laineen Ihmemaa",
    "1-1653834": "Levylautakunta",
    "1-3262577": "Kissankehto - Susanna Vainiola",
    "1-3210491": "Tuuli Saksalan keidas",
    "1-75855232": "Sillanpään sunnuntai",
    "1-64590159": "Radio Suomen Musiikki-ilta",
    "1-4409832": "Iskelmäradio",
    "1-1479287": "Entisten nuorten sävellahja",
}


async def _areena_get(path: str, extra: dict = None) -> dict:
    params = {**AREENA_PARAMS, **(extra or {})}
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{AREENA_API}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


def _parse_tracks(description: str) -> list[dict]:
    """Parsii 'Artisti: Kappale' -rivit kuvauksesta."""
    tracks = []
    for line in description.splitlines():
        line = line.strip()
        if ":" in line and len(line) > 5:
            parts = line.split(":", 1)
            artist = parts[0].strip()
            title = parts[1].strip()
            # Poista juokseva numero artistin edestä: "1. Foo" tai "1.Foo"
            artist = re.sub(r"^\d+\.\s*", "", artist).strip()
            # Poista vuosiluku kappaleen lopusta: "Foo (1983)" tai "Foo - Live (1992)"
            title = re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip()
            # Poista tekijätiedot sulkeissa: "Foo (Tekijä, Toinen)"
            title = re.sub(r"\s*\([^)]{0,60}\)\s*$", "", title).strip()
            # Poista välilehtimerkki ja sen jälkeen tuleva teksti (tekijätiedot)
            title = title.split("\t")[0].strip()
            # Suodatetaan pois selvästi ei-kappaleet
            if artist and title and len(artist) < 80 and not artist.lower().startswith("ohjelma"):
                tracks.append({"artist": artist, "title": title})
    return tracks


async def _get_series_episode_ids(series_id: str, limit: int = 10) -> list[str]:
    """Hakee sarjan viimeisimmät episodi-ID:t Areena-sivulta JWT+content/list-APIa käyttäen."""
    import base64
    page_url = f"https://areena.yle.fi/{series_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # 1) Hae HTML → ota JWT-token episodeille
        page_resp = await client.get(page_url, headers=headers)
        page_resp.raise_for_status()
        html = page_resp.text
        cookie_hdr = "; ".join(
            f"{name}={value}" for name, value in client.cookies.items()
        ) if client.cookies else ""

        # 2) Poimi JWT jossa cardOptionsTemplate=episodes + availability=current
        tokens = re.findall(
            r'content/list\?token=(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)',
            html,
        )
        ep_token = None
        for t in tokens:
            try:
                payload = json.loads(base64.urlsafe_b64decode(t.split(".")[1] + "=="))
                if "episodes" in payload.get("cardOptionsTemplate", "") and "current" in payload.get("source", ""):
                    ep_token = t
                    break
            except Exception:
                continue

        if not ep_token:
            return []

        # 3) Kutsu content/list-API tokenilla
        api_url = (
            f"https://areena.api.yle.fi/v1/ui/content/list?token={ep_token}"
            f"&language=fi&v=10&client=yle-areena-web"
            f"&app_id=areena-web-items&app_key=wlTs5D9OjIdeS9krPzRQR4I1PYVzoazN"
            f"&limit={limit}"
        )
        req_headers = {**headers, "Cookie": cookie_hdr} if cookie_hdr else headers
        api_resp = await client.get(api_url, headers=req_headers)
        api_resp.raise_for_status()
        api_data = api_resp.json()

    # 4) Kerää episodi-ID:t cardeista
    seen = set()
    result = []
    cards = api_data.get("data", {})
    if isinstance(cards, dict):
        cards = cards.get("cards", [])
    if not isinstance(cards, list):
        cards = []
    for card in cards:
        for label in card.get("labels", []):
            if label.get("type") == "itemId":
                eid = label.get("raw", "")
                if eid and eid != series_id and eid not in seen:
                    seen.add(eid)
                    result.append(eid)
    # Fallback: poimi kaikki 1-XXXXXXX ID:t JSON:sta
    if not result:
        for eid in re.findall(r"1-\d{7,}", json.dumps(api_data)):
            if eid != series_id and eid not in seen:
                seen.add(eid)
                result.append(eid)
    return result[:limit]


@mcp.tool()
async def list_music_shows() -> list[dict]:
    """Listaa tuetut Yle Areena -musiikkiohjelmat ja niiden sarja-ID:t."""
    return [{"id": sid, "name": name} for sid, name in MUSIC_SHOWS.items()]


@mcp.tool()
async def get_show_episodes(series_id: str, limit: int = 5) -> list[dict]:
    """Hae musiikkiohjelman viimeisimmät jaksot.
    series_id: esim. '1-3201240' (Pekka Laineen Ihmemaa)
    """
    episode_ids = await _get_series_episode_ids(series_id, limit=limit)
    episodes = []
    for eid in episode_ids:
        try:
            data = await _areena_get(f"/items/{eid}.json")
            card = (data.get("data", {}).get("cards") or [{}])[0]
            episodes.append({
                "id": eid,
                "title": card.get("title", ""),
                "date": next(
                    (l["formatted"] for l in card.get("labels", []) if l.get("type") == "generic" and "." in l.get("formatted", "")),
                    ""
                ),
                "url": f"https://areena.yle.fi/{eid}",
            })
        except Exception:
            continue
    return episodes


@mcp.tool()
async def get_episode_tracks(episode_id: str) -> dict:
    """Hae jakson soittolista Yle Areenasta kuvauksesta.
    episode_id: esim. '1-77120456'
    Palauttaa ohjelman nimen, jakson otsikon, päivämäärän ja kappalelistan.
    """
    data = await _areena_get(f"/items/{episode_id}.json")
    d = data.get("data", {})
    card = (d.get("cards") or [{}])[0]
    description = card.get("description", "")
    tracks = _parse_tracks(description)
    date = next(
        (l["formatted"] for l in card.get("labels", []) if l.get("type") == "generic" and "." in l.get("formatted", "")),
        ""
    )
    return {
        "show": d.get("title", ""),
        "episode": card.get("title", ""),
        "date": date,
        "tracks": tracks,
        "track_count": len(tracks),
    }


@mcp.tool()
async def get_latest_show_tracks(series_id: str) -> dict:
    """Hae musiikkiohjelman viimeisimmän jakson kappalelista suoraan.
    series_id: esim. '1-3201240'
    """
    ids = await _get_series_episode_ids(series_id, limit=1)
    if not ids:
        return {"error": "Ei jaksoja löydetty"}
    return await get_episode_tracks(ids[0])


@mcp.tool()
async def get_finnish_new_releases(year_month: str = "2026-02") -> list[dict]:
    """Hae uudet suomalaiset albumijulkaisut MusicBrainzista.
    year_month: esim. '2026-02' hakee helmikuun julkaisut
    """
    start = f"{year_month}-01"
    # Kuukauden viimeinen päivä — käytetään 31 jota MusicBrainz osaa käsitellä
    end = f"{year_month}-31"
    query = f"date:[{start} TO {end}] AND country:FI AND status:Official"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://musicbrainz.org/ws/2/release",
            params={"query": query, "fmt": "json", "limit": 25},
            headers={"User-Agent": "soittolista-suosittelija/0.1 (dev)"},
        )
        resp.raise_for_status()
        data = resp.json()
    seen = set()
    results = []
    for r in data.get("releases", []):
        key = (r.get("title", ""), tuple(a["name"] for a in r.get("artist-credit", [])))
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "title": r.get("title"),
            "artists": [a["name"] for a in r.get("artist-credit", [])],
            "date": r.get("date"),
            "type": r.get("release-group", {}).get("primary-type"),
            "label": (r.get("label-info") or [{}])[0].get("label", {}).get("name"),
        })
    return results


@mcp.tool()
async def get_finland_top_songs(limit: int = 25) -> list[dict]:
    """Hae Suomen top-kappaleet Apple Music RSS:stä (sisältää releaseDate-kentän)."""
    url = f"https://rss.applemarketingtools.com/api/v2/fi/music/most-played/{min(limit, 25)}/songs.json"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    results = data.get("feed", {}).get("results", [])
    return [
        {
            "name": r.get("name"),
            "artist": r.get("artistName"),
            "release_date": r.get("releaseDate"),
            "genre": r.get("genres", [{}])[0].get("name"),
            "url": r.get("url"),
        }
        for r in results
    ]


if __name__ == "__main__":
    mcp.run()

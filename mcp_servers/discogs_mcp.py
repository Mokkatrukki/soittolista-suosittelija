"""Discogs MCP-palvelin kehityskäyttöön.

Autentikoi Personal Access Tokenilla (DISCOGS_TOKEN .env-tiedostossa).
Rate limit: 60 req/min autentikoituna.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("discogs")

DISCOGS_BASE = "https://api.discogs.com"
USER_AGENT = "SoittolistaSuosittelija/1.0"


def _headers() -> dict:
    token = os.environ.get("DISCOGS_TOKEN")
    if not token or token == "your_discogs_token_here":
        raise RuntimeError(
            "DISCOGS_TOKEN puuttuu .env-tiedostosta. "
            "Hanki osoitteesta: https://www.discogs.com/settings/developers"
        )
    return {
        "Authorization": f"Discogs token={token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.discogs.v2.plaintext+json",
    }


async def _get(path: str, params: dict = None) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DISCOGS_BASE}{path}",
            headers=_headers(),
            params=params or {},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()


# --- HAKU ---

@mcp.tool()
async def search_releases(
    query: str = "",
    artist: str = "",
    title: str = "",
    genre: str = "",
    style: str = "",
    year: str = "",
    limit: int = 10,
) -> list[dict]:
    """Hae julkaisuja Discogsin tietokannasta.
    Voit hakea vapaalla tekstillä (query) tai tarkentaa artist/title/genre/style/year-kentillä.
    Palauttaa albumit/julkaisut metatietoineen.
    """
    params = {"type": "release", "per_page": min(limit, 50), "page": 1}
    if query:
        params["q"] = query
    if artist:
        params["artist"] = artist
    if title:
        params["title"] = title
    if genre:
        params["genre"] = genre
    if style:
        params["style"] = style
    if year:
        params["year"] = year

    data = await _get("/database/search", params)
    results = []
    for r in data.get("results", []):
        results.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "year": r.get("year"),
            "genre": r.get("genre", []),
            "style": r.get("style", []),
            "format": r.get("format", []),
            "label": r.get("label", []),
            "country": r.get("country", ""),
            "master_id": r.get("master_id"),
            "resource_url": r.get("resource_url"),
        })
    return results


@mcp.tool()
async def search_artists(query: str, limit: int = 10) -> list[dict]:
    """Hae artisteja nimellä Discogsin tietokannasta."""
    data = await _get("/database/search", {"type": "artist", "q": query, "per_page": min(limit, 25)})
    return [
        {
            "id": a.get("id"),
            "name": a.get("title"),
            "resource_url": a.get("resource_url"),
        }
        for a in data.get("results", [])
    ]


@mcp.tool()
async def search_masters(
    artist: str = "",
    title: str = "",
    genre: str = "",
    style: str = "",
    year: str = "",
    limit: int = 10,
) -> list[dict]:
    """Hae master releaseja — kukin master edustaa yhtä albumia kaikissa julkaisuversioissaan.
    Hyvä albumien etsimiseen ilman duplikaatteja (eri painate-/maaversiot).
    """
    params = {"type": "master", "per_page": min(limit, 50), "page": 1}
    if artist:
        params["artist"] = artist
    if title:
        params["title"] = title
    if genre:
        params["genre"] = genre
    if style:
        params["style"] = style
    if year:
        params["year"] = year

    data = await _get("/database/search", params)
    return [
        {
            "id": r.get("id"),
            "title": r.get("title"),
            "year": r.get("year"),
            "genre": r.get("genre", []),
            "style": r.get("style", []),
            "main_release": r.get("main_release"),
        }
        for r in data.get("results", [])
    ]


# --- JULKAISUTIEDOT ---

@mcp.tool()
async def get_release(release_id: int) -> dict:
    """Hae julkaisun tarkemmat tiedot: kappalelista, kreditit, tyylit, formaatti.
    release_id saadaan search_releases-tuloksista.
    """
    data = await _get(f"/releases/{release_id}")
    tracks = [
        {
            "position": t.get("position"),
            "title": t.get("title"),
            "duration": t.get("duration"),
            "artists": [a.get("name") for a in t.get("artists", [])],
        }
        for t in data.get("tracklist", [])
    ]
    artists = [{"id": a.get("id"), "name": a.get("name")} for a in data.get("artists", [])]
    labels = [{"name": l.get("name"), "catno": l.get("catno")} for l in data.get("labels", [])]
    extraartists = [
        {
            "id": ea.get("id"),
            "name": ea.get("name"),
            "role": ea.get("role"),
            "tracks": ea.get("tracks", ""),
        }
        for ea in data.get("extraartists", [])
    ]
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "artists": artists,
        "year": data.get("year"),
        "country": data.get("country"),
        "genres": data.get("genres", []),
        "styles": data.get("styles", []),
        "formats": [f.get("name") for f in data.get("formats", [])],
        "labels": labels,
        "tracklist": tracks,
        "extraartists": extraartists,
        "notes": (data.get("notes") or "")[:300],
        "community": {
            "have": data.get("community", {}).get("have"),
            "want": data.get("community", {}).get("want"),
            "rating_average": data.get("community", {}).get("rating", {}).get("average"),
            "rating_count": data.get("community", {}).get("rating", {}).get("count"),
        },
    }


@mcp.tool()
async def get_master_release(master_id: int) -> dict:
    """Hae master releasen tiedot ja kappalelista.
    Master release edustaa albumin "kanonista" versiota kaikista painoksista.
    """
    data = await _get(f"/masters/{master_id}")
    tracks = [
        {
            "position": t.get("position"),
            "title": t.get("title"),
            "duration": t.get("duration"),
        }
        for t in data.get("tracklist", [])
    ]
    artists = [{"id": a.get("id"), "name": a.get("name")} for a in data.get("artists", [])]
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "artists": artists,
        "year": data.get("year"),
        "genres": data.get("genres", []),
        "styles": data.get("styles", []),
        "tracklist": tracks,
        "versions_count": data.get("num_for_sale", 0),
        "main_release": data.get("main_release"),
    }


# --- ARTISTI ---

@mcp.tool()
async def get_artist(artist_id: int) -> dict:
    """Hae artistin tiedot Discogsin tietokannasta.
    artist_id saadaan search_artists-tuloksista.
    """
    data = await _get(f"/artists/{artist_id}")
    members = [m.get("name") for m in data.get("members", [])]
    aliases = [a.get("name") for a in data.get("aliases", [])]
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "real_name": data.get("realname", ""),
        "profile": (data.get("profile") or "")[:400],
        "members": members,
        "aliases": aliases,
        "urls": data.get("urls", []),
    }


@mcp.tool()
async def get_artist_releases(artist_id: int, sort: str = "year", limit: int = 20) -> list[dict]:
    """Hae artistin julkaisut Discogsin tietokannasta uusimmasta vanhimpaan.
    sort: 'year' | 'title' | 'format'
    Palauttaa albumit, singlet ja EP:t.
    """
    data = await _get(
        f"/artists/{artist_id}/releases",
        {"sort": sort, "sort_order": "desc", "per_page": min(limit, 100), "page": 1},
    )
    return [
        {
            "id": r.get("id"),
            "title": r.get("title"),
            "year": r.get("year"),
            "type": r.get("type"),
            "role": r.get("role"),
            "format": r.get("format", ""),
            "label": r.get("label", ""),
            "main_release": r.get("main_release"),
        }
        for r in data.get("releases", [])
    ]


# --- KÄYTTÄJÄN KOKOELMA ---

@mcp.tool()
async def get_user_collection(username: str, limit: int = 50, sort: str = "added") -> list[dict]:
    """Hae käyttäjän Discogs-kokoelma (kaikki kansiot / folder 0).
    Vaatii että kokoelma on julkinen TAI käytät omaa tokeniaasi.
    sort: 'added' | 'artist' | 'title' | 'year' | 'rating'
    """
    data = await _get(
        f"/users/{username}/collection/folders/0/releases",
        {"sort": sort, "sort_order": "desc", "per_page": min(limit, 100), "page": 1},
    )
    results = []
    for item in data.get("releases", []):
        info = item.get("basic_information", {})
        artists = [a.get("name") for a in info.get("artists", [])]
        results.append({
            "instance_id": item.get("instance_id"),
            "release_id": item.get("id"),
            "title": info.get("title"),
            "artists": artists,
            "year": info.get("year"),
            "genres": info.get("genres", []),
            "styles": info.get("styles", []),
            "formats": [f.get("name") for f in info.get("formats", [])],
            "labels": [l.get("name") for l in info.get("labels", [])],
            "rating": item.get("rating", 0),
            "date_added": item.get("date_added", ""),
        })
    return results


@mcp.tool()
async def get_collection_value(username: str) -> dict:
    """Hae käyttäjän kokoelman markkina-arvo (minimi, mediaani, maksimi).
    Vaatii autentikaation kokoelman omistajana.
    """
    data = await _get(f"/users/{username}/collection/value")
    return {
        "minimum": data.get("minimum"),
        "median": data.get("median"),
        "maximum": data.get("maximum"),
    }


@mcp.tool()
async def get_user_wantlist(username: str, limit: int = 50) -> list[dict]:
    """Hae käyttäjän wantlist — levyt joita käyttäjä haluaa hankkia.
    Hyvä musiikkimaun selvittämiseen: mitä he etsiytyvät mutta eivät vielä omista.
    """
    data = await _get(
        f"/users/{username}/wants",
        {"per_page": min(limit, 100), "page": 1},
    )
    results = []
    for item in data.get("wants", []):
        info = item.get("basic_information", {})
        artists = [a.get("name") for a in info.get("artists", [])]
        results.append({
            "release_id": item.get("id"),
            "title": info.get("title"),
            "artists": artists,
            "year": info.get("year"),
            "genres": info.get("genres", []),
            "styles": info.get("styles", []),
            "formats": [f.get("name") for f in info.get("formats", [])],
        })
    return results


# --- OMA IDENTITEETTI ---

@mcp.tool()
async def get_identity() -> dict:
    """Hae oman tokenin käyttäjätiedot — palauttaa käyttäjänimen jota tarvitaan
    collection/wantlist-kutsuissa. Hyödyllinen kun käyttäjänimi ei ole tiedossa.
    """
    data = await _get("/oauth/identity")
    return {
        "id": data.get("id"),
        "username": data.get("username"),
        "resource_url": data.get("resource_url"),
        "consumer_name": data.get("consumer_name"),
    }


@mcp.tool()
async def get_user_profile(username: str) -> dict:
    """Hae käyttäjän julkinen profiili: sijainti, rekisteröitymispäivä, kokoelman koko.
    Hyvä kun haluat lisätietoa kokoelman omistajasta.
    """
    data = await _get(f"/users/{username}")
    return {
        "username": data.get("username"),
        "name": data.get("name", ""),
        "location": data.get("location", ""),
        "registered": data.get("registered", ""),
        "num_collection": data.get("num_collection", 0),
        "num_wantlist": data.get("num_wantlist", 0),
        "num_lists": data.get("num_lists", 0),
        "releases_contributed": data.get("releases_contributed", 0),
        "profile": (data.get("profile") or "")[:300],
    }


# --- LEVY-YHTIÖT (LABEL) ---

@mcp.tool()
async def search_labels(query: str, limit: int = 10) -> list[dict]:
    """Hae levy-yhtiöitä nimellä. Levy-yhtiöt ovat loistava musiikkimaun proxy —
    esim. ECM, Warp, Sub Pop, Blue Note kertovat paljon soundista.
    """
    data = await _get("/database/search", {"type": "label", "q": query, "per_page": min(limit, 25)})
    return [
        {
            "id": l.get("id"),
            "name": l.get("title"),
            "resource_url": l.get("resource_url"),
        }
        for l in data.get("results", [])
    ]


@mcp.tool()
async def get_label(label_id: int) -> dict:
    """Hae levy-yhtiön tiedot ja kuvaus.
    label_id saadaan search_labels-tuloksista.
    """
    data = await _get(f"/labels/{label_id}")
    sublabels = [{"id": s.get("id"), "name": s.get("name")} for s in data.get("sublabels", [])]
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "profile": (data.get("profile") or "")[:400],
        "parent_label": data.get("parent_label", {}).get("name", ""),
        "sublabels": sublabels,
        "urls": data.get("urls", []),
    }


@mcp.tool()
async def get_label_releases(label_id: int, limit: int = 50, page: int = 1) -> list[dict]:
    """Hae kaikki levy-yhtiön julkaisut. Erinomainen tapa löytää tietyn labelin soundia
    edustavia albumeja — esim. 'Hae kaikki Warp Recordsin julkaisut'.
    """
    data = await _get(
        f"/labels/{label_id}/releases",
        {"per_page": min(limit, 100), "page": page},
    )
    return [
        {
            "id": r.get("id"),
            "title": r.get("title"),
            "artist": r.get("artist", ""),
            "year": r.get("year"),
            "format": r.get("format", ""),
            "catno": r.get("catno", ""),
            "thumb": r.get("thumb", ""),
        }
        for r in data.get("releases", [])
    ]


# --- JULKAISUN LAATU JA MARKKINADATA ---

@mcp.tool()
async def get_community_release_rating(release_id: int) -> dict:
    """Hae julkaisun yhteisöarvio: keskiarvo ja äänten määrä.
    Hyödyllinen laadukkuuden suodatukseen — korkea rating = fanien suosiossa.
    """
    data = await _get(f"/releases/{release_id}/rating")
    rating = data.get("rating", {})
    return {
        "release_id": release_id,
        "average": rating.get("average", 0.0),
        "count": rating.get("count", 0),
    }


@mcp.tool()
async def get_release_stats(release_id: int) -> dict:
    """Hae julkaisun markkinatilastot: kuinka moni omistaa sen ja haluaa sen.
    num_have + num_want = suosion mittari. Hyvä soittolistan relevanssiarviointiin.
    """
    data = await _get(f"/releases/{release_id}/stats")
    community = data.get("community", {})
    return {
        "release_id": release_id,
        "num_have": community.get("have", 0),
        "num_want": community.get("want", 0),
        "want_to_have_ratio": round(
            community.get("want", 0) / max(community.get("have", 1), 1), 2
        ),
    }


# --- MASTER VERSIOT ---

@mcp.tool()
async def get_master_versions(master_id: int, limit: int = 20) -> list[dict]:
    """Hae kaikki painokset/versiot yhdestä master releasesta (eri maat, formaatit, vuodet).
    Hyvä kun haluat löytää tietyn albumin alkuperäisen tai tietyn painoksen.
    """
    data = await _get(
        f"/masters/{master_id}/versions",
        {"per_page": min(limit, 100), "page": 1},
    )
    return [
        {
            "id": v.get("id"),
            "title": v.get("title"),
            "year": v.get("released"),
            "country": v.get("country", ""),
            "format": v.get("format", ""),
            "label": v.get("label", ""),
            "catno": v.get("catno", ""),
            "status": v.get("status", ""),
        }
        for v in data.get("versions", [])
    ]


# --- KÄYTTÄJÄN LISTAT ---

@mcp.tool()
async def get_user_lists(username: str, limit: int = 20) -> list[dict]:
    """Hae käyttäjän Discogs-listat — kuratoituja kokoelmia kuten
    'Essential Detroit Techno' tai 'Best Finnish Jazz 1970s'.
    Erittäin hyödyllinen musiikkilöytöihin.
    """
    data = await _get(f"/users/{username}/lists", {"per_page": min(limit, 100), "page": 1})
    return [
        {
            "id": l.get("id"),
            "name": l.get("name"),
            "description": (l.get("description") or "")[:200],
            "public": l.get("public", True),
            "date_changed": l.get("date_changed", ""),
            "uri": l.get("uri", ""),
        }
        for l in data.get("lists", [])
    ]


@mcp.tool()
async def get_list(list_id: int) -> dict:
    """Hae käyttäjän listan sisältö — julkaisut joita listalle on lisätty.
    list_id saadaan get_user_lists-tuloksista.
    Palauttaa listan nimen, kuvauksen ja kaikki julkaisut.
    """
    data = await _get(f"/lists/{list_id}")
    items = [
        {
            "id": i.get("id"),
            "title": i.get("display_title", i.get("title", "")),
            "comment": (i.get("comment") or "")[:150],
            "type": i.get("type", "release"),
            "year": i.get("year"),
            "artist": i.get("artist", ""),
        }
        for i in data.get("items", [])
    ]
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "description": (data.get("description") or "")[:400],
        "public": data.get("public", True),
        "date_changed": data.get("date_changed", ""),
        "items": items,
        "items_count": len(items),
    }


if __name__ == "__main__":
    mcp.run()

"""artist_tracks_mcp.py — Artistin kappalehaku eri fiiliksin.

Päätyökalu:
  get_artist_tracks(artist, flavor, count)

  flavor="hits"         → Last.fm globaali top, playcountin mukaan (#1-N)
  flavor="deep_cuts"    → Last.fm globaali, ohita top-10 (sijat 11-30+)
  flavor="album_spread" → Jokaisen top-albumin sisältä 2-3 kappaletta,
                          ei globaalia top-listaa — tuottaa aidosti monipuolisen listan
  flavor="hidden_gems"  → ListenBrainz lb-radio, alhainen suosio (pop 0-30%)
  flavor="newest"       → Discogs uusin albumi/EP ja sen kapparelista
  flavor="mixed"        → Automaattinen yhdistelmä: muutama hitti + album_spread

Kaikki flavorit ottavat artist-nimen suoraan. MBID-haku automaattinen tarvittaessa.

Apuvälineet:
  discogs_artist_releases(artist_name)  → diskografia id:ineen
  discogs_release_tracks(release_id)    → tietyn julkaisun kapparelista
"""
import asyncio
import os
import re
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("artist_tracks")

LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
MB_BASE = "https://musicbrainz.org/ws/2"
LB_BASE = "https://api.listenbrainz.org"
DISCOGS_BASE = "https://api.discogs.com"
USER_AGENT = "SoittolistaSuosittelija/1.0"

_mb_sem = asyncio.Semaphore(1)
_mb_last: float = 0.0


# ---------------------------------------------------------------------------
# Apufunktiot
# ---------------------------------------------------------------------------

async def _lastfm(method: str, params: dict = None) -> dict:
    api_key = os.environ.get("LASTFM_API_KEY")
    if not api_key or api_key == "your_lastfm_api_key_here":
        raise RuntimeError("LASTFM_API_KEY puuttuu .env-tiedostosta.")
    p = {"method": method, "api_key": api_key, "format": "json", "autocorrect": 1, **(params or {})}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(LASTFM_BASE, params=p)
        resp.raise_for_status()
        data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Last.fm virhe {data['error']}: {data.get('message')}")
    return data


async def _mb_get(path: str, params: dict = None) -> dict:
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


async def _lb_get(path: str, params: dict = None) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{LB_BASE}{path}",
            params=params or {},
            headers=_lb_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _discogs_headers() -> dict:
    token = os.environ.get("DISCOGS_TOKEN")
    if not token or token == "your_discogs_token_here":
        raise RuntimeError("DISCOGS_TOKEN puuttuu .env-tiedostosta.")
    return {
        "Authorization": f"Discogs token={token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.discogs.v2.plaintext+json",
    }


async def _discogs_get(path: str, params: dict = None) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{DISCOGS_BASE}{path}",
            headers=_discogs_headers(),
            params=params or {},
        )
        resp.raise_for_status()
        return resp.json()


def _normalize(s: str) -> str:
    """Normalisoi kappaleen nimi vertailua varten (lowercase, ei erikoismerkkejä)."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


async def _resolve_mbid(artist_name: str) -> str | None:
    try:
        data = await _mb_get("/artist", {"query": f'artist:"{artist_name}"', "limit": 1})
        artists = data.get("artists", [])
        if artists:
            return artists[0].get("id")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Haku-strategiat
# ---------------------------------------------------------------------------

async def _fetch_hits(artist: str, count: int) -> list[dict]:
    """Last.fm: globaali playcountin top (sivu 1)."""
    data = await _lastfm("artist.getTopTracks", {"artist": artist, "limit": min(count, 50), "page": 1})
    return [
        {
            "artist": artist,
            "title": t["name"],
            "playcount": int(t.get("playcount", 0)),
            "listeners": int(t.get("listeners", 0)),
            "global_rank": i + 1,
            "source": "lastfm_hits",
        }
        for i, t in enumerate(data.get("toptracks", {}).get("track", [])[:count])
    ]


async def _fetch_deep_cuts(artist: str, count: int, skip: int = 10) -> list[dict]:
    """Last.fm: ohita top-N globaalia, palauta seuraavat."""
    page_size = 50
    start_page = skip // page_size + 1
    end_page = (skip + count - 1) // page_size + 1

    tasks = [
        _lastfm("artist.getTopTracks", {"artist": artist, "limit": page_size, "page": p})
        for p in range(start_page, end_page + 1)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    tracks = []
    for page_idx, res in enumerate(results):
        if isinstance(res, Exception):
            continue
        page_num = start_page + page_idx
        page_tracks = res.get("toptracks", {}).get("track", [])
        global_offset = (page_num - 1) * page_size
        for i, t in enumerate(page_tracks):
            global_rank = global_offset + i + 1
            if skip < global_rank <= skip + count:
                tracks.append({
                    "artist": artist,
                    "title": t["name"],
                    "playcount": int(t.get("playcount", 0)),
                    "listeners": int(t.get("listeners", 0)),
                    "global_rank": global_rank,
                    "source": "lastfm_deep_cuts",
                })

    return sorted(tracks, key=lambda x: x["global_rank"])


async def _fetch_album_spread(artist: str, count: int) -> list[dict]:
    """Album-aware valinta: jokaisen top-albumin sisältä ei-ilmeiset kappaleet.

    Prosessi:
    1. Hae artistin top-albumit (Last.fm)
    2. Hae jokaisen albumin kapparelista rinnakkain (Last.fm album.getInfo)
    3. Ristiviittaa globaali playcount (artist.getTopTracks) → tiedetään
       mikä on jokaisen albumin "ilmeisin" kappale
    4. Jokaisesta albumista: ota kappaleet mutta hyppää globaalin top-3:n yli
       (tai album-position #1 jos ei löydy top-listalta)
    5. Palauta tasaisesti eri albumeista — eri aikakausia, eri tyylejä
    """
    # 1. Top-albumit + top-100 kappaleet (playcount-data) rinnakkain
    albums_task = _lastfm("artist.getTopAlbums", {"artist": artist, "limit": 7})
    tracks_p1_task = _lastfm("artist.getTopTracks", {"artist": artist, "limit": 50, "page": 1})
    tracks_p2_task = _lastfm("artist.getTopTracks", {"artist": artist, "limit": 50, "page": 2})

    albums_data, tracks_p1, tracks_p2 = await asyncio.gather(
        albums_task, tracks_p1_task, tracks_p2_task, return_exceptions=True
    )

    # Rakenna playcount-hakemisto: normaalistettu nimi → {playcount, global_rank}
    playcount_index: dict[str, dict] = {}
    for page_result, offset in [(tracks_p1, 0), (tracks_p2, 50)]:
        if isinstance(page_result, Exception):
            continue
        for i, t in enumerate(page_result.get("toptracks", {}).get("track", [])):
            key = _normalize(t["name"])
            playcount_index[key] = {
                "playcount": int(t.get("playcount", 0)),
                "global_rank": offset + i + 1,
            }

    # Top-3 globaalisti — näitä vältetään album_spread:issa
    top3_global = {k for k, v in playcount_index.items() if v["global_rank"] <= 3}

    if isinstance(albums_data, Exception) or not albums_data:
        # Fallback: deep cuts jos albumit ei löydy
        return await _fetch_deep_cuts(artist, count, skip=5)

    albums = albums_data.get("topalbums", {}).get("album", [])
    if not albums:
        return await _fetch_deep_cuts(artist, count, skip=5)

    # 2. Hae jokaisen albumin kapparelista rinnakkain
    album_info_tasks = [
        _lastfm("album.getInfo", {"artist": artist, "album": alb["name"]})
        for alb in albums[:6]
    ]
    album_results = await asyncio.gather(*album_info_tasks, return_exceptions=True)

    tracks_per_album: int = max(2, count // len(albums[:6]) + 1)
    selected: list[dict] = []
    seen_normalized: set[str] = set()

    for alb, result in zip(albums[:6], album_results):
        if isinstance(result, Exception):
            continue

        album_tracks = result.get("album", {}).get("tracks", {}).get("track", [])
        if not album_tracks:
            continue

        # Laske jokaiselle albumin kappaleelle global_rank playcount-indeksistä
        ranked = []
        for t in album_tracks:
            name = t.get("name", "")
            norm = _normalize(name)
            info = playcount_index.get(norm, {})
            ranked.append({
                "name": name,
                "norm": norm,
                "album_position": int(t.get("@attr", {}).get("rank", 99)),
                "global_rank": info.get("global_rank", 999),
                "playcount": info.get("playcount", 0),
            })

        # Järjestä: suosituin ensin (global_rank), mutta hyppää top3-globaalit yli
        # Ota enintään tracks_per_album kappaletta per albumi
        taken = 0
        for t in sorted(ranked, key=lambda x: x["global_rank"]):
            if taken >= tracks_per_album:
                break
            norm = t["norm"]
            if not norm or norm in seen_normalized:
                continue
            # Hyppää ilmeisin single yli — jos kappale on top-3 globaalisti
            if norm in top3_global:
                continue
            seen_normalized.add(norm)
            selected.append({
                "artist": artist,
                "title": t["name"],
                "album": alb["name"],
                "playcount": t["playcount"],
                "global_rank": t["global_rank"],
                "album_position": t["album_position"],
                "source": "lastfm_album_spread",
            })
            taken += 1

    # Jos ei saatu tarpeeksi, täydennä deep_cuts:illa
    if len(selected) < count // 2:
        fallback = await _fetch_deep_cuts(artist, count - len(selected), skip=10)
        for t in fallback:
            if _normalize(t["title"]) not in seen_normalized:
                selected.append(t)

    return selected[:count]


async def _fetch_hidden_gems(artist: str, count: int) -> list[dict]:
    """ListenBrainz lb-radio: alhainen suosio (pop 0-30%), MBID haetaan automaattisesti."""
    mbid = await _resolve_mbid(artist)
    if not mbid:
        return await _fetch_album_spread(artist, count)

    try:
        data = await _lb_get(f"/1/lb-radio/artist/{mbid}", {
            "mode": "easy",
            "max_similar_artists": 0,
            "max_recordings_per_artist": min(count * 2, 50),
            "pop_begin": 0,
            "pop_end": 30,
        })
    except Exception:
        return await _fetch_album_spread(artist, count)

    results = []
    for _key, recordings in data.items():
        for rec in recordings or []:
            title = rec.get("recording_name", "")
            artist_name = rec.get("artist_name", "")
            if not title or not artist_name:
                continue
            results.append({
                "artist": artist_name,
                "title": title,
                "recording_mbid": rec.get("recording_mbid", ""),
                "listen_count": rec.get("total_listen_count", 0),
                "source": "listenbrainz_hidden_gems",
            })

    results = sorted(results, key=lambda x: x["listen_count"], reverse=True)[:count]

    # Täydennä album_spread:illa jos LB antoi liian vähän
    if len(results) < count // 2:
        seen = {_normalize(r["title"]) for r in results}
        fallback = await _fetch_album_spread(artist, count - len(results))
        for t in fallback:
            if _normalize(t["title"]) not in seen:
                results.append(t)

    return results


async def _fetch_newest(artist: str) -> dict:
    """Discogs: uusimman albumin kapparelista."""
    search_data = await _discogs_get("/database/search", {"q": artist, "type": "artist", "per_page": 5})
    results = search_data.get("results", [])
    if not results:
        raise RuntimeError(f"Artistia '{artist}' ei löydy Discogsin tietokannasta.")

    artist_id = results[0]["id"]
    releases_data = await _discogs_get(
        f"/artists/{artist_id}/releases",
        {"sort": "year", "sort_order": "desc", "per_page": 25},
    )
    releases = releases_data.get("releases", [])
    if not releases:
        raise RuntimeError(f"Artistille '{artist}' ei löydy julkaisuja Discogsin tietokannasta.")

    def _is_album(r: dict) -> bool:
        fmt = (r.get("format") or "").lower()
        rtype = (r.get("type") or "").lower()
        role = (r.get("role") or "").lower()
        return "album" in rtype or ("main" in role and "single" not in fmt and "ep" not in fmt)

    newest = ([r for r in releases if _is_album(r)] or releases)[0]
    release_data = await _discogs_get(f"/releases/{newest['id']}")

    tracklist = [
        {
            "artist": artist,
            "title": t.get("title", ""),
            "position": t.get("position", ""),
            "duration": t.get("duration", ""),
            "source": "discogs_newest",
        }
        for t in release_data.get("tracklist", [])
        if t.get("title") and t.get("type_", "track") != "heading"
    ]

    return {
        "release_title": release_data.get("title", newest.get("title", "")),
        "year": release_data.get("year", newest.get("year", "")),
        "tracks": tracklist,
    }


async def _fetch_mixed(artist: str, count: int) -> list[dict]:
    """Yhdistelmä: ~25% hittejä + ~75% album_spread.

    Tuottaa soittolistan jossa on muutama tuttu ankkuri + paljon muuta.
    Hyvä oletusvaihtoehto kun ei tiedetä mitä käyttäjä haluaa.
    """
    hits_count = max(2, count // 4)
    spread_count = count - hits_count

    hits_task = _fetch_hits(artist, hits_count)
    spread_task = _fetch_album_spread(artist, spread_count + 5)  # +5 deduplikoinnin varalle

    hits, spread = await asyncio.gather(hits_task, spread_task)

    hits_normalized = {_normalize(t["title"]) for t in hits}
    spread_deduped = [t for t in spread if _normalize(t["title"]) not in hits_normalized]

    return hits + spread_deduped[:spread_count]


# ---------------------------------------------------------------------------
# Päätyökalu
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_artist_tracks(
    artist: str,
    flavor: str = "album_spread",
    count: int = 20,
) -> dict:
    """Hae artistin kappaleet valitulla fiiliksellä (flavor).

    flavor-vaihtoehdot:

      "hits"         — Last.fm globaali top playcountin mukaan. Tutut hitet ja klasikot.
                       Käytä kun käyttäjä tutustuu artistiin tai haluaa vain tunnettuja biisejä.

      "deep_cuts"    — Ohita top-10 globaalisti, palauta sijat 11-30. Fanien
                       suosikit, albumikappaleet joita ei soiteta radiossa.

      "album_spread" — (OLETUS) Jokaisen top-albumin sisältä 2-3 kappaletta,
                       välttää globaalin top-3:n. Tuottaa monipuolisen listan
                       eri aikakausia ja tyylejä — ei vain hittejä, ei vain
                       obscure-kamaa. Paras yleisvalinta.

      "hidden_gems"  — ListenBrainz-kuunteludata, alhainen suosio (pop 0-30%).
                       Piilohelmia joita Last.fm ei nosta esiin. Fallback
                       album_spread:iin jos LB ei löydä tarpeeksi.

      "newest"       — Discogs: uusin albumi/EP ja sen koko kapparelista.
                       Käytä kun käyttäjä haluaa "mitä X on viimeksi julkaissut".

      "mixed"        — ~25% tunnettuja hittejä + ~75% album_spread. Hyvä kun
                       halutaan sekä tuttu ankkuri että monipuolisuus.

    Palauttaa:
      {flavor, artist, tracks: [{artist, title, album?, playcount?, global_rank?, source}]}
    flavor="newest" palauttaa myös {release_title, year}.
    """
    flavor = flavor.lower().strip()

    if flavor == "hits":
        tracks = await _fetch_hits(artist, count)
        return {"flavor": "hits", "artist": artist, "tracks": tracks}

    elif flavor == "deep_cuts":
        tracks = await _fetch_deep_cuts(artist, count)
        return {"flavor": "deep_cuts", "artist": artist, "tracks": tracks}

    elif flavor == "album_spread":
        tracks = await _fetch_album_spread(artist, count)
        return {"flavor": "album_spread", "artist": artist, "tracks": tracks}

    elif flavor == "hidden_gems":
        tracks = await _fetch_hidden_gems(artist, count)
        return {"flavor": "hidden_gems", "artist": artist, "tracks": tracks}

    elif flavor == "newest":
        result = await _fetch_newest(artist)
        return {"flavor": "newest", "artist": artist, **result}

    elif flavor == "mixed":
        tracks = await _fetch_mixed(artist, count)
        return {"flavor": "mixed", "artist": artist, "tracks": tracks}

    else:
        raise ValueError(
            f"Tuntematon flavor '{flavor}'. Käytä: hits, deep_cuts, album_spread, hidden_gems, newest, mixed"
        )


# ---------------------------------------------------------------------------
# Apuvälineet Discogs-diskografian selaukseen
# ---------------------------------------------------------------------------

@mcp.tool()
async def discogs_artist_releases(artist_name: str, limit: int = 15) -> list[dict]:
    """Hae artistin viimeisimmät julkaisut Discogsin kautta (uusimmat ensin).

    Käyttö: selaa diskografiaa tai löydä tietty albumi id:ineen.
    Jatka discogs_release_tracks(id)-työkalulla saadaksesi kapparelistan.

    Palauttaa: [{id, title, year, type, format, role}]
    """
    search_data = await _discogs_get("/database/search", {"q": artist_name, "type": "artist", "per_page": 5})
    results = search_data.get("results", [])
    if not results:
        raise RuntimeError(f"Artistia '{artist_name}' ei löydy Discogsin tietokannasta.")

    artist_id = results[0]["id"]
    releases_data = await _discogs_get(
        f"/artists/{artist_id}/releases",
        {"sort": "year", "sort_order": "desc", "per_page": min(limit, 50)},
    )
    return [
        {
            "id": r.get("id"),
            "title": r.get("title", ""),
            "year": r.get("year", ""),
            "type": r.get("type", ""),
            "format": r.get("format", ""),
            "role": r.get("role", ""),
        }
        for r in releases_data.get("releases", [])[:limit]
    ]


@mcp.tool()
async def discogs_release_tracks(release_id: int) -> dict:
    """Hae tietyn Discogs-julkaisun kapparelista release-ID:llä.

    Hae ID ensin discogs_artist_releases-työkalulla.

    Palauttaa: {title, year, artists, label, tracks: [{position, title, duration, artists}]}
    """
    data = await _discogs_get(f"/releases/{release_id}")

    tracklist = [
        {
            "position": t.get("position", ""),
            "title": t.get("title", ""),
            "duration": t.get("duration", ""),
            "artists": [a["name"] for a in t.get("artists", [])],
        }
        for t in data.get("tracklist", [])
        if t.get("title") and t.get("type_", "track") != "heading"
    ]

    return {
        "title": data.get("title", ""),
        "year": data.get("year", ""),
        "artists": [a["name"] for a in data.get("artists", [])],
        "label": [l["name"] for l in data.get("labels", [])],
        "tracks": tracklist,
    }


if __name__ == "__main__":
    mcp.run()

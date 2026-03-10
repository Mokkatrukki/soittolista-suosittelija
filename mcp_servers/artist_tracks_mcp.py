"""artist_tracks_mcp.py — Artistin kappalehaku eri fiiliksin.

═══════════════════════════════════════════════════════════
TYYPILLISET KÄYTTÖTAPAUKSET JA SUOSITELTU TYÖKALUKETJU
═══════════════════════════════════════════════════════════

1. "Tee soittolista artistin tunnetuimmista kappaleista"
   → get_artist_tracks(flavor="hits")

2. "Tee soittolista jossa ei ole ilmeisimpiä hittejä"
   → get_artist_tracks(flavor="album_spread") tai flavor="deep_cuts"

3. "Tee deep dive -soittolista — koko ura, kaikki bändit"
   → get_artist_universe(resolve_spotify=True)
   Löytää automaattisesti: bändit (MusicBrainz member_of),
   jäsenten sivuprojektit (include_member_projects=True),
   aliakset/hahmot (Discogs aliases).

4. "Löydä kenen kanssa artisti on tehnyt yhteistyötä"
   → get_artist_collaborators()
   Kaivaa Discogs-julkaisujen extraartists: vierailijat, live-bändi,
   tuottajat. Hyvä täydennys get_artist_universe:lle.

   Tyypillinen yhdistelmä deep diveen:
     a) get_artist_universe(resolve_spotify=True)   ← bändit + sivuprojektit
     b) get_artist_collaborators()                  ← yhteistyöartistit
     c) Yhdistä: universe candidates + collaborator-artistien
        get_artist_tracks(flavor="hits", count=3) per merkittävä collaborator

5. "Mitä uutta artistilta on tullut?"
   → get_artist_tracks(flavor="newest")

6. "Etsi samankaltaisia artisteja"
   → Käytä lastfm MCP: artist_similar
   → Käytä musicbrainz MCP: lb_similar_artists (MBID tarvitaan)

═══════════════════════════════════════════════════════════
TYÖKALUT
═══════════════════════════════════════════════════════════

Päätyökalu:
  get_artist_tracks(artist, flavor, count)

  flavor="hits"         → Last.fm globaali top, playcountin mukaan (#1-N)
  flavor="deep_cuts"    → Last.fm globaali, ohita top-10 (sijat 11-30+)
  flavor="album_spread" → Jokaisen top-albumin sisältä 2-3 kappaletta,
                          ei globaalia top-listaa — tuottaa aidosti monipuolisen listan
  flavor="hidden_gems"  → ListenBrainz lb-radio, alhainen suosio (pop 0-30%)
  flavor="newest"       → Discogs uusin albumi/EP ja sen kapparelista
  flavor="mixed"        → Automaattinen yhdistelmä: muutama hitti + album_spread

Deep-dive:
  get_artist_universe(artist, target_count, resolve_spotify, include_member_projects)
    → Koko universum: soolot + bändit + jäsenten sivuprojektit + aliakset
    → Raskas kutsu (~30-50 API-pyyntöä) — tarkoitettu yhdelle artistille kerrallaan

Yhteistyöartistit:
  get_artist_collaborators(artist, max_releases)
    → Discogs extraartists: live-muusikot, vierailijat, tuottajat
    → Kevyt täydennys universe-hakuun — suorita erikseen tarvittaessa

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


SPOTIFY_BASE = "https://api.spotify.com/v1"
_spotify_token: str = ""
_spotify_token_expires: float = 0.0


async def _spotify_client_token() -> str:
    """Hae Spotify Client Credentials -token (app-only, ei user auth)."""
    global _spotify_token, _spotify_token_expires
    if _spotify_token and time.monotonic() < _spotify_token_expires - 30:
        return _spotify_token
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET puuttuu .env:stä")
    import base64
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {creds}"},
            data={"grant_type": "client_credentials"},
        )
        resp.raise_for_status()
        data = resp.json()
    _spotify_token = data["access_token"]
    _spotify_token_expires = time.monotonic() + data.get("expires_in", 3600)
    return _spotify_token


def _artist_name_matches(expected: str, found: str) -> bool:
    """Tarkista vastaako Spotifyn palauttama artistinimi haettua.

    Sallii pienet erot (ä→a, erikoismerkit) mutta hylkää selvästi eri artistit.
    Esim. "Euthanásia" ~ "Euthanasia" → True
          "Martti" vs "Martti Innanen" → False (6/13 = 0.46 < 0.7)
    """
    exp = _normalize(expected)
    fnd = _normalize(found)
    if exp == fnd:
        return True
    # Toinen sisältää toisen — tarkista pituussuhde
    shorter, longer = (exp, fnd) if len(exp) <= len(fnd) else (fnd, exp)
    if shorter and shorter in longer:
        return len(shorter) / len(longer) >= 0.7
    return False


async def _spotify_search_track(artist: str, title: str) -> dict | None:
    """Hae yksittäinen kappale Spotifysta. Palauttaa {uri, id, name, artist} tai None.

    Validoi artistinimen — hylkää tulokset joissa Spotifyn artisti ei vastaa haettua.
    Esim. haku artist=Martti ei hyväksy "Martti Innanen" -artistia.
    """
    try:
        token = await _spotify_client_token()
        q = f"track:{title} artist:{artist}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{SPOTIFY_BASE}/search",
                params={"q": q, "type": "track", "limit": 3},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()
        items = data.get("tracks", {}).get("items", [])
        # Käy läpi tulokset, palauta ensimmäinen jossa artistinimi täsmää
        for t in items:
            track_artists = [a["name"] for a in t.get("artists", [])]
            if any(_artist_name_matches(artist, a) for a in track_artists):
                return {
                    "spotify_uri": t["uri"],
                    "spotify_id": t["id"],
                    "spotify_name": t["name"],
                    "spotify_artist": track_artists[0] if track_artists else "",
                }
        return None
    except Exception:
        return None


async def _spotify_resolve_candidates(candidates: list[dict]) -> list[dict]:
    """Resolvo Spotify URI kaikille kandidaateille rinnakkain (20 kerrallaan)."""
    BATCH = 20
    resolved = []
    for i in range(0, len(candidates), BATCH):
        batch = candidates[i : i + BATCH]
        tasks = [_spotify_search_track(t.get("artist", t.get("source_name", "")), t["title"]) for t in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for track, result in zip(batch, results):
            if isinstance(result, Exception) or result is None:
                resolved.append({**track, "spotify_uri": None})
            else:
                resolved.append({**track, **result})
    return resolved


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
        return None
    except Exception:
        return None


async def _mb_artist_relations(mbid: str) -> dict:
    """MusicBrainz: hae artistin bändisuhteet (member_of, members) ja muut relaatiot."""
    try:
        data = await _mb_get(f"/artist/{mbid}", {"inc": "artist-rels"})
        relations = data.get("relations", [])

        member_of = []
        members = []
        other = []
        seen_mbids = set()

        for rel in relations:
            rtype = rel.get("type", "")
            direction = rel.get("direction", "")
            target = rel.get("artist", {})
            target_mbid = target.get("id", "")

            if not target_mbid or target_mbid in seen_mbids:
                continue
            seen_mbids.add(target_mbid)

            entry = {
                "name": target.get("name", ""),
                "mbid": target_mbid,
                "disambiguation": target.get("disambiguation", ""),
            }

            if rtype == "member of band" and direction == "forward":
                member_of.append(entry)
            elif rtype == "member of band" and direction == "backward":
                # Bändin jäsen (henkilö) — backward = "tämä henkilö on bändimme jäsen"
                members.append(entry)
            elif rtype == "is person":
                other.append({**entry, "type": "is_person"})
            elif rtype in ("collaboration", "supporting musician", "vocal support",
                           "tribute artist", "guest", "performing orchestra"):
                other.append({**entry, "type": rtype.replace(" ", "_")})

        return {"member_of": member_of, "members": members, "other": other}
    except Exception:
        return {"member_of": [], "members": [], "other": []}


async def _discogs_artist_aliases(artist_name: str) -> list[dict]:
    """Discogs: hae artistin aliakset (Kummeli-hahmot ym.) ja namevariations."""
    try:
        search_data = await _discogs_get(
            "/database/search", {"q": artist_name, "type": "artist", "per_page": 3}
        )
        results = search_data.get("results", [])
        if not results:
            return []
        artist_id = results[0]["id"]
        artist_data = await _discogs_get(f"/artists/{artist_id}")
        aliases_raw = artist_data.get("aliases", [])
        return [
            {"name": a.get("name", ""), "id": a.get("id")}
            for a in aliases_raw
            if a.get("name")
        ]
    except Exception:
        return []


async def _lastfm_by_mbid(mbid: str, method: str = "artist.getTopTracks", extra: dict = None) -> dict:
    """Last.fm -kutsu MBID:llä artist-nimen sijaan — välttää nimikollisiot."""
    params = {"mbid": mbid, **(extra or {})}
    # Poistetaan autocorrect koska käytämme MBID:tä
    api_key = os.environ.get("LASTFM_API_KEY")
    if not api_key or api_key == "your_lastfm_api_key_here":
        raise RuntimeError("LASTFM_API_KEY puuttuu .env-tiedostosta.")
    p = {"method": method, "api_key": api_key, "format": "json", **params}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(LASTFM_BASE, params=p)
        resp.raise_for_status()
        data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Last.fm virhe {data['error']}: {data.get('message')}")
    return data


def _tags_overlap(tags_a: list[str], tags_b: list[str], threshold: int = 1) -> bool:
    """Tarkista onko kahdella artistilla tarpeeksi yhteisiä tageja."""
    a = {t.lower() for t in tags_a}
    b = {t.lower() for t in tags_b}
    return len(a & b) >= threshold


async def _fetch_tracks_by_mbid(
    band_name: str,
    mbid: str,
    count: int,
    reference_tags: list[str],
) -> list[dict]:
    """Hae bändin top-kappaleet Last.fm:stä MBID:llä, validoi tagit.

    reference_tags: pääartistin tagit — jos bändin tagit eivät täsmää lainkaan,
    hylkää (väärä artisti samalla nimellä).
    """
    try:
        # Hae artist info + top tracks rinnakkain
        info_task = _lastfm_by_mbid(mbid, "artist.getInfo")
        tracks_task = _lastfm_by_mbid(
            mbid, "artist.getTopTracks", {"limit": min(count * 2, 30)}
        )
        info, tracks_data = await asyncio.gather(info_task, tracks_task, return_exceptions=True)

        # Tagi-validointi
        if not isinstance(info, Exception):
            band_tags = [
                t["name"]
                for t in info.get("artist", {}).get("tags", {}).get("tag", [])
            ]
            if reference_tags and band_tags:
                if not _tags_overlap(reference_tags, band_tags):
                    return []  # Väärä artisti

        if isinstance(tracks_data, Exception):
            return []

        raw = tracks_data.get("toptracks", {}).get("track", [])[:count]
        return [
            {
                "artist": band_name,
                "title": t["name"],
                "playcount": int(t.get("playcount", 0)),
                "source": "lastfm_band_mbid",
                "band": band_name,
            }
            for t in raw
            if t.get("name")
        ]
    except Exception:
        return []
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


# ---------------------------------------------------------------------------
# Yhteistyöartistit
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_artist_collaborators(
    artist: str,
    max_releases: int = 5,
) -> dict:
    """Hae artistin yhteistyöartistit Discogs-julkaisujen kautta.

    Käy läpi artistin uusimmat julkaisut ja poimii extraartists-kentästä
    kaikki mainitut artistit (vierailijat, muusikot, tuottajat).

    Hyödyllinen kun halutaan löytää:
    - Live-yhteistyöt (esim. Erja Lyytinen × Heikki Silvennoinen)
    - Vakiomuusikot (basisti joka soittaa kaikilla levyillä)
    - Tuottajat/miksaajat

    Palauttaa:
      {
        artist,
        releases_checked: int,
        collaborators: [{name, roles: [str], appearances: int, release_ids: [int]}]
      }
    """
    search_data = await _discogs_get("/database/search", {"q": artist, "type": "artist", "per_page": 5})
    results = search_data.get("results", [])
    if not results:
        return {"artist": artist, "releases_checked": 0, "collaborators": []}

    artist_id = results[0]["id"]
    releases_data = await _discogs_get(
        f"/artists/{artist_id}/releases",
        {"sort": "year", "sort_order": "desc", "per_page": 25},
    )
    releases = releases_data.get("releases", [])

    def _is_album(r: dict) -> bool:
        fmt = (r.get("format") or "").lower()
        rtype = (r.get("type") or "").lower()
        role = (r.get("role") or "").lower()
        return "album" in rtype or ("main" in role and "single" not in fmt and "ep" not in fmt)

    albums = [r for r in releases if _is_album(r)][:max_releases]
    if not albums:
        albums = releases[:max_releases]

    # Hae jokaisen albumin data rinnakkain
    release_details = await asyncio.gather(
        *[_discogs_get(f"/releases/{r['id']}") for r in albums],
        return_exceptions=True,
    )

    # Kerää kaikki extraartists: release-taso + per-kappale
    from collections import defaultdict
    collabs: dict[str, dict] = defaultdict(lambda: {"roles": set(), "appearances": 0, "release_ids": []})

    skip_roles = {"mastered by", "lacquer cut by", "design", "photography", "artwork",
                  "layout", "liner notes", "written-by", "words by", "music by",
                  "published by", "licensed from", "distributed by", "manufactured by"}

    for album, detail in zip(albums, release_details):
        if isinstance(detail, Exception):
            continue
        release_id = album["id"]

        def _add(name: str, role: str, rid: int) -> None:
            if not name or name.lower() == artist.lower():
                return
            role_clean = role.strip().lower()
            if any(skip in role_clean for skip in skip_roles):
                return
            collabs[name]["roles"].add(role.strip())
            collabs[name]["appearances"] += 1
            if rid not in collabs[name]["release_ids"]:
                collabs[name]["release_ids"].append(rid)

        for ea in detail.get("extraartists", []):
            _add(ea.get("name", ""), ea.get("role", ""), release_id)

        for track in detail.get("tracklist", []):
            for ea in track.get("extraartists", []):
                _add(ea.get("name", ""), ea.get("role", ""), release_id)

    sorted_collabs = sorted(
        [{"name": n, "roles": sorted(d["roles"]), "appearances": d["appearances"], "release_ids": d["release_ids"]}
         for n, d in collabs.items()],
        key=lambda x: (-x["appearances"], x["name"]),
    )

    return {
        "artist": artist,
        "releases_checked": len([d for d in release_details if not isinstance(d, Exception)]),
        "collaborators": sorted_collabs,
    }


# ---------------------------------------------------------------------------
# Deep-dive työkalu
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_artist_universe(
    artist: str,
    target_count: int = 20,
    resolve_spotify: bool = False,
    include_member_projects: bool = True,
) -> dict:
    """Syväluotaava haku: artistin koko diskografinen universumi.

    Tavoite: ~target_count kappaletta jotka leikkaavat koko uran —
    alkupää, keskivaihe, loppupää, eri bändit/projektit. Ei vain
    Spotifyn top-5 hittejä vaan aito discography tour.

    Per lähde käytetään album_spread:ia (ei hits-listaa):
      → Jokaisen albumin sisältä 2-3 kappaletta, vältetään globaali top-3
      → Tuottaa automaattisesti aikakausihajonnan ilman erillistä logiikkaa

    resolve_spotify=True:
      → Resolvo spotify_uri kaikille, karsii ei-streamattavat
      → Vaatii SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET .env:ssä
      → Suositellaan aina kun rakennetaan soittolistaa

    include_member_projects=True (oletus):
      → Jos artisti on bändi, hakee jokaisen jäsenen sivuprojektit (2-taso)
      → Esim. Stam1na → Antti Hyyrynen → Wöyh!

    Huom: get_artist_universe EI hae yhteistyöartisteja (live-vierailijat, feat.).
    Käytä get_artist_collaborators() erikseen jos haluat myös ne.

    Prosessi:
      1. MBID MusicBrainzista
      2. mb_artist_relations → member_of[] bändit tai members[] jäsenet
      3. [include_member_projects] jäsenten omat member_of-projektit
      4. Discogs aliases → aliakset/hahmot (deduplikoitu)
      5. Laske per_source = target_count / lähteiden_määrä (kerroin jos resolve_spotify)
      6. Soolot + bändit + aliakset album_spread:illa rinnakkain
      7. [resolve_spotify] Spotify batch → spotify_uri, karsii ei-löytyvät

    Palauttaa:
      {
        artist, target_count,
        universe_map: {bands: [{name, disambiguation, mbid, source_type}], aliases: [{name}]},
        candidates: [{artist, title, album?, source_type, source_name,
                      playcount?, global_rank?, spotify_uri?}]
      }
    """
    # 1. MBID + Discogs aliakset rinnakkain
    mbid = await _resolve_mbid(artist)

    async def _empty_relations() -> dict:
        return {"member_of": [], "members": [], "other": []}

    mb_task = _mb_artist_relations(mbid) if mbid else _empty_relations()
    discogs_task = _discogs_artist_aliases(artist)

    mb_relations, discogs_aliases = await asyncio.gather(mb_task, discogs_task, return_exceptions=True)
    if isinstance(mb_relations, Exception):
        mb_relations = {"member_of": [], "members": [], "other": []}
    if isinstance(discogs_aliases, Exception):
        discogs_aliases = []

    bands = mb_relations.get("member_of", [])

    # 1b. Jos artisti on bändi (members löytyy), hae jäsenten omat sivuprojektit
    members = mb_relations.get("members", [])
    if include_member_projects and members and not bands:
        # Hae jokaisen jäsenen projektit rinnakkain
        member_relations = await asyncio.gather(
            *[_mb_artist_relations(m["mbid"]) for m in members if m.get("mbid")],
            return_exceptions=True,
        )
        seen_project_mbids = {mbid} if mbid else set()
        member_projects: list[dict] = []
        for rel in member_relations:
            if isinstance(rel, Exception):
                continue
            for proj in rel.get("member_of", []):
                pmid = proj.get("mbid", "")
                if pmid and pmid not in seen_project_mbids:
                    seen_project_mbids.add(pmid)
                    member_projects.append({**proj, "source_type": "member_project"})
        bands = member_projects

    # Lisää "is_person" -relaatiot aliaksiin, deduplikoi
    existing_alias_names = {_normalize(a["name"]) for a in discogs_aliases}
    for rel in mb_relations.get("other", []):
        if rel.get("type") == "is_person" and rel.get("name"):
            if _normalize(rel["name"]) not in existing_alias_names:
                discogs_aliases.append({"name": rel["name"]})
                existing_alias_names.add(_normalize(rel["name"]))

    seen_alias_names: set[str] = set()
    unique_aliases = []
    for a in discogs_aliases:
        key = _normalize(a.get("name", ""))
        if key and key not in seen_alias_names:
            seen_alias_names.add(key)
            unique_aliases.append(a)
    discogs_aliases = unique_aliases

    # 2. Laske per_source
    # Spotify karsii ~50-70% pienten artistien kappaleita — haetaan kerroin verran enemmän.
    # Mutta jos lähteitä on vain yksi (sooloartisti), ei pidä hakea tolkuttomasti.
    num_sources = 1 + len(bands) + len(discogs_aliases)
    spotify_multiplier = 2 if resolve_spotify else 1
    per_source = max(6, min(
        target_count * spotify_multiplier,                        # yläraja: 2× target
        (target_count * spotify_multiplier) // max(num_sources, 1) + 6,  # per lähde
    ))

    # Sooloura saa enemmän — se on pääartisti, mutta max 2× target_count
    solo_fetch = min(per_source * 2, target_count * spotify_multiplier)

    # 3. Kaikki haut rinnakkain — kaikki käyttävät album_spread:ia
    solo_task = _fetch_album_spread(artist, solo_fetch)

    band_tasks = [
        _fetch_album_spread(b["name"], per_source)
        for b in bands
        if b.get("name")
    ]

    alias_tasks = [
        _fetch_album_spread(a["name"], per_source)
        for a in discogs_aliases
        if a.get("name")
    ]

    all_results = await asyncio.gather(
        solo_task, *band_tasks, *alias_tasks, return_exceptions=True
    )

    solo_raw = all_results[0] if not isinstance(all_results[0], Exception) else []
    band_results = all_results[1 : 1 + len(band_tasks)]
    alias_results = all_results[1 + len(band_tasks) :]

    # 4. Koosta kandidaattipooli
    candidates: list[dict] = []

    for t in solo_raw:
        candidates.append({**t, "source_type": "solo", "source_name": artist})

    for band, result in zip(bands, band_results):
        if isinstance(result, Exception) or not result:
            # Fallback hits jos album_spread ei toimi (esim. ei albumitietoja Last.fm:ssä)
            try:
                result = await _fetch_hits(band["name"], per_source)
            except Exception:
                result = []
        for t in result:
            candidates.append({**t, "source_type": "band", "source_name": band["name"]})

    for alias, result in zip(discogs_aliases, alias_results):
        if isinstance(result, Exception) or not result:
            try:
                result = await _fetch_hits(alias["name"], per_source)
            except Exception:
                result = []
        for t in result:
            candidates.append({**t, "source_type": "alias", "source_name": alias["name"]})

    # 5. Spotify-resolvointi (valinnainen)
    if resolve_spotify:
        candidates = await _spotify_resolve_candidates(candidates)
        # Karsii ei-löytyvät ja deduplikoi saman spotify_uri:n
        seen_uris: set[str] = set()
        deduped = []
        for c in candidates:
            uri = c.get("spotify_uri")
            if uri and uri not in seen_uris:
                seen_uris.add(uri)
                deduped.append(c)
        candidates = deduped
    else:
        # Deduplikoi normalisoidulla artisti+title-avaimella
        seen_keys: set[str] = set()
        deduped = []
        for c in candidates:
            key = _normalize(f"{c.get('artist','')}{c.get('title','')}")
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped.append(c)
        candidates = deduped

    # Trimmaa target_count:iin — round-robin albumeittain/lähteittäin säilyttää kaarihajonnan
    if len(candidates) > target_count:
        from collections import defaultdict
        groups: dict[str, list[dict]] = defaultdict(list)
        for c in candidates:
            key = f"{c.get('source_name','')}/{c.get('album','')}"
            groups[key].append(c)
        group_lists = list(groups.values())
        trimmed: list[dict] = []
        i = 0
        while len(trimmed) < target_count:
            added = False
            for g in group_lists:
                if i < len(g) and len(trimmed) < target_count:
                    trimmed.append(g[i])
                    added = True
            if not added:
                break
            i += 1
        candidates = trimmed

    return {
        "artist": artist,
        "target_count": target_count,
        "universe_map": {
            "bands": [
                {"name": b["name"], "disambiguation": b.get("disambiguation", ""), "mbid": b.get("mbid", ""), "source_type": b.get("source_type", "band")}
                for b in bands
            ],
            "aliases": [{"name": a["name"]} for a in discogs_aliases],
        },
        "candidates": candidates,
    }


if __name__ == "__main__":
    mcp.run()

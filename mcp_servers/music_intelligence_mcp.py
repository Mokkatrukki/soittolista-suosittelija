"""Music Intelligence MCP — artistisuosittelu ilman tekoälyä.

Yhdistää Last.fm similar-graafin (L1+L2), tag-listan ja Discogs-labeldatan
yhdeksi pisteytetyksi artistilistaksi. Ei LLM:ää — pelkkää graafin läpikäyntiä
ja pisteytysmatikkaa.
"""
import asyncio
import math
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("music_intelligence")

LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
DISCOGS_BASE = "https://api.discogs.com"
MB_BASE = "https://musicbrainz.org/ws/2"
LB_BASE = "https://api.listenbrainz.org"
USER_AGENT = "SoittolistaSuosittelija/1.0"

# MusicBrainz: max 1 req/s
_mb_sem = asyncio.Semaphore(1)
_mb_last: float = 0.0

MAJOR_LABELS = {
    "warner", "sony", "universal", "emi", "atlantic", "columbia",
    "capitol", "rca", "island", "interscope", "def jam", "republic",
    "virgin", "epic", "polydor", "mercury", "decca", "arista",
    "elektra", "geffen", "mca", "chrysalis", "sire", "bmg",
    "wea", "parlophone", "london records", "jive", "zomba",
}

# Geneerisiä tageja joita ei kannata hakea genreinä
GENERIC_TAGS = {
    "rock", "metal", "alternative", "pop", "electronic", "indie",
    "classic rock", "seen live", "all", "favorites", "music",
    "male vocalists", "female vocalists", "singer-songwriter",
    "00s", "90s", "80s", "70s", "60s",
    # Pelkät kansallisuussanat (liian laajoja — tuo kaiken maalaisen musiikin)
    # "finnish metal" tai "suomirock" ovat OK, mutta pelkkä "finnish"/"suomi" ei
    "finnish", "swedish", "norwegian", "danish", "icelandic",
    "german", "french", "british", "american", "australian", "canadian",
    "japanese", "korean", "chinese",
    # Suomenkieliset maaviitteet — liian laajoja
    "suomi", "sverige", "norge", "deutschland",
}

# Kieli/alue-etuliitteet joilla tunnistetaan skene-spesifit tagit automaattisesti.
# Lisää tähän uusia kieliä/alueita — kaikki muodot (suomirock, suomi-pop, suomi rock)
# tunnistetaan automaattisesti ilman jokaisen variantin luettelointia.
SCENE_PREFIXES = [
    # Pohjoismaat
    "suomi", "norsk", "svensk", "dansk", "islenska",
    # Muu Eurooppa
    "deutsch", "german", "french", "español", "italian", "portuguese",
    "british", "celtic", "balkan",
    # Aasia
    "j-", "k-", "c-",          # j-pop, k-pop, c-pop
    "mandopop", "cantopop",
    # Amerikka / muu
    "latin", "afro", "aussie", "canadian",
]


# ---------------------------------------------------------------------------
# Apufunktiot
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """Normalisoi nimi deduplausta varten: poista pisteet, välilyönnit ja unicode-tavuviivat."""
    # Normalisoi kaikki unicode-tavuviivat ASCII-yhdysviivaksi ensin
    name = re.sub(r"[\u2010-\u2015\u2212\uFE58\uFE63\uFF0D]", "-", name)
    return re.sub(r"[\s.]+", "", name.lower())


def _is_major_label(label_name: str) -> bool:
    label_lower = label_name.lower()
    return any(major in label_lower for major in MAJOR_LABELS)


def _is_scene_tag(tag: str) -> bool:
    """Onko tagi skene/kieli-spesifi? Tunnistaa etuliitteiden perusteella automaattisesti.

    Esim. "suomirock", "suomi-pop", "norsk musikk", "j-pop", "german metal" → True
    "thrash metal", "rock", "indie" → False
    """
    t = tag.lower()
    return any(t.startswith(p) or f" {p}" in t for p in SCENE_PREFIXES)


def _split_tags(tags: list[str]) -> tuple[list[str], list[str]]:
    """Jaa tagit genre-tageihin ja skene/kieli-tageihin."""
    scene = [t for t in tags if _is_scene_tag(t) and t.lower() not in GENERIC_TAGS]
    genre = [t for t in tags if not _is_scene_tag(t) and t.lower() not in GENERIC_TAGS]
    return genre, scene


# ---------------------------------------------------------------------------
# API-apufunktiot
# ---------------------------------------------------------------------------

async def _lastfm(method: str, params: dict | None = None) -> dict:
    api_key = os.environ.get("LASTFM_API_KEY")
    if not api_key or api_key == "your_lastfm_api_key_here":
        raise RuntimeError("LASTFM_API_KEY puuttuu .env-tiedostosta")
    p = {"method": method, "api_key": api_key, "format": "json", "autocorrect": 1, **(params or {})}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(LASTFM_BASE, params=p)
        resp.raise_for_status()
        data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Last.fm virhe {data['error']}: {data.get('message')}")
    return data


async def _discogs_get(path: str, params: dict | None = None) -> dict:
    token = os.environ.get("DISCOGS_TOKEN")
    if not token or token == "your_discogs_token_here":
        raise RuntimeError("DISCOGS_TOKEN puuttuu .env-tiedostosta")
    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.discogs.v2.plaintext+json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{DISCOGS_BASE}{path}", headers=headers, params=params or {})
        resp.raise_for_status()
        return resp.json()


async def _mb_get(path: str, params: dict | None = None) -> dict:
    """MusicBrainz GET — serialisoitu, 1 req/s rate limit."""
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


async def _lb_get(path: str, params: dict | None = None) -> dict:
    """ListenBrainz GET."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{LB_BASE}{path}",
            params=params or {},
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Pipeline-vaiheet
# ---------------------------------------------------------------------------

async def _fetch_l2_candidates(
    top_l1: list[dict],
    known_norms: set[str],
) -> tuple[dict[str, dict], dict[str, set[str]]]:
    """Hae L2-similar-artistit rinnakkain top_l1-artisteille.

    known_norms sisältää normalisoituja nimiä (_norm(name)) deduplausta varten.

    Palauttaa:
      - l2_new:      {norm_key: {name, sources}} — uudet artistit
      - l2_confirms: {norm_key: set[source]}     — L1-artistit joille tuli L2-vahvistus
    """
    results = await asyncio.gather(
        *[_lastfm("artist.getSimilar", {"artist": a["name"], "limit": 20}) for a in top_l1],
        return_exceptions=True,
    )

    l2_new: dict[str, dict] = {}
    l2_confirms: dict[str, set[str]] = {}

    for i, raw in enumerate(results):
        if isinstance(raw, Exception):
            continue
        source = top_l1[i]["name"]
        for a in raw.get("similarartists", {}).get("artist", []):
            key = _norm(a["name"])
            if key in known_norms:
                if key not in l2_confirms:
                    l2_confirms[key] = set()
                l2_confirms[key].add(source)
            else:
                known_norms.add(key)
                if key not in l2_new:
                    l2_new[key] = {"name": a["name"], "sources": set()}
                l2_new[key]["sources"].add(source)

    return l2_new, l2_confirms


async def _fetch_tag_artists(
    tags: list[str],
    known_norms: set[str],
) -> dict[str, str]:
    """Hae top-artistit tageille rinnakkain. Palauttaa: {norm_key: display_name}"""
    if not tags:
        return {}
    results = await asyncio.gather(
        *[_lastfm("tag.getTopArtists", {"tag": tag, "limit": 20}) for tag in tags],
        return_exceptions=True,
    )
    tag_artists: dict[str, str] = {}
    for raw in results:
        if isinstance(raw, Exception):
            continue
        for a in raw.get("topartists", {}).get("artist", []):
            key = _norm(a["name"])
            if key not in known_norms:
                tag_artists[key] = a["name"]
    return tag_artists


async def _expand_tags_from_l1_artists(
    top_l1: list[dict],
    known_genre_tags: set[str],
) -> list[str]:
    """Laajenna genre-tageja hakemalla L1-artistien tagit.

    tag.getSimilar ei käytännössä toimi Last.fm:ssä (palauttaa tyhjän).
    Sen sijaan haetaan top-3 L1-artistin tagit ja otetaan mukaan tagit
    joita kohdeartistilla ei itse ole — ne paljastavat lähisukulaisgenret.

    Esim. Stam1na → L1: Mokoma, Kotiteollisuus, Diablo
    → niiden tagit: "industrial metal", "melodic metal", "gothic metal"
    → haetaan näistäkin artisteja
    """
    if not top_l1:
        return []
    results = await asyncio.gather(
        *[_lastfm("artist.getTopTags", {"artist": a["name"]}) for a in top_l1[:3]],
        return_exceptions=True,
    )
    seen = {t.lower() for t in known_genre_tags}
    expanded = []
    for raw in results:
        if isinstance(raw, Exception):
            continue
        # Otetaan vain riittävästi käytetyt tagit (count >= 5) — suodattaa satunnaiset
        for t in raw.get("toptags", {}).get("tag", [])[:8]:
            if int(t.get("count", 0)) < 5:
                continue
            name = t["name"]
            low = name.lower()
            if low not in seen and low not in GENERIC_TAGS and not _is_scene_tag(low):
                seen.add(low)
                expanded.append(name)
    return expanded[:6]


async def _get_artist_mbid(artist: str) -> str | None:
    """Hae artistin MusicBrainz MBID nimellä. Palauttaa None jos ei löydy."""
    try:
        data = await _mb_get("/artist", {"query": f'artist:"{artist}"', "limit": 1})
        artists = data.get("artists", [])
        if artists and int(artists[0].get("score", 0)) >= 90:
            return artists[0]["id"]
    except Exception:
        pass
    return None


async def _fetch_lb_similar(mbid: str, known_norms: set[str]) -> dict[str, str]:
    """Hae ListenBrainz lb-radio similar artistit. Palauttaa {norm_key: display_name}."""
    try:
        data = await _lb_get(f"/1/lb-radio/artist/{mbid}", {
            "mode": "easy",
            "max_similar_artists": 30,
            "max_recordings_per_artist": 1,
            "pop_begin": 0,
            "pop_end": 100,
        })
        result: dict[str, str] = {}
        for artist_mbid, recordings in data.items():
            if artist_mbid == mbid or not recordings:
                continue
            name = recordings[0].get("similar_artist_name", "")
            if not name:
                continue
            key = _norm(name)
            if key not in known_norms:
                result[key] = name
        return result
    except Exception:
        return {}


async def _fetch_discogs_label_artists(
    artist: str,
    known_norms: set[str],
) -> dict[str, str]:
    """Etsi artistin levy-yhtiö ja palauta muut saman labelin artistit.

    Ohittaa major-labelit. Palauttaa: {norm_key: display_name}
    """
    _SKIP = {"various", "variousartists", "unknown", "v/a", "va", "unknownartist"}

    try:
        search = await _discogs_get("/database/search", {
            "type": "release", "artist": artist, "per_page": 5, "page": 1,
        })
        releases = search.get("results", [])
        if not releases:
            return {}

        label_name = None
        for release in releases[:3]:
            labels = release.get("label", [])
            if labels and not _is_major_label(labels[0]):
                label_name = labels[0]
                break

        if not label_name:
            return {}

        label_search = await _discogs_get("/database/search", {
            "type": "label", "q": label_name, "per_page": 5,
        })
        label_results = label_search.get("results", [])
        if not label_results:
            return {}

        label_id = label_results[0]["id"]
        label_releases = await _discogs_get(f"/labels/{label_id}/releases", {
            "per_page": 50, "page": 1,
        })

        label_artists: dict[str, str] = {}
        artist_norm = _norm(artist)

        for r in label_releases.get("releases", []):
            name = r.get("artist", "")
            if not name or "/" in name:
                continue
            # Poista Discogs-disambiguointi: "Diablo (8)" → "Diablo"
            name = re.sub(r"\s*\(\d+\)\s*$", "", name).strip()
            norm = _norm(name)
            if not norm or norm in _SKIP or norm.startswith("various"):
                continue
            if norm not in known_norms and norm != artist_norm:
                label_artists[norm] = name

        return label_artists

    except Exception:
        return {}


def _score_and_tier(
    l1_artists: list[dict],
    l2_new: dict[str, dict],
    l2_confirms: dict[str, set[str]],
    genre_tag_artists: dict[str, str],
    country_tag_artists: dict[str, str],
    label_artists: dict[str, str],
    lb_artists: dict[str, str],
    target_artist: str,
) -> dict[str, list[dict]]:
    """Pisteytä kaikki kandidaatit ja jaa kolmeen tieriin.

    Pisteytys:
      - L1 match * 50 (max 50)
      - L2 vahvistus: +15 per lähde (max 30)
      - ListenBrainz similar: +20
      - Sama label: +15
      - Maa-tagi: +10
      - Genre-tagi: 0 (surprising-tieri)
    """
    candidates: dict[str, dict] = {}

    # L1
    for a in l1_artists:
        key = _norm(a["name"])
        score = round(a["match"] * 50)
        candidates[key] = {
            "name": a["name"],
            "score": score,
            "l1_match": a["match"],
            "signals": [f"lastfm_l1({a['match']:.2f})"],
            "band_member_solo": a["match"] >= 0.98,
        }

    # L2-vahvistukset L1-artisteille
    for key, sources in l2_confirms.items():
        if key not in candidates:
            continue
        bonus = min(len(sources) * 15, 30)
        candidates[key]["score"] += bonus
        candidates[key]["signals"].append(
            f"lastfm_l2×{len(sources)}({', '.join(sorted(sources))})"
        )

    # L2-uudet kandidaatit
    for key, info in l2_new.items():
        bonus = min(len(info["sources"]) * 15, 30)
        if key not in candidates:
            candidates[key] = {
                "name": info["name"],
                "score": 0,
                "l1_match": 0.0,
                "signals": [],
                "band_member_solo": False,
            }
        candidates[key]["score"] += bonus
        candidates[key]["signals"].append(
            f"lastfm_l2×{len(info['sources'])}({', '.join(sorted(info['sources']))})"
        )

    # Label (+15)
    for key, name in label_artists.items():
        if key not in candidates:
            candidates[key] = {
                "name": name, "score": 0, "l1_match": 0.0,
                "signals": [], "band_member_solo": False,
            }
        candidates[key]["score"] += 15
        candidates[key]["signals"].append("same_label")

    # Maa-tagi (+10) — kansallinen skene -bonus
    for key, name in country_tag_artists.items():
        if key not in candidates:
            candidates[key] = {
                "name": name, "score": 0, "l1_match": 0.0,
                "signals": [], "band_member_solo": False,
            }
        candidates[key]["score"] += 10
        candidates[key]["signals"].append("country_tag")

    # Genre-tagi (0 pistettä, merkitään vain signaaliksi)
    for key, name in genre_tag_artists.items():
        if key not in candidates:
            candidates[key] = {
                "name": name, "score": 0, "l1_match": 0.0,
                "signals": ["tag_only"], "band_member_solo": False,
            }

    # ListenBrainz similar (+20)
    for key, name in lb_artists.items():
        if key not in candidates:
            candidates[key] = {
                "name": name, "score": 0, "l1_match": 0.0,
                "signals": [], "band_member_solo": False,
            }
        candidates[key]["score"] += 20
        candidates[key]["signals"].append("lb")

    # Poista kohdeartisti
    candidates.pop(_norm(target_artist), None)

    # Tiering
    obvious, close, surprising = [], [], []
    for c in sorted(candidates.values(), key=lambda x: x["score"], reverse=True):
        if c["score"] >= 25:
            obvious.append(c)
        elif c["score"] >= 10:
            close.append(c)
        else:
            surprising.append(c)

    return {"obvious": obvious, "close": close, "surprising": surprising}


async def _add_discovery_scores(candidates: list[dict]) -> list[dict]:
    """Hae kuuntelijamäärät ja laske discovery_score = similarity / log10(listeners) × 10."""
    if not candidates:
        return candidates

    results = await asyncio.gather(
        *[_lastfm("artist.getInfo", {"artist": c["name"]}) for c in candidates],
        return_exceptions=True,
    )

    for i, raw in enumerate(results):
        if isinstance(raw, Exception):
            candidates[i]["listeners"] = 0
            candidates[i]["familiarity"] = "unknown"
            candidates[i]["discovery_score"] = 0.0
            continue

        listeners = int(raw.get("artist", {}).get("stats", {}).get("listeners", 0))
        candidates[i]["listeners"] = listeners

        if listeners >= 5_000_000:
            familiarity = "mega"
        elif listeners >= 1_000_000:
            familiarity = "popular"
        elif listeners >= 200_000:
            familiarity = "mid"
        elif listeners >= 50_000:
            familiarity = "underground"
        else:
            familiarity = "hidden_gem"

        candidates[i]["familiarity"] = familiarity

        if listeners > 0:
            sim = candidates[i].get("l1_match") or (candidates[i]["score"] / 50)
            candidates[i]["discovery_score"] = round(
                sim / math.log10(max(listeners, 10)) * 10, 2
            )
        else:
            candidates[i]["discovery_score"] = 0.0

    return candidates


# ---------------------------------------------------------------------------
# Kompakti tekstiformaatti
# ---------------------------------------------------------------------------

def _fmt_listeners(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n) if n > 0 else "?"


def _fmt_artist_line(c: dict) -> str:
    name = c["name"]
    if c.get("band_member_solo"):
        name += " [bändi/solo]"
    lis = _fmt_listeners(c.get("listeners", 0))
    disc = c.get("discovery_score", 0)
    disc_str = f" disc={disc:.2f}" if disc > 0 else ""

    sig_parts = []
    for s in c.get("signals", []):
        if s.startswith("lastfm_l1("):
            sig_parts.append(s.replace("lastfm_", ""))
        elif s.startswith("lastfm_l2×"):
            sig_parts.append(s.replace("lastfm_", ""))
        elif s == "same_label":
            sig_parts.append("label")
        elif s == "country_tag":
            sig_parts.append("ctag")
        elif s == "tag_only":
            sig_parts.append("tag")
        elif s == "lb":
            sig_parts.append("lb")

    return f"  • {name} | {lis}{disc_str}  {', '.join(sig_parts)}"


def _format_output(
    artist: str,
    target_tags: list[str],
    active_country_tags: list[str],
    sources: dict,
    tiers: dict,
    mode: str,
) -> str:
    obvious = tiers["obvious"]
    close = tiers["close"]
    surprising = tiers["surprising"]

    total = len(obvious) + len(close) + len(surprising)
    tags_str = ", ".join(target_tags[:5]) or "—"
    ctag_str = f" | maa-tagit: {', '.join(active_country_tags)}" if active_country_tags else ""
    exp = sources.get("genre_tag_expanded", 0)
    tag_str = f"{sources['genre_tag']}(+{exp}exp)" if exp else str(sources["genre_tag"])
    lb_str = f" lb={sources['lb']}" if sources.get("lb") else ""
    src_str = (
        f"L1={sources['l1']} L2={sources['l2_new']} "
        f"tag={tag_str} ctag={sources['country_tag']} label={sources['label']}{lb_str}"
    )

    lines = [
        f"=== {artist} — {total} artistia ===",
        f"Tagit: {tags_str}{ctag_str} | Lähteet: {src_str}",
        "",
    ]

    if mode == "blend":
        # Yhdistelmäpisteytys: similarity + discovery painotettuna
        # blend = score * 0.7 + discovery_score * 15 * 0.3
        # Blend-modessa jätetään pois pelkät tag-only artistit (score=0, vain genre-tagi)
        # — niillä ei ole tarpeeksi signaalia muista lähteistä
        all_c = [
            c for c in obvious + close + surprising
            if not (c["score"] == 0 and c.get("signals") == ["tag_only"])
        ]
        def _blend_key(c: dict) -> float:
            return c["score"] * 0.7 + c.get("discovery_score", 0) * 15 * 0.3
        ranked = sorted(all_c, key=_blend_key, reverse=True)
        lines.append(f"BLEND — {len(ranked)} artistia (similarity + discovery)")
        for c in ranked:
            lines.append(_fmt_artist_line(c))
    elif mode == "discovery":
        ranked = sorted(
            obvious + close + surprising,
            key=lambda x: x.get("discovery_score", 0), reverse=True,
        )
        lines.append(f"DISCOVERY — {len(ranked)} artistia (discovery_score järjestyksessä)")
        for c in ranked:
            lines.append(_fmt_artist_line(c))
    elif mode == "familiar":
        ranked = sorted(
            obvious + close + surprising,
            key=lambda x: x["score"], reverse=True,
        )
        lines.append(f"FAMILIAR — {len(ranked)} artistia (similarity järjestyksessä)")
        for c in ranked:
            lines.append(_fmt_artist_line(c))
    else:
        lines.append(f"OBVIOUS ({len(obvious)})")
        for c in obvious:
            lines.append(_fmt_artist_line(c))
        lines.append(f"\nCLOSE ({len(close)})")
        for c in close:
            lines.append(_fmt_artist_line(c))
        lines.append(f"\nSURPRISING ({len(surprising)}) — discovery järjestyksessä")
        for c in sorted(surprising, key=lambda x: x.get("discovery_score", 0), reverse=True):
            lines.append(_fmt_artist_line(c))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP-työkalu
# ---------------------------------------------------------------------------

@mcp.tool()
async def find_similar_artists(
    artist: str,
    mode: str = "blend",
    include_band_members: bool = True,
) -> str:
    """Löydä samankaltaisia artisteja yhdistämällä useita datansignaaleja.

    Signaalit: Last.fm L1/L2-graafi, ListenBrainz kuunteludata,
    genre-tagit (kohdeartistin + L1-artistien tagit), skene-tagit, Discogs-label.

    Args:
        artist: Artistin nimi.
        mode: "blend" (suositeltu: pisteet + discovery yhdistettynä),
              "all" (tiereittäin: obvious/close/surprising),
              "discovery" (tuntemattomien suosiminen, disc-score järjestys),
              "familiar" (similarity-pisteet järjestys).
        include_band_members: Jos False, suodattaa pois match >= 0.98 artistit.

    Returns:
        Kompakti tekstitiivistelmä artistisuosituksista.
    """
    # --- Vaihe 1: L1 + tagit + MusicBrainz MBID rinnakkain ---
    try:
        l1_raw, tags_raw, mbid = await asyncio.gather(
            _lastfm("artist.getSimilar", {"artist": artist, "limit": 15}),
            _lastfm("artist.getTopTags", {"artist": artist}),
            _get_artist_mbid(artist),
        )
    except RuntimeError as e:
        return f"Virhe: {e}"

    l1_artists = [
        {"name": a["name"], "match": float(a.get("match", 0))}
        for a in l1_raw.get("similarartists", {}).get("artist", [])
    ]
    target_tags = [t["name"] for t in tags_raw.get("toptags", {}).get("tag", [])]
    genre_tags, country_tags = _split_tags(target_tags)

    # known_norms: normalisoituja nimiä deduplausta varten
    known_norms: set[str] = {_norm(artist)} | {_norm(a["name"]) for a in l1_artists}

    # --- Vaihe 1b: Laajenna genre-tageja L1-artistien tageista ---
    top3_l1 = l1_artists[:3]
    expanded_genre_tags = await _expand_tags_from_l1_artists(top3_l1, set(t.lower() for t in genre_tags))
    all_genre_tags = genre_tags + expanded_genre_tags

    # --- Vaihe 2: L2 + tagit + label + ListenBrainz rinnakkain ---
    async def _empty() -> dict:
        return {}

    (l2_new, l2_confirms), genre_tag_artists, country_tag_artists, label_artists, lb_artists = (
        await asyncio.gather(
            _fetch_l2_candidates(top3_l1, known_norms),
            _fetch_tag_artists(all_genre_tags[:5], known_norms),
            _fetch_tag_artists(country_tags[:2], known_norms),
            _fetch_discogs_label_artists(artist, known_norms),
            _fetch_lb_similar(mbid, known_norms) if mbid else _empty(),
        )
    )

    # --- Vaihe 3: pisteytys ja tiering ---
    tiers = _score_and_tier(
        l1_artists, l2_new, l2_confirms,
        genre_tag_artists, country_tag_artists, label_artists, lb_artists,
        artist,
    )

    # --- Vaihe 4: discovery_score kaikille tiereille ---
    tiers["obvious"], tiers["close"], tiers["surprising"] = await asyncio.gather(
        _add_discovery_scores(tiers["obvious"]),
        _add_discovery_scores(tiers["close"]),
        _add_discovery_scores(tiers["surprising"]),
    )

    # --- Vaihe 5: suodatukset ---
    artist_norm = _norm(artist)

    def _keep(c: dict) -> bool:
        c_norm = _norm(c["name"])
        if artist_norm in c_norm or c_norm in artist_norm:
            return False
        if not include_band_members and c.get("band_member_solo"):
            return False
        if c["score"] == 0 and c.get("familiarity") in {"mega", "popular"}:
            return False
        return True

    for tier in ("obvious", "close", "surprising"):
        tiers[tier] = [c for c in tiers[tier] if _keep(c)]

    sources = {
        "l1": len(l1_artists),
        "l2_new": len(l2_new),
        "genre_tag": len(genre_tag_artists),
        "genre_tag_expanded": len(expanded_genre_tags),
        "country_tag": len(country_tag_artists),
        "label": len(label_artists),
        "lb": len(lb_artists),
        "mbid": mbid or "",
    }

    return _format_output(artist, target_tags, country_tags[:2], sources, tiers, mode)


if __name__ == "__main__":
    mcp.run()

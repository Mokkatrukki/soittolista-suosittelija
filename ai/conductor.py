"""DSPy-pohjainen DJ-ohjaaja — multi-turn chat soittolistan rakentamiseen."""
import asyncio
import logging
import os
import re
from typing import Literal

import dspy
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from ai.fetcher import get_artist_genres
from app.db import clear_conversation, get_conversation, save_conversation

load_dotenv()

logger = logging.getLogger(__name__)

_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
_DEFAULT_MODEL = "gemini-2.5-flash-lite-preview-09-2025"

# ---------------------------------------------------------------------------
# DSPy alustus
# ---------------------------------------------------------------------------

_lm = dspy.LM(f"gemini/{_DEFAULT_MODEL}", api_key=_GEMINI_API_KEY)
dspy.configure(lm=_lm)


# ---------------------------------------------------------------------------
# DSPy Signature + Module
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """Olet kokenut DJ ja musiikkiasiantuntija nimeltä Maestro. \
Autat käyttäjää rakentamaan täydellisen Spotify-soittolistan.

PROSESSI:
1. Kuuntele mitä käyttäjä haluaa
2. Kysy tarkentavia kysymyksiä jos tarvitset enemmän tietoa
3. Kun sinulla on riittävästi tietoa → palauta state="ready"

STATE-KENTTÄ — käytä AINA täsmälleen toinen näistä arvoista:
- state="asking"  → tarvitset vielä tietoa, palautat yhden tarkentavan kysymyksen
- state="ready"   → tiedät tarpeeksi, rakennat soittolistan

MILLOIN state="ready":
- Olet selvittänyt musiikin tyyli/tunnelma/artisti tai tilanne
- Ei tarvita enempää tarkennuksia
- Tyypillisesti 1–2 viestinvaihdon jälkeen riittää

KYSY KERRALLAAN vain yksi lyhyt, selkeä kysymys. Ole innostunut mutta ytimekäs.

STRATEGIAT (valitse yksi strategy-kenttään):
- "genre_tags"      → käyttäjä mainitsee genren, tunnelman tai tyylin
- "similar_artists" → käyttäjä mainitsee artistin referenssinä
- "top_charts"      → käyttäjä haluaa suosittua/trendaavaa musiikkia

INTENT-KENTTIEN TÄYTTÖ:
- genres: Last.fm-tagit ENGLANNIKSI, esim. ["chill", "jazz", "indie", "80s"]
- artists: artistien nimet tarkalleen, esim. ["Radiohead", "Haloo Helsinki"]
- country: maa englanniksi, esim. "Finland", tai tyhjä = globaali
- era: "60s", "70s", "80s", "90s", "2000s", "2010s", "recent" tai tyhjä
- count: kappalemäärä 10–40 (oletus 20)
- max_per_artist: max kappaletta per artisti (0 = ei rajoitusta).
  Aseta kun käyttäjä sanoo "vain N kpl artistilta X" tai haluaa monipuolisen listan.
  Esim. "vain 3 Infected Mushroom" → max_per_artist=3
- playlist_name: lyhyt kuvaava SUOMENKIELINEN nimi, esim. "Rentoa jazz-iltaa varten"
- playlist_description: 1–2 lausetta suomeksi

KOMMUNIKOI käyttäjän kielellä (suomi tai englanti).
"""


class ConductorTurn(dspy.Signature):
    __doc__ = _SYSTEM_PROMPT

    history: str = dspy.InputField(
        desc="Aiempi keskustelu muodossa 'user: ...\nmodel: ...' — tyhjä jos ensimmäinen viesti"
    )
    message: str = dspy.InputField(desc="Käyttäjän viimeisin viesti")

    state: Literal["asking", "ready"] = dspy.OutputField(
        desc="'asking' jos tarvitset lisätietoa, 'ready' jos voit rakentaa soittolistan"
    )
    reply: str = dspy.OutputField(desc="Vastausviesti käyttäjälle")
    strategy: Literal["genre_tags", "similar_artists", "top_charts"] = dspy.OutputField(
        desc="Hakustrategia — merkityksellinen vain kun state='ready'"
    )
    genres: list[str] = dspy.OutputField(
        desc="Last.fm-tagit englanniksi, esim. ['jazz', 'chill'] — tyhjä lista jos ei käytetä"
    )
    artists: list[str] = dspy.OutputField(
        desc="Referenssiartistit — tyhjä lista jos ei käytetä"
    )
    country: str = dspy.OutputField(desc="Maa englanniksi tai tyhjä merkkijono")
    era: str = dspy.OutputField(desc="Aikakausi kuten '80s' tai tyhjä merkkijono")
    count: int = dspy.OutputField(desc="Kappalemäärä 10–40")
    max_per_artist: int = dspy.OutputField(desc="Max kappaletta per artisti, 0 = ei rajoitusta")
    playlist_name: str = dspy.OutputField(desc="Soittolistan nimi suomeksi")
    playlist_description: str = dspy.OutputField(desc="Soittolistan kuvaus suomeksi")


class ConductorDSPy(dspy.Module):
    def __init__(self) -> None:
        self.turn = dspy.ChainOfThought(ConductorTurn)

    def forward(self, history: str, message: str) -> dspy.Prediction:
        return self.turn(history=history, message=message)


# ---------------------------------------------------------------------------
# Singleton + _get_module (eval.py käyttää)
# ---------------------------------------------------------------------------

_module: ConductorDSPy | None = None


def _get_module(model: str | None = None) -> ConductorDSPy:
    """Palauta singleton tai luo uusi annetulla mallilla (eval.py)."""
    global _module
    if model is not None:
        model_id = model if model.startswith("gemini/") else f"gemini/{model}"
        lm = dspy.LM(model_id, api_key=_GEMINI_API_KEY)
        dspy.configure(lm=lm)
        return ConductorDSPy()
    if _module is None:
        _module = ConductorDSPy()
    return _module


# ---------------------------------------------------------------------------
# Postprocess — deterministiset korjaukset LLM:n paluuarvoihin
# ---------------------------------------------------------------------------

_GENRE_HINTS: dict[str, list[str]] = {
    "treenimusiikki": ["workout", "energetic"],
    "treeni":         ["workout", "energetic"],
    "biletys":        ["party", "dance"],
    "bileet":         ["party", "dance"],
    "juhla":          ["party"],
    "rentou":         ["chill", "relax"],
    "rentoa":         ["chill", "relax"],
    "nukkuminen":     ["sleep", "ambient"],
    "nukkumis":       ["sleep", "ambient"],
    "keskittyminen":  ["focus", "instrumental"],
    "fokus":          ["focus", "instrumental"],
    "surullin":       ["sad", "melancholic"],
    "iloinen":        ["happy", "upbeat"],
    "romanttinen":    ["romantic"],
    "akustinen":      ["acoustic"],
    "suomibiis":      ["finnish", "suomi-pop"],
    "suomimusiikki":  ["finnish", "suomi-pop"],
    "suomalain":      ["finnish"],
    "klassinen":      ["classical"],
    "klassista":      ["classical"],
}

_ERA_HINTS: dict[str, str] = {
    "60-luk": "60s",
    "70-luk": "70s",
    "80-luk": "80s",
    "90-luk": "90s",
    "2000-luk": "2000s",
    "2010-luk": "2010s",
    "nykymusiikki": "recent",
    "uusin": "recent",
    "uusimpi": "recent",
    "tänä vuonna": "recent",
}

_COUNTRY_HINTS: dict[str, str] = {
    "suomalain": "Finland",
    "suomessa": "Finland",
    "ruotsissa": "Sweden",
    "norjassa": "Norway",
    "saksassa": "Germany",
    "britanniassa": "United Kingdom",
}


def _postprocess(
    genres: list[str],
    era: str,
    country: str,
    strategy: str,
    query: str,
    count: int,
) -> tuple[list[str], str, str, str, int]:
    """Deterministiset korjaukset — palauttaa (genres, era, country, strategy, count)."""
    q = query.lower() if query else ""

    # Genrevihjeet tekstistä
    if q:
        extra: list[str] = []
        for hint, tags in _GENRE_HINTS.items():
            if hint in q:
                extra.extend(t for t in tags if t not in extra)
        if extra:
            genres = list(dict.fromkeys(genres + extra))

    # Aikakausivihjeet (vain jos LLM ei tunnistanut)
    if q and not era:
        for hint, era_val in _ERA_HINTS.items():
            if hint in q:
                era = era_val
                break

    # Maavihjeet (vain jos LLM ei tunnistanut)
    if q and not country:
        for hint, country_val in _COUNTRY_HINTS.items():
            if hint in q:
                country = country_val
                break

    # Maa tunnistettu + genre_tags ilman genrejä → top_charts
    if country and strategy == "genre_tags" and not genres:
        strategy = "top_charts"

    # Radio-vihjeet → tuleva ominaisuus
    if q and re.search(r'\byle\b|\bradio\b|\bareena\b', q, re.IGNORECASE):
        strategy = "radio_show"

    # Count-rajaus
    if count < 5:
        count = 5
    elif count > 50:
        count = 50

    return genres, era, country, strategy, count


# ---------------------------------------------------------------------------
# Apufunktio
# ---------------------------------------------------------------------------

def _list_to_history_str(history: list[dict]) -> str:
    """Muunna db:n historia-lista merkkijonoksi DSPy:tä varten."""
    parts = []
    for h in history:
        role = h.get("role", "user")
        text = " ".join(p.get("text", "") for p in h.get("parts", []))
        parts.append(f"{role}: {text}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Julkinen API
# ---------------------------------------------------------------------------

class SearchParams(BaseModel):
    """Hakuparametrit — välitetään /create/confirm-endpointille."""
    strategy: str = "genre_tags"
    genres: list[str] = Field(default_factory=list)
    genre_source: str = ""        # display: "Infected Mushroom / Last.fm"
    artists: list[str] = Field(default_factory=list)
    country: str | None = None
    era: str | None = None
    count: int = 20
    max_per_artist: int = 0
    exclude_artists: list[str] = Field(default_factory=list)
    playlist_name: str = ""
    playlist_description: str = ""


class ConductorResponse(BaseModel):
    state: Literal["asking", "confirming"]
    message: str
    params: SearchParams | None = None


async def conduct(user_id: str, message: str) -> ConductorResponse:
    """Käsittele yksi käyttäjän viesti — palauta ConductorResponse."""
    history = await get_conversation(user_id)
    history.append({"role": "user", "parts": [{"text": message}]})

    try:
        history_str = _list_to_history_str(history[:-1])
        pred = await asyncio.to_thread(
            lambda: _get_module()(history=history_str, message=message)
        )

        state = pred.state if pred.state in ("asking", "ready") else "asking"
        strategy = (
            pred.strategy
            if pred.strategy in ("genre_tags", "similar_artists", "top_charts")
            else "genre_tags"
        )
        genres = list(pred.genres or [])
        artists = list(pred.artists or [])
        country = pred.country or ""
        era = pred.era or ""
        count = max(5, min(50, int(pred.count or 20)))
        max_per_artist = max(0, int(pred.max_per_artist or 0))
        reply = pred.reply or ""
        playlist_name = pred.playlist_name or ""
        playlist_description = pred.playlist_description or ""

        logger.info(
            "[conduct] user=%s state=%s message=%r",
            user_id, state, reply[:80],
        )
    except Exception as e:
        logger.error("[conduct] DSPy error user=%s: %s", user_id, e)
        history.pop()
        return ConductorResponse(
            state="asking",
            message="Anteeksi, jokin meni pieleen. Voisitko toistaa pyyntösi?",
        )

    # Deterministiset korjaukset
    genres, era, country, strategy, count = _postprocess(
        genres, era, country, strategy, message, count
    )

    # Tallenna historia
    history.append({"role": "model", "parts": [{"text": reply}]})
    await save_conversation(user_id, history)

    if state != "ready":
        return ConductorResponse(state="asking", message=reply)

    # state == "ready" — rakenna SearchParams
    exclude_artists: list[str] = []
    genre_source = ""

    if strategy == "similar_artists" and artists:
        all_genres: list[str] = []
        for artist in artists[:2]:
            try:
                ag = await get_artist_genres(artist, top_n=3)
                for g in ag:
                    if g not in all_genres:
                        all_genres.append(g)
            except Exception as exc:
                logger.warning("[conduct] get_artist_genres %r: %s", artist, exc)

        if all_genres:
            genres = all_genres
            strategy = "genre_tags"
            genre_source = ", ".join(artists) + " / Last.fm"
            exclude_artists = list(artists)
            logger.info(
                "[conduct] artist→genre: %s → genres=%s exclude=%s",
                artists, genres, exclude_artists,
            )
        else:
            logger.warning(
                "[conduct] genre lookup failed for %s, fallback similar_artists",
                artists,
            )

    params = SearchParams(
        strategy=strategy,
        genres=genres,
        genre_source=genre_source,
        artists=artists,
        country=country or None,
        era=era or None,
        count=count,
        max_per_artist=max_per_artist,
        exclude_artists=exclude_artists,
        playlist_name=playlist_name or "Uusi soittolista",
        playlist_description=playlist_description or "",
    )
    logger.info("[conduct] confirming params=%s", params.model_dump())

    return ConductorResponse(state="confirming", message=reply, params=params)


async def reset_conversation(user_id: str) -> None:
    """Tyhjennä käyttäjän keskusteluhistoria."""
    await clear_conversation(user_id)

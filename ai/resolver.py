"""Spotify URI -resolvoija — muuntaa artisti/kappale-parit Spotify-URI:ksi."""
import asyncio
import logging
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

SPOTIFY_API = "https://api.spotify.com/v1"


async def _search_one(
    client: httpx.AsyncClient, artist: str, title: str
) -> dict | None:
    """Hae yksittäisen kappaleen Spotify URI ja kansikuva."""
    q = f"track:{title} artist:{artist}"
    try:
        resp = await client.get(
            f"{SPOTIFY_API}/search",
            params={"q": q, "type": "track", "limit": 1},
        )
        if resp.status_code != 200:
            return None
        items = resp.json().get("tracks", {}).get("items", [])
        if not items:
            return None
        track = items[0]
        images = track.get("album", {}).get("images", [])
        image_url = images[0]["url"] if images else ""
        return {"uri": track["uri"], "spotify_image_url": image_url}
    except Exception as e:
        logger.debug("Spotify search failed for %s – %s: %s", artist, title, e)
        return None


async def resolve_uris(tracks: list[dict], access_token: str) -> list[dict]:
    """Resolvoii Spotify URI:t kaikille kappaleille rinnakkain.

    Palauttaa vain kappaleet joille löytyi URI.
    Lisää kentät: uri, spotify_image_url.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
        results = await asyncio.gather(
            *[_search_one(client, t["artist"], t["title"]) for t in tracks],
            return_exceptions=True,
        )

    resolved: list[dict] = []
    for track, result in zip(tracks, results):
        if isinstance(result, Exception) or result is None:
            continue
        merged = {**track, **result}
        # Käytä spotify_image_url:ä image_url:n sijaan jos löytyi parempi
        if merged.get("spotify_image_url"):
            merged["image_url"] = merged["spotify_image_url"]
        resolved.append(merged)

    return resolved

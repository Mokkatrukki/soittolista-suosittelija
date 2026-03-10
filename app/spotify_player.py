"""Spotify Player API -wrapper."""
import httpx

BASE = "https://api.spotify.com/v1/me/player"


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


async def get_devices(access_token: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE}/devices", headers=_headers(access_token))
        if resp.status_code == 204 or resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json().get("devices", [])


async def get_playback_state(access_token: str) -> dict | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(BASE, headers=_headers(access_token))
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return resp.json()


async def play_track(access_token: str, track_uri: str, device_id: str | None = None) -> bool:
    params = {"device_id": device_id} if device_id else {}
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{BASE}/play",
            headers=_headers(access_token),
            params=params,
            json={"uris": [track_uri]},
        )
        return resp.status_code in (200, 204)


async def pause_playback(access_token: str, device_id: str | None = None) -> bool:
    params = {"device_id": device_id} if device_id else {}
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{BASE}/pause",
            headers=_headers(access_token),
            params=params,
        )
        return resp.status_code in (200, 204)

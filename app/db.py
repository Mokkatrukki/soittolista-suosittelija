"""SQLite-tietokanta aiosqlite:lla."""
import time
import json
import aiosqlite

DB_PATH = "data/app.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS tokens (
                user_id     TEXT PRIMARY KEY,
                access_token  TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS playlists (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                spotify_id  TEXT,
                name        TEXT NOT NULL,
                description TEXT,
                source      TEXT NOT NULL,  -- 'lastfm' | 'yle' | 'custom'
                tracks_json TEXT NOT NULL,  -- JSON-lista [{artist, title, uri}]
                created_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discovery_tracks (
                id          INTEGER PRIMARY KEY DEFAULT 1,
                tracks_json TEXT NOT NULL,
                updated_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discovery_ratings (
                track_uri   TEXT PRIMARY KEY,
                rating      INTEGER NOT NULL,
                rated_at    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                user_id      TEXT PRIMARY KEY,
                history_json TEXT NOT NULL,
                updated_at   REAL NOT NULL
            );
        """)
        await db.commit()


async def save_token(user_id: str, token_data: dict):
    expires_at = time.time() + token_data.get("expires_in", 3600)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO tokens (user_id, access_token, refresh_token, expires_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 access_token=excluded.access_token,
                 refresh_token=COALESCE(excluded.refresh_token, refresh_token),
                 expires_at=excluded.expires_at,
                 updated_at=excluded.updated_at""",
            (
                user_id,
                token_data["access_token"],
                token_data.get("refresh_token", ""),
                expires_at,
                time.time(),
            ),
        )
        await db.commit()


async def get_token(user_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tokens WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def save_playlist(user_id: str, name: str, description: str, source: str, tracks: list, spotify_id: str | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO playlists (user_id, spotify_id, name, description, source, tracks_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, spotify_id, name, description, source, json.dumps(tracks), time.time()),
        )
        await db.commit()
        return cur.lastrowid


async def get_playlists(user_id: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM playlists WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["tracks"] = json.loads(d["tracks_json"])
        del d["tracks_json"]
        result.append(d)
    return result


async def save_discovery_tracks(tracks: list[dict]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO discovery_tracks (id, tracks_json, updated_at)
               VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 tracks_json=excluded.tracks_json,
                 updated_at=excluded.updated_at""",
            (json.dumps(tracks), time.time()),
        )
        await db.commit()


async def get_discovery_tracks() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT tracks_json FROM discovery_tracks WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
    return json.loads(row[0]) if row else []


async def save_discovery_rating(track_uri: str, rating: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO discovery_ratings (track_uri, rating, rated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(track_uri) DO UPDATE SET
                 rating=excluded.rating,
                 rated_at=excluded.rated_at""",
            (track_uri, rating, time.time()),
        )
        await db.commit()


async def get_discovery_ratings() -> dict[str, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT track_uri, rating FROM discovery_ratings"
        ) as cur:
            rows = await cur.fetchall()
    return {row[0]: row[1] for row in rows}


async def clear_discovery_ratings() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM discovery_ratings")
        await db.commit()


async def save_conversation(user_id: str, history: list) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO conversations (user_id, history_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 history_json=excluded.history_json,
                 updated_at=excluded.updated_at""",
            (user_id, json.dumps(history), time.time()),
        )
        await db.commit()


async def get_conversation(user_id: str) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT history_json FROM conversations WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return json.loads(row[0]) if row else []


async def clear_conversation(user_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
        await db.commit()

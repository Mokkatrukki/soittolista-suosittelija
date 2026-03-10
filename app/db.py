"""SQLite-tietokanta — token-hallinta."""
import time
import aiosqlite

DB_PATH = "data/app.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS tokens (
                user_id       TEXT PRIMARY KEY,
                access_token  TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
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

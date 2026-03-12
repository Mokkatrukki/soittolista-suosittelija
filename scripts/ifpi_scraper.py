"""
IFPI Finland chart scraper — hakee Suomen viralliset listat ifpi.fi:stä SQLiteen.

URL-kaava:
  https://ifpi.fi/lista/singlet/{year}/{week}/
  https://ifpi.fi/lista/albumit/{year}/{week}/
  https://ifpi.fi/lista/radio/{year}/{week}/

Käyttö:
  uv run python scripts/ifpi_scraper.py                          # 2000-2025, kaikki chartit
  uv run python scripts/ifpi_scraper.py --years 2000-2005        # vuosiväli
  uv run python scripts/ifpi_scraper.py --charts singlet albumit # vain nämä
  uv run python scripts/ifpi_scraper.py --years 2002 --weeks 1-10
  uv run python scripts/ifpi_scraper.py --status                 # näytä mitä on jo haettu

Ominaisuudet:
  - Jatkaa aiemmasta (skipataan jo scrapetut viikot)
  - Satunnaiset viiveet + User-Agent rotaatio
  - Async httpx, max 2 rinnakkaista pyyntöä
  - Tallentaa data/ifpi_charts.db:hen
"""

import asyncio
import argparse
import random
import re
import sqlite3
import time
from datetime import datetime
from html import unescape
from pathlib import Path

import httpx

DB_PATH = Path(__file__).parent.parent / "data" / "ifpi_charts.db"

CHART_TYPES = ["singlet", "albumit", "radio"]

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


# ---------------------------------------------------------------------------
# Tietokanta
# ---------------------------------------------------------------------------

def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chart_entries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            chart_type      TEXT    NOT NULL,
            year            INTEGER NOT NULL,
            week            INTEGER NOT NULL,
            position        INTEGER NOT NULL,
            artist          TEXT    NOT NULL,
            title           TEXT    NOT NULL,
            label           TEXT,
            weeks_on_chart  INTEGER,
            scraped_at      TEXT    DEFAULT (datetime('now')),
            UNIQUE(chart_type, year, week, position)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scraped_weeks (
            chart_type  TEXT    NOT NULL,
            year        INTEGER NOT NULL,
            week        INTEGER NOT NULL,
            entries     INTEGER NOT NULL,
            scraped_at  TEXT    DEFAULT (datetime('now')),
            PRIMARY KEY (chart_type, year, week)
        )
    """)
    conn.commit()
    return conn


def is_scraped(conn: sqlite3.Connection, chart_type: str, year: int, week: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM scraped_weeks WHERE chart_type=? AND year=? AND week=?",
        (chart_type, year, week)
    ).fetchone()
    return row is not None


def save_entries(conn: sqlite3.Connection, chart_type: str, year: int, week: int,
                 entries: list[dict]) -> None:
    conn.executemany(
        """INSERT OR IGNORE INTO chart_entries
           (chart_type, year, week, position, artist, title, label, weeks_on_chart)
           VALUES (:chart_type, :year, :week, :position, :artist, :title, :label, :weeks_on_chart)""",
        [{**e, "chart_type": chart_type, "year": year, "week": week} for e in entries]
    )
    conn.execute(
        "INSERT OR REPLACE INTO scraped_weeks (chart_type, year, week, entries) VALUES (?,?,?,?)",
        (chart_type, year, week, len(entries))
    )
    conn.commit()


def show_status(conn: sqlite3.Connection) -> None:
    print("\n=== Scrapettu data ===")
    for row in conn.execute("""
        SELECT chart_type, MIN(year), MAX(year), COUNT(*) as weeks,
               SUM(entries) as total
        FROM scraped_weeks
        GROUP BY chart_type ORDER BY chart_type
    """):
        print(f"  {row[0]:10s}  {row[1]}–{row[2]}  {row[3]} viikkoa  {row[4]} merkintää")

    print("\n=== Puuttuvat viikot (esimerkki, max 3) ===")
    for chart in CHART_TYPES:
        missing = conn.execute("""
            WITH RECURSIVE weeks(y,w) AS (
                SELECT 2000, 1
                UNION ALL
                SELECT CASE WHEN w=52 THEN y+1 ELSE y END,
                       CASE WHEN w=52 THEN 1 ELSE w+1 END
                FROM weeks WHERE y < 2026
            )
            SELECT y, w FROM weeks
            WHERE NOT EXISTS (
                SELECT 1 FROM scraped_weeks s
                WHERE s.chart_type=? AND s.year=y AND s.week=w
            )
            LIMIT 3
        """, (chart,)).fetchall()
        if missing:
            print(f"  {chart}: {missing}")


# ---------------------------------------------------------------------------
# HTML-parsinta
# ---------------------------------------------------------------------------

def strip_tags(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_chart_page(html: str) -> list[dict]:
    entries = []

    # Jokainen lista-alkio alkaa chart-position -elementillä
    # Jaetaan sivu "lohkoihin" jokaisen position-elementin kohdalta
    blocks = re.split(r'<strong\s+class="chart-position">', html)

    for block in blocks[1:]:  # ensimmäinen on header-junk
        # Sija: "1." tai "12."
        pos_match = re.match(r"(\d+)\.", block)
        if not pos_match:
            continue
        position = int(pos_match.group(1))

        # Artisti(t): <div class="chart-artist">...</div>
        artist_match = re.search(
            r'<div\s+class="chart-artist">(.*?)</div>', block, re.DOTALL
        )
        artist = strip_tags(artist_match.group(1)) if artist_match else ""
        # Useampi artisti erotettu " & " tai " feat. " — yhdistetään
        artist = re.sub(r"\s+", " ", artist)

        # Kappale/albumi: <div class="chart-title">...</div>
        title_match = re.search(
            r'<div\s+class="chart-title">(.*?)</div>', block, re.DOTALL
        )
        title_html = title_match.group(1) if title_match else ""
        # Suositaan title-attribuuttia jos se on olemassa
        title_attr = re.search(r'title="([^"]+)"', title_html)
        title = title_attr.group(1) if title_attr else strip_tags(title_html)

        # Levy-yhtiö: <div class="chart-label">...</div>
        label_match = re.search(
            r'<div\s+class="chart-label">(.*?)</div>', block, re.DOTALL
        )
        label = strip_tags(label_match.group(1)) if label_match else ""

        # Viikkoja listalla: <div class="chart-woc" ...>N</div>
        woc_match = re.search(
            r'<div\s+class="chart-woc"[^>]*>(\d+)</div>', block, re.DOTALL
        )
        woc = int(woc_match.group(1)) if woc_match else None

        if not artist or not title:
            continue

        entries.append({
            "position": position,
            "artist": artist,
            "title": title,
            "label": label or None,
            "weeks_on_chart": woc,
        })

    return entries


# ---------------------------------------------------------------------------
# Async fetcher
# ---------------------------------------------------------------------------

async def fetch_week(
    client: httpx.AsyncClient,
    chart_type: str,
    year: int,
    week: int,
    semaphore: asyncio.Semaphore,
) -> list[dict] | None:
    url = f"https://ifpi.fi/lista/{chart_type}/{year}/{week}/"
    async with semaphore:
        try:
            headers = {"User-Agent": random.choice(USER_AGENTS)}
            r = await client.get(url, headers=headers, timeout=20, follow_redirects=True)
            if r.status_code == 404:
                return []  # Viikko ei ole olemassa
            r.raise_for_status()
            return parse_chart_page(r.text)
        except httpx.HTTPStatusError as e:
            print(f"  ! HTTP {e.response.status_code}: {url}")
            return None
        except Exception as e:
            print(f"  ! Virhe {url}: {e}")
            return None


# ---------------------------------------------------------------------------
# Päälooppi
# ---------------------------------------------------------------------------

async def scrape(
    years: list[int],
    weeks: list[int],
    charts: list[str],
    conn: sqlite3.Connection,
    delay_min: float = 1.0,
    delay_max: float = 3.5,
) -> None:
    semaphore = asyncio.Semaphore(2)  # max 2 rinnakkaista pyyntöä

    total_weeks = len(years) * len(weeks) * len(charts)
    done = 0
    skipped = 0

    async with httpx.AsyncClient() as client:
        for chart in charts:
            for year in years:
                for week in weeks:
                    done += 1

                    if is_scraped(conn, chart, year, week):
                        skipped += 1
                        continue

                    print(f"[{done}/{total_weeks}] {chart} {year}/vk{week:02d} ...", end=" ", flush=True)
                    entries = await fetch_week(client, chart, year, week, semaphore)

                    if entries is None:
                        print("VIRHE")
                        continue
                    elif len(entries) == 0:
                        print("tyhjä/ei löydy")
                        save_entries(conn, chart, year, week, [])
                    else:
                        save_entries(conn, chart, year, week, entries)
                        print(f"{len(entries)} kpl")

                    # Satunnainen viive — näyttää luonnollisemmalta
                    delay = random.uniform(delay_min, delay_max)
                    # Satunnaisesti pidempi tauko ~15% ajasta
                    if random.random() < 0.15:
                        delay += random.uniform(2.0, 5.0)
                    await asyncio.sleep(delay)

    print(f"\nValmis. Skipattu (jo haettu): {skipped}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_range(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(s)]


def main() -> None:
    parser = argparse.ArgumentParser(description="IFPI Finland chart scraper")
    parser.add_argument("--years", default="2000-2025", help="Vuosi tai väli, esim. 2000-2005")
    parser.add_argument("--weeks", default="1-52", help="Viikkonumero tai väli, esim. 1-52")
    parser.add_argument("--charts", nargs="+", default=CHART_TYPES,
                        choices=CHART_TYPES, help="Haettavat chartit")
    parser.add_argument("--delay-min", type=float, default=1.0, help="Min viive sekunteina")
    parser.add_argument("--delay-max", type=float, default=3.5, help="Max viive sekunteina")
    parser.add_argument("--status", action="store_true", help="Näytä DB:n tila ja poistu")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite-tiedoston polku")
    args = parser.parse_args()

    conn = init_db(Path(args.db))

    if args.status:
        show_status(conn)
        conn.close()
        return

    years = parse_range(args.years)
    weeks = parse_range(args.weeks)

    print(f"Haetaan: {args.charts}")
    print(f"Vuodet:  {years[0]}–{years[-1]}  ({len(years)} vuotta)")
    print(f"Viikot:  {weeks[0]}–{weeks[-1]}  ({len(weeks)} viikkoa)")
    print(f"Yhteensä max {len(years) * len(weeks) * len(args.charts)} pyyntöä")
    print(f"DB: {args.db}\n")

    start = time.time()
    asyncio.run(scrape(years, weeks, args.charts, conn, args.delay_min, args.delay_max))
    elapsed = time.time() - start
    print(f"Aika: {elapsed/60:.1f} min")
    conn.close()


if __name__ == "__main__":
    main()

"""
Finnish Charts MCP-palvelin.

Dataa kahdesta lähteestä (automaattinen valinta):
  1. ifpi_charts.db  — viikottaiset IFPI-listat (singlet, albumit, radio)
  2. Blogi-scraper   — vuosittaiset top-listat (fallback jos DB tyhjä)

Työkalut:
  - get_year_top_tracks      — vuoden top-kappaleet (aggregoitu)
  - get_week_chart           — yksittäisen viikon lista
  - search_chart_history     — hae artisti/kappaleen charttihistoria
  - get_era_hits             — aikakauden hitit (esim. 2000-2005 top 50)
"""

import os
import sys
import sqlite3
import asyncio
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("finnish_charts")

DB_PATH = Path(__file__).parent.parent / "data" / "ifpi_charts.db"
BLOG_SCRAPER = Path(__file__).parent.parent / "scripts" / "finnish_charts.py"


# ---------------------------------------------------------------------------
# DB-apufunktiot
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_has_data(conn: sqlite3.Connection, chart_type: str = "singlet") -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM scraped_weeks WHERE chart_type=?", (chart_type,)
    ).fetchone()
    return row[0] > 0


# ---------------------------------------------------------------------------
# Blogi-fallback (scripts/finnish_charts.py)
# ---------------------------------------------------------------------------

async def _blog_fetch_year(year: int) -> list[dict]:
    """Hakee vuoden top-kappaleet blogista asynkronisesti."""
    import httpx, re
    from html import unescape

    url = f"https://suomenvuosilistat.blogspot.com/2015/09/suurimmat-hitit-{year}.html"

    def strip_tags(s):
        return unescape(re.sub(r"<[^>]+>", "", s)).strip()

    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        r = await client.get(url, timeout=15, follow_redirects=True)
        r.raise_for_status()
        html = r.text

    tracks = []
    for row in re.finditer(r"<tr>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.DOTALL | re.IGNORECASE)
        if len(cells) < 3:
            continue
        rank_text = strip_tags(cells[0])
        artist = strip_tags(cells[1])
        title = strip_tags(cells[2])
        if not rank_text.isdigit() or not artist or not title:
            continue
        tracks.append({
            "rank": int(rank_text),
            "artist": artist,
            "title": title,
            "year": year,
            "source": "blog",
        })
    return tracks


# ---------------------------------------------------------------------------
# Työkalut
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_year_top_tracks(
    year: int,
    limit: int = 40,
    chart_type: str = "singlet",
) -> list[dict]:
    """Hae vuoden suosituimmat kappaleet Suomessa.

    Käyttää IFPI-dataa jos saatavilla (viikkosijojen aggregaatio),
    muuten hakee vuositason top-listan blogista.

    Args:
        year: Vuosi, esim. 2002
        limit: Kuinka monta kappaletta palautetaan (max 100)
        chart_type: singlet | albumit | radio
    """
    conn = get_conn()

    if conn and db_has_data(conn, chart_type):
        # Aggregoi viikkosijojen perusteella: lasketaan pisteet
        # Sija 1 = 50 pistettä, sija 2 = 49, ... sija 50 = 1
        rows = conn.execute("""
            SELECT artist, title,
                   COUNT(*) as weeks_on_chart,
                   SUM(CASE WHEN position <= 50 THEN 51 - position ELSE 0 END) as score,
                   MIN(position) as peak_position,
                   MIN(week) as first_week
            FROM chart_entries
            WHERE chart_type = ? AND year = ?
            GROUP BY artist, title
            ORDER BY score DESC, weeks_on_chart DESC
            LIMIT ?
        """, (chart_type, year, limit)).fetchall()
        conn.close()

        return [
            {
                "rank": i + 1,
                "artist": r["artist"],
                "title": r["title"],
                "score": r["score"],
                "weeks_on_chart": r["weeks_on_chart"],
                "peak_position": r["peak_position"],
                "year": year,
                "source": "ifpi",
            }
            for i, r in enumerate(rows)
        ]

    if conn:
        conn.close()

    # Fallback: blogi
    tracks = await _blog_fetch_year(year)
    return tracks[:limit]


@mcp.tool()
async def get_week_chart(
    year: int,
    week: int,
    chart_type: str = "singlet",
    limit: int = 20,
) -> list[dict]:
    """Hae tietyn viikon virallinen lista.

    Args:
        year: Vuosi, esim. 2003
        week: Viikkonumero 1-52
        chart_type: singlet | albumit | radio
        limit: Kuinka monta sijaa palautetaan
    """
    conn = get_conn()
    if not conn:
        return [{"error": "IFPI-tietokanta ei saatavilla. Aja ensin: uv run python scripts/ifpi_scraper.py"}]

    if not db_has_data(conn, chart_type):
        conn.close()
        return [{"error": f"Ei dataa chartille '{chart_type}'. Aja scraper."}]

    rows = conn.execute("""
        SELECT position, artist, title, label, weeks_on_chart
        FROM chart_entries
        WHERE chart_type=? AND year=? AND week=?
        ORDER BY position
        LIMIT ?
    """, (chart_type, year, week, limit)).fetchall()
    conn.close()

    if not rows:
        return [{"error": f"Viikkoa {year}/vk{week} ei löydy DB:stä. Ehkä ei vielä scrapattu."}]

    return [
        {
            "position": r["position"],
            "artist": r["artist"],
            "title": r["title"],
            "label": r["label"],
            "weeks_on_chart": r["weeks_on_chart"],
            "year": year,
            "week": week,
            "chart_type": chart_type,
        }
        for r in rows
    ]


@mcp.tool()
async def search_chart_history(
    query: str,
    chart_type: str = "singlet",
    year_from: int = 2000,
    year_to: int = 2025,
) -> list[dict]:
    """Hae artistin tai kappaleen charttihistoria.

    Args:
        query: Haettava artisti tai kappaleen nimi (osittainen haku)
        chart_type: singlet | albumit | radio
        year_from: Alkaen vuodesta
        year_to: Päättyen vuoteen
    """
    conn = get_conn()
    if not conn or not db_has_data(conn, chart_type):
        return [{"error": "IFPI-tietokanta ei saatavilla tai tyhjä."}]

    rows = conn.execute("""
        SELECT artist, title, year, week, position, weeks_on_chart
        FROM chart_entries
        WHERE chart_type=?
          AND year BETWEEN ? AND ?
          AND (artist LIKE ? OR title LIKE ?)
        ORDER BY year, week, position
        LIMIT 200
    """, (chart_type, year_from, year_to, f"%{query}%", f"%{query}%")).fetchall()
    conn.close()

    return [
        {
            "artist": r["artist"],
            "title": r["title"],
            "year": r["year"],
            "week": r["week"],
            "position": r["position"],
            "weeks_on_chart": r["weeks_on_chart"],
        }
        for r in rows
    ]


@mcp.tool()
async def get_era_hits(
    year_from: int = 2000,
    year_to: int = 2005,
    limit: int = 50,
    chart_type: str = "singlet",
    finnish_only: bool = False,
) -> list[dict]:
    """Hae aikakauden suurimmat hitit aggregoituna usealta vuodelta.

    Hyvä nostalgialistatyökalulle: palauttaa esim. 2000-2005 top 50 hitit.

    Args:
        year_from: Alkuvuosi
        year_to: Loppuvuosi
        limit: Kuinka monta kappaletta (max 200)
        chart_type: singlet | albumit | radio
        finnish_only: Jos True, yrittää suodattaa vain suomalaiset artistit
    """
    conn = get_conn()

    if conn and db_has_data(conn, chart_type):
        rows = conn.execute("""
            SELECT artist, title,
                   COUNT(DISTINCT year) as years_charted,
                   COUNT(*) as total_weeks,
                   SUM(CASE WHEN position <= 50 THEN 51 - position ELSE 0 END) as score,
                   MIN(position) as peak_position,
                   MIN(year) as first_year
            FROM chart_entries
            WHERE chart_type=? AND year BETWEEN ? AND ?
            GROUP BY artist, title
            ORDER BY score DESC, total_weeks DESC
            LIMIT ?
        """, (chart_type, year_from, year_to, limit * 2)).fetchall()
        conn.close()

        results = [
            {
                "rank": i + 1,
                "artist": r["artist"],
                "title": r["title"],
                "score": r["score"],
                "peak_position": r["peak_position"],
                "total_weeks": r["total_weeks"],
                "years_charted": r["years_charted"],
                "first_year": r["first_year"],
                "source": "ifpi",
            }
            for i, r in enumerate(rows)
        ]
        return results[:limit]

    if conn:
        conn.close()

    # Fallback: blogi, kaikki vuodet yhteen
    all_tracks: list[dict] = []
    for year in range(year_from, year_to + 1):
        try:
            tracks = await _blog_fetch_year(year)
            all_tracks.extend(tracks)
            await asyncio.sleep(0.5)
        except Exception:
            continue

    # Aggregoi pisteyttämällä: sija 1 = 75 pistettä jne.
    scores: dict[tuple, dict] = {}
    for t in all_tracks:
        key = (t["artist"].lower(), t["title"].lower())
        pts = max(0, 76 - t["rank"])
        if key not in scores:
            scores[key] = {**t, "score": 0, "peak_position": t["rank"], "total_weeks": 0}
        scores[key]["score"] += pts
        scores[key]["peak_position"] = min(scores[key]["peak_position"], t["rank"])
        scores[key]["total_weeks"] += 1

    ranked = sorted(scores.values(), key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(ranked):
        r["rank"] = i + 1
    return ranked[:limit]


@mcp.tool()
async def get_week_range_hits(
    week_from: int,
    week_to: int,
    year_from: int = 2000,
    year_to: int = 2025,
    limit: int = 50,
    chart_type: str = "singlet",
    peaked_in_range: bool = False,
) -> list[dict]:
    """Hae tietyn viikkovälin suurimmat hitit — esim. kesä (22-35) tai joulu (48-52).

    Args:
        week_from: Alkuviikko (1-52), esim. 22 kesäkuun alku
        week_to: Loppuviikko (1-52), esim. 35 elokuun loppu
        year_from: Alkaen vuodesta
        year_to: Päättyen vuoteen
        limit: Kuinka monta kappaletta palautetaan (max 200)
        chart_type: singlet | albumit | radio
        peaked_in_range: Jos True, palauttaa vain kappaleet joiden huippu osui tälle viikkovälille
    """
    conn = get_conn()
    if not conn or not db_has_data(conn, chart_type):
        return [{"error": "IFPI-tietokanta ei saatavilla tai tyhjä."}]

    if peaked_in_range:
        # Vain kappaleet joiden paras sijoitus saavutettiin viikkovälillä
        rows = conn.execute("""
            SELECT artist, title,
                   COUNT(DISTINCT year) as years_charted,
                   COUNT(*) as total_weeks,
                   SUM(CASE WHEN position <= 50 THEN 51 - position ELSE 0 END) as score,
                   MIN(position) as peak_position,
                   MIN(year) as first_year
            FROM chart_entries
            WHERE chart_type=? AND year BETWEEN ? AND ?
              AND week BETWEEN ? AND ?
            GROUP BY artist, title
            HAVING peak_position = (
                SELECT MIN(e2.position) FROM chart_entries e2
                WHERE e2.chart_type = chart_entries.chart_type
                  AND e2.artist = chart_entries.artist
                  AND e2.title = chart_entries.title
                  AND e2.year BETWEEN ? AND ?
            )
            ORDER BY score DESC, total_weeks DESC
            LIMIT ?
        """, (chart_type, year_from, year_to, week_from, week_to,
              year_from, year_to, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT artist, title,
                   COUNT(DISTINCT year) as years_charted,
                   COUNT(*) as total_weeks,
                   SUM(CASE WHEN position <= 50 THEN 51 - position ELSE 0 END) as score,
                   MIN(position) as peak_position,
                   MIN(year) as first_year
            FROM chart_entries
            WHERE chart_type=? AND year BETWEEN ? AND ?
              AND week BETWEEN ? AND ?
            GROUP BY artist, title
            ORDER BY score DESC, total_weeks DESC
            LIMIT ?
        """, (chart_type, year_from, year_to, week_from, week_to, limit)).fetchall()

    conn.close()

    return [
        {
            "rank": i + 1,
            "artist": r["artist"],
            "title": r["title"],
            "score": r["score"],
            "peak_position": r["peak_position"],
            "total_weeks": r["total_weeks"],
            "years_charted": r["years_charted"],
            "first_year": r["first_year"],
            "source": "ifpi",
        }
        for i, r in enumerate(rows)
    ]


@mcp.tool()
async def get_chart_stats(year_from: int = 2000, year_to: int = 2025) -> dict:
    """Näytä tietokannan tila: mitä dataa on saatavilla.

    Args:
        year_from: Alkaen vuodesta
        year_to: Päättyen vuoteen
    """
    conn = get_conn()
    if not conn:
        return {
            "db_available": False,
            "db_path": str(DB_PATH),
            "message": "Tietokantaa ei löydy. Aja: uv run python scripts/ifpi_scraper.py",
        }

    stats = {}
    for chart in ["singlet", "albumit", "radio"]:
        row = conn.execute("""
            SELECT COUNT(*) as weeks, SUM(entries) as entries,
                   MIN(year) as min_year, MAX(year) as max_year
            FROM scraped_weeks
            WHERE chart_type=? AND year BETWEEN ? AND ?
        """, (chart, year_from, year_to)).fetchone()
        stats[chart] = {
            "weeks_scraped": row["weeks"],
            "total_entries": row["entries"] or 0,
            "year_range": f"{row['min_year']}–{row['max_year']}" if row["min_year"] else "ei dataa",
        }

    conn.close()
    return {
        "db_available": True,
        "db_path": str(DB_PATH),
        "charts": stats,
        "tip": "Aja 'uv run python scripts/ifpi_scraper.py' kerätäksesi lisää dataa",
    }


if __name__ == "__main__":
    mcp.run()

"""
Hakee Suomen vuosihittilistoja suomenvuosilistat.blogspot.com -blogista.
Käyttää vain httpx + stdlib, ei raskaita riippuvuuksia.

Käyttö:
    uv run python scripts/finnish_charts.py --years 2000-2005
    uv run python scripts/finnish_charts.py --years 2000-2005 --top 20
    uv run python scripts/finnish_charts.py --years 2002 --out data/charts.json
"""

import re
import json
import time
import argparse
from html import unescape

import httpx

BASE_URL = "https://suomenvuosilistat.blogspot.com/2015/09/suurimmat-hitit-{year}.html"


def fetch_year(year: int, client: httpx.Client) -> list[dict]:
    url = BASE_URL.format(year=year)
    r = client.get(url, timeout=15, follow_redirects=True)
    r.raise_for_status()
    html = r.text

    # Rakenne: <tr><td><b>SIJA</b></td><td>ARTISTI</td><td>KAPPALE</td></tr>
    def strip_tags(s: str) -> str:
        return unescape(re.sub(r'<[^>]+>', '', s)).strip()

    tracks = []
    for row in re.finditer(r'<tr>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row.group(1), re.DOTALL | re.IGNORECASE)
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
        })

    return tracks


def parse_year_range(s: str) -> list[int]:
    if "-" in s:
        parts = s.split("-")
        return list(range(int(parts[0]), int(parts[1]) + 1))
    return [int(s)]


def main():
    parser = argparse.ArgumentParser(description="Suomen vuosihittilistojen scraper")
    parser.add_argument("--years", default="2000-2005", help="Vuosi tai vuosiväli, esim. 2000-2005 tai 2002")
    parser.add_argument("--top", type=int, default=None, help="Kuinka monta kappaletta per vuosi (oletus: kaikki)")
    parser.add_argument("--out", default=None, help="Tallenna JSON-tiedostoon (oletus: tulostus terminaaliin)")
    args = parser.parse_args()

    years = parse_year_range(args.years)
    all_tracks = []

    with httpx.Client(headers={"User-Agent": "Mozilla/5.0 (compatible; chart-scraper/1.0)"}) as client:
        for year in years:
            print(f"Haetaan vuosi {year}...")
            try:
                tracks = fetch_year(year, client)
                if args.top:
                    tracks = tracks[: args.top]
                all_tracks.extend(tracks)
                print(f"  → {len(tracks)} kappaletta")
            except httpx.HTTPStatusError as e:
                print(f"  ! HTTP {e.response.status_code} vuodelle {year}, ohitetaan")
            except Exception as e:
                print(f"  ! Virhe vuodelle {year}: {e}")
            time.sleep(0.5)  # Kohteliaisuusviive

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(all_tracks, f, ensure_ascii=False, indent=2)
        print(f"\nTallennettu {len(all_tracks)} kappaletta → {args.out}")
    else:
        for t in all_tracks:
            print(f"{t['year']} #{t['rank']:3d}  {t['artist']} — {t['title']}")


if __name__ == "__main__":
    main()

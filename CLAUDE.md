# Soittolista-suosittelija — Claude Code ohjeet

## Projekti lyhyesti

Web-sovellus joka luo AI-avusteisia Spotify-soittolistoja. Käyttäjä kirjautuu Spotify-tilillä,
kertoo millaista musiikkia haluaa, ja sovellus rakentaa soittolistan yhdistämällä Last.fm-dataa,
Yle Areena -ohjelmistoja ja Gemini-mallilla tehtävää orkestrointia.

## Tekninen stack

- **Python 3.12+** + `uv` pakettienhallintaan
- **FastAPI** + **Jinja2** — web-sovellus + HTML-templatet
- **HTMX** — reaktiiviset UI-komponentit ilman JS-frameworkia
- **SQLite** + **aiosqlite** — soittolista- ja käyttäjädata
- **httpx** — async HTTP Spotify- ja Last.fm-kutsuille
- **DSPy** — LLM-orkestrointi (prompt-optimointi, loopitus)
- **google-genai** — Gemini 2.5 Flash suoraan raakakutsuihin
- **python-dotenv** — ympäristömuuttujat

## Projektirakenne

```
app/
  main.py          ← FastAPI-sovellus, reitit, lifespan
  auth.py          ← Spotify OAuth 2.0 (Authorization Code flow)
  db.py            ← SQLite-yhteys ja skeema (aiosqlite)
  models.py        ← Pydantic-mallit + tietokantarakenteet
  templates/       ← Jinja2-templatet
    base.html
    index.html
    playlist.html
  static/
    htmx.min.js
    style.css
ai/
  orchestrator.py  ← DSPy-pohjainen soittolistan rakentaja
  signatures.py    ← DSPy Signature -luokat
mcp_servers/
  spotify_mcp.py   ← Spotify MCP-palvelin (kehityskäyttöön)
  lastfm_mcp.py    ← Last.fm MCP-palvelin (kehityskäyttöön)
  HUOM: Hakemisto on mcp_servers/ eikä mcp/ — vältetään konflikti mcp-kirjaston kanssa
data/
  app.db           ← SQLite-tietokanta (ei versionhallintaan)
  examples.json    ← DSPy harjoitusesimerkit
pyproject.toml
.env               ← API-avaimet (ei versionhallintaan)
.mcp.json          ← Claude Code MCP-yhteydet
```

## Spotify Web API — helmikuu 2026 muutokset

Virallinen changelog: https://developer.spotify.com/changelog

**Toimivat endpointit (valitut):**
- `GET /me` — käyttäjäprofiili (country, email, product poistettu)
- `GET /me/top/{type}` — top artistit/kappaleet
- `GET /me/player/recently-played` — viimeksi kuunnellut
- `GET /me/playlists` — käyttäjän soittolistat
- `POST /me/playlists` — luo soittolista
- `GET /playlists/{id}/items` — soittolistan kappaleet, limit max=50
- `POST /playlists/{id}/items` — lisää kappaleita
- `GET /artists/{id}` — yksittäinen artisti
- `GET /artists/{id}/albums` — artistin albumit (käytä uusien julkaisujen hakuun)
- `GET /albums/{id}` — yksittäinen albumi
- `GET /albums/{id}/tracks` — albumin kappaleet
- `GET /search` — haku, limit max=10, default=5

**Poistettu (älä käytä):**
- `GET /browse/new-releases` — POISTETTU
- `GET /artists/{id}/top-tracks` — käytä Last.fm `artist.getTopTracks`
- `POST /users/{user_id}/playlists` — käytä `POST /me/playlists`
- `GET /playlists/{id}/tracks` — käytä `/items`
- `GET /tracks`, `GET /artists`, `GET /albums` (bulk) — poistettu
- `GET /browse/categories`, `GET /markets` — poistettu

**Poistetut kentät:**
- Track: `popularity`, `available_markets`, `external_ids`, `linked_from`
- Artist: `popularity`, `followers` (genres saattaa puuttua, käytä `.get()`)
- Album: `popularity`, `available_markets`, `external_ids`, `label`, `album_group`
- Playlist: `tracks` → uudelleennimetty `items`; `items.track` → `items.item`
- User: `country`, `email`, `product`, `followers`, `explicit_content`

**Hakustrategia (track-resolvointi):**
```python
# Haetaan yksi tietty kappale → limit=1 riittää
search?q=track:{name}+artist:{artist}&type=track&limit=1
# Rinnakkaishaut asyncio.gather():lla → nopea
```

## Spotify OAuth 2.0

Authorization Code flow + PKCE. Scopes joita tarvitaan:
```
playlist-read-private
playlist-modify-public
playlist-modify-private
user-read-private
user-top-read
user-read-recently-played
```

Token tallennetaan SQLiteen käyttäjäkohtaisesti. Refresh automaattisesti kun token vanhenee.

Callback URL: `http://127.0.0.1:8000/auth/callback`

Huom: Spotify ei hyväksy `localhost` — käytä aina `127.0.0.1` tai `[::1]`.

## Last.fm API

Ei OAuth-vaatimusta lukuoperaatioihin — pelkkä API-avain riittää.

Käytetyt metodit:
- `artist.getTopTracks` — artistin suosituimmat kappaleet (popularity-korvike)
- `tag.getTopTracks` — genren top-kappaleet
- `chart.getTopTracks` — globaali top
- `artist.getSimilar` — samanlaiset artistit

## AI-arkkitehtuuri

DSPy orkestrointi + Gemini 2.5 Flash:

```
Käyttäjän pyyntö
      ↓
DSPy: tulkitse pyyntö → PlaylistIntent
      ↓
Last.fm/Yle: hae kandidaattikappaleet (50-100 kpl)
      ↓
DSPy: suodata + järjestä → 30 parasta kandidaattia
      ↓
Spotify search: resolvo URI:t rinnakkain (asyncio.gather)
      ↓
DSPy: lopullinen järjestys + soittolistan nimi/kuvaus
      ↓
Spotify: luo playlist + lisää kappaleet
```

## MCP-palvelimet (kehityskäyttöön)

`mcp/spotify_mcp.py` ja `mcp/lastfm_mcp.py` ovat FastMCP-palvelimia joita
käytetään kehityksen aikana Claudella testaukseen. Ne eivät ole osa tuotantosovellusta.

`.mcp.json` sisältää molemmat palvelimet.

## Komennot

```bash
uv sync                          # asenna riippuvuudet
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload   # käynnistä dev-palvelin
uv run pytest                    # testit
uv run python mcp_servers/spotify_mcp.py # käynnistä Spotify MCP erikseen
```

## Periaatteet

- Yksinkertainen ensin — ei ylisuunnitella
- Jokainen integraatio testataan erikseen ennen yhdistämistä
- LLM rankkailee/suodattaa olemassa olevaa dataa — ei hallusinoi kappaleita
- Spotify-track resolvointi: AI ehdottaa "artisti - kappale" -pareja, Spotify-haku löytää URI:n
- HTMX-fragmentit palauttavat aina vain muuttuneen HTML-osan

## Git

- Commit-viesteissä ei mainita Claudea, Claude Codea tai Anthropicia
- Ei `Co-Authored-By: Claude` -rivejä

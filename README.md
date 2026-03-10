# Soittolista-suosittelija

Web-sovellus Spotify-soittolistojen rakentamiseen MCP-palvelinten avulla.
Käyttäjä kirjautuu Spotify-tilillä, sovellus hakee musiikkidataa Last.fm:stä,
ListenBrainzista, MusicBrainzista ja Discogsin kautta.

## Käynnistys

```bash
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Sovellus osoitteessa: http://127.0.0.1:8000

## Projektirakenne

```
app/              FastAPI-sovellus (OAuth, playlist-luonti, UI)
ai/resolver.py    Spotify URI -resolvoija (artisti+nimi → spotify:track:xxx)
mcp_servers/      MCP-palvelimet kehityskäyttöön (Claude Code)
data/app.db       SQLite-tietokanta
```

---

## MCP-palvelimet

Kaikki palvelimet käynnistyvät automaattisesti `.mcp.json`-konfiguraatiosta.
Kehityskäytössä näillä tutkitaan musiikkidataa ja rakennetaan soittolistoja.

---

## Tyypilliset työnkulut

### 1. Soittolista artistin tyyliin

```
1. find_similar_artists("Stam1na")
   → palauttaa tiered-listan: obvious / close / surprising

2. get_artist_tracks("Stam1na", flavor="album_spread", count=5)
   get_artist_tracks("Mokoma", flavor="album_spread", count=4)
   get_artist_tracks("Turmion Kätilöt", flavor="album_spread", count=4)
   (rinnakkain eri artisteille, jokaiselle sopiva count)

3. search_tracks_batch([{artist, title}, ...])
   → Spotify URI:t rinnakkain

4. create_playlist(name, description)
   add_tracks_to_playlist(playlist_id, uris)
```

### 2. Genresoittolista

```
1. tag_top_artists("suomi-rock", limit=15)
   tai tag_top_tracks("suomi-rock", limit=30)

2. get_artist_tracks(artist, flavor="hits", count=3)
   (per artisti rinnakkain — hae tutut kappaleet genreen tutustuessa)

3. search_tracks_batch + create_playlist + add_tracks_to_playlist
```

### 3. Uudet suomalaiset julkaisut

```
1. get_finnish_new_releases(year_month="2026-03")
   → albumilista: title, artists, date

2. discogs_newest_tracks(artist_name)
   tai get_artist_tracks(artist, flavor="newest")

3. search_tracks_batch + create_playlist + add_tracks_to_playlist
```

### 4. Yle Areena -ohjelman soittolista

```
1. list_music_shows()  →  hae sarjan ID

2. get_latest_show_tracks(series_id)
   tai get_episode_tracks(episode_id)

3. search_tracks_batch + create_playlist + add_tracks_to_playlist
```

### 5. Käyttäjän kuunteluhistoriaan perustuva soittolista

```
1. get_top_artists(time_range="medium_term")
   tai get_recently_played()

2. find_similar_artists(artist) per top-artisti

3. get_artist_tracks(artist, flavor="mixed") per samankaltainen artisti

4. search_tracks_batch + create_playlist + add_tracks_to_playlist
```

---

## MCP-palvelimien kuvaukset

### `artist_tracks` — Artistin kappalehaku eri fiiliksin

**Päätyökalu: `get_artist_tracks(artist, flavor, count=20)`**

Tämä on ensisijainen työkalu kun haetaan kappaleita artistille.
Kaikki flavorit ottavat artist-nimen suoraan — ei tarvita MBID:tä etukäteen.

| flavor | Milloin käyttää | Miten toimii |
|--------|----------------|--------------|
| `album_spread` | **Oletus** — monipuolinen lista | Jokaisen top-albumin sisältä 2-3 kpl, ohittaa globaalin top-3. Eri aikakausia ja tyylejä. |
| `hits` | Käyttäjä tutustuu artistiin, haluaa tunnettua | Last.fm globaali top playcountin mukaan |
| `deep_cuts` | "Ei niitä ilmeisiä", fanien suosikit | Ohittaa top-10 globaalisti, palauttaa sijat 11–30 |
| `hidden_gems` | Oikeasti harvinaiset, underground | ListenBrainz pop 0–30%, fallback album_spread |
| `newest` | "Mitä X on viimeksi julkaissut" | Discogs uusin albumi/EP + koko kapparelista |
| `mixed` | Sekä tuttua että uutta | ~25% hittejä + ~75% album_spread |

Muut työkalut:
- `discogs_artist_releases(artist_name, limit=15)` — diskografialista id:ineen
- `discogs_release_tracks(release_id)` — tietyn julkaisun kapparelista

---

### `music_intelligence` — Samankaltaisten artistien löytö

**Päätyökalu: `find_similar_artists(artist, mode, include_band_members)`**

Yhdistää usean lähteen signaalit: Last.fm L1+L2-graafi, ListenBrainz-kuunteludata,
genre-tagit, Discogs-label-tieto. Palauttaa tiered-listan:
- `obvious` — selkeästi samankaltaiset (score ≥ 25)
- `close` — lähipiiri (score 10–24)
- `surprising` — yllättävät yhteydet (score < 10)

| mode | Käyttö |
|------|--------|
| `blend` | Tasapainottaa tuttua ja uutta — **oletus** |
| `familiar` | Painottaa tunnettuja, selkeitä yhteyksiä |
| `discovery` | Löytää vähemmän tunnettuja (discovery score = similarity / log10(listeners)) |
| `all` | Kaikki löydetyt artistit ilman suodatusta |

---

### `lastfm` — Last.fm API

Perustyökalut artistien, kappaleiden ja genrejen tutkimiseen.

**Artisti:**
- `artist_info(artist)` — kuuntelijamäärät, tagit, bio
- `artist_top_tracks(artist, limit=20)` — suosituimmat kappaleet playcountilla
- `artist_top_tags(artist)` — genre-tagit
- `artist_similar(artist, limit=10)` — samankaltaiset artistit (Last.fm-signaali)
- `artist_search(artist)` — haku kirjoitusvirheiden korjaukseen

**Genre/Tag:**
- `tag_top_tracks(tag, limit=30)` — genren suosituimmat kappaleet
- `tag_top_artists(tag, limit=20)` — genren suosituimmat artistit
- `tag_similar(tag)` — samankaltaiset genret
- `tag_info(tag)` — tagin laatu (reach, taggings)

**Kappale:**
- `track_similar(artist, title, limit=20)` — samankaltaiset kappaleet
- `track_info(artist, title)` — kappaleen metatiedot
- `track_search(title)` — haku nimellä

**Listat:**
- `chart_top_tracks(limit=30)` — globaali top nyt
- `geo_top_tracks(country="Finland", limit=30)` — maakohtainen top

> **Huom:** `artist_top_tracks` palauttaa aina samat globaalit hitit.
> Käytä `get_artist_tracks` (artist_tracks MCP) monipuolisempaan hakuun.

---

### `musicbrainz` — MusicBrainz + ListenBrainz

**MusicBrainz (mb_*)** — MBID-haku ja artistisuhteet:
- `mb_artist_search(artist)` → palauttaa MBID:n (tarvitaan lb_* -kutsuihin)
- `mb_artist_relations(mbid)` — vaikutteet (influenced_by), bändijäsenet
- `mb_artist_tags(mbid)` — yhteisön äänestämät genre-tagit
- `mb_release_search(artist)` — diskografia

**ListenBrainz (lb_*)** — kuunteludata:
- `lb_similar_artists(mbid, count=20)` — samankaltaiset artistit kuunteluhistorian perusteella
- `lb_artist_popularity(mbids)` — suosiotilastot usealle artistille kerralla

> **Huom:** MusicBrainz rate limit 1 req/s (automatisoitu). MBID tarvitaan
> lb_*-kutsuihin — hae ensin `mb_artist_search`.

---

### `music_discovery` — Yle Areena + suomalaiset julkaisut

- `list_music_shows()` — tuetut Yle Areena -ohjelmat
- `get_latest_show_tracks(series_id)` — viimeisimmän jakson kappaleet
- `get_episode_tracks(episode_id)` — tietyn jakson kappaleet
- `get_finnish_new_releases(year_month="2026-03")` — uudet suomalaiset albumit
- `get_finland_top_songs(limit=25)` — Suomen Apple Music top

---

### `discogs` — Discogs-tietokanta

Diskografia, levy-yhtiöt, julkaisutiedot.

**Haku:**
- `search_releases(artist, title, genre, year)` — julkaisuhaku
- `search_artists(query)` — artistihaku
- `search_labels(query)` — levy-yhtiöhaku

**Tietyn julkaisun/artistin tiedot:**
- `get_release(release_id)` — julkaisun tiedot + kapparelista
- `get_artist(artist_id)` — artistin tiedot
- `get_artist_releases(artist_id, sort="year")` — diskografia
- `get_label_releases(label_id)` — labelin kaikki julkaisut

**Yhteisödata:**
- `get_community_release_rating(release_id)` — yhteisöarvio
- `get_release_stats(release_id)` — omistajamäärät

> **Huom:** `country`-filtteri search_releases:ssa EI toimi luotettavasti.
> Suomalaisten artistien löytämiseen: käytä label-pohjaista hakua
> (esim. Poko Rekords, Love Records) tai artistin ID:tä.

---

### `spotify` — Spotify Web API

**Käyttäjädata:**
- `get_my_profile()` — profiili
- `get_top_tracks(time_range)` — top-kappaleet (short/medium/long_term)
- `get_top_artists(time_range)` — top-artistit
- `get_recently_played()` — viimeksi kuunnellut

**Kappalehaku:**
- `search_track(artist, title)` → `{found, uri, name, artist, album}`
- `search_tracks_batch(tracks)` — useita kappaleita rinnakkain (max 20)

**Playlist-hallinta:**
- `create_playlist(name, description, public=False)` → `{id, uri}`
- `add_tracks_to_playlist(playlist_id, uris)` — lisää kappaleet (max 100 kerralla)
- `get_my_playlists()` — omat soittolistat
- `get_playlist_items(playlist_id)` — soittolistan kappaleet
- `remove_tracks_from_playlist(playlist_id, uris)` — poista kappaleet
- `update_playlist(playlist_id, name, description)` — muokkaa tietoja
- `get_artist_albums(artist_id)` — artistin albumit julkaisupäivän mukaan

---

## Spotify API — rajoitukset (helmikuu 2026)

**Poistetut endpointit (älä käytä):**
- `GET /browse/new-releases` → käytä `get_finnish_new_releases` tai Discogs
- `GET /artists/{id}/top-tracks` → käytä `artist_top_tracks` (Last.fm)
- `GET /tracks`, `/artists`, `/albums` (bulk) → poistettu

**Poistetut kentät:**
- Track: `popularity`, `available_markets`
- Artist: `popularity`, `followers` — `genres` saattaa puuttua → `.get("genres", [])`
- Playlist: `tracks` → `items`; `items.track` → `items.item`
- User: `country`, `email`, `product`

**Search:** limit max=10, käytä limit=1 yksittäisen kappaleen haussa.

---

## Ympäristömuuttujat (.env)

```env
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
LASTFM_API_KEY=...
DISCOGS_TOKEN=...
LB_TOKEN=...          # ListenBrainz-token (kasvattaa rate limittiä merkittävästi)
SECRET_KEY=...        # FastAPI session-avain
```

---

## API-avainten hankkiminen

### Spotify

1. Mene: https://developer.spotify.com/dashboard
2. Kirjaudu sisään Spotify-tunnuksilla
3. **Create app** → anna nimi ja kuvaus
4. Redirect URI: `http://127.0.0.1:8000/auth/callback` (tärkeä: ei localhost)
5. Kopioi **Client ID** ja **Client Secret** `.env`:iin

> Huom: Spotifyn kehittäjätili hyväksyy oletuksena vain 25 kutsuttua käyttäjää.
> Omaan käyttöön riittää — laajempaan jakeluun tarvitaan quota extension.

---

### Last.fm

1. Mene: https://www.last.fm/api/account/create
2. Kirjaudu Last.fm-tunnuksilla (luo tili jos ei ole)
3. Täytä **Application name** ja **Description** — muut kentät vapaaehtoisia
4. Kopioi **API key** `.env`:iin (`LASTFM_API_KEY`)

> API Secret ei tarvita — sovellus käyttää vain lukuoperaatioita.
> Last.fm API on ilmainen eikä vaadi maksutietoja.

---

### Discogs

1. Mene: https://www.discogs.com/settings/developers
2. Kirjaudu Discogs-tunnuksilla (luo tili jos ei ole)
3. Scrollaa kohtaan **Personal Access Tokens**
4. Klikkaa **Generate new token**
5. Kopioi token `.env`:iin (`DISCOGS_TOKEN`)

> Rate limit: 60 req/min autentikoituna, 25 req/min ilman tokenia.
> Discogs-tili on ilmainen.

---

### ListenBrainz

1. Mene: https://listenbrainz.org
2. Luo tili tai kirjaudu sisään
3. Mene: https://listenbrainz.org/settings/ → kohta **User Token**
4. Kopioi token `.env`:iin (`LB_TOKEN`)

> Ilman tokenia: ~60 req/h lb-radio-endpointille.
> Tokenilla: ~3600 req/h. Token on ilmainen.
> ListenBrainz on open source -projekti (MetaBrainz Foundation).

---

### SECRET_KEY (FastAPI-sessiot)

Generoi satunnainen merkkijono:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Kopioi tulos `.env`:iin. Pidä salainen — tällä salataan sessio-cookiet.

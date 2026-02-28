Käytä Discogs MCP:tä seuraavien ohjeiden mukaan.

## Työnjako: Discogs vs Last.fm

- **Discogs**: mitä on olemassa — labelit, kappalelista, julkaisuvuosi, harvinaisuus
- **Last.fm**: mikä on suosittua — top tracks, similar artists, genre charts

## Parametrit

Toimivat: `query`, `artist`, `style`, `genre`, `year`
**EI toimi**: `country` — ignoroidaan, älä luota siihen

## Hakustrategiat

**Suomenkieliset käännökset** — hae molemmilla nimillä:
```
query: "California Dreamin Kalajoen"
```
Discogs indeksoi kappalelistan sisällön, löytää `Kalajoen Hiekat = California Dreamin'`.

**Scenen kartoitus** — label-lähtöisesti:
`search_labels` → `get_label` → `get_label_releases` → uusia artistinimiä

**Albumin sisältö** — hae `get_release` ennen Spotify-hakua, valitse parhaat kappaleet kappalelista käsillä.

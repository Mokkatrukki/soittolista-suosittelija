Tee git-commit seuraavien sääntöjen mukaan:

## Tarkistukset ennen commitia

1. **Aja `git diff --staged`** — lue kaikki staged-muutokset läpi.

2. **Tarkista ettei repolle päädy salaisuuksia:**
   - Etsi staged-tiedostoista: API-avaimet, salasanat, tokenid, client_secret, private key -merkit
   - Vaaralliset tiedostonimet: `.env`, `*.key`, `*.pem`, `secrets.*`
   - Jos löytyy epäilyttävää, **keskeytä ja kerro käyttäjälle** — älä committaa

3. **Tarkista commit-viesti:**
   - Ei mainintoja: Claude, Claude Code, Anthropic, AI, LLM
   - Ei `Co-Authored-By` -rivejä
   - Viesti suomeksi tai englanniksi, lyhyt ja kuvaava

## Commitin tekeminen

- Aja `git status` nähdäksesi tilanteen
- Kysy käyttäjältä mitä halutaan committaa jos epäselvää
- Kirjoita commit-viesti joka kuvaa **mitä ja miksi** muutettiin
- Käytä: `git commit -m "viesti"`
- Älä pushaa — käyttäjä tekee sen itse

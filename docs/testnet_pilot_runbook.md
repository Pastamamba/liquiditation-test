# Testnet Pilot — Operatiivinen runbook

Tämä on STEP 15 (1. testnet-pilot) operatiivinen ohje. Pilot ajetaan
**Hyperliquid TESTNETILLÄ** oikeilla testnet-USDC-ordereilla, ei dry-runina.
Tavoite: 1h aktiivinen ajo, jonka jälkeen kirjoitetaan
`docs/testnet_pilot_report_001.md` ja päätetään, mennäänkö STEP 16:een
(parametrisäätö) vai STEP 17:ään (mainnet-mikropilot).

> **Tärkeä ero:** mainnet-pilotti ei kuulu STEP 15:een. Tämä on testnet.

---

## 1. Pre-flight checklist

Käy läpi joka kohta ennen kuin käynnistät botin. Jos joku puuttuu, älä jatka.

- [ ] **Hyperliquid testnet-tili luotu** osoitteessa `https://app.hyperliquid-testnet.xyz`
      (sama wallet voi olla mainnet-tili, mutta käytät testnetin UI:ta)
- [ ] **Testnet-USDC haettu** faucetista (≥ 200 USDC suositellaan, jotta
      cap=100 mahtuu mukavasti ja jää puskuria liikkua)
- [ ] **API wallet (agent) -avain generoitu**
      - Generoi UUSI EVM-wallet (esim. `eth_account.Account.create()` tai MetaMaskissa)
      - Approve agent master-walletista Hyperliquidin `approveAgent`-actionilla
        (UI: Settings → API → Generate API Wallet)
      - **EI master-walletin avain.** Tappiomaksimi rajoittuu testnetiin
- [ ] **`HL_PRIVATE_KEY` `.env`-tiedostossa**
      - `.env` EI ole gitattu (`git status` ei näytä sitä — varmista)
      - `cat .env` näyttää 64-merkkisen hex-stringin ilman `0x`-prefixiä,
        TAI `0x`-prefixin kanssa (lib hyväksyy molemmat)
- [ ] **`HL_API_WALLET_ADDRESS` `.env`-tiedostossa** (master-walletin osoite)
- [ ] **Pilot-config kopioitu päälle**
      ```powershell
      Copy-Item config.testnet.yaml config.yaml -Force
      ```
      Tarkista että `config.yaml` sisältää: `network: testnet`, `dry_run: false`,
      `capital_usdc: 100`. Päivitä `api_wallet_address` master-osoitteeksi.
- [ ] **Telegram-botti toimii**
      - `TELEGRAM_BOT_TOKEN` ja `TELEGRAM_CHAT_ID` `.env`:issä
      - Lähetä manuaalinen testiviesti BotFather:ille luodulle botille,
        varmista että botti vastaa `/start`-komentoon omasta chatistasi
- [ ] **`logs/`- ja `data/`-kansiot olemassa**
      ```powershell
      New-Item -ItemType Directory -Force -Path logs, data | Out-Null
      ```
- [ ] **Riippuvuudet asennettu venv:iin**
      ```powershell
      .\.venv\Scripts\Activate.ps1
      python -c "import hyperliquid, telegram, aiosqlite, structlog; print('ok')"
      ```
- [ ] **Testit ajettu vihreänä viimeksi** (`pytest -q`)
- [ ] **Kill-switch testit dry-runina** — jos haluat lisävarmuutta, aja
      `pytest tests/test_risk.py -v` ja tarkista että hard-stoppi laukeaa testissä
- [ ] **Tiedät miten sammutat botin** (ks. §5 ja §6)

---

## 2. Käynnistys (Windows / PowerShell)

```powershell
# venv aktivoinnin jälkeen
.\.venv\Scripts\Activate.ps1

# Käynnistä botti taustaprosessina, ohjaa stdout+stderr lokiin
$bot = Start-Process -FilePath python `
    -ArgumentList '-m', 'src.main' `
    -RedirectStandardOutput logs\stdout_001.log `
    -RedirectStandardError  logs\stderr_001.log `
    -PassThru -WindowStyle Hidden
$bot.Id | Out-File bot.pid -Encoding ascii
"Bot PID: $($bot.Id)"
```

Ensimmäinen vahvistus tulee Telegramiin (banner + "BOT STARTED"). Jos sitä
ei tule 30 sekunnissa, sammuta heti (ks. §6) ja katso `logs\stderr_001.log`.

---

## 3. Aktiivinen seuranta (1 tunti)

Pidä kolme ikkunaa auki:

1. **Telegram** — botti lähettää tärkeät tapahtumat (fillit, killit, varoitukset)
   - Pyydä `/status` joka 10 minuutti
   - Pyydä `/pnl`, `/inventory`, `/orders` jos haluat tarkemman kuvan
2. **PowerShell-ikkuna lokin tail:lle**
   ```powershell
   Get-Content logs\mm_bot_testnet_001.log -Wait -Tail 50
   ```
3. **Hyperliquid testnet web-UI** (`https://app.hyperliquid-testnet.xyz`)
   - Tarkista että orderit näkyvät order book:issa
   - Tarkista että position päivittyy fillien jälkeen
   - Tarkista että maker-fee menee oikein

### Mitä etsiä lokeista

- ✅ `INFO market_data l2_book updated mid=...` säännöllisesti (joka <2 s)
- ✅ `INFO order_manager placed bid=... ask=...` joka quote_refresh_ms (3 s)
- ✅ `INFO inventory fill_applied side=...` kun fillaa
- ⚠️ `WARNING risk vol_halt active` — vol-halt aktivoituu, ok lyhytaikaisesti
- ❌ `ERROR ws.disconnected` toistuvasti — verkko-ongelma
- ❌ `ERROR hl_client.api_error` toistuvasti — rate limit tai SDK-bugi
- ❌ Mikä tahansa `Traceback` exception-spam — sammuta

---

## 4. Stop-criteria (sammuta heti jos)

- 🛑 **PnL < -10 USDC** (= -10 % capitalista 100 USDC:stä)
- 🛑 **>3 API-virhettä 1 min sisään** (kill-switch laukeaa
      `max_api_errors_per_minute=5`, mutta sammuta itse manuaalisesti
      jos näet trendin)
- 🛑 **Inventory hard stop osuu** (Telegram ilmoittaa, botti laukaisee
      emergency_close — anna sen valmistua, sitten sammuta)
- 🛑 **Exception-spam lokissa** (sama traceback toistuu enemmän kuin 5 kertaa)
- 🛑 **WebSocket disconnect-loop** (botti yrittää uudelleenyhdistää
      kerran toisensa jälkeen yli 2 min)
- 🛑 **Hyperliquid testnet ei vastaa** (UI:ssa "API down" tai vastaava)

---

## 5. Manuaalinen pause (älä sammuta, vaan pysäytä quoting)

Jos haluat tutkia tilanteen mutta et sammuttaa:

- Telegram: lähetä `/pause`
- Botti peruuttaa kaikki orderit, lopettaa uusien quote-asettamisen,
  mutta pitää WebSocket:in ja metriikat päällä
- `/resume` jatkaa quotaamista normaalisti

---

## 6. Hallittu sammutus

```powershell
# Telegram-kommentti varmistus
# (botti vastaa: "Killing — cancelling all orders...")
# TAI suoraan PID:n kautta:

$pid = Get-Content bot.pid
Stop-Process -Id $pid -Force:$false   # SIGTERM-tyylinen
# Jos botti ei sammu 10 sekunnissa:
# Stop-Process -Id $pid -Force          # SIGKILL — viimeinen oljenkorsi
Remove-Item bot.pid
```

Botti pyrkii hallittuun shutdown:iin: cancel_all_orders → tasks-stop →
state_store.close. Tarkista lokin viimeiset rivit:

```powershell
Get-Content logs\mm_bot_testnet_001.log -Tail 30
```

Etsi `INFO main shutdown_complete`. Jos sitä ei ole, voi olla että
joku orderi jäi roikkumaan testnet:iin — tarkista UI:sta ja peruuta käsin.

---

## 7. Post-mortem — ennen raportin kirjoittamista

```powershell
# Kerää metriikat tietokannasta
python scripts\pilot_summary.py --db data\mm_bot_testnet_001.db
```

Tämä printtaa:

- Total fills (bid/ask) ja kpl
- Realized PnL, unrealized PnL, total fees, net
- Quote-latenssi p50/p95/p99
- Adverse selection ka. (bps)
- Order lifetime keskiarvo (placed → filled/cancelled)
- Top 5 ERROR-tason eventtiä (jos niitä on)

Vie nuo numerot raporttiin (`docs/testnet_pilot_report_001.md`).

---

## 8. Mitä tehdä raportin jälkeen

- Jos pilot meni hyvin (ei kaatumisia, fillejä tuli, PnL järkeväksi):
  → `docs/testnet_pilot_report_001.md` täyteen, päätös:
     **STEP 16** (säädöt + uusi 1h ajo) tai jatka pidempään testnetille
- Jos löytyi bugeja: korjaa, lisää testit, aja uusi pilot
  (`testnet_pilot_report_002.md`, ei korvaa 001:tä)
- ÄLÄ siirry STEP 17:ään (mainnet) ennen kuin:
  - 7 päivää testnettiä ilman kaatumista
  - Sharpe (annualisoitu) > 0
  - Adverse selection < 30 %
  - Kaikki kill-switchit testattu manuaalisesti

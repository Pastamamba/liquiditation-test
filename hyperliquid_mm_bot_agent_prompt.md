# Hyperliquid Market Making Bot — Sequential Agent Prompt for Claude Code

> **Käyttöohje:** Avaa tämä tiedosto VS Codessa. Käytä Claude Code CLI:tä projektihakemistossa. Anna agentille yksi STEP kerrallaan, odota että se valmistuu, tarkista output, sitten siirry seuraavaan. Älä anna kaikkia steppejä kerralla — agentti hukkuu kontekstiin.

---

## PROJEKTIN TAVOITE

Rakenna delta-neutraali market making botti Hyperliquid-perpetual-pörssiin. Botti quotaa molemmilla puolilla mid-pricea, ansaitsee spread:in + maker rebatet, ja skewaa quotet kun inventory poikkeaa nollasta. Ei suuntaennustusta — pelkkä spread capture.

**EI tavoitteena:** rikastua. Tavoitteena: oppia market microstructure, validoida edge testnetillä, ajaa pieni live-pilotti turvallisesti.

**Realistinen tuotto-odotus:** -20% — +10% kuukaudessa testivaiheessa. Älä kasvata pääomaa ennen kuin 4 viikkoa positiivista PnL:ää livenä.

---

## OSA 1: API-TUTKIMUS (TEE TÄMÄ ENSIN)

### STEP 0: Hyperliquid API discovery

**Prompt agentille:**

```
Tutki Hyperliquid API täydellisesti ennen kuin kirjoitamme yhtään koodia. Käytä WebFetch-työkalua näihin URLeihin ja kirjoita kaikki löydökset tiedostoon `docs/hyperliquid_api_research.md`:

1. https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
2. https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
3. https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint
4. https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket
5. https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
6. https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
7. https://github.com/hyperliquid-dex/hyperliquid-python-sdk

Dokumentoi tarkasti:

A) AUTHENTICATION
- Miten API-avain luodaan (privaatti vs. agent-wallet)
- Miten signataan requestit (EIP-712? L1 action signing?)
- Onko erillistä read-only vs. trading -avainta
- Voiko käyttää "API wallet" -konseptia (subaccount-tyylinen) jossa pääwallettiin ei tarvitse lähettää privaattiavainta botille

B) RATE LIMITS
- Per-IP rate limits info-endpointille
- Per-address rate limits exchange-endpointille
- WebSocket subscription -rajat
- Mitä tapahtuu kun rajat ylittyy (429? ban?)
- Onko rate-limit-headereita

C) FEES (KRIITTINEN MARKET MAKINGISSA)
- Maker fee (rebate vai positive fee?)
- Taker fee
- Onko volume-tier-alennuksia
- HYPE staking -alennus jos relevantti
- Fee per symbol (BTC vs ETH vs alts)

D) ORDER TYPES
- Limit, Market, Stop, TP/SL
- Post-only flag (ESSENTIAL market makingissa)
- Reduce-only flag
- IOC, FOK, GTC time-in-force vaihtoehdot
- Onko "ALO" (add-liquidity-only) vai post-only sama asia

E) ORDER MANAGEMENT
- Max active orders per address
- Modify order vs. cancel+new (kumpi nopeampi/halvempi)
- Bulk cancel ("cancel all by symbol")
- Order ID format (UUID vs. integer)
- Client order ID -tuki

F) WEBSOCKET
- Available channels: l2Book, trades, userFills, userEvents, allMids, candle
- Heartbeat / ping mechanism
- Reconnect best practices
- Subscription rate limits

G) MARKET DATA
- L2 order book depth (kuinka monta tasoa)
- Tick size per symbol
- Min order size per symbol
- Max leverage per symbol
- Funding rate timing (8h? 1h?)

H) TESTNET
- URL: https://app.hyperliquid-testnet.xyz
- Miten saa testnet-USDC (faucet?)
- Onko testnet-API erillinen endpoint
- Eroaako matching engine mainnetistä

I) SDK
- Onko virallinen Python SDK production-ready
- Mitkä metodit/luokat tärkeimmät market makingiin
- Esimerkit order placement, cancel, WebSocket subscription

J) GOTCHAT
- Tunnetut bugit / quirks
- Builder codes (referral fee jaot)
- Vault-mekanismi (jos haluamme jossain vaiheessa avata oman vaultin)

Lisäksi tutki:
- https://app.hyperliquid.xyz (frontend - katso miten heidän tilastonsa näyttävät)
- Reddit /r/HyperliquidX viimeaikaisista API-muutoksista
- Mitkä symbolit ovat likvideimpiä (ETH, BTC, SOL todennäköisesti) — tarvitsemme tämän symbol-valintaan

Kirjoita lopuksi `docs/hyperliquid_api_research.md` -tiedosto jossa on:
1. Yhteenveto (1 sivu) — mitä tarvitsemme tietää
2. Tarkat API-endpointit + parametrit jotka botti käyttää
3. Lista kysymyksistä joihin et löytänyt vastausta (Q&A:lle myöhemmin)
4. Suositeltava symbol aloitukseen (perustelut: likviditeetti, tick size, fee)
```

**Tarkistus ennen STEP 1:n aloittamista:**
- Lue `docs/hyperliquid_api_research.md` itse läpi
- Varmista että fee-rakenne on selkeä (maker rebate vai fee?)
- Varmista että post-only / ALO -flagi on tuettu
- Päätä symbol (suositus: ETH, jos tutkimus ei kerro muuta)

---

## OSA 2: TEKNINEN SPEKSI

### Arkkitehtuuri

```
┌─────────────────────────────────────────────────────────────┐
│                      MAIN EVENT LOOP                         │
│                  (asyncio.gather kaikille)                   │
└────────┬──────────────┬──────────────┬──────────────┬──────┘
         │              │              │              │
    ┌────▼────┐   ┌────▼─────┐   ┌────▼─────┐   ┌────▼─────┐
    │ Market  │   │Inventory │   │  Quote   │   │  Order   │
    │  Data   │   │ Manager  │   │  Engine  │   │ Manager  │
    │  Feed   │   │          │   │          │   │          │
    └────┬────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘
         │             │              │              │
         └─────────────┴──────────────┴──────────────┘
                       │
                ┌──────▼──────┐
                │  Hyperliquid│
                │     API     │
                └─────────────┘

         ┌─────────────────────────────────┐
         │     CROSS-CUTTING CONCERNS       │
         ├─────────────────────────────────┤
         │ • Risk Manager (kill switches)  │
         │ • State Store (SQLite)          │
         │ • Telegram Notifier              │
         │ • Metrics Logger                 │
         │ • Config Loader                  │
         └─────────────────────────────────┘
```

### Komponentit

**1. MarketDataFeed (`src/market_data.py`)**
- WebSocket-yhteys Hyperliquid public feediin
- Subscribe: `l2Book` (oma symbol), `trades` (oma symbol), `allMids`
- Pidä viimeisin order book muistissa (deque, max 100 snapshot historiaa)
- Laske rolling realized volatility 1min, 5min, 15min ikkunoilla
- Emit events: `on_book_update`, `on_trade`, `on_disconnect`
- Auto-reconnect exponential backoffilla
- Heartbeat: jos ei viestejä 5s, oletetaan disconnect

**2. InventoryManager (`src/inventory.py`)**
- Pidä kirjaa nykyisestä positiosta (long/short, koko)
- Subscribe `userFills` WebSocket-kanavaan
- Päivitä position joka fillin jälkeen atomisesti (asyncio.Lock)
- Laske inventory skew: `skew = current_position / max_position` (-1 to +1)
- Emit `on_inventory_change` event
- Reconcile API:n kanssa joka 30s (haetaan oikea position state)

**3. QuoteEngine (`src/quote_engine.py`)**
- Input: mid_price, current_inventory, volatility
- Output: lista (price, size, side) -tupleja
- Algoritmi:
  ```
  base_spread_bps = config.spread_bps
  vol_adjustment = max(1.0, current_volatility / target_volatility)
  effective_spread = base_spread_bps * vol_adjustment

  inventory_skew = current_position / max_position  # -1 to +1
  skew_adjustment_bps = inventory_skew * config.skew_factor * effective_spread

  bid_price = mid * (1 - effective_spread/2/10000 - skew_adjustment_bps/10000)
  ask_price = mid * (1 + effective_spread/2/10000 - skew_adjustment_bps/10000)

  # Useita tasoja:
  for level in range(num_levels):
      level_offset = level * config.level_spacing_bps / 10000
      bids.append((bid_price * (1 - level_offset), order_size))
      asks.append((ask_price * (1 + level_offset), order_size))
  ```
- Pyöristä hinnat tick_size:iin
- Pyöristä koot lot_size:iin
- Älä quotaa puolta jos `abs(inventory) >= max_position` siltä puolelta

**4. OrderManager (`src/order_manager.py`)**
- Pidä kirjaa aktiivisista ordereista (dict: order_id -> order_info)
- `update_quotes(new_bids, new_asks)`:
  - Vertaa uusia quotteja olemassa oleviin
  - Cancel orderit jotka eivät ole enää tavoite-listalla
  - Place uudet orderit jotka puuttuvat
  - Käytä bulk-cancel jos tuettu
  - Käytä `modify` jos vain hinta muuttuu (jos API tukee, halvempi kuin cancel+new)
- Käsittele fillit: poista order tracking-listalta, päivitä InventoryManager
- Käsittele rejectit: logita, älä retryä sokeasti
- Käytä client_order_id -kenttää duplikaattien estoon
- Kaikki orderit `post_only=True` (älä koskaan ota likviditeettiä = älä maksa taker feetä)

**5. RiskManager (`src/risk.py`)**
Kill switchit (joka tick:illä):
- `pnl_check`: jos session PnL < -config.max_loss_pct * capital, kill
- `volatility_halt`: jos 1min realized vol > config.max_vol_pct, peruuta quotet (älä kill)
- `inventory_check`: jos abs(inventory) > max_position * 1.2, peruuta sen puolen quotet
- `connection_check`: jos WebSocket ollut alhaalla > 10s, peruuta KAIKKI orderit market orderilla
- `funding_check`: jos funding rate > 0.01%/8h sinun nykyisen position vastaisesti, älä lisää sitä position
- `error_rate_check`: jos > 5 API-virhettä viimeisen minuutin aikana, kill ja lähetä Telegram-hälytys
- `daily_loss_check`: jos vuorokauden PnL < -2 * max_loss_pct, kill kunnes manuaalinen restart

Kill = kaikki orderit peruutetaan, position suljetaan market-orderilla, botti pysähtyy ja lähettää Telegram-hälytyksen.

**6. StateStore (`src/state.py`)**
SQLite (aiosqlite, WAL mode), taulut:
```sql
CREATE TABLE fills (
    id TEXT PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,  -- Decimal as string
    size TEXT NOT NULL,
    fee TEXT NOT NULL,
    order_id TEXT NOT NULL,
    is_maker INTEGER NOT NULL
);

CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    client_order_id TEXT,
    timestamp INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    size TEXT NOT NULL,
    status TEXT NOT NULL,  -- placed, filled, cancelled, rejected
    cancel_timestamp INTEGER
);

CREATE TABLE pnl_snapshots (
    timestamp INTEGER PRIMARY KEY,
    realized_pnl TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    inventory TEXT NOT NULL,
    capital TEXT NOT NULL,
    spread_pnl TEXT NOT NULL,
    rebate_earned TEXT NOT NULL
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    level TEXT NOT NULL,  -- info, warning, error, kill
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT
);
```

Yksi shared aiosqlite connection (kuten Kraken-botissa). Kaikki kirjoitukset menevät asyncio.Queue:n läpi yhdelle writer-taskille.

**7. TelegramNotifier (`src/notifier.py`)**
- Hälytykset: kill events, isot PnL-liikkeet, connection-ongelmat
- Komento-rajapinta: `/status`, `/pnl`, `/inventory`, `/kill`, `/pause`, `/resume`
- chat_id-tarkistus (auth) — sama kuvio kuin Kraken-botissa
- Rate limit: max 1 viesti / 10s normaali-kategoriasta, kriittiset aina läpi

**8. MetricsLogger (`src/metrics.py`)**
Mittaa joka 10 sekunti ja kirjoita SQLite:
- Realized PnL (€)
- Unrealized PnL (€)
- Inventory (size, $-arvo)
- Spread PnL (fill bid - fill ask -tuotot)
- Rebate earned ($)
- Fill count (bid, ask)
- Cancel count
- Adverse selection: hinnan liike 10s fillin jälkeen (sun position vastaisesti = +)
- Quote update latency (ms)
- WebSocket message lag (sekunnit)

### Konfiguraatio (`config.yaml`)

```yaml
hyperliquid:
  network: testnet  # testnet | mainnet
  api_wallet_address: "0x..."  # ÄLÄ commitoi privaattia avainta!
  # private key luetaan ympäristömuuttujasta HL_PRIVATE_KEY

trading:
  symbol: ETH
  capital_usdc: 500
  max_position_size: 0.1  # ETH
  spread_bps: 5  # 0.05% mid:stä molempiin suuntiin
  num_levels: 5
  level_spacing_bps: 3
  order_size: 0.02  # ETH per level
  skew_factor: 0.5  # kuinka paljon skewataan kun inventory täysi
  quote_refresh_ms: 2000
  max_order_age_seconds: 30

risk:
  max_loss_pct: 10  # session
  daily_max_loss_pct: 20
  max_vol_pct_1min: 2.0  # halt jos ylitetään
  inventory_hard_stop_multiplier: 1.2
  max_api_errors_per_minute: 5
  funding_rate_threshold_8h: 0.01  # %

telegram:
  bot_token_env: TELEGRAM_BOT_TOKEN
  chat_id_env: TELEGRAM_CHAT_ID
  notification_rate_limit_seconds: 10

storage:
  db_path: ./data/mm_bot.db
  log_path: ./logs/mm_bot.log
  metrics_interval_seconds: 10

operations:
  reconnect_initial_backoff_ms: 1000
  reconnect_max_backoff_ms: 60000
  websocket_heartbeat_seconds: 5
  inventory_reconcile_seconds: 30
  dry_run: false  # true = älä lähetä oikeita ordereita
```

### Riippuvuudet (`requirements.txt`)

```
hyperliquid-python-sdk>=0.1.0
aiosqlite>=0.19.0
pydantic>=2.5.0
pyyaml>=6.0
python-telegram-bot>=20.0
websockets>=12.0
aiohttp>=3.9.0
python-dotenv>=1.0.0
structlog>=24.1.0
pytest>=7.4.0
pytest-asyncio>=0.23.0
pytest-mock>=3.12.0
```

---

## OSA 3: SEQUENTIAL AGENT PROMPTIT (KÄYTÄ JÄRJESTYKSESSÄ)

> **TÄRKEÄ:** Jokaisen STEPin jälkeen tarkista output, aja testit, vahvista että toimii. Älä siirry seuraavaan ennen kuin edellinen on vihreä.

---

### STEP 1: Projektin alustus

**Prompt:**
```
Alusta uusi Python-projekti hakemistoon `hyperliquid-mm-bot/`:

1. Luo seuraava hakemistorakenne:
   ```
   hyperliquid-mm-bot/
   ├── src/
   │   ├── __init__.py
   │   ├── config.py
   │   ├── market_data.py
   │   ├── inventory.py
   │   ├── quote_engine.py
   │   ├── order_manager.py
   │   ├── risk.py
   │   ├── state.py
   │   ├── notifier.py
   │   ├── metrics.py
   │   └── main.py
   ├── tests/
   │   ├── __init__.py
   │   └── conftest.py
   ├── docs/
   │   └── hyperliquid_api_research.md  # (jo olemassa STEP 0:sta)
   ├── data/
   │   └── .gitkeep
   ├── logs/
   │   └── .gitkeep
   ├── config.yaml
   ├── config.example.yaml
   ├── requirements.txt
   ├── .env.example
   ├── .gitignore
   ├── README.md
   └── pyproject.toml
   ```

2. `.gitignore`:n on sisällettävä:
   - `.env`
   - `config.yaml` (vain example commitoidaan)
   - `data/*.db`, `data/*.db-wal`, `data/*.db-shm`
   - `logs/*.log`
   - `__pycache__/`, `*.pyc`
   - `.venv/`, `venv/`

3. `requirements.txt`: kirjoita yllä määritelty lista.

4. `pyproject.toml`: konfiguroi pytest, ruff, mypy. Strict mode mypylle.

5. `.env.example`: listaa kaikki tarvittavat env-muuttujat:
   ```
   HL_PRIVATE_KEY=
   HL_API_WALLET_ADDRESS=
   TELEGRAM_BOT_TOKEN=
   TELEGRAM_CHAT_ID=
   ```

6. `README.md`: lyhyt kuvaus projektista, varoitus että tämä on kokeellinen koodi joka voi hävitä rahaa, asennusohjeet, käynnistysohjeet.

7. `config.example.yaml`: kopioi yllä määritelty konfig.

ÄLÄ vielä asenna paketteja äläkä luo virtualenviä — teemme sen STEP 2:ssa.

Lopuksi tulosta tree-näkymä luodusta rakenteesta.
```

---

### STEP 2: Asennus + ympäristö

**Prompt:**
```
Aseta Python-ympäristö projektille:

1. Luo `.venv` Python 3.11+ versiolla
2. Aktivoi venv ja asenna riippuvuudet `requirements.txt`:sta
3. Verifioi että `hyperliquid-python-sdk` toimii ajamalla:
   ```python
   from hyperliquid.info import Info
   info = Info(base_url="https://api.hyperliquid-testnet.xyz")
   print(info.meta())
   ```
4. Kopioi `config.example.yaml` -> `config.yaml` ja `.env.example` -> `.env`
5. Lisää `config.yaml` ja `.env` `.gitignore`:en jos eivät jo ole

Kerro tarkasti mitä komentoja ajoit ja mikä oli output. Älä laita näitä git-historiaan, älä myöskään yritä committaa mitään tässä vaiheessa.
```

**TARKISTA:** Hyperliquid SDK toimi → meta-output näkyy → ei import-virheitä.

---

### STEP 3: Config + logging

**Prompt:**
```
Toteuta `src/config.py`:

1. Käytä Pydantic v2 BaseSettings -luokkaa
2. Luo Pydantic-mallit:
   - HyperliquidConfig (network, api_wallet_address)
   - TradingConfig (symbol, capital_usdc, max_position_size, spread_bps, ...)
   - RiskConfig
   - TelegramConfig
   - StorageConfig
   - OperationsConfig
   - BotConfig (kokoaa kaikki yllä olevat)
3. Lue config `config.yaml` + override `.env` ympäristömuuttujilla
4. Validoinnit:
   - capital_usdc > 0
   - 0 < spread_bps < 100
   - 0 < num_levels <= 20
   - 0 <= skew_factor <= 1
   - jos network == "mainnet", lisää extra-varoitus logiin
5. Tarjoa singleton `get_config()` funktio

Toteuta `src/logger.py`:
1. Käytä `structlog`:ia
2. Output: JSON tiedostoon `logs/mm_bot.log`, ihmis-luettava console:lle
3. Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
4. Kaikki logit sisältävät: timestamp (ISO 8601, UTC), level, component, message + arbitrary key-value pairs
5. Ei koskaan logita privaattia avainta tai chat_id:tä — lisää redaction-filter

Kirjoita `tests/test_config.py`:
- Testaa että config latautuu oikein esimerkki-yamllista
- Testaa että väärät arvot rejectataan (esim. negative capital)
- Testaa että env-muuttujat ylikirjoittavat yaml:n

Aja `pytest tests/test_config.py -v` ja varmista että kaikki testit menevät läpi.
```

**TARKISTA:** Pytest vihreä, lokit kirjoittuvat sekä konsoliin että tiedostoon.

---

### STEP 4: SQLite state store

**Prompt:**
```
Toteuta `src/state.py`:

1. `StateStore`-luokka aiosqlite-pohjalla
2. WAL mode käyttöön (`PRAGMA journal_mode=WAL`)
3. Luo taulut spec:in mukaan: fills, orders, pnl_snapshots, events
4. Kaikki rahasummat tallennetaan TEXT-kenttiin (Decimal stringinä) — EI floateja
5. Yksi shared connection per processi
6. Käytä asyncio.Queue-pohjaista writer-pattern:ia:
   - StateStore tarjoaa async-metodeja: `record_fill`, `record_order`, `record_pnl_snapshot`, `record_event`
   - Nämä laittavat write-operaation queue:en
   - Yksi background-task kuluttaa queue:n ja tekee oikean DB-write:n
   - Tämä estää race conditioneja
7. Read-metodit: `get_recent_fills`, `get_open_orders_db`, `get_pnl_history` — nämä lukevat suoraan
8. Lisää `migrate()`-metodi joka luo taulut jos eivät ole olemassa

Kirjoita `tests/test_state.py`:
- Testaa että taulut luodaan
- Testaa fillin kirjaus + haku
- Testaa Decimal-arvojen säilyminen (kirjoita "0.00012345", lue takaisin, vertaa)
- Testaa että concurrent writes eivät korruptoi dataa (aja 100 task:ia rinnakkain)

Aja testit ja varmista vihreys.
```

**TARKISTA:** Concurrent-kirjoitustesti menee läpi.

---

### STEP 5: Hyperliquid client wrapper

**Prompt:**
```
Toteuta `src/hl_client.py`:

Tämä on thin wrapper hyperliquid-python-sdk:n päälle. Tarkoitus: keskittää kaikki API-kutsut yhteen paikkaan, lisätä retry-logiikka, rate limiting, ja virheenkäsittely.

1. `HLClient`-luokka:
   - `__init__(config: HyperliquidConfig, private_key: str)`
   - `async place_order(symbol, side, price, size, post_only=True, client_order_id=None) -> OrderResult`
   - `async cancel_order(order_id) -> bool`
   - `async cancel_all_orders(symbol) -> int` (palauttaa cancellattujen määrän)
   - `async modify_order(order_id, new_price, new_size) -> OrderResult`
   - `async get_open_orders(symbol) -> list[Order]`
   - `async get_position(symbol) -> Position`  # nykyinen size
   - `async get_user_fills(start_time) -> list[Fill]`
   - `async get_funding_rate(symbol) -> Decimal`

2. Retry-logiikka:
   - Network-virheet: retry 3x exponential backoff (100ms, 500ms, 2s)
   - Rate-limit virheet (429): respect retry-after header, max 3 retryä
   - Insufficient balance / invalid order: ÄLÄ retryä, raise exception
   - Auth-virhe: log critical, raise — botti pitää sammuttaa

3. Rate limit token bucket:
   - Estä > N requestia per sekunti (lue luku api_research.md:stä)
   - Pidä laskuria ja blockaa jos lähestyy rajaa

4. Hinnat ja koot:
   - Aina Decimal sisäisesti
   - Pyöristä tick_size:n / lot_size:n mukaan ENNEN API-kutsua
   - Cache symbol-meta (tick_size, lot_size, min_size) muistissa, refresh joka 5min

5. Logita kaikki API-kutsut DEBUG-tasolla (mutta EI privaattia avainta)

Kirjoita `tests/test_hl_client.py`:
- Mockaa hyperliquid SDK:n metodit
- Testaa retry-logiikka network-erroreissa
- Testaa rate limit
- Testaa että Decimal-pyöristys tick_size:iin toimii
- Testaa että post_only=True menee oikein API-kutsuun

Aja testit. Älä vielä yhdistä oikeaan API:in.
```

**TARKISTA:** Mock-testit menevät läpi.

---

### STEP 6: WebSocket market data feed

**Prompt:**
```
Toteuta `src/market_data.py`:

1. `MarketDataFeed`-luokka:
   - `__init__(symbol, on_book_update, on_trade, on_disconnect)`
   - `async start()` — yhdistä WebSocket, subscribe kanaviin
   - `async stop()`
   - Properties: `current_mid`, `current_book`, `realized_volatility(window_seconds)`

2. WebSocket-yhteys Hyperliquid feediin:
   - Subscribe: l2Book(symbol), trades(symbol), allMids
   - Heartbeat: lähetä ping joka 15s, odota pong
   - Jos ei viestejä 5s, oletetaan disconnect → trigger reconnect

3. Reconnect:
   - Exponential backoff: 1s, 2s, 4s, ..., max 60s
   - Älä missään tilanteessa kaadu — pidä yritystä jatkuvasti

4. Order book maintenance:
   - Pidä viimeisin L2 snapshot muistissa
   - Laske mid_price = (best_bid + best_ask) / 2
   - Pidä deque viimeiseksi 100 mid_price-arvoa + timestamp

5. Realized volatility:
   - `realized_volatility(window_seconds=60)` palauttaa annualisoidun vol:n
   - Käytä log-returneja: `np.std(np.diff(np.log(prices))) * sqrt(periods_per_year)`

6. Callback-pattern:
   - Kun order book päivittyy, kutsu `on_book_update(new_book)`
   - Kun trade tapahtuu, kutsu `on_trade(trade)`
   - Kun disconnect, kutsu `on_disconnect()`

7. KRIITTISTÄ: Älä blockkaa WebSocket-loop:ia callback-koodilla. Callbackit lähetetään asyncio.Queue:n kautta tai task-poolille.

Kirjoita `tests/test_market_data.py`:
- Mockaa WebSocket
- Syötä fake-viestejä, varmista että callback:it kutsutaan
- Testaa volatility-laskenta tunnetuilla arvoilla
- Testaa reconnect-logiikka

Aja testit.
```

**TARKISTA:** Volatility-testin tulokset matchaa odotettuja arvoja.

---

### STEP 7: Inventory manager

**Prompt:**
```
Toteuta `src/inventory.py`:

1. `InventoryManager`-luokka:
   - `__init__(symbol, max_position, hl_client, state_store)`
   - State: `current_position` (Decimal, signed: + long, - short), `avg_entry_price`, `realized_pnl`
   - Properties: `inventory_skew` (-1 to +1), `position_value_usd`, `is_long`, `is_short`

2. Subscribe `userFills` WebSocket-kanavaan:
   - Joka fill päivittää position atomically (asyncio.Lock)
   - Päivitä avg_entry_price (weighted average)
   - Tallenna fill StateStore:en
   - Emit `on_inventory_change` callback

3. Reconcile API:n kanssa:
   - Joka 30 sekuntia hae oikea position API:lla
   - Vertaa muistissa olevaan
   - Jos ero > tolerance, log WARNING ja korjaa muistissa oleva
   - Tämä saa kiinni missatut WebSocket-viestit

4. PnL-laskenta:
   - Realized PnL = sum( (sell_price - buy_price) * size ) yli matchattujen fillien
   - Käytä FIFO-matching:ia
   - Tallenna jokaisen päivityksen jälkeen pnl_snapshot StateStore:en

5. Helper-metodit:
   - `can_quote_bid()` -> bool (false jos already at max long)
   - `can_quote_ask()` -> bool
   - `get_skew_adjustment_factor()` -> Decimal

Kirjoita `tests/test_inventory.py`:
- Testaa weighted average -laskenta
- Testaa skew-laskenta eri positioilla
- Testaa että reconcile toimii kun on ero
- Testaa FIFO PnL realized fill-sekvenssillä

Aja testit.
```

---

### STEP 8: Quote engine

**Prompt:**
```
Toteuta `src/quote_engine.py`:

1. `QuoteEngine`-luokka:
   - `__init__(config: TradingConfig)`
   - `compute_quotes(mid: Decimal, inventory_skew: float, volatility: float, can_bid: bool, can_ask: bool) -> QuoteSet`

2. `QuoteSet` dataclass:
   - `bids: list[Quote]`  # Quote = (price, size)
   - `asks: list[Quote]`
   - `mid: Decimal`
   - `effective_spread_bps: float`
   - `skew_adjustment_bps: float`

3. Algoritmi (Decimal arithmetic, ÄLÄ käytä floatia rahalle):
   ```
   base_spread = config.spread_bps
   vol_target = 1.0  # baseline annualized vol
   vol_multiplier = max(1.0, min(5.0, current_vol / vol_target))
   effective_spread_bps = base_spread * vol_multiplier

   skew_adj_bps = inventory_skew * config.skew_factor * effective_spread_bps

   bid_mid_offset_bps = -effective_spread_bps / 2 - skew_adj_bps
   ask_mid_offset_bps = +effective_spread_bps / 2 - skew_adj_bps

   bid_base_price = mid * (1 + bid_mid_offset_bps / 10000)
   ask_base_price = mid * (1 + ask_mid_offset_bps / 10000)

   for level in 0..num_levels:
       bid_price = bid_base_price * (1 - level * level_spacing_bps / 10000)
       ask_price = ask_base_price * (1 + level * level_spacing_bps / 10000)
       bids.append(Quote(bid_price, order_size))
       asks.append(Quote(ask_price, order_size))
   ```

4. Pyöristykset:
   - Hinnat -> tick_size (down for bid, up for ask, jotta ovat aina maker-puolella)
   - Koot -> lot_size (down)

5. Jos `can_bid=False`, palauta tyhjä bids-lista. Sama ask:lle.

6. Jos `effective_spread_bps > config.max_spread_bps`, palauta tyhjä quoteset (markkina liian volatiili, älä quotaa).

Kirjoita `tests/test_quote_engine.py`:
- Testaa että nollainventory + nollavolatiliteetti tuottaa symmetriset quotet
- Testaa että positiivinen skew (long) siirtää MOLEMPIA quoteja alaspäin (myydään ennemmin, ostetaan halvemmalla)
- Testaa että negatiivinen skew tekee päinvastoin
- Testaa että korkea volatility leventää spread:iä
- Testaa pyöristykset
- Testaa edge case: skew=1.0 (max long) → bidit ovat vielä parhaalla bidillä? (Riippuu factor:ista)

Aja testit.
```

**TARKISTA:** Skew-logiikka toimii oikein — long position → quotet siirretty alas → todennäköisempää että myyt → palauttaa kohti nollainventoryä.

---

### STEP 9: Order manager

**Prompt:**
```
Toteuta `src/order_manager.py`:

1. `OrderManager`-luokka:
   - `__init__(hl_client, state_store, symbol)`
   - State: `active_orders: dict[order_id, OrderInfo]`
   - asyncio.Lock joka kerran kun mutoidaan active_orders

2. `async update_quotes(target_quotes: QuoteSet) -> UpdateResult`:
   Vertaa target_quotes:ia nykyisiin active_orders:iin. Toimi näin:
   - Etsi orderit jotka ovat target-listalla samalla side:lla ja sopivalla hinnalla → pidä
   - Etsi orderit joita ei ole target-listalla → cancel
   - Etsi target-orderit joita ei ole vielä placed → place uudet
   - Käytä hinta-toleranssia: jos olemassa oleva order hinta on < 1 tick erilainen kuin target, pidä se (välttää turhia cancel/place-syklejä)
   - Bulk-cancel jos peruutetaan kaikki saman puolen orderit

3. `async cancel_all() -> int`:
   - Peruuta KAIKKI active orderit
   - Kutsutaan emergencyssä
   - Älä luota muistissa olevaan listaan — kysy myös API:lta ja peruuta kaikki

4. Order ID tracking:
   - Käytä client_order_id = "mm_{timestamp_ms}_{nonce}" duplikaattien välttämiseen
   - Jos saman client_order_id:n yritys epäonnistuu, ÄLÄ retry samalla ID:llä

5. Fill-käsittely:
   - InventoryManager:ilta saadaan fill-eventti
   - Poista vastaava order active_orders:ista
   - Logita fill latency (order placement → fill ms)

6. Stale order cleanup:
   - Joka 10s tarkista: onko orderia joka on > config.max_order_age_seconds vanha?
   - Jos on, peruuta se (koska market on liikkunut, hinta on stale)

7. KRIITTINEN: order placement ja cancel on eri pyyntöjä, joten on race conditioneja:
   - Voit yrittää peruuttaa orderin joka jo täyttyi → API palauttaa "order not found"
   - Käsittele tämä gracefully (älä kaadu, vain logita)
   - Voit place:n jälkeen heti yrittää cancel:in ja saada "order not yet acknowledged" → retry kerran

Kirjoita `tests/test_order_manager.py` (mock hl_client):
- Testaa happy path: tyhjä state → place 5 bidiä + 5 askiä
- Testaa update: 5 olemassa olevaa, target on samat hinnat → ei tehdä mitään
- Testaa update: 3 hintaa muuttunut → cancel 3, place 3
- Testaa cancel_all
- Testaa että client_order_id on uniikki

Aja testit.
```

**TARKISTA:** Update-logiikka ei tee turhia cancel/place-pareja kun hinnat ovat lähellä.

---

### STEP 10: Risk manager

**Prompt:**
```
Toteuta `src/risk.py`:

1. `RiskManager`-luokka:
   - `__init__(config: RiskConfig, inventory: InventoryManager, market_data: MarketDataFeed, state_store, on_kill: callback)`
   - State: `is_killed: bool`, `is_paused: bool`, `kill_reason: str`, `session_start_capital: Decimal`, `session_start_time`
   - State: `api_error_timestamps: deque` (sliding window)
   - State: `last_websocket_message_time`

2. `async check_all() -> RiskStatus`:
   Kutsutaan joka tickillä tai joka 1s, kumpi nopeampi.
   Tarkistaa kaikki kill switchit järjestyksessä:

   a) **Connection check** (ensimmäisenä, kriittisin):
      jos `now - last_websocket_message_time > 10s`: trigger emergency_close(), set is_killed
   
   b) **PnL check (session)**:
      session_pnl_pct = (current_capital - session_start_capital) / session_start_capital
      jos session_pnl_pct < -config.max_loss_pct/100: trigger kill("session loss limit")
   
   c) **PnL check (daily)**:
      sama mutta vuorokauden ajalta
   
   d) **Inventory hard stop**:
      jos abs(inventory.current_position) > inventory.max_position * config.inventory_hard_stop_multiplier:
          → kill("inventory exceeded hard stop")
   
   e) **Volatility halt** (ei kill, vain pause):
      jos market_data.realized_volatility(60) > config.max_vol_pct_1min:
          set is_paused=True
      muuten jos volatility < threshold * 0.7:
          set is_paused=False  # hysteresis
   
   f) **API error rate**:
      api_errors_last_60s = count(timestamp > now-60 in api_error_timestamps)
      jos api_errors_last_60s > config.max_api_errors_per_minute:
          → kill("API error rate exceeded")
   
   g) **Funding rate check** (ei kill, vain warning):
      jos abs(funding) > config.funding_rate_threshold_8h JA on sun position vastaisesti:
          set warning state, notifier sends Telegram

3. `async emergency_close()`:
   - Peruuta kaikki orderit
   - Sulje position market-orderilla (tämä on AINOA kerta kun käytämme taker-orderia)
   - Lähetä Telegram-hälytys

4. `async on_kill_event(reason)`:
   - Kutsu emergency_close()
   - Kirjoita event StateStore:en
   - Lähetä CRITICAL Telegram
   - Set is_killed=True (estää uudet quotet)

5. `record_api_error(error)`:
   - Lisää timestamp api_error_timestamps:iin
   - Trim vanhat (> 60s)

Kirjoita `tests/test_risk.py`:
- Testaa että session loss > limit triggeröi kill
- Testaa että volatility halt pause/resume hysteresis toimii
- Testaa API error rate counting
- Testaa että emergency_close kutsuu cancel_all + position close

Aja testit.
```

**TARKISTA:** Volatility hysteresis estää pause/resume-flickerin.

---

### STEP 11: Telegram notifier

**Prompt:**
```
Toteuta `src/notifier.py`:

1. `TelegramNotifier`-luokka:
   - `__init__(bot_token, chat_id, rate_limit_seconds=10)`
   - `async start()` — käynnistä polling komentojen kuuntelua varten
   - `async stop()`
   - `async send_alert(level, message, force=False)` — level: info, warning, error, critical
   - critical aina läpi (ei rate limit)

2. Auth:
   - Tarkista chat_id sisään tulevista viesteistä — jos ei matchaa, ignore
   - ÄLÄ ikinä logita botin tokenia

3. Komennot:
   - `/status` → yhteenveto: uptime, position, session PnL, is_paused, is_killed
   - `/pnl` → tarkempi PnL breakdown (realized, unrealized, rebate)
   - `/inventory` → current position size, value, avg entry
   - `/orders` → lista aktiivisista ordereista
   - `/pause` → set is_paused (ei kill, peruuta vain quotet)
   - `/resume` → unpause
   - `/kill` → vahvista (kysyy "VAHVISTA: kirjoita 'KILL CONFIRM'") → emergency_close

4. Rate limit:
   - Per kategoria (info, warning, error)
   - Critical: ei rate limittiä
   - Käytä token bucket -pattern:ia

5. Formatointi:
   - Käytä MarkdownV2 (escapaa erikoismerkit oikein)
   - Lisää emoji severity:n mukaan: ℹ️ ⚠️ 🚨 💀
   - Aikaleima joka viestissä (UTC + Helsinki)

Kirjoita `tests/test_notifier.py`:
- Mockaa python-telegram-bot
- Testaa että rate limit toimii
- Testaa että väärä chat_id ignoroidaan
- Testaa /status komennon vastaus

Aja testit.
```

---

### STEP 12: Metrics logger

**Prompt:**
```
Toteuta `src/metrics.py`:

1. `MetricsCollector`-luokka:
   - `__init__(state_store, inventory, market_data, order_manager, interval=10)`
   - `async run()` — background task joka mittaa joka N sekunti

2. Mittaa:
   - timestamp (UTC)
   - realized_pnl
   - unrealized_pnl (mark-to-market mid-pricella)
   - inventory (size, side, $-arvo)
   - rebate_earned (cumulative)
   - spread_pnl: laske matchatuista fill-pareista (FIFO)
   - active_order_count (bid + ask erikseen)
   - fill_count_session (bid + ask erikseen)
   - cancel_count_session
   - quote_update_latency_p50, p95, p99 (millisekunneissa)
   - websocket_message_lag_seconds (now - last_message_time)
   - adverse_selection_score: keskimääräinen mid-pricen liike sun position vastaisesti 10s fillin jälkeen

3. Tallenna SQLite pnl_snapshots-tauluun

4. Tarjoa `get_summary()` -metodi joka palauttaa dictin (Telegram /status:ia varten)

5. Päivän alussa (klo 00:00 UTC) rotate session metrics:
   - Tallenna daily-summary erilliseen tauluun (tai event log:iin)
   - Resetoi session-counterit (mutta säilytä position!)

Kirjoita `tests/test_metrics.py`:
- Testaa että adverse selection -laskenta toimii synthetic-datalla
- Testaa että FIFO spread PnL matchaa odotettua arvoa

Aja testit.
```

---

### STEP 13: Main event loop

**Prompt:**
```
Toteuta `src/main.py` — orchestrator joka kokoaa kaiken:

1. `async main()`:
   ```python
   async def main():
       config = get_config()
       setup_logging(config)
       
       state_store = StateStore(config.storage.db_path)
       await state_store.migrate()
       
       hl_client = HLClient(config.hyperliquid, os.getenv("HL_PRIVATE_KEY"))
       
       market_data = MarketDataFeed(config.trading.symbol)
       inventory = InventoryManager(config.trading, hl_client, state_store)
       quote_engine = QuoteEngine(config.trading)
       order_manager = OrderManager(hl_client, state_store, config.trading.symbol)
       risk_manager = RiskManager(config.risk, inventory, market_data, state_store, on_kill=...)
       notifier = TelegramNotifier(...)
       metrics = MetricsCollector(state_store, inventory, market_data, order_manager)
       
       # Hook callbacks
       market_data.on_book_update = lambda book: trigger_quote_update(...)
       market_data.on_disconnect = lambda: risk_manager.handle_disconnect()
       inventory.on_inventory_change = lambda: trigger_quote_update(...)
       
       # Aloita kaikki
       await asyncio.gather(
           market_data.start(),
           inventory.start(),
           order_manager.start(),
           risk_manager.start(),
           notifier.start(),
           metrics.run(),
           quote_loop(),  # alla
       )
   ```

2. `async quote_loop()` — main quoting cycle:
   ```python
   async def quote_loop():
       while not risk_manager.is_killed:
           if risk_manager.is_paused:
               await order_manager.cancel_all()
               await asyncio.sleep(1)
               continue
           
           mid = market_data.current_mid
           if mid is None:
               await asyncio.sleep(0.5)
               continue
           
           vol = market_data.realized_volatility(60)
           skew = inventory.inventory_skew
           can_bid = inventory.can_quote_bid()
           can_ask = inventory.can_quote_ask()
           
           target_quotes = quote_engine.compute_quotes(mid, skew, vol, can_bid, can_ask)
           
           t0 = time.perf_counter()
           await order_manager.update_quotes(target_quotes)
           latency_ms = (time.perf_counter() - t0) * 1000
           metrics.record_quote_latency(latency_ms)
           
           await asyncio.sleep(config.trading.quote_refresh_ms / 1000)
   ```

3. Signaalinkäsittely (SIGINT, SIGTERM):
   - Graceful shutdown: peruuta kaikki orderit, sulje WebSocket, sulje DB

4. Task registry pattern (kuten Kraken-botissa):
   - Pidä kirjaa kaikista käynnistetyistä taskeista
   - Shutdown:in yhteydessä cancel kaikki ja odota niiden valmistumista (timeout 5s, sitten force)

5. Käynnistyslogit:
   - Print iso banner: "HYPERLIQUID MM BOT v0.1 — TESTNET" tai "MAINNET ⚠️"
   - Lokita config (ilman secretsejä)
   - Lokita symbol-meta (tick_size, lot_size, jne)

Kirjoita `tests/test_integration.py`:
- Aja main() pienellä mocked-setupilla 5 sekuntia, varmista ettei kaadu
- Tämä on smoke test, ei täydellinen

Aja testit.
```

---

### STEP 14: Dry run -tila

**Prompt:**
```
Lisää dry-run -tila joka simuloi orderit lähettämättä niitä oikeasti.

1. `src/hl_client.py`:
   - Jos `config.operations.dry_run == True`:
     - place_order ei kutsu API:a — palauttaa fake OrderResult oletuksena "filled" 50% todennäköisyydellä jos hinta on book:in best bid/ask:in sisällä
     - cancel_order palauttaa True
     - get_position palauttaa muistissa olevan simuloidun position
   - Logita kaikki dry-run kutsut DEBUG-tasolla

2. Käytä oikeaa market data feediä myös dry-runissa (saamme oikean book:in)

3. Lisää `src/sim_fill.py`:
   - SimulatedFillEngine joka kuuntelee market_dataa
   - Jos sun bid >= mid → täytä se 80% todennäköisyydellä (optimistinen)
   - Jos sun ask <= mid → täytä se 80% todennäköisyydellä
   - Triggeröi inventory.handle_fill()

4. Päivitä `config.example.yaml`: dry_run: true (oletuksena)

Aja botti dry-run-tilassa testnet-market-datalla 30 minuuttia. Tarkista logeista että:
- Quotet päivittyvät
- Simuloidut fillit kirjautuvat
- PnL laskee
- Telegram /status toimii
- Mikään ei kaadu
```

**TARKISTA:** 30 min dry-run ilman kaatumista.

---

### STEP 15: Testnet pilot

**Prompt:**
```
Aja botti Hyperliquid testnetillä oikeilla (testnet-USDC) ordereilla.

1. Pre-flight checklist:
   - [ ] Olen luonut Hyperliquid testnet-tilin (https://app.hyperliquid-testnet.xyz)
   - [ ] Olen saanut testnet-USDC faucetista
   - [ ] Olen luonut API wallet -avaimen (EI päämain-walletin avain!)
   - [ ] HL_PRIVATE_KEY on .env:issä eikä git-historiassa
   - [ ] config.yaml: network=testnet, dry_run=false, capital_usdc=100
   - [ ] Telegram bot toimii (testaa /status manuaalisesti)
   - [ ] Lokit menevät logs/mm_bot.log:iin

2. Käynnistä botti taustalle:
   ```bash
   nohup python -m src.main > logs/stdout.log 2>&1 &
   echo $! > bot.pid
   ```

3. Seuraa 1 tunti aktiivisesti:
   - Telegram /status joka 10 min
   - Tarkkaile lokeja `tail -f logs/mm_bot.log`
   - Hyperliquid web-UI näytä position + orderit

4. Stop-criteria (sammuta botti jos):
   - PnL < -10% capitalista
   - > 3 API-virhettä 1 min sisään
   - Inventory hard stop osuu
   - Bugiloki näyttää exception-spamia

5. Ajan jälkeen analysoi:
   - Total fills (bid + ask)
   - Realized PnL
   - Average fill latency
   - Adverse selection score
   - Kuinka kauan orderit elivät keskimäärin ennen täyttöä tai cancellia

6. Kirjoita raportti `docs/testnet_pilot_report_001.md`:
   - Mitä toimi
   - Mitä bugeja löytyi
   - Mitä parametreja tulee säätää
   - Mene seuraavaan vaiheeseen vai säädetäänkö ensin
```

---

### STEP 16: Iterointi

**Prompt:**
```
Käy läpi pilot-raportti. Listaa konkreettiset säädöt ja tee ne yksi kerrallaan:

Mahdolliset säädöt:
- spread_bps liian kapea (paljon adverse selectionia) → leveämmäksi
- spread_bps liian leveä (ei filleja) → kapeammaksi
- skew_factor liian alhainen (inventory karkaa) → korkeammaksi
- num_levels liian iso (ordereita peruutetaan paljon) → vähemmäksi
- quote_refresh_ms liian tiheä (rate limit hits) → harvempaan
- volatility threshold säätö

Jokaisen säädön jälkeen:
1. Aja 1h testnetillä
2. Vertaa metriikoita aiempaan
3. Päätä: pidetäänkö, peruutetaanko

Kun olet tyytyväinen, mene STEP 17:een (mainnet pilot).

VAROITUS: ÄLÄ siirry mainnetille ennen kuin:
- 7 päivää testnetillä ilman kaatumista
- Sharpe (annualisoitu) > 0 testnet-PnL:llä
- Adverse selection score < 30% (eli 70% fillien jälkeen hinta liikkuu sun puolelle tai pysyy)
- Kaikki kill switchit on testattu (laukaise jokainen manuaalisesti)
```

---

### STEP 17: Mainnet mikropilot

**Prompt:**
```
Mainnet-vaihe. Pieni capital, tiukat rajat.

1. Pre-flight (TÄRKEÄÄ):
   - [ ] Erillinen mainnet API wallet -avain (EI sama kuin testnet!)
   - [ ] config.yaml: network=mainnet, capital_usdc=100, max_loss_pct=10
   - [ ] Oletkin lähettänyt vain 100 USDC:tä API walletille (ei enempää!)
   - [ ] Telegram-hälytykset toimii — testaa lähettämällä manuaalinen /status
   - [ ] Olen lukenut Hyperliquidin terms of service
   - [ ] Olen valmis menettämään 100 USDC:tä

2. Käynnistä botti ja seuraa AKTIIVISESTI:
   - Ensimmäiset 2h: tuijota lokeja jatkuvasti
   - Seuraavat 6h: tarkista joka 30 min
   - 24h asti: tarkista joka 2h

3. Stop heti jos:
   - Mikä tahansa odottamaton käytös
   - PnL < -5% (50% session limit:istä) → halt manuaalisesti, analysoi
   - Mikä tahansa data näyttää oudolta

4. 1 viikko mainnet-pilottia. Sitten päätös:
   - Toimii? Skaalaa 500 USDC:hen
   - Ei toimi? Pivot tai luovuta
   - Bugeja? Korjaa, palaa testnetille

ÄLÄ koskaan lähetä isoa pääomaa botille jonka edge ei ole todennettu. Pieni capital pitkä aika > iso capital lyhyt aika.
```

---

## OSA 4: TARKISTUSLISTA OPERATIONAALISILLE ASIOILLE

### Ennen ensimmäistä testnet-ajoa
- [ ] Kaikki yksikkötestit menevät läpi
- [ ] Linter (ruff) ei valita
- [ ] mypy strict mode ei valita
- [ ] config.yaml on validoitu
- [ ] .env ei ole git-historiassa
- [ ] docs/hyperliquid_api_research.md on luettu
- [ ] Olen ymmärtänyt fee-rakenteen
- [ ] Olen ymmärtänyt mitä tapahtuu kun WebSocket katkeaa

### Ennen mainnet-ajoa (lisäksi)
- [ ] 7 päivää onnistunutta testnet-ajoa
- [ ] Kaikki kill switchit on manuaalisesti testattu
- [ ] Telegram /kill toimii
- [ ] API wallet -avain on erillinen pää-walletista
- [ ] Capital walletissa on vain pilot-summa
- [ ] On olemassa rollback-suunnitelma (miten suljen position manuaalisesti jos botti hajoaa)

### Päivittäin (live-vaiheessa)
- [ ] Tarkista Telegram /status
- [ ] Vilkaise lokit (`grep ERROR logs/mm_bot.log | tail -50`)
- [ ] Tarkista PnL trend
- [ ] Tarkista että WebSocket-disconnect-määrä on järkevä (< 5/päivä)

### Viikoittain
- [ ] Backuppaa `data/mm_bot.db`
- [ ] Käy läpi adverse selection -trendi
- [ ] Päivitä riippuvuudet (security)
- [ ] Tarkista että Hyperliquid ei ole muuttanut API:a (lue muutoslogit)

---

## OSA 5: VAROITUKSET JA RAJOITUKSET

**Tämä botti voi hävitä rahaa. Käytä vain capitalia jonka olet valmis menettämään täysin.**

Tunnetut riskit:

1. **Adverse selection:** Informoidut traderit ottavat fillisi juuri ennen suuntaliikettä. Tämä on market makingin perussyöte joka lopulta määrittää voitatko vai häviätkö. Et voi täysin estää tätä.

2. **Inventory risk:** Vaikka skew-mekanismi auttaa, isossa yksisuuntaisessa liikkeessä jäät pussiin. Volatility halt auttaa mutta ei poista riskiä.

3. **Hyperliquid-spesifiset riskit:** Smart contract -bugi, exchange downtime, oraakkeli-manipulaatio. Hajauta ÄLÄ kaikkea tilille.

4. **API-muutokset:** Hyperliquid voi muuttaa API:a → botti hajoaa hiljaa. Lue muutoslogit.

5. **Sinun bugisi:** Off-by-one, race condition, väärä etumerkki — todennäköisin tappion lähde. Siksi pieni capital ja tiukat kill switchit.

6. **Verot:** Suomessa krypto-tappiot ovat vähennettävissä, mutta market making tuottaa valtavan määrän kirjanpitorivejä. Vie data CSV:nä Koinly tms. -palveluun.

---

## YHTEENVETO

Tämä on 17 vaiheen suunnitelma jolla rakennat market making -botin Hyperliquidiin Claude Codella VS Codessa. Aja stepit järjestyksessä, tarkista jokainen ennen seuraavaan siirtymistä. Älä oikaise — jokainen kill switch ja testi on siellä syystä.

**Kokonaisaikataulu:**
- API-tutkimus: 1 päivä
- Stepit 1-13 (rakennus): 2-3 viikkoa
- Stepit 14-15 (testnet): 1-2 viikkoa
- Stepit 16-17 (iterointi + mainnet): 2-4 viikkoa
- **Yhteensä:** 6-10 viikkoa ennen kuin tiedät toimiiko

**Realistinen lopputulos:** 50% mahdollisuus että botti ei tee voittoa edes pitkän iteroinnin jälkeen. Se on OK. Opit market microstructurea, parannat coding skillejäsi, ja saat työkaluja joita voit hyödyntää muissa boteissa (Kraken).

Onnea matkaan. Aloita STEP 0:sta.

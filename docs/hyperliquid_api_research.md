# Hyperliquid API Research — STEP 0

**Projekti:** Hyperliquid delta-neutraali market making -botti (Python + asyncio + virallinen `hyperliquid-python-sdk`)
**Tutkimuspäivä:** 2026-05-05
**Status:** Pre-implementation research. Ei koodia tässä dokumentissa.

---

## 1. Yhteenveto — Top 10 löydöstä jotka botin tekijän PITÄÄ tietää

1. **Maker-fee on +0.015 % (positiivinen) base-tasolla — EI rebate.** Negatiivisen makerin saa vasta kun oman makerin osuus pörssin 14d makerin volyymistä ylittää 0.5 % (-0.001 %), 1.5 % (-0.002 %) tai 3.0 % (-0.003 %). Pieni mikropilotti (100 USDC) EI tee rebatea — strategian kannattavuus tulee spreadistä, ei rebatesta. Taker = +0.045 %.
2. **Käytä API-walletia (agent), älä master-walletin avainta botissa.** `approveAgent`-toiminto on ilmainen, agent voi vain *signata* tradeja eikä withdraw'tä. Master-walletilla pidetään fundit ja tehdään withdraw'it. Jopa 3 nimettyä + 1 nimeämätön agentti per master.
3. **Funding maksetaan tunneittain** (vrt. Binance 8h). Hyperliquid laskee 8h-rahoitusasteen ja maksaa 1/8 siitä joka tunti. Cap 4 %/h. Tämä vaikuttaa inventory hold cost -laskelmiin merkittävästi.
4. **Rate-limitit on kaksi tasoa:**
   - **Per-IP REST:** 1200 weight/min (l2Book/allMids = 2, useimmat info = 20).
   - **Per-address exchange:** ~1 request per 1 USDC kumulatiivista volyymiä, alkupuskuri 10 000 requestia. Liian agressiivinen quote-päivitystiheys → tilien rate-limit. Cancel-pyyntöjä saa enemmän: `min(limit + 100000, limit*2)`.
5. **WebSocket-rajat:** 10 yhteyttä/IP, 1 000 subscriptionia, 2 000 viestiä/min, 100 inflight post-viestiä. Pingaa `{"method":"ping"}` jos kanavassa on hiljaista — palvelin sulkee yhteyden 60 s ilman viestiä.
6. **Order field-nimet ovat lyhyitä ja kirjainherkkiä.** `a` = asset id, `b` = isBuy, `p` = price, `s` = size, `r` = reduceOnly, `t.limit.tif` ∈ {`Alo`, `Ioc`, `Gtc`}. Post-only = `tif: "Alo"`. Cloid = optional `c`-kenttä (128-bit hex string).
7. **Nonces:** 100 viimeisintä per signer säilytetään. Uusi nonce > min(set), ei saa toistua. Aikaikkuna `(T-2pv, T+1pv)`. Käytä per-process atomic counter, batchaa joka 0.1 s. Jokaiselle prosessille/subaccountille oma agent-wallet → ei nonce-kollisioita.
8. **Price- & size-rounding -säännöt:**
   - Price: max 5 sig.figs JA enintään `MAX_DECIMALS - szDecimals` desimaalia (perp `MAX_DECIMALS=6`, spot `=8`). Integer-hinnat aina sallittuja.
   - Size: pyöristä `szDecimals`-tarkkuuteen (haetaan `meta`-vastauksesta).
   - **Min order: 10 USDC (perp).** Order rejectataan "Price must be divisible by tick size", jos rounding väärä.
9. **Modify-toiminto on natiivisti tuettu** (`type: "modify"` tai `batchModify`) — botin ei tarvitse cancel+replace, mikä säästää rate-limit-budjettia ja vähentää queue-position-menetystä.
10. **Suositeltu aloitussymbol: ETH-PERP.** BTC syvempi mutta kalliimpi tikkari → suurempi inventory-arvo per quote → riskialttiimpi 100 USDC pilotille. ETH:lla riittävä syvyys, pienempi notional per kontrakti ja max leverage 25–50x. Vältä matalien szDecimals altcoineja aluksi.

---

## A. Authentication

### A.1 Yleistä signing
- Kaikki `/exchange`-kutsut signataan **EIP-712-tyylisesti**. Hyperliquid käyttää L1 action signing -konseptia (msgpack-koodatun action-objektin hashin signaus), mutta SDK kapseloi tämän. Botin tasolla riittää että tiedämme: signaaja on EVM-yhteensopiva privaattiavain.
- HTTP-payload-rakenne `/exchange`:
  ```
  {
    "action": { ... },
    "nonce": <ms timestamp>,
    "signature": { "r": "0x...", "s": "0x...", "v": <int> },
    "vaultAddress": "0x..."   // optional, subaccount/vault
  }
  ```
- `Content-Type: application/json` -header pakollinen.
- `/info`-endpointti **EI vaadi signaturea** — kaikki info-kyselyt ovat avoimia (mutta IP-rate-limited).

### A.2 API wallet (agent) -konsepti — KÄYTÄ TÄTÄ
- `approveAgent`-toiminto antaa erilliselle EVM-walletille oikeuden signata kauppoja master-walletin puolesta.
- Action-format:
  ```json
  {
    "type": "approveAgent",
    "hyperliquidChain": "Mainnet",         // tai "Testnet"
    "signatureChainId": "0xa4b1",           // Arbitrum chain id
    "agentAddress": "0xAGENT...",
    "agentName": "mm-bot-prod",             // optional, max 3 named per master
    "nonce": <ms timestamp>
  }
  ```
- **On-chain cost:** ei eksplisiittistä fee. (Master-account täytyy olla aktivoitu mainnetillä eli vähintään $5 USDC sillattu Arbitrumista.)
- **Mitä agent voi:** signata orderit, cancelit, modify, updateLeverage. **Mitä agent EI voi:** withdraw3 (varoja ei voi siirtää pois). Tämä on suuri turvallisuusominaisuus mm-botille.
- **Max agentit:** 1 nimeämätön + 3 nimettyä per master, +2 nimettyä per subaccount.
- **Revoke:** lähetä uusi `approveAgent` samalla `agentName`-kentällä → vanha pyyhitään. Nimeämätön korvataan uudella nimeämättömällä.
- **Expiration:** ei eksplisiittistä expiration-kenttää, mutta agent prunataan jos master-tili tyhjenee tai agent uudelleen-approvataan.
- **Älä reuse vanhojen agenttien osoitteita** (replay-riski deregistroinnin jälkeen).

### A.3 Read-only vs. trading
- Erillistä read-only -avainta ei ole. Info-endpoint on julkinen, joten read-only = ei avainta lainkaan. Trading vaatii joko master-avaimen tai agent-avaimen.

**Lähde:** https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets

---

## B. Rate Limits (KRIITTINEN — botin pitää toteuttaa token-bucket)

### B.1 Per-IP REST (info + exchange yhteensä)
- **Aggregaatti-paino:** 1200 / minuutti.
- **Info-painot:**
  - Weight 2: `l2Book`, `allMids`, `clearinghouseState`, `orderStatus`, `spotClearinghouseState`, `exchangeStatus`
  - Weight 20: useimmat muut info-endpointit
  - Weight 60: `userRole`
  - Lisäpaino "per 20 items returned" mm. `userFills`, `historicalOrders`, `recentTrades`. `candleSnapshot` "per 60 items".
- **Exchange-paino:** `1 + floor(batch_length / 40)`. Yksittäinen order = paino 1. 40 orderin batch = paino 1. 80 orderin batch = paino 2.
- **Explorer-paino:** 40 / pyyntö.

### B.2 Per-address (exchange-only)
- **Allowance-formula:** 1 request per 1 USDC kumulatiivista volyymiä address inceptionistä lähtien.
- **Alkupuskuri:** 10 000 requestia (annetaan ennen volyymiä).
- **Throttle:** kun rajan yli, 1 request / 10 s sallitaan.
- **Cancel-bonus:** cancel-rajat ovat `min(limit + 100000, limit * 2)` — eli paljon väljempi kuin order-rajat. Tämä on tärkeää mm-botille (joka cancellaa paljon).
- **Open order -raja:** 1 000 base + 1 lisä per 5 M USDC volyymi, max 5 000 yhtäaikaista open orderia.
- **Subaccountit lasketaan omina addresseina** → omat rate-limitit.
- **Batched-pyynnöt:** IP-rajan kannalta 1 request, mutta address-rajan kannalta `n` requestia.

### B.3 WebSocket
- 10 yhteyttä per IP
- 30 uutta yhteyttä per minuutti
- 1 000 subscriptionia (yhteensä)
- 10 unique users user-specific-subscriptioneissa
- 2 000 viestiä per minuutti
- 100 yhtäaikaista inflight post-viestiä

### B.4 Mitä tapahtuu rajan ylittyessä
- **HTTP 429 Too Many Requests** REST-puolella.
- WS:llä yhteys voi sulkeutua / viestit hylätään hiljaisesti.
- **Bani-aika:** dokumentaatiossa ei eksplisiittistä bani-pituutta; throttled-tilassa 1 req/10s.
- **Lisäkapasiteetti:** voi ostaa `reserveRequestWeight`-actionilla hintaan **0.0005 USDC / request**.

**Lähde:** https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits

---

## C. Fees (KRIITTINEN strategialle)

Kaikki fee-numerot perustuvat 14d rolling volyymiin (UTC daily settlement). Spot-volyymi tuplautuu tier-laskennassa.

### C.1 Perp-fee-tier (base)
| Tier | 14d volume | Taker | Maker |
|------|-----------|-------|-------|
| 0 | < 5 M | 0.045 % | **0.015 %** (positiivinen!) |
| 1 | > 5 M | 0.040 % | 0.012 % |
| 2 | > 25 M | 0.035 % | 0.008 % |
| 3 | > 100 M | 0.030 % | 0.004 % |
| 4 | > 500 M | 0.028 % | 0.000 % |
| 5 | > 2 B | 0.026 % | 0.000 % |
| 6 | > 7 B | 0.024 % | 0.000 % |

(Lukuja täydennetty fees-dokumentista; tarkista uusin taulukko ennen tuotantoa.)

### C.2 Maker-rebate-tier (vain jos olet iso maker)
Vaatii että 14d weighted maker-osuutesi koko Hyperliquid-makerin volyymistä ylittää:
- > 0.5 % → **-0.001 %** (rebate)
- > 1.5 % → **-0.002 %**
- > 3.0 % → **-0.003 %**

**Käytännön implikaatio:** 100 USDC mikropilotti EI saa rebatea. Botin pitää olla profitable pelkän spreadin (taker-puolen täyttöjen) varassa. Älä mallinna negatiivista makeria.

### C.3 HYPE staking -alennukset
Kaikkiin fee-tiereihin sovellettava kerroin:
- Wood (>10 HYPE): -5 %
- Bronze (>100): -10 %
- Silver (>1 000): -15 %
- Gold (>10 000): -20 %
- Platinum (>100 000): -30 %
- Diamond (>500 000): -40 %

### C.4 Muut modifierit
- Aligned quote assets: -20 % taker, +50 % maker rebate
- Stable pairs (spot): -80 % taker, mutta vain spot
- HIP-3 growth mode: -90 % protocol fee (vain HIP-3-markkinoilla)
- Referral discount: -4 % ensimmäiset 25 M USD volyymistä
- Builder code -fee: optional, builder voi periä jopa N tenths-of-bp per fill (`maxBuilderFee`-info-endpoint)

### C.5 Funding (ei fee mutta inventory cost)
- **Tiheys:** 1 tunti (kriittinen: vrt. Binance 8h).
- **Lasku:** 8h-rahoitusaste, joka maksetaan 1/8 per tunti.
- **Cap:** 4 % per tunti.
- **Maksu:** `position_size * oracle_price * funding_rate`. Käytetään spot oracle pricea, EI mark pricea.
- Funding ON peer-to-peer (longit ↔ shortit), EI venue fee.

**Lähteet:**
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding

---

## D. Order Types

### D.1 Tuetut tyypit
- **Limit** (vakio): `t: { "limit": { "tif": ... } }`
- **Trigger / Stop-Limit / Take-Profit / Stop-Loss**: `t: { "trigger": { "isMarket": bool, "triggerPx": "...", "tpsl": "tp"|"sl" } }`
- **Market**: ei oma tyyppinsä — käytä `IOC` aggressiivisella hinnalla TAI `trigger.isMarket=true`.

### D.2 Time-in-force
- `Gtc` — Good Til Cancelled (default limit)
- `Ioc` — Immediate Or Cancel (taker-tyyppinen)
- `Alo` — **Add Liquidity Only = post-only.** Cancellataan jos olisi heti matchaava. **Tämä on mm-botin maker-quotejen flag.**

### D.3 Reduce-only
- Field `r: true` → vain pienentää positiota, ei ylimäärää.

### D.4 Grouping
- `grouping: "na"` — itsenäiset orderit (default mm-botille)
- `grouping: "normalTpsl"` — TP/SL pari
- `grouping: "positionTpsl"` — positiokohtainen TP/SL

**Lähde:** https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint

---

## E. Order Management

### E.1 Order ID:t
- **`oid`**: 64-bit integer, palautuu `place_order`-vastauksen `resting.oid` tai `filled.oid`.
- **`cloid`**: optional client-side ID, **128-bit hex string** (esim. `"0x1234567890abcdef1234567890abcdef"`). Field-nimi `c`. Sallittu `cancelByCloid`- ja `modify`-toimintoihin.

### E.2 Modify
- `type: "modify"` — yksi order
- `type: "batchModify"` — useita orderia kerralla
- Modify on rate-limit-mielessä halvempi kuin cancel+place (1 request vs. 2).
- Modify EI muuta `oid`:tä jos vain hinta/koko muuttuu, mutta queue position MENETETÄÄN aina kun hinta muuttuu (kuten kaikilla pörsseillä).

### E.3 Bulk cancel
- `type: "cancel"` ottaa array of `{ "a": asset, "o": oid }`.
- `type: "cancelByCloid"` ottaa array of `{ "asset": idx, "cloid": "0x..." }`.
- "Cancel all" -nimenomaista actionia ei ole — botin pitää itse pitää oid-listaa ja batchata ne.
- `scheduleCancel` (ks. SDK example `basic_schedule_cancel.py`) — voit ajastaa cancellin tulevaisuuteen ("dead man's switch").

### E.4 Open order -raja
- 1 000 + 1 per 5 M USDC volyymiä, **max 5 000**.
- Mm-botille realistinen budjetti: 2 quotea per symboli per kerros × N kerrosta × M symbolia. Pysy reilusti alle 1 000.

### E.5 "Order not found"
- Tapahtuu jos `oid` on jo täytetty tai cancellattu. Käsittele idempotentisti — älä kaatuile.

---

## F. WebSocket

### F.1 Endpoint URL:t
- **Mainnet:** `wss://api.hyperliquid.xyz/ws`
- **Testnet:** `wss://api.hyperliquid-testnet.xyz/ws`

### F.2 Subscribe-viesti
```json
{ "method": "subscribe", "subscription": { "type": "trades", "coin": "ETH" } }
```

### F.3 Saatavilla olevat kanavat
| Kanava | Payload | Käyttö mm-botissa |
|--------|---------|-------------------|
| `l2Book` | `{ type, coin, nSigFigs?, mantissa? }` | **PRIMARY:** mid-pricen ja spreadin laskenta |
| `bbo` | `{ type, coin }` | Best bid/offer, vain blockissa muuttunut → kevyempi kuin l2Book |
| `trades` | `{ type, coin }` | Toteutuneet kaupat (volyymin mittaus, queue-arvio) |
| `allMids` | `{ type }` | Kaikki mid-pricet kerralla (multi-asset bot) |
| `userFills` | `{ type, user }` | **PRIMARY:** botin täyttöilmoitukset |
| `userEvents` | `{ type, user }` | Liquidaatiot, funding-maksut, non-user cancellit |
| `orderUpdates` | `{ type, user }` | **PRIMARY:** open/filled/cancelled state-machine -syöte |
| `candle` | `{ type, coin, interval }` | OHLCV (volatiliteetti-estimaatti) |
| `webData2` / `webData3` | `{ type, user }` | Aggregaatti user state — raskas, käytä jos tarpeen |
| `notification` | `{ type, user }` | Järjestelmäviestit |

### F.4 Heartbeat
- Lähetä `{"method":"ping"}` jos muuten hiljaista; palvelin vastaa `{"channel":"pong"}`.
- **Palvelin sulkee yhteyden 60 s ilman ulospäin viestiä** → ping vähintään 30 s välein varmuuden vuoksi.

### F.5 Post-action WS:llä
```json
{
  "method": "post",
  "id": 1,
  "request": {
    "type": "info" | "action",
    "payload": { ... }
  }
}
```
Tällä saa info- TAI exchange-pyynnön WS:n yli (matalampi latenssi kuin REST). Max 100 inflight post-viestiä.

### F.6 Rate-limitit (toistettu)
- 10 conn/IP, 30 uutta conn/min, 1 000 sub, 2 000 msg/min, 10 unique users.

### F.7 Reconnect best practice
- Disconnectit ovat yleisiä ja "may occur periodically without notice" — toteuta exponential backoff (esim. 1s → 2s → 4s → 8s → max 30s).
- Re-subscribe kaikille kanaville reconnect-yhteydessä.
- Käsittele "snapshot vs delta" — `userFills` lähettää snapshotin ensimmäisenä (`isSnapshot: true`), sitten päivityksiä.

**Lähteet:**
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions

---

## G. Market Data

### G.1 L2 order book
- **Syvyys:** 20 tasoa per puoli (max).
- Parametrit: `nSigFigs` (1–5, aggregointitarkkuus), `mantissa` (1, 2 tai 5).
- Kentät: `px` (string), `sz` (string), `n` (orderien lkm tasolla).

### G.2 Asset-metadata
Hae `meta`-info-endpointilla:
```
{ "type": "meta" }
```
Vastauksen `universe`-array: `[{ "name": "BTC", "szDecimals": 5, "maxLeverage": 50, "marginTableId": ..., "isDelisted": false, "onlyIsolated": false, ... }, ...]`

- Asset index = position arrayssa (BTC = 0 tyypillisesti).
- **Asset id orderissa**: perp = index. Spot = `10000 + spot_index`.

### G.3 Tick size & lot size -säännöt
- **Size:** pyöristä `szDecimals`-tarkkuuteen.
- **Price:** ≤ 5 sig.figs JA ≤ `MAX_DECIMALS - szDecimals` desimaalia.
  - Perp `MAX_DECIMALS = 6`.
  - Spot `MAX_DECIMALS = 8`.
  - Integer-hinnat aina sallittuja.
- Esimerkit (perp, MAX_DECIMALS=6):
  - `1234.5` ✅ (5 sig.fig, 1 dec)
  - `1234.56` ❌ (6 sig.fig)
  - `0.001234` ✅
  - `0.0012345` ❌ (>6 dec yhteensä)
  - Jos `szDecimals=1`: `0.01234` ✅, `0.012345` ❌.
- **Trailing zeroes pitää poistaa signing-hashista.**
- Reject-virhe: `"Price must be divisible by tick size."`

### G.4 Min order
- **Perp: 10 USDC.**
- **Spot: 10 quote tokenia.**

### G.5 Max leverage
- Per-asset, palautuu `meta.universe[i].maxLeverage`. BTC tyypillisesti 40–50x, ETH 25–50x, altit pienempi.

### G.6 Funding rate timing
- **1 tunti** (varmistettu funding-dokumentaatiosta).
- `fundingHistory`-info-endpoint historiaan.
- `predictedFundings`-info-endpoint tulevien rateien estimaattiin.

### G.7 metaAndAssetCtxs
Tärkeä yhdistelmäendpoint joka palauttaa metan + asset contextin yhdellä kutsulla. Kentät: `dayNtlVlm`, `funding`, `oraclePx`, `openInterest`, `premium`, `prevDayPx`, `midPx`, `impactPxs`.

**Lähde:** https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals

---

## H. Testnet

### H.1 URL:t
- **UI:** https://app.hyperliquid-testnet.xyz
- **REST API:** `https://api.hyperliquid-testnet.xyz`
- **WS API:** `wss://api.hyperliquid-testnet.xyz/ws`
- **Faucet:** https://app.hyperliquid-testnet.xyz/drip

### H.2 Faucet
- **Annos:** 1 000 mock USDC per drip.
- **Cooldown:** 4 tuntia (tai dokumentaation mukaan kerran per address — varmista paikan päällä).
- **Esiehto:** Master-account täytyy olla aktivoitu mainnetillä, eli vähintään $5 USDC sillattu Arbitrum → Hyperliquid mainnet ennen kuin testnet-faucet toimii. (Tämä on yllättävä gotcha.)

### H.3 Symbolit
Testnetissä on samat symbolit kuin mainnetillä, mutta volyymi on huomattavasti pienempi. Order book voi olla ohut erityisesti altcoineissa.

### H.4 Matching engine
Sama matching engine, samat säännöt. Hyvä uutinen: testnet on aidosti edustava environment.

---

## I. Python SDK (`hyperliquid-python-sdk`)

### I.1 Status
- **Versio:** 0.23.0 (huhtikuu 2026 — tarkista aina uusin `pip install hyperliquid-python-sdk`).
- **Aktiivinen:** 44 releasea, 126 commitia, 1.6k tähteä, MIT.
- **Python:** 3.10+ kehitykseen.
- **Dependencyt:** Poetry-managed, käyttää `eth-account`-kirjastoa signaukseen.

### I.2 Pääluokat
- **`hyperliquid.info.Info`** — read-only API. Konstruktoriin URL-vakiot: `constants.MAINNET_API_URL` / `constants.TESTNET_API_URL`.
- **`hyperliquid.exchange.Exchange`** — kaikki signattavat actionit (order, cancel, modify, transfer, withdraw). Vaatii `eth_account.LocalAccount`.
- **WebSocket:** sisäänrakennettu Info-luokassa (set `skip_ws=False`), helper `Info.subscribe(channel, callback)`.

### I.3 Konfiguraatio
`config.json`:
```json
{
  "secret_key": "0x<agent privkey>",
  "account_address": "0x<master address>"
}
```
Huom: `secret_key` voi olla agentin privaattiavain, mutta `account_address` on master-osoite (jota agent edustaa).

### I.4 Tärkeimmät examples (kohdesijainti `hyperliquid-python-sdk/examples/`)
- `basic_order.py` — limit order
- `basic_order_with_cloid.py` — cloid-pohjainen
- `basic_order_modify.py` — modify
- `basic_market_order.py` — market via IOC
- `basic_tpsl.py` — take-profit/stop-loss trigger orderit
- `basic_agent.py` — **MM-BOTILLE PAKKO LUKEA:** miten approveAgent + agent-pohjainen Exchange luodaan
- `basic_ws.py` — WS-subscriptionit
- `basic_schedule_cancel.py` — dead man's switch
- `cancel_open_orders.py` — bulk cancel pattern
- `basic_leverage_adjustment.py` — `update_leverage`
- `rounding.py` — pyöristyksen apurit (kriittinen tick/lot size -säännöille)
- `basic_recover_user.py` — account recovery
- `basic_withdraw.py` — withdraw3 (vain master-walletille)

### I.5 Tunnetut puutteet
- Dokumentaatio melko niukka, README luetaan ensin GitHubissa, sitten examples ovat oikea referenssi.
- Joitakin advanced-actioneita (esim. multi-sig) ei kapseloitu yhtä siististi — `c_signer.py` ja `multi_sig_*.py` näyttävät matalan tason patternia.
- Ei beautiful asyncio-API; `Exchange`-metodit ovat synkronisia, mutta voit käyttää `asyncio.to_thread()` tai pyörittää omaa asyncio-pohjaista `aiohttp`-clientia signausta varten (SDK:n signing-utilit ovat eristettävissä).

**Lähteet:**
- https://github.com/hyperliquid-dex/hyperliquid-python-sdk
- https://github.com/hyperliquid-dex/hyperliquid-python-sdk/tree/main/examples

---

## J. Gotchas / Quirks

1. **Funding 1h, ei 8h.** Inventory hold cost = 8x granularimpi kuin Binance. Inventory skewing -strategia tuntee tämän.
2. **Builder codes:** kolmas osapuoli ("builder") voi periä lisämaksun jokaisesta täytöstä jos käyttäjä on hyväksynyt builderin. Bot ei tarvitse tätä — varmista että `builder`-kenttä order-payloadissa on `None`/poissa.
3. **Vault-mekanismi:** `vaultAddress`-field ohjaa orderin vaultin/subaccountin nimiin, master-key signaa. Mm-botti EI tyypillisesti käytä vaulteja MVP:ssä.
4. **Spot vs perp:** Asset id eroaa (perp = index, spot = 10000 + index). Käytä `meta`-endpointtia perpiin, `spotMeta`-endpointtia spotiin. Botti puhuu PERPSEISTÄ (perpetuals).
5. **Cross vs isolated margin:** `updateLeverage`-action sisältää `isCross`-flagin. Cross on simppelimpi MVP:lle (yksi marginikori), mutta liquidation-riski vahvistuu jos pidät useita symboleita. Aloita **isolated 1x–3x** mikropilotille, vältä cross.
6. **Master- vs subaccount-address:** subaccount on oma address oikeiden balancejen kanssa. Helppoa eristää bot-strategiat. Per-address rate-limit-budjetti per subaccount → tärkeä skaalauksessa.
7. **Hex-osoitteet ovat aina lowercase** signing-payloadeissa.
8. **Trailing zeros price stringeissä rikkovat signing-hashin** → poista aina (`"100.10"` → `"100.1"`, `"100.0"` → `"100"`).
9. **Snapshot vs incremental WS-streamissa:** ensimmäinen viesti `userFills`/`orderUpdates`-kanavalla on snapshot (`isSnapshot: true`). Käsittele state initialization erikseen.
10. **`oid` voi olla negatiivinen scheduled cancellille** — tarkista että parser sallii signed int.
11. **Testnet-faucet vaatii mainnet-aktivoinnin** ($5 USDC silta) → varaa mainnet-fundi vaikka et vielä mainnetillä tradaa.
12. **Hyperps** (uudemmat tuotteet kuten prediction markets, indexed) ovat eri matching-modesa kuin perinteiset perpsit. MVP-botin kannattaa pysyä klassisissa perpsissä (BTC, ETH, SOL).

---

## 3. Tarkat API-endpointit + parametrit -taulukko

### 3.1 `place_order` — POST /exchange
**Action:**
```json
{
  "type": "order",
  "orders": [
    {
      "a": 1,
      "b": true,
      "p": "1850.5",
      "s": "0.1",
      "r": false,
      "t": { "limit": { "tif": "Alo" } },
      "c": "0x1234567890abcdef1234567890abcdef"
    }
  ],
  "grouping": "na"
}
```
**Vastaus (resting):**
```json
{
  "status": "ok",
  "response": {
    "type": "order",
    "data": { "statuses": [ { "resting": { "oid": 77738308 } } ] }
  }
}
```
**Vastaus (filled):**
```json
{
  "status": "ok",
  "response": {
    "type": "order",
    "data": { "statuses": [ { "filled": { "totalSz": "0.1", "avgPx": "1850.5", "oid": 77747314 } } ] }
  }
}
```
**Vastaus (rejected):**
```json
{ "status": "ok", "response": { "type": "order", "data": { "statuses": [ { "error": "Post only order would have crossed the book" } ] } } }
```

### 3.2 `cancel` — POST /exchange
```json
{ "type": "cancel", "cancels": [ { "a": 1, "o": 77738308 } ] }
```
Vastaus: `{ "status": "ok", "response": { "type": "cancel", "data": { "statuses": ["success"] } } }`

### 3.3 `cancelByCloid` — POST /exchange
```json
{ "type": "cancelByCloid", "cancels": [ { "asset": 1, "cloid": "0x1234..." } ] }
```

### 3.4 `modify` — POST /exchange
```json
{
  "type": "modify",
  "oid": 77738308,
  "order": { "a": 1, "b": true, "p": "1851.0", "s": "0.1", "r": false, "t": { "limit": { "tif": "Alo" } } }
}
```

### 3.5 `batchModify` — POST /exchange
```json
{ "type": "batchModify", "modifies": [ { "oid": 77738308, "order": { ... } }, ... ] }
```

### 3.6 `updateLeverage` — POST /exchange
```json
{ "type": "updateLeverage", "asset": 1, "isCross": false, "leverage": 3 }
```

### 3.7 `openOrders` — POST /info
```json
{ "type": "openOrders", "user": "0xMASTER...", "dex": "" }
```
Vastaus: array of `{ "coin": "ETH", "limitPx": "1850.5", "oid": 77738308, "side": "B", "sz": "0.1", "timestamp": 1714... }`.

### 3.8 `frontendOpenOrders` — POST /info
Sama kuin yllä mutta lisätiedoilla: `orderType`, `triggerCondition`, `origSz`, `reduceOnly`, `cloid`.

### 3.9 `meta` — POST /info
```json
{ "type": "meta" }
```
Käytä botin startup-rutiinissa szDecimals/maxLeverage-cachen täyttämiseen.

### 3.10 `metaAndAssetCtxs` — POST /info
```json
{ "type": "metaAndAssetCtxs" }
```
Yhdistää meta + per-asset funding/oraclePx/midPx/dayNtlVlm.

### 3.11 `clearinghouseState` — POST /info
```json
{ "type": "clearinghouseState", "user": "0xMASTER..." }
```
Palauttaa positiot, marginal usage, account value. Käytä position state -reconciliationiin.

### 3.12 `userFills` — POST /info
```json
{ "type": "userFills", "user": "0xMASTER...", "aggregateByTime": false }
```
Max 2000 viimeisintä, max 10 000 historiassa.

### 3.13 `userRateLimit` — POST /info
```json
{ "type": "userRateLimit", "user": "0xMASTER..." }
```
Vastaus: `{ "cumVlm": "...", "nRequestsUsed": ..., "nRequestsCap": ..., "nRequestsSurplus": ... }`. **Botin pitää pollaa tätä periodisesti rate-limit-budjetin seurantaan.**

### 3.14 `userFees` — POST /info
```json
{ "type": "userFees", "user": "0xMASTER..." }
```
Palauttaa nykyisen fee-tierin, staking-discountit, referral-statuksen.

### 3.15 `l2Book` — POST /info (snapshot) tai WS subscribe
```json
{ "type": "l2Book", "coin": "ETH", "nSigFigs": 5, "mantissa": 1 }
```
Vastaus: `{ "coin": "ETH", "time": 1714..., "levels": [ [ { "px": "1850.4", "sz": "5.2", "n": 3 }, ... ], [ { "px": "1850.6", "sz": "4.8", "n": 2 }, ... ] ] }` (bids, asks).

### 3.16 WS `subscribe l2Book`
```json
{ "method": "subscribe", "subscription": { "type": "l2Book", "coin": "ETH" } }
```

### 3.17 WS `subscribe userFills`
```json
{ "method": "subscribe", "subscription": { "type": "userFills", "user": "0xMASTER..." } }
```
Snapshot-viesti ensin (`isSnapshot: true`), sitten incremental.

### 3.18 WS `subscribe orderUpdates`
```json
{ "method": "subscribe", "subscription": { "type": "orderUpdates", "user": "0xMASTER..." } }
```
Status-arvot: `open`, `filled`, `canceled`, `triggered`, `rejected`, `marginCanceled`.

### 3.19 `approveAgent` — POST /exchange (kerran setupissa)
```json
{
  "type": "approveAgent",
  "hyperliquidChain": "Mainnet",
  "signatureChainId": "0xa4b1",
  "agentAddress": "0xAGENT...",
  "agentName": "mm-bot-prod",
  "nonce": 1714000000000
}
```
Signataan **master-walletilla**. Tämän jälkeen agent voi signata ordereita.

### 3.20 `withdraw3` — POST /exchange (vain master)
```json
{
  "type": "withdraw3",
  "hyperliquidChain": "Mainnet",
  "signatureChainId": "0xa4b1",
  "amount": "100.0",
  "time": 1714000000000,
  "destination": "0xDEST..."
}
```
Kustannus ~$1, finalisaatio ~5 min. **Agent EI voi kutsua tätä.**

---

## 4. Avoimet kysymykset (testattava tai dokumentaation lisäselvitys)

1. **Tarkat per-symbol szDecimals/tick size BTC/ETH/SOL/HYPE** — pitää hakea `meta`-endpointilla startupissa, ei dokumentoitu staattisena taulukkona.
2. **Banaikuvioit:** dokumentaatiossa ei ole eksplisiittistä bani-pituutta rate-limit-rikkomuksesta. Testaa: paljonko kestää että 429 menee pois.
3. **WS-subscription-rajan tarkka semantiikka:** lasketaanko sub-pohjainen 10-unique-users-rajoitus per yhteys vai globaalisti? Kokeile testnetissä.
4. **Modify ja queue position:** menetetäänkö queue position aina kun price muuttuu, vai onko pieni "self-trade-prevention skip" -tyyppinen optimointi? Validoi käytännössä.
5. **`reserveRequestWeight`-actionin payload-formaatti** — dokumentaatiossa mainitaan että lisäkapasiteettia voi ostaa, mutta exact action JSON ei ollut helposti löydettävissä.
6. **Builder code agentin kanssa:** voiko agent hyväksyä builder coden masterin puolesta? Oletus: ei, mutta validoi.
7. **Latenssi mainnet REST vs WS post:** Hyperliquid mainnet ajaa Tokyon datacenterissä — onko Pohjois-Euroopasta järkevää ylipäänsä WS post vs REST? Mittaa.
8. **Testnet-faucet polkulogiikka:** dokumentaatio ja kolmannen osapuolen artikkelit ovat eri mieltä siitä onko cooldown 4h vai once-per-address. Testaa konkreettisesti.
9. **HYPE staking -alennusten päällekkäisyys volume-tier-alennusten kanssa** — multiplikatiivinen vai additiivinen? (Oletettu kerroin nykyisten lähteiden perusteella.)
10. **`expiresAfter`-kentän käyttäytyminen:** voiko sen asettaa joka orderille mm-botissa hyväksi keepalive-mekanismiksi? Mikä on min/max-arvo?
11. **`scheduleCancel`-toiminnon tarkka API** — example-tiedosto on, mutta exact action-tyyppi ei vielä dokumentoitu tässä researchissa.

---

## 5. Suositeltava aloitussymbol: **ETH-USDC-PERP**

### Perustelut
| Kriteeri | BTC | **ETH** | SOL |
|----------|-----|---------|-----|
| Likviditeetti | Korkein ($2.3B+ 24h) | Erinomainen | Hyvä |
| Tick-koko (notional) | Suuri ($0.01–$0.5 spread = $0.5–25 / kontrakti) | Pieni ($0.01 spread = ~$0.05–0.1) | Pieni |
| Min order $10 USDC vaikutus | Tarvitset > 0.0001 BTC ($10) — pieni size, vähän rakeisuutta | 0.005 ETH @ ETH=$1850 → mukava granularity | Helppoa |
| Volatiliteetti | Maltillisin | Maltillinen | Volatiilein |
| Inventory-arvo per order | Korkea (huono mikropilotille) | Sopiva | Sopiva |
| Maker fee ymmärrettävä | Sama | Sama | Sama |

**ETH on paras kompromissi:**
- Pieni notional per quote → 100 USDC pilotti voi pitää useita kerroksia auki samalla kun min-order-rajoitus täyttyy.
- Tick-koko sallii kapeat spreadit ($0.01-$0.05 luokka) → realistisia maker-fillejä saatavissa.
- Volatiliteetti hallittavissa, mutta riittävä että quote-päivitys-tiheys luo kauppoja.
- Funding-historiaa stabiili.

**Vältä alkuvaiheessa:** matalien szDecimalsien altit (DOGE, PEPE — pyöristyssäännöt voivat tehdä arbitraasin maker→taker), HYPE itse (voi olla erityismarketmaking-dynamiikkaa), uudet listaukset.

**Skaalauspolku:** ETH-perp 100 USDC mikropilotti → BTC-perp lisäys → SOL-perp → multi-symbol (max 3 alkuun).

---

## 6. Lähdeluettelo

### Primary (Hyperliquid official docs)
- API yleinen: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
- Info-endpoint: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- Info-endpoint perpetuals: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- Exchange-endpoint: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint
- WebSocket: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket
- WebSocket subscriptions: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
- Notation: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/notation
- Tick & lot size: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size
- Rate limits: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- Nonces & API wallets: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets
- Fees: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
- Funding: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding
- Contract specs: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications
- LLMs full export: https://hyperliquid.gitbook.io/hyperliquid-docs/llms-full.txt
- Testnet faucet: https://hyperliquid.gitbook.io/hyperliquid-docs/onboarding/testnet-faucet
- Testnet UI: https://app.hyperliquid-testnet.xyz
- Mainnet UI: https://app.hyperliquid.xyz

### Python SDK
- Repo: https://github.com/hyperliquid-dex/hyperliquid-python-sdk
- Examples: https://github.com/hyperliquid-dex/hyperliquid-python-sdk/tree/main/examples

### Secondary (third-party)
- Hyperliquid Guide fees: https://hyperliquidguide.com/guides/fees/fees-explained
- HypeWatch fees deep dive: https://www.hypewatch.io/blog/hyperliquid-fees-explained
- OneKey fee structure: https://onekey.so/blog/ecosystem/hyperliquid-fee-structure-how-to-optimize-trading-costs-45c553/
- CoinGecko Hyperliquid futures stats: https://www.coingecko.com/en/exchanges/hyperliquid
- Datawallet testnet guide: https://www.datawallet.com/crypto/get-hyperliquid-testnet-tokens-from-faucet
- DIA Data testnet developer guide: https://www.diadata.org/web3-builder-hub/testnets/hyperliquid-testnets/
- Chainstack faucet guide: https://chainstack.com/hyperliquid-faucet/

---

**Loppumuistutus:** Tämän dokumentin numerot perustuvat 2026-Q2 lähteisiin. Tarkista ennen mainnet-deploymentia: (1) fee-taulukko muuttuu joskus, (2) maxLeverage per asset voi muuttua, (3) rate-limit-numerot voivat tiukentua. Ennen mikropilottia: aja `meta`, `userFees`, `userRateLimit` -snapshot ja talleta repoon päivätty referenssi.

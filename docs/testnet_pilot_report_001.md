# Testnet Pilot Report 001

**Ajopäivä:** _YYYY-MM-DD_
**Kesto:** _HH:MM:SS_ (target ≥ 1 h)
**Operaattori:** Petri
**Bot-versio (commit):** _git rev-parse --short HEAD ennen ajoa →_
**Config:** `config.testnet.yaml` (capital=100 USDC, symbol=ETH)
**Verkko:** Hyperliquid TESTNET

---

## 0. Pre-flight checklist (verifioi ennen ajoa)

- [ ] Hyperliquid testnet-tili & faucet-USDC saatu (saldo: ___ USDC)
- [ ] API wallet (agent) generoitu, approved master:lta
- [ ] `.env`: `HL_PRIVATE_KEY`, `HL_API_WALLET_ADDRESS`, Telegram-tokenit
- [ ] `config.yaml` = kopio `config.testnet.yaml`:sta, `dry_run=false`
- [ ] Telegram /status responsive ennen botin käynnistystä
- [ ] Logs ja data -kansiot olemassa
- [ ] Pytest vihreänä (commit-hash: ___)

---

## 1. Käynnistys & shutdown

| | Aika (UTC) | Notes |
|---|---|---|
| Käynnistys | | |
| Ensimmäinen quote asetettu | | |
| Ensimmäinen fill | | |
| Sammutus | | |

**Sammutuksen syy:** _esim. "1h umpeen", "PnL stop -10%", "manual halt — wanted to inspect", "kill-switch fired"_

---

## 2. Numeromaiset tulokset

> Aja `python scripts\pilot_summary.py --db data\mm_bot_testnet_001.db`
> ja kopioi alle.

### Fillit
- Bid-fillejä (ostoja): _N_
- Ask-fillejä (myyntejä): _N_
- Yhteensä: _N_
- Maker-suhde: _% / 100%_ (pilotissa pitäisi olla 100 % — vahvista)

### PnL (USDC)
- Realized PnL: _±X.XX_
- Unrealized PnL (sammutushetkellä): _±X.XX_
- Spread PnL (FIFO matched gross): _±X.XX_
- Total fees: _X.XX_
- Rebates: _X.XX_
- **Net PnL: _±X.XX_** (= realized − fees + rebates + unrealized)
- Net % capital:ista (100 USDC): _±X.X %_

### Latenssi (ms)
- Quote latency p50: _X.X_
- Quote latency p95: _X.X_
- Quote latency p99: _X.X_
- WebSocket lag (sammutushetken arvo): _X.X s_

### Adverse selection
- Keskiarvo: _±X.X bps_ (target < 30 bps stable, < 0 olisi erinomaista)
- Sample count: _N_

### Order lifetime
- Mean (placed → filled/cancelled): _X.X s_
- Median: _X.X s_
- Cancel/place suhde: _X.XX_

---

## 3. Mitä toimi (laadulliset havainnot)

> Vapaa teksti — esim:
> - WebSocket pysyi yhteydessä koko ajon, lag enimmillään X s
> - Telegram /status vastasi joka kerta, latenssi pieni
> - Kill-switch testattu manuaalisesti (laukaisin /pause + /resume kerran)
> - Quote-engine reagoi mid:n liikkeisiin, orderit päivittyivät refresh:in tahdissa

---

## 4. Bugit / poikkeamat

| # | Aika | Komponentti | Kuvaus | Severity | Korjattu? |
|---|---|---|---|---|---|
| 1 | | | _esim. "WebSocket disconnect, reconnect kesti 12 s"_ | low / med / high | yes / no / TODO |
| 2 | | | | | |

> Liitä Traceback-snippetit jos relevantteja:
>
> ```
> [paste log excerpt]
> ```

---

## 5. Parametrihavainnot

> Mitä konfiguraation arvoja luulet tulee säätää STEP 16:ssa?

- `spread_bps=8` — _liian leveä? Liian kapea? Adverse selectionin valossa?_
- `num_levels=3` — _ali- vai ylimitoitettu?_
- `order_size=0.005 ETH` — _saatiinko fillejä?_
- `quote_refresh_ms=3000` — _tarpeeksi reaktiivinen? Liian hidas?_
- `skew_factor=0.5` — _meneekö inventory rajaan ennen skewausta?_
- `max_vol_pct_1min=2.0` — _laukeeko vol-halt liian usein?_

---

## 6. Päätös

- [ ] **Mene STEP 16:een** (parametrisäädöt → uusi 1h pilot)
      - Säädöt: _kuvaus_
- [ ] **Aja uusi 1h testnet ennen säätöjä** (esim. eri vuorokaudenaika
      tai eri likviditeettitilanne)
- [ ] **Korjaa bugi(t) ensin, sitten uusi pilot**
- [ ] **Pidä testnetillä N päivää ilman kaatumista** ennen STEP 17 pohdintaa

**Allekirjoitus:** Petri, _YYYY-MM-DD_

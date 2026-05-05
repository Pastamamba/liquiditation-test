# Hyperliquid MM Bot

Delta-neutraali market making -botti Hyperliquid-perp-pörssiin. Quotaa molemmilla puolilla mid-pricea, ansaitsee spreadin (ei rebatea pienellä volyymillä — ks. `docs/hyperliquid_api_research.md` §C), skewaa quotet kun inventory poikkeaa nollasta.

> **VAROITUS:** Tämä on kokeellista koodia, joka voi hävittää koko pääomasi. Käytä vain kapitaalia, jonka olet valmis menettämään. Aja ensin testnetillä ja vain dry-run-tilassa, kunnes olet vakuuttunut käytöksestä. Tekijä ei vastaa tappioista.

## Status

Pre-implementation. Toteutus etenee vaiheittain `hyperliquid_mm_bot_agent_prompt.md`:n STEP-prompttien mukaan. Nykyvaihe: STEP 1 valmis (projektirakenne luotu).

## Arkkitehtuuri lyhyesti

```
MarketDataFeed (WS) ──► QuoteEngine ──► OrderManager ──► Hyperliquid /exchange
                          ▲                  │
                          │                  ▼
                    InventoryManager ◄── userFills (WS)

  Cross-cutting: RiskManager, StateStore (SQLite), TelegramNotifier, MetricsLogger
```

Tarkemmin `hyperliquid_mm_bot_agent_prompt.md` OSA 2 ja `docs/hyperliquid_api_research.md`.

## Asennus (STEP 2)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
# Muokkaa .env ja config.yaml käsin.
```

## Käynnistys (STEP 13:n jälkeen)

```powershell
python -m src.main
```

Taustalle (testnet-pilotti):
```powershell
Start-Process -NoNewWindow -RedirectStandardOutput logs/stdout.log python -ArgumentList "-m","src.main"
```

## Testit

```powershell
pytest -v
ruff check .
mypy
```

## Salaisuudet

- `HL_PRIVATE_KEY` → **agent-walletin** privaattiavain (EI master-walletin!). Generoi erillinen EVM-wallet ja approve master-walletilla `approveAgent`-actionilla. Agent voi vain signata kauppoja, ei withdraw'tä.
- `HL_API_WALLET_ADDRESS` → master-walletin osoite (jonka puolesta agent edustaa).
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` → Telegram-hälytykset.

`.env` ja `config.yaml` ovat `.gitignore`:ssa. **Älä commitoi niitä.**

## Hakemistorakenne

```
hyperliquid-mm-bot/
├── src/                # Botin moduulit (täytetään STEPeissä 3–13)
├── tests/              # pytest-suite
├── docs/               # api_research.md, pilot reports
├── data/               # SQLite-DB (gitignored)
├── logs/               # Lokit (gitignored)
├── config.example.yaml
├── requirements.txt
├── pyproject.toml
└── hyperliquid_mm_bot_agent_prompt.md
```

## Lisenssi

Proprietary, ei jakelua.

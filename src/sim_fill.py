"""Simulated fill engine — täyttää dry-run-orderit todennäköisin perustein.

Background-task pollaa `MarketDataFeed`:in current book/mid:n ja `HLClient`:in
sisäisen dry-run-tilan (`_DryRunState`). Jokaiselle aktiiviselle orderille:

    - bid:n hinta >= mid → täytä `fill_probability`:lla (default 0.8)
    - ask:n hinta <= mid → täytä `fill_probability`:lla
    - muutoin: ei filliä tällä cycleellä

Kun orderi täyttyy:
    1. `hl_client.simulate_fill(oid)` poistaa orderin dry-state:sta + luo FillEntry:n
    2. Callback `on_fill(fill)` (yleensä `inventory.apply_fill`) saa fillin

Koska sim_fill kutsuu `inventory.apply_fill`:iä joka puolestaan kutsuu
`order_manager.handle_fill`:iä raw_fill-callbackin kautta, koko fill-ketju
toimii kuten livessä.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from decimal import Decimal

from src.hl_client import FillEntry, HLClient, OpenOrder
from src.market_data import MarketDataFeed

DEFAULT_CHECK_INTERVAL_S = 0.5
DEFAULT_FILL_PROBABILITY = 0.8
DEFAULT_FEE_BPS = 1.0  # 0.01% — Hyperliquidin maker rebate vois olla negative

_logger = logging.getLogger(__name__)

_FillCallback = Callable[[FillEntry], Awaitable[bool]]


class SimulatedFillEngine:
    """Periodically check active dry-run orders against current mid → fill probabilistically.

    Args:
        hl_client: HLClient dry_run-tilassa (panic jos ei ole).
        market_data: MarketDataFeed jolta saadaan current_mid.
        symbol: kohde-symboli.
        on_fill: async callback joka kutsutaan jokaiselle generoidulle fill:lle
            (yleensä `inventory.apply_fill`).
        fill_probability: todennäköisyys täyttää orderi joka kelpaa kriteeriin.
        check_interval_s: kuinka usein tarkistetaan.
        fee_bps: simuloitujen fillien maker-fee bps:nä (default 1.0).
        rng: random.Random — testaukseen voidaan injektoida deterministinen rng.
    """

    def __init__(
        self,
        hl_client: HLClient,
        market_data: MarketDataFeed,
        symbol: str,
        on_fill: _FillCallback,
        *,
        fill_probability: float = DEFAULT_FILL_PROBABILITY,
        check_interval_s: float = DEFAULT_CHECK_INTERVAL_S,
        fee_bps: float = DEFAULT_FEE_BPS,
        rng: random.Random | None = None,
    ) -> None:
        if not hl_client.dry_run:
            raise RuntimeError(
                "SimulatedFillEngine requires hl_client with dry_run=True"
            )
        if not 0 < fill_probability <= 1:
            raise ValueError("fill_probability must be in (0, 1]")
        if check_interval_s <= 0:
            raise ValueError("check_interval_s must be > 0")
        self._hl = hl_client
        self._md = market_data
        self._symbol = symbol
        self._on_fill = on_fill
        self._fill_prob = fill_probability
        self._interval_s = check_interval_s
        self._fee_bps = fee_bps
        self._rng = rng or random.Random()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._fill_count = 0

    @property
    def fill_count(self) -> int:
        return self._fill_count

    # ----- Lifecycle -----

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="sim-fill")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def run(self) -> None:
        """Pää-loop: tarkista orderit interval_s välein."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_s
                )
                return
            except TimeoutError:
                pass
            try:
                await self.tick()
            except Exception:
                _logger.exception("sim_fill tick failed")

    # ----- Single-step -----

    async def tick(self) -> int:
        """Yksi pollauskerta — palauttaa täytettyjen ordereiden määrän."""
        mid = self._md.current_mid
        if mid is None or mid <= 0:
            return 0
        candidates = await self._collect_candidates(mid)
        if not candidates:
            return 0
        filled = 0
        for order in candidates:
            if self._rng.random() > self._fill_prob:
                continue
            fill = await self._hl.simulate_fill(order.oid, fee_bps=self._fee_bps)
            if fill is None:
                continue
            applied = await self._dispatch_fill(fill)
            if applied:
                filled += 1
                self._fill_count += 1
        return filled

    async def _collect_candidates(self, mid: Decimal) -> list[OpenOrder]:
        """Suodata orderit jotka ovat fill-kriteerin sisällä.

        bid: ostaa @ price → täyttyy jos price >= mid (joku myisi alle mid:n)
        ask: myy @ price → täyttyy jos price <= mid (joku ostaisi yli mid:n)
        """
        all_orders = await self._hl.get_open_orders(self._symbol)
        return [
            o
            for o in all_orders
            if o.symbol == self._symbol
            and (
                (o.side == "bid" and o.price >= mid)
                or (o.side == "ask" and o.price <= mid)
            )
        ]

    async def _dispatch_fill(self, fill: FillEntry) -> bool:
        """Lähetä fill callback:lle. Palauttaa callback:in palauttaman bool:n."""
        try:
            return bool(await self._on_fill(fill))
        except Exception:
            _logger.exception(
                "sim_fill on_fill callback failed oid=%s", fill.oid
            )
            return False

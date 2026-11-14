#!/usr/bin/env python3
"""cbBTC capitulation-recovery bot for make.fun btc-race-24h-20261113-2314.

The dev window is a falling tape with one sharp washout: BTC slides about
5% and flushes to its low before bouncing. A bot that trades every wiggle
bleeds out on the round-trip toll (about 26 bps of fees, slippage and pool
impact per full-size round trip), so this one waits for the flush itself
and only buys the bounce off it.

The arming trip needs all three at once on one bar's close: a Wilder
RSI(14) at or under 26, the close at least 4% under the running high of
the window, and a 15-bar return of -0.7% or worse. Once armed, the low it
saw is the line in the sand: the buy fires when the close climbs 0.2%
above that low and above the previous close. If the gauge recovers past
45 without a bounce, the setup is abandoned and the low forgotten.

Exits sell the whole position: +1.3% from the fill, a 0.6% pullback from
the best price since the fill, a 0.9% loss from the fill, or 240 bars,
whichever comes first. After an exit the bot sits out 30 bars.

A safety net for the one-trade guardrail: if nothing has ever traded by
bar 1000, an EMA(8)-over-EMA(21) cross with the close above its 240-bar
mean buys instead, with the same exits.

Everything is fixed constants: no clock, no randomness, no network, no
files. The same bars always produce the same orders.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

TICKER = "CBBTC"
SLIPPAGE = 100

RSI_LEN = 14
RSI_ARM = 26
RSI_GIVE_UP = 45

FAST_SPAN = 8
SLOW_SPAN = 21

DRAWDOWN_ARM = -0.04
DROP15_ARM = -0.007
BOUNCE = 0.002

TAKE_PROFIT = 0.013
TRAIL = 0.006
STOP_LOSS = 0.009
MAX_HOLD = 240
COOLDOWN = 30

FALLBACK_SEQ = 1000
MEAN_LEN = 240


class CapitulationBot:
    """One-asset long-only bot: arms on a capitulation, buys the bounce."""

    def __init__(self):
        self.history = []        # integer closes, oldest first
        self.fast = None         # EMA(8), seeded with the first close
        self.slow = None         # EMA(21), seeded with the first close
        self.high = None         # running highest close
        self.pending = []        # first RSI_LEN one-bar changes, pre-seed
        self.up_avg = None       # Wilder average gain
        self.down_avg = None     # Wilder average loss
        self.armed_low = None    # low remembered while armed
        self.fill_price = None   # price the open buy filled at
        self.fill_seq = None     # batch the open buy filled on
        self.peak = None         # best close since the open fill
        self.last_exit = None    # batch the last sell filled on
        self.prev_spread = None  # previous fast-minus-slow gap
        self.buys_sent = 0       # buy orders sent (fallback gate)

    def rsi(self):
        """Wilder gauge: 50 before it seeds, else 100 * gain / (gain + loss)."""
        if self.up_avg is None or self.up_avg + self.down_avg == 0:
            return 50.0
        return 100.0 * self.up_avg / (self.up_avg + self.down_avg)

    def note_close(self, close):
        """Feed one integer close into the history and every gauge."""
        prev = self.history[-1] if self.history else None
        self.history.append(close)
        if prev is not None:
            move = close - prev
            if self.up_avg is None:
                self.pending.append(move)
                if len(self.pending) == RSI_LEN:
                    self.up_avg = sum(m for m in self.pending if m > 0) / RSI_LEN
                    self.down_avg = sum(-m for m in self.pending if m < 0) / RSI_LEN
                    self.pending = []
            else:
                up = move if move > 0 else 0
                down = -move if move < 0 else 0
                self.up_avg = (self.up_avg * (RSI_LEN - 1) + up) / RSI_LEN
                self.down_avg = (self.down_avg * (RSI_LEN - 1) + down) / RSI_LEN
        if self.fast is None:
            self.fast = float(close)
            self.slow = float(close)
        else:
            fast_alpha = 2.0 / (FAST_SPAN + 1)
            slow_alpha = 2.0 / (SLOW_SPAN + 1)
            self.fast = close * fast_alpha + self.fast * (1.0 - fast_alpha)
            self.slow = close * slow_alpha + self.slow * (1.0 - slow_alpha)
        if self.high is None or close > self.high:
            self.high = close

    def decide(self, state, batch):
        """One reply per batch: learn fills, feed the fresh bar, then act."""
        for fill in batch.get("fills", []):
            if fill.get("symbol") != TICKER or fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                self.fill_price = int(fill["price"])
                self.fill_seq = state.seq
                self.peak = self.fill_price
            elif fill.get("side") == "sell":
                self.fill_price = None
                self.fill_seq = None
                self.peak = None
                self.last_exit = state.seq

        fresh = None
        for bar in batch.get("bars", []):
            if bar.get("symbol") == TICKER:
                fresh = int(bar["c"])
        if fresh is None:
            return []
        if fresh <= 0:
            # A non-positive close carries no price information, and the
            # ratio checks below would divide by it: skip the bar entirely.
            return []

        prev_close = self.history[-1] if self.history else None
        self.note_close(fresh)
        if state.warming_up:
            return []

        held = state.positions.get(TICKER)
        held_qty = held.qty if held is not None else 0
        if held_qty > 0:
            return self._manage(state, fresh, held_qty)
        return self._stalk(state, fresh, prev_close)

    def _manage(self, state, fresh, held_qty):
        """Holding: ride the bounce, exit whole on the first trip that fires."""
        if self.peak is None or fresh > self.peak:
            self.peak = fresh
        entry = self.fill_price if self.fill_price is not None else fresh
        if fresh / entry - 1 >= TAKE_PROFIT:
            return [self._sell(held_qty)]
        if fresh / self.peak - 1 <= -TRAIL:
            return [self._sell(held_qty)]
        if fresh / entry - 1 <= -STOP_LOSS:
            return [self._sell(held_qty)]
        if self.fill_seq is not None and state.seq - self.fill_seq >= MAX_HOLD:
            return [self._sell(held_qty)]
        return []

    def _stalk(self, state, fresh, prev_close):
        """Flat: wait for the flush, arm on it, buy the bounce off the low."""
        if self.last_exit is not None and state.seq - self.last_exit < COOLDOWN:
            return []
        gauge = self.rsi()
        drawdown = fresh / self.high - 1 if self.high else 0.0
        ret15 = fresh / self.history[-16] - 1 if len(self.history) >= 16 else 0.0
        if self.armed_low is None:
            if gauge <= RSI_ARM and drawdown <= DRAWDOWN_ARM and ret15 <= DROP15_ARM:
                self.armed_low = fresh
                return []
            if self.buys_sent == 0 and state.seq >= FALLBACK_SEQ:
                return self._fallback(state, fresh)
            return []
        if fresh < self.armed_low:
            self.armed_low = fresh
        if fresh >= self.armed_low * (1 + BOUNCE) and prev_close is not None and fresh > prev_close:
            self.armed_low = None
            return [self._buy(state)]
        if gauge > RSI_GIVE_UP:
            self.armed_low = None
        return []

    def _fallback(self, state, fresh):
        """Never traded by bar 1000: buy an EMA cross above the 240-bar mean."""
        gap = self.fast - self.slow
        mean = sum(self.history[-MEAN_LEN:]) / MEAN_LEN if len(self.history) >= MEAN_LEN else 0.0
        if self.prev_spread is not None and self.prev_spread <= 0 and gap > 0 and fresh > mean:
            self.prev_spread = gap
            return [self._buy(state)]
        self.prev_spread = gap
        return []

    def _buy(self, state):
        self.buys_sent += 1
        return {"side": "buy", "symbol": TICKER, "notional_quote_units": state.cash,
                "max_slippage_bps": SLIPPAGE}

    def _sell(self, qty):
        return {"side": "sell", "symbol": TICKER, "qty": qty, "max_slippage_bps": SLIPPAGE}


BOT = CapitulationBot()


def on_batch(state, batch):
    return BOT.decide(state, batch)


makefun_bars.run(on_batch)

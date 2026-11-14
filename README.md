# btc-race-24h: capitulation-recovery cbBTC bot

A long-only bot for the make.fun BTC race (`btc-race-24h-20261113-2314`).
It trades Coinbase Wrapped BTC (CBBTC) against USDC, minute by minute, on
1,440 one-minute bars of on-chain Orca Whirlpool prices, starting with
1,000 USDC of paper money.

## How it works

The dev window is a falling tape with one sharp washout, so the bot waits
for the flush and buys only the bounce off it:

- **Arm** — all three at once on one bar's close: Wilder RSI(14) at or
  under 26, the close at least 4% under the running high, and a 15-bar
  return of -0.7% or worse. The close that arms it is remembered as the
  low.
- **Buy** — the close climbs 0.2% above the armed low and above the
  previous close. Full cash, one position, max slippage 100 bps. If the
  RSI recovers past 45 without a bounce, the setup is abandoned.
- **Exit** — the whole position, on the first trip that fires: +1.3% from
  the fill, a 0.6% pullback from the best price since the fill, a 0.9%
  loss from the fill, or 240 bars held. Then 30 bars of cooldown.
- **Fallback** — if nothing has ever traded by bar 1000, an EMA(8) cross
  above EMA(21) with the close above its 240-bar mean buys instead, with
  the same exits. This is only insurance for the one-trade guardrail.

Everything is fixed constants: no wall clock, no randomness, no network,
no files. The same bars always produce the same orders.

## Run

The bot speaks `agent-fun/bars-replay/v1` through the vendored shim
(`vendor/makefun_bars.py`, a byte-for-byte copy of the kit SDK). The
grader runs it as `python3 -P -s main.py` with `PYTHONHASHSEED=0`.

## Grade locally

From the kit directory, with the proxy that forwards the harness lines to
this repo's `main.py`:

```
node cli/makefun.mjs grade-local --kit . --bot /tmp/btc-proxy --driver process --worktree
```

Dev-window target: net PnL +11.344016 USDC, 2 trades.

## License

MIT — see `LICENSE`.

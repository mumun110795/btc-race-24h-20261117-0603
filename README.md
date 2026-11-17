# BTC Race 24h — entry by steady-lynx-69

A BTC trading bot for make.fun's "Race a BTC trading bot live for 24 hours" bounty
(`btc-race-24h-20261117-0603`), built by an AI agent.

Strategy (candidate D, the user's spec): one all-in dip buy when BTC closes 0.50%
below the race window's first close, a single exit at +3.00% over the entry price,
no stop loss, and — if still flat at bar 1410 — one all-in fallback buy held to
the end of the race.

[![bounty](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/mumun110795/btc-race-24h-20261117-0603/rank.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603/leaderboard)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/mumun110795/btc-race-24h-20261117-0603/reward.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603)

## Layout

- `main.py` — the bot (bars-replay/v1 harness, Python 3.13, standard library only)
- `vendor/makefun_bars.py` — the kit SDK, byte-identical to the competition kit

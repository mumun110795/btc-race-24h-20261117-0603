# BTC Race 24h — btc-race-24h-20261117-0603
# Fresh build for this instance (dev window 16 Nov 06:03 UTC -> 17 Nov 06:03 UTC,
# +0.49% up, choppy). Long-only, 1 position, probe-first entries.
#
# Legs (all sizes in bps of equity; costs ~26 bps full round trip):
#   A. Washout: 240-bar return <= -1.70% -> 20% probe, add to 100% on a
#      bounce (+0.30% over probe cost) or a deeper dip (-0.80% under probe
#      close). TP +1.50%, stop -2.50%, max hold 500 bars.
#   B. Range dip: close <= SMA-90 - 0.48% while 240-bar return > -1.70%
#      (below that, leg A owns the dip: one V-canyon cannot kill both legs)
#      -> 30% probe, add to 100% on a turn-up or a deeper dip (-0.80% vs
#      SMA-90). Exit at SMA-90 + 0.60%, TP +1.50%, stop -2.50%, max 300 bars.
#   C. Trend momentum: EMA-8 > EMA-21 with EMA-21 rising, close > SMA-90,
#      240-bar return in [-0.50%, +1.00%] -> 40% probe. Exit on EMA cross
#      down, TP +1.20%, stop -1.20%, max 180 bars.
#   D. Shallow backup: if no filled trade by bar 720, a shallow dip
#      (close <= SMA-20 - 0.25%, 240-bar return > -1.00%) buys a 20% probe
#      so the entry can never sit out the whole race. Last-hour fallback:
#      if still no trade by bar 1380, buy 50% on a dip, else buy 50% at
#      bar 1410 and hold to the forced close.
# Guards: global equity drawdown guard at -8% from peak (sell all +
# 180-bar cooldown); no fresh entries in the last 30 bars except the
# bar-1410 fallback buy; flat bars never trigger an entry.
#
# Deterministic: decisions are pure functions of the closes seen so far.
# No clock, no environment, no randomness, no network, stdlib only.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
BPS = 10_000
SLIP_BPS = 300

LOOKBACK = 240
SMA_LONG = 90
SMA_SHORT = 20
EMA_FAST = 8
EMA_SLOW = 21

# Leg A — washout (user band -1.50%..-2.20%; mid-point -1.70% chosen).
WASH_TRIP_BPS = 170
WASH_PROBE_BPS = 2000
WASH_BOUNCE_BPS = 30
WASH_DEEPER_BPS = 80
WASH_TP_BPS = 150
WASH_STOP_BPS = 250
WASH_HOLD_BARS = 500
WASH_RECOVER_BPS = 80  # re-arm only once 240-bar return is above -0.80%

# Leg B — range dip (dip ~= 1/4 of the washout trip, per user guidance).
RANGE_DIP_BPS = 48
RANGE_PROBE_BPS = 3000
RANGE_DEEPER_BPS = 80
RANGE_EXIT_ABOVE_BPS = 60
RANGE_TP_BPS = 150
RANGE_STOP_BPS = 250
RANGE_HOLD_BARS = 300
RANGE_TURN_BARS = 3

# Leg C — trend momentum.
MOM_PROBE_BPS = 4000
MOM_TP_BPS = 120
MOM_STOP_BPS = 120
MOM_HOLD_BARS = 180

# Leg D — shallow backup / last-hour fallback.
BACKUP_ARM_BAR = 720
BACKUP_DIP_BPS = 25
BACKUP_PROBE_BPS = 2000
BACKUP_TP_BPS = 80
BACKUP_STOP_BPS = 150
BACKUP_HOLD_BARS = 120
LAST_HOUR_BAR = 1380
LAST_BUY_BAR = 1410
LAST_HOUR_SIZE_BPS = 5000

DD_GUARD_BPS = 800  # -8% from peak equity: flatten + cooldown.
DD_COOLDOWN_BARS = 180
STOP_COOLDOWN_BARS = 60
NO_NEW_AFTER_BAR = 1410

# --- module state (rebuilt deterministically from the batch stream) ---
_closes = []          # closes seen, ints
_mode = None          # "wash" | "range" | "mom" | "backup" | "lasthour" | None
_entry_seq = -1       # seq of the first fill of the current position
_probe_close = None   # close at which the probe was decided
_added = False        # scale-in already done for this position
_peak_equity = 0      # peak equity seen (micro-USDC)
_cooldown_until = -1  # no new entries before this seq
_wash_locked = False  # washout leg locked after a washout stop-out
_trades = 0           # filled orders seen (excludes the final flatten)


def _sma(n):
    if len(_closes) < n:
        return None
    return sum(_closes[-n:]) / n


def _ema_on(series, period):
    if len(series) < period:
        return None
    k = 2.0 / (period + 1)
    e = float(series[0])
    for c in series[1:]:
        e = c * k + e * (1.0 - k)
    return e


def _ema(period):
    return _ema_on(_closes, period)


def _ret240_bps():
    if len(_closes) <= LOOKBACK:
        return None
    return (_closes[-1] - _closes[-1 - LOOKBACK]) * BPS // _closes[-1 - LOOKBACK]


def _worth_above(cost, value, bps):
    return value * BPS >= cost * (BPS + bps)


def _worth_below(cost, value, bps):
    return value * BPS <= cost * (BPS - bps)


def _buy(state, notional):
    notional = min(notional, state.cash)
    room = state.weight_room(SYMBOL)
    if room < notional:
        notional = room
    if notional < 1_000_000:  # min buy 1 USDC
        return None
    return {"side": "buy", "symbol": SYMBOL,
            "notional_quote_units": int(notional),
            "max_slippage_bps": SLIP_BPS}


def _sell_all(state):
    held = state.positions.get(SYMBOL)
    if held is None or held.qty <= 0:
        return None
    return {"side": "sell", "symbol": SYMBOL, "qty": int(held.qty),
            "max_slippage_bps": SLIP_BPS}


def _probe_size(state, size_bps):
    return state.equity * size_bps // BPS


def _top_up(state):
    """Buy enough to take the position to ~100% of equity, or None."""
    held = state.positions.get(SYMBOL)
    held_value = state.value(SYMBOL, held.qty) if held is not None else 0
    need = state.equity - held_value
    if need < 1_000_000:
        return None
    return _buy(state, need)


def on_batch(state, batch):
    global _mode, _entry_seq, _probe_close, _added
    global _peak_equity, _cooldown_until, _wash_locked, _trades

    for fill in batch.get("fills", []):
        if fill.get("status") == "filled":
            _trades += 1

    bar = state.latest.get(SYMBOL)
    if bar is None:
        return []
    close = int(bar["c"])
    prev_close = _closes[-1] if _closes else None
    _closes.append(close)
    seq = state.seq

    # Peak tracking for the drawdown guard (equity is marked at each close).
    if state.equity > _peak_equity:
        _peak_equity = state.equity

    if state.warming_up:
        return []

    held = state.positions.get(SYMBOL)
    if held is not None and _entry_seq < 0:
        # Position appeared without our own entry bookkeeping (should not
        # happen); adopt it as a range position so exits still work.
        _mode, _entry_seq, _added = "range", seq, True
        _probe_close = close
    if held is None:
        _mode, _entry_seq, _probe_close, _added = None, -1, None, False

    r240 = _ret240_bps()
    if r240 is not None and r240 > -WASH_RECOVER_BPS:
        _wash_locked = False

    # --- global drawdown guard: flatten + cooldown ---
    if held is not None and _peak_equity > 0:
        if state.equity * BPS <= _peak_equity * (BPS - DD_GUARD_BPS):
            _cooldown_until = seq + DD_COOLDOWN_BARS
            if _mode == "wash":
                _wash_locked = True
            order = _sell_all(state)
            return [order] if order else []

    # --- exits for an open position ---
    if held is not None:
        cost = held.cost
        value = state.value(SYMBOL, held.qty)
        age = seq - _entry_seq
        sma_long = _sma(SMA_LONG)
        stop_bps = {"wash": WASH_STOP_BPS, "range": RANGE_STOP_BPS,
                    "mom": MOM_STOP_BPS, "backup": BACKUP_STOP_BPS}.get(_mode, 250)
        tp_bps = {"wash": WASH_TP_BPS, "range": RANGE_TP_BPS,
                  "mom": MOM_TP_BPS, "backup": BACKUP_TP_BPS}.get(_mode, 150)
        hold_bars = {"wash": WASH_HOLD_BARS, "range": RANGE_HOLD_BARS,
                     "mom": MOM_HOLD_BARS, "backup": BACKUP_HOLD_BARS}.get(_mode, 300)
        exit_now = False
        exit_stop = False
        if _worth_below(cost, value, stop_bps):
            exit_now, exit_stop = True, True
        elif _worth_above(cost, value, tp_bps):
            exit_now = True
        elif age >= hold_bars:
            exit_now = True
        elif _mode == "range" and sma_long is not None:
            if close * BPS >= int(sma_long) * (BPS + RANGE_EXIT_ABOVE_BPS):
                exit_now = True
        elif _mode == "mom":
            ema_f, ema_s = _ema(EMA_FAST), _ema(EMA_SLOW)
            if ema_f is not None and ema_s is not None and ema_f < ema_s:
                exit_now = True
        if exit_now:
            if exit_stop:
                _cooldown_until = seq + STOP_COOLDOWN_BARS
                if _mode == "wash":
                    _wash_locked = True
            order = _sell_all(state)
            return [order] if order else []

        # --- scale-in: add to full size once, on a bounce or deeper dip ---
        if not _added and _probe_close is not None and _mode in ("wash", "range"):
            deeper_bps = WASH_DEEPER_BPS if _mode == "wash" else RANGE_DEEPER_BPS
            bounce = close * BPS >= _probe_close * (BPS + WASH_BOUNCE_BPS)
            if _mode == "range":
                sma_now = _sma(SMA_LONG)
                deeper = (sma_now is not None
                          and close * BPS <= int(sma_now) * (BPS - RANGE_DEEPER_BPS))
                turn = (len(_closes) > RANGE_TURN_BARS
                        and close > _closes[-RANGE_TURN_BARS - 1])
                add = deeper or turn
            else:
                add = bounce or close * BPS <= _probe_close * (BPS - deeper_bps)
            if add:
                order = _top_up(state)
                if order is not None:
                    _added = True
                    return [order]
        return []

    # --- entries (flat) ---
    if seq >= NO_NEW_AFTER_BAR and not (seq == LAST_BUY_BAR and _trades == 0):
        return []
    if seq < _cooldown_until:
        return []
    if prev_close is not None and close == prev_close:
        return []  # flat bar: no signal
    if len(_closes) <= LOOKBACK:
        return []

    sma_long = _sma(SMA_LONG)
    sma_short = _sma(SMA_SHORT)

    # Leg A — washout.
    if (r240 is not None and r240 <= -WASH_TRIP_BPS and not _wash_locked
            and sma_long is not None):
        order = _buy(state, _probe_size(state, WASH_PROBE_BPS))
        if order is not None:
            _mode, _entry_seq, _probe_close, _added = "wash", seq, close, False
            return [order]

    # Leg B — range dip (only above washout territory; leg A owns it below).
    if (r240 is not None and r240 > -WASH_TRIP_BPS and sma_long is not None
            and close * BPS <= int(sma_long) * (BPS - RANGE_DIP_BPS)):
        order = _buy(state, _probe_size(state, RANGE_PROBE_BPS))
        if order is not None:
            _mode, _entry_seq, _probe_close, _added = "range", seq, close, False
            return [order]

    # Leg C — trend momentum.
    ema_f, ema_s = _ema(EMA_FAST), _ema(EMA_SLOW)
    if (ema_f is not None and ema_s is not None and sma_long is not None
            and ema_f > ema_s and close > int(sma_long)
            and r240 is not None and -50 <= r240 <= 100):
        ema_prev = _ema_on(_closes[:-30], EMA_SLOW) if len(_closes) > 30 else None
        rising = ema_prev is not None and ema_s > ema_prev
        if rising:
            order = _buy(state, _probe_size(state, MOM_PROBE_BPS))
            if order is not None:
                _mode, _entry_seq, _probe_close, _added = "mom", seq, close, True
                return [order]

    # Leg D — shallow backup: guarantee at least one trade by mid-race.
    if (seq >= BACKUP_ARM_BAR and _trades == 0 and sma_short is not None
            and r240 is not None and r240 > -100
            and close * BPS <= int(sma_short) * (BPS - BACKUP_DIP_BPS)):
        order = _buy(state, _probe_size(state, BACKUP_PROBE_BPS))
        if order is not None:
            _mode, _entry_seq, _probe_close, _added = "backup", seq, close, True
            return [order]

    # Leg D — last-hour fallback: buy on a dip, else at bar 1410, hold on.
    if seq >= LAST_HOUR_BAR and _trades == 0:
        dip = (sma_short is not None
               and close * BPS <= int(sma_short) * (BPS - BACKUP_DIP_BPS))
        if dip or seq >= LAST_BUY_BAR:
            order = _buy(state, _probe_size(state, LAST_HOUR_SIZE_BPS))
            if order is not None:
                _mode, _entry_seq, _probe_close, _added = "lasthour", seq, close, True
                return [order]

    return []


makefun_bars.run(on_batch)

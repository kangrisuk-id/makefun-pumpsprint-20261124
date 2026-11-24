"""Pump.fun Replay Sprint — paper bot (agent-fun/replay/v2).

Strategy: "late-stage organic strength".

The bot ignores a coin until its bonding curve already holds real reserves,
then buys only coins whose demand looks broad and organic:

  * real reserves inside a late-stage band, still close to their peak;
  * many trades and many distinct buyers behind it;
  * recent flow (last 500 slots) still buy-heavy;
  * no single dominant buyer (top-5 net buyers under 65% of net inflow,
    biggest single buy under 35% of all buys);
  * no bundled rush in the first 10 slots (reserves then at most 12 SOL);
  * the creator has never sold;
  * the price is stable, not spiking (at least 97% of its level ~150
    slots ago).

Positions (0.05 SOL, at most one per coin, never re-entered) are cut by a
take-profit at +90%, a stop-loss at -20%, a trailing stop (armed at +45%,
selling at 88% of the peak value), a stale timer (6,000 slots without a
trade) and a max hold of 15,000 slots. A timer exit is skipped when its
proceeds would not cover the transaction fee — the harness flattens what
is left at the end for free.

Everything is deterministic: no clock, no environment, no randomness — the
harness runs every entry twice and the two action logs must match byte for
byte. Pure standard library; the wire protocol lives in vendor/makefun_bot.py
(the kit's own shim).
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402

SOL = makefun_bot.LAMPORTS_PER_SOL

# ----------------------------- parameters ---------------------------------
STAKE = 50_000_000            # 0.05 SOL per position
WATCH_RQ = 8 * SOL            # start deep tracking at this reserve level
BAND_LO = 20 * SOL            # entry band for real quote reserves
BAND_HI = 85 * SOL
PEAK_SHARE_MIN = 0.80         # reserves >= this share of their peak
MIN_TRADES = 40
MIN_BUYERS = 20
WIN_SLOTS = 500               # recent-flow window, in slots
BUY_SHARE_MIN = 0.52          # buy share of window flow (by SOL)
STAB_SLOTS = 150              # stability lookback
STAB_MIN = 0.97               # price now >= STAB_MIN * price then
EARLY_SLOTS = 10
EARLY_RQ_MAX = 12 * SOL       # reserves after the first slots (anti-bundle)
MAX_BUY_SHARE = 0.35          # biggest single buy / total buys
TOP5_MAX = 0.65               # top-5 net buyers' share of positive net flow
TP_RATIO = 1.90               # sell when value/cost reaches this
SL_RATIO = 0.80               # sell when value/cost falls to this
TRAIL_ARM = 1.45              # trailing stop arms at this value/cost
TRAIL_DROP = 0.88             # ... and sells at this share of the peak value
STALE_SLOTS = 6_000           # sell after this long with no trade on the mint
MAX_HOLD = 15_000             # sell after this many slots in the position
BUY_SLIP_BPS = 1000
SELL_SLIP_BPS = 2500


class Mint:
    __slots__ = (
        "cslot", "creator", "csold", "trades", "early", "peak", "lastrq",
        "tbuy", "maxbuy", "lasttrade", "complete", "w",
    )

    def __init__(self, cslot, creator):
        self.cslot = cslot
        self.creator = creator
        self.csold = False
        self.trades = 0
        self.early = None
        self.peak = 0
        self.lastrq = 0
        self.tbuy = 0
        self.maxbuy = 0
        self.lasttrade = cslot
        self.complete = False
        self.w = None


class Watch:
    __slots__ = ("nets", "buyers", "win", "px")

    def __init__(self):
        self.nets = {}
        self.buyers = set()
        self.win = deque()
        self.px = deque()


MINTS = {}          # mint -> Mint, every universe mint seen
HELD = {}           # mint -> dict(slot, cost, peak_ratio)
DONE = set()        # mints already bought once: never re-entered


def _watch_trade(w, slot, user, is_buy, sol, vq, vt):
    if is_buy:
        w.nets[user] = w.nets.get(user, 0) + sol
        w.buyers.add(user)
    else:
        w.nets[user] = w.nets.get(user, 0) - sol
    w.win.append((slot, is_buy, sol))
    w.px.append((slot, vq, vt))
    cutoff = slot - 1200
    while len(w.px) > 2 and w.px[1][0] <= cutoff:
        w.px.popleft()


def _process_events(batch):
    touched = set()
    for e in batch["events"]:
        kind = e["event"]
        mint = e["mint"]
        if kind == "CreateEvent":
            if mint not in MINTS:
                MINTS[mint] = Mint(e["slot"], e.get("creator") or e.get("user"))
            continue
        m = MINTS.get(mint)
        if m is None:
            m = MINTS[mint] = Mint(e["slot"], None)
        if kind == "CompleteEvent":
            m.complete = True
            continue
        # TradeEvent
        rq = int(e["real_quote_reserves"])
        vq = int(e["virtual_quote_reserves"])
        vt = int(e["virtual_token_reserves"])
        sol = int(e["sol_amount"])
        user = e["user"]
        is_buy = bool(e["is_buy"])
        m.trades += 1
        m.lastrq = rq
        if rq > m.peak:
            m.peak = rq
        m.lasttrade = e["slot"]
        if is_buy:
            m.tbuy += sol
            if sol > m.maxbuy:
                m.maxbuy = sol
        elif user is not None and user == m.creator:
            m.csold = True
        if m.early is None and e["slot"] > m.cslot + EARLY_SLOTS:
            m.early = rq
        if m.w is None and rq >= WATCH_RQ:
            m.w = Watch()
        if m.w is not None:
            _watch_trade(m.w, e["slot"], user, is_buy, sol, vq, vt)
        touched.add(mint)
    return touched


def _est_proceeds(curve, tokens):
    """Estimated lamports a full sell of `tokens` returns, curve fees deducted."""
    if curve is None or tokens <= 0:
        return 0
    vq = curve.virtual_quote_reserves
    vt = curve.virtual_token_reserves
    if vq <= 0 or vt <= 0:
        return 0
    q = vq * tokens // (vt + tokens)
    return q - q * 95 // 10_000 - q * 30 // 10_000


def _value_ratio(curve, tokens, cost):
    """Estimated sell proceeds / cost for the whole position, fees included."""
    if cost <= 0:
        return 0.0
    return _est_proceeds(curve, tokens) / cost


def _sync_positions(state):
    for mint in list(HELD):
        if mint not in state.positions:
            del HELD[mint]
            DONE.add(mint)
    for mint, h in state.positions.items():
        if mint not in HELD:
            HELD[mint] = {"slot": state.slot, "cost": h.cost_lamports, "peak": 1.0}
            DONE.add(mint)


def _exit_orders(state):
    orders = []
    pending_sells = {
        p.order["mint"] for p in state.pending.values() if p.order["side"] == "sell"
    }
    for mint in sorted(HELD):
        if mint in pending_sells:
            continue
        pos = state.positions.get(mint)
        if pos is None:
            continue
        curve = state.curves.get(mint)
        if curve is not None and curve.complete:
            continue
        info = HELD[mint]
        ratio = _value_ratio(curve, pos.token_amount, info["cost"])
        if ratio > info["peak"]:
            info["peak"] = ratio
        m = MINTS.get(mint)
        last_trade = m.lasttrade if m is not None else info["slot"]
        price_exit = (
            ratio >= TP_RATIO
            or ratio <= SL_RATIO
            or (info["peak"] >= TRAIL_ARM and ratio <= info["peak"] * TRAIL_DROP)
        )
        timer_exit = (
            state.slot - last_trade >= STALE_SLOTS
            or state.slot - info["slot"] >= MAX_HOLD
        )
        # A timer exit whose proceeds would not even cover its own tx fee is
        # left alone: the harness flattens what is left at the end and
        # abandons a close that cannot pay its fees, which costs nothing.
        if timer_exit and not price_exit:
            if _est_proceeds(curve, pos.token_amount) < state.tx_fee_lamports():
                timer_exit = False
        if price_exit or timer_exit:
            orders.append({
                "side": "sell",
                "mint": mint,
                "token_amount": pos.token_amount,
                "max_slippage_bps": SELL_SLIP_BPS,
            })
    return orders


def _qualifies(state, mint):
    m = MINTS.get(mint)
    if m is None or m.w is None or m.complete or m.csold:
        return False
    if mint in DONE or mint in state.positions:
        return False
    rq = m.lastrq
    if rq < BAND_LO or rq > BAND_HI:
        return False
    if m.peak <= 0 or rq < m.peak * PEAK_SHARE_MIN:
        return False
    if m.trades < MIN_TRADES:
        return False
    if m.early is None or m.early > EARLY_RQ_MAX:
        return False
    if m.tbuy <= 0 or m.maxbuy > m.tbuy * MAX_BUY_SHARE:
        return False
    w = m.w
    if len(w.buyers) < MIN_BUYERS:
        return False
    # recent-flow buy share
    cutoff = state.slot - WIN_SLOTS
    buy = sell = 0
    for s, is_buy, sol in w.win:
        if s < cutoff:
            continue
        if is_buy:
            buy += sol
        else:
            sell += sol
    if buy + sell <= 0 or buy < (buy + sell) * BUY_SHARE_MIN:
        return False
    # buyer concentration: top-5 positive nets vs all positive nets
    pos_nets = sorted((v for v in w.nets.values() if v > 0), reverse=True)
    tot = sum(pos_nets)
    if tot <= 0 or sum(pos_nets[:5]) >= tot * TOP5_MAX:
        return False
    # price stability vs ~STAB_SLOTS ago
    px = w.px
    if len(px) < 2:
        return False
    now_slot, nvq, nvt = px[-1]
    if nvt <= 0:
        return False
    ref = None
    for s, vq, vt in px:
        if s <= now_slot - STAB_SLOTS:
            ref = (vq, vt)
        else:
            break
    if ref is None or ref[1] <= 0:
        return False
    if nvq * ref[1] < ref[0] * nvt * STAB_MIN:
        return False
    return True


def _entry_orders(state, touched, budget):
    orders = []
    caps = state.hello["caps"]
    free = state.free_cash_lamports()
    tx = state.tx_fee_lamports()
    open_slots = caps["max_open_positions"] - state.committed_position_count()
    pending_buys = {
        p.order["mint"] for p in state.pending.values() if p.order["side"] == "buy"
    }
    for mint in sorted(touched):
        if len(orders) >= budget or open_slots <= 0:
            break
        if mint in pending_buys:
            continue
        if not _qualifies(state, mint):
            continue
        cap = state.max_buy_lamports(mint)
        amount = min(STAKE, cap)
        if amount < 10_000_000 or free < amount + tx:
            continue
        orders.append({
            "side": "buy",
            "mint": mint,
            "quote_lamports": amount,
            "max_slippage_bps": BUY_SLIP_BPS,
        })
        free -= amount + tx
        open_slots -= 1
    return orders


def on_batch(state, batch):
    touched = _process_events(batch)
    _sync_positions(state)
    orders = _exit_orders(state)
    room = state.hello["caps"]["max_orders_per_batch"] - len(orders)
    if room > 0:
        orders.extend(_entry_orders(state, touched, room))
    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)

#!/usr/bin/env python3
"""Local stock portfolio tracker.

Reads a bank transaction export (.xlsx), works out holdings with the average-cost
method, and serves a live dashboard on http://127.0.0.1 only. The only outbound
traffic is price lookups by ticker symbol; no portfolio data leaves this machine.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from xlsx_reader import read_workbook, excel_date

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
QUOTE_TTL = 20          # seconds a live quote is reused
HISTORY_TTL = 3600      # seconds daily history is reused
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) portfolio-local/1.0"}

# Column headers in the bank export (Danish).
COL_TRADE_DATE = "Handelsdato"
COL_NAME = "Værdipapir"
COL_QTY = "Nom./Stk."
COL_PRICE = "Kurs"
COL_AMOUNT = "Afreg. beløb"
COL_STATUS = "Status"
COL_TYPE = "Ordretype"
DIVIDEND = "Udbytte"
COMPLETED = "Gennemført"


def load_config(pid=None):
    """Main config, or with another person's portfolio file laid over it.

    config.json → "portfolios": {"id": {"label": "...", "file": "portfolios/id.json"}}.
    The portfolio file holds that person's trades, tickers, sectors and core; rules, timing
    and the screen come from config.json. Tickers and sectors are merged with the main ones.
    """
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_pid"] = "main"
    if pid and pid != "main":
        meta = cfg.get("portfolios", {}).get(pid)
        if not meta:
            raise ValueError(f"Unknown portfolio '{pid}'.")
        with open(HERE / meta["file"], encoding="utf-8") as f:
            sub = json.load(f)
        for k in ("tickers", "sectors"):
            cfg[k] = {**cfg.get(k, {}), **sub.pop(k, {})}
        cfg["transactions_file"] = None
        cfg["opening_positions"] = {}
        cfg["manual_transactions"] = []
        cfg["core"] = []
        cfg["core_split"] = {}
        cfg.update(sub)
        cfg["_pid"] = pid
    return cfg


def portfolio_list(cfg):
    out = [{"id": "main", "label": cfg.get("label", "Mine")}]
    out += [{"id": k, "label": v.get("label", k)} for k, v in cfg.get("portfolios", {}).items()
            if not k.startswith("_")]
    return out


# --------------------------------------------------------------------------- transactions

def read_transactions(path):
    sheet = next(iter(read_workbook(path).values()))
    header = [str(h or "").strip() for h in sheet[0]]
    idx = {h: i for i, h in enumerate(header)}
    # The currency column has an empty header right after the amount column.
    ccy_col = idx[COL_AMOUNT] + 1

    txs = []
    for row in sheet[1:]:
        row = row + [None] * (len(header) - len(row))
        if not row[idx[COL_NAME]]:
            continue
        if idx.get(COL_STATUS) is not None and row[idx[COL_STATUS]] not in (None, COMPLETED):
            continue
        txs.append({
            "date": excel_date(row[idx[COL_TRADE_DATE]]).date(),
            "name": str(row[idx[COL_NAME]]).strip(),
            "qty": float(row[idx[COL_QTY]] or 0),
            "price": float(row[idx[COL_PRICE]] or 0),
            "amount": float(row[idx[COL_AMOUNT]] or 0),
            "currency": row[ccy_col] or "DKK",
            "type": str(row[idx[COL_TYPE]] or "").strip(),
        })
    txs.sort(key=lambda t: t["date"])
    return txs


def merge_manual(txs, manual):
    """Add trades from config.json that aren't in the bank export yet.

    A manual trade is skipped once the export has one with the same date, name and qty.
    Sells have a negative qty; amount_dkk is the settlement amount (positive number).
    """
    seen = {(t["date"], t["name"], t["qty"]) for t in txs}
    out = list(txs)
    for m in manual:
        d = datetime.strptime(m["date"], "%Y-%m-%d").date()
        qty = float(m["qty"])
        if (d, m["name"], qty) in seen:
            continue
        amount = abs(float(m["amount_dkk"]))
        if m.get("type") == "dividend":   # qty = shares the dividend was paid on
            out.append({"date": d, "name": m["name"], "qty": qty, "price": 0.0, "amount": amount,
                        "currency": "DKK", "type": DIVIDEND})
            continue
        out.append({"date": d, "name": m["name"], "qty": qty, "price": float(m.get("price") or 0),
                    "amount": amount if qty < 0 else -amount, "currency": "DKK", "type": "Manual"})
    out.sort(key=lambda t: t["date"])
    return out


def build_positions(txs, opening):
    """Average-cost positions in DKK. Returns (positions, warnings)."""
    pos = {}
    warnings = []

    def get(name):
        if name not in pos:
            pos[name] = {"name": name, "shares": 0.0, "cost": 0.0, "cost_known": True,
                         "realised": 0.0, "dividends": 0.0, "since": None, "lots": [],
                         "cost_native": 0.0, "native_known": True}
        return pos[name]

    for name, o in opening.items():
        p = get(name)
        p["shares"] = float(o.get("shares") or 0)
        if o.get("cost_dkk") is None:
            p["cost_known"] = False
        else:
            p["cost"] = float(o["cost_dkk"])
        p["native_known"] = False   # no purchase price in the original currency
        p["lots"].append({"date": None, "qty": p["shares"], "remaining": p["shares"], "price": None,
                          "cost": p["cost"] if p["cost_known"] else None})

    for t in txs:
        p = get(t["name"])
        if t["type"] == DIVIDEND:
            p["dividends"] += t["amount"]
            if t["qty"] > p["shares"] + 1e-9:
                warnings.append(
                    f"{t['name']}: dividend on {t['date']:%d.%m.%Y} was paid on {t['qty']:g} shares, "
                    f"but only {p['shares']:g} are known. Add the missing shares under "
                    f"\"opening_positions\" in config.json.")
            continue
        if t["qty"] > 0:      # buy: settlement amount is negative and includes fees
            if p["shares"] < 1e-9:
                p["since"] = t["date"]   # start of the current holding period
            p["shares"] += t["qty"]
            p["cost"] += -t["amount"]
            p["cost_native"] += t["qty"] * (t["price"] or 0)   # price excl. fees, original currency
            p["lots"].append({"date": t["date"].isoformat(), "qty": t["qty"], "remaining": t["qty"],
                              "price": t["price"], "cost": -t["amount"]})
        elif t["qty"] < 0:    # sell
            qty = -t["qty"]
            avg = p["cost"] / p["shares"] if p["shares"] else 0.0
            if p["cost_known"]:
                p["realised"] += t["amount"] - avg * qty
            p["cost"] -= avg * qty
            if p["shares"]:
                p["cost_native"] -= p["cost_native"] / p["shares"] * qty
            p["shares"] -= qty
            # Lots are for display only: sold shares are taken from the oldest lots first (FIFO).
            # Cost basis and tax figures above stay on the average-cost method.
            left = qty
            for lot in p["lots"]:
                take = min(lot["remaining"], left)
                lot["remaining"] -= take
                left -= take
                if left <= 1e-9:
                    break
            if p["shares"] < 1e-9:
                p["shares"], p["cost"], p["since"], p["lots"] = 0.0, 0.0, None, []
                p["cost_native"], p["native_known"] = 0.0, True
    return pos, warnings


# --------------------------------------------------------------------------- market data

_cache = {}
_cache_lock = threading.Lock()


def _yahoo_chart(symbol, rng, interval, ttl):
    key = (symbol, rng, interval)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range={rng}&interval={interval}")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
        data = json.load(r)["chart"]["result"][0]
    with _cache_lock:
        _cache[key] = (time.time(), data)
    return data


def _scale(currency):
    # London quotes in pence.
    return (0.01, "GBP") if currency in ("GBp", "GBX") else (1.0, currency)


def fetch_quote(symbol):
    d = _yahoo_chart(symbol, "1d", "5m", QUOTE_TTL)
    m = d["meta"]
    mult, ccy = _scale(m.get("currency", "DKK"))
    closes = d.get("indicators", {}).get("quote", [{}])[0].get("close") or []
    spark = [[t, c * mult] for t, c in zip(d.get("timestamp") or [], closes) if c is not None]
    prev = m.get("previousClose") or m.get("chartPreviousClose")
    return {
        "symbol": symbol,
        "currency": ccy,
        "price": m["regularMarketPrice"] * mult,
        "prev_close": prev * mult if prev else None,
        "time": m.get("regularMarketTime"),
        "exchange": m.get("fullExchangeName") or m.get("exchangeName"),
        "spark": spark,
    }


def fetch_history(symbol, start):
    rng = "5y" if (date.today() - start).days > 700 else "2y"
    d = _yahoo_chart(symbol, rng, "1d", HISTORY_TTL)
    mult, _ = _scale(d["meta"].get("currency", "DKK"))
    closes = d.get("indicators", {}).get("quote", [{}])[0].get("close") or []
    out = {}
    for t, c in zip(d.get("timestamp") or [], closes):
        if c is not None:
            out[datetime.fromtimestamp(t).date()] = c * mult
    return out


CONSENSUS_TTL = 6 * 3600
_yahoo_session = {"opener": None, "crumb": None}


def _yahoo_crumb(refresh=False):
    """quoteSummary needs a cookie + crumb pair; fetch once and reuse."""
    import http.cookiejar
    if refresh or not _yahoo_session["crumb"]:
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        op.addheaders = list(UA.items())
        try:
            op.open("https://fc.yahoo.com", timeout=10)
        except Exception:
            pass  # returns 404 but still sets the cookie
        crumb = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10).read().decode()
        _yahoo_session.update(opener=op, crumb=crumb)
    return _yahoo_session["opener"], _yahoo_session["crumb"]


def fetch_consensus(symbol):
    """Published analyst consensus (rating, # analysts, price targets). None if not covered."""
    key = (symbol, "consensus")
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CONSENSUS_TTL:
            return hit[1]
    result = None
    for attempt in range(2):
        op, crumb = _yahoo_crumb(refresh=attempt > 0)
        url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(symbol)}"
               f"?modules=financialData,recommendationTrend,assetProfile&crumb={urllib.parse.quote(crumb)}")
        try:
            d = json.load(op.open(url, timeout=10))["quoteSummary"]["result"][0]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break          # not covered (e.g. ETFs)
            if e.code == 401 and attempt == 0:
                continue       # stale crumb
            raise
        f = d.get("financialData") or {}
        raw = lambda k: f[k].get("raw") if isinstance(f.get(k), dict) else f.get(k)
        trend = ((d.get("recommendationTrend") or {}).get("trend") or [{}])[0]
        sector = (d.get("assetProfile") or {}).get("sector")
        if raw("numberOfAnalystOpinions"):
            result = {
                "sector": sector,
                "rating": f.get("recommendationKey"),
                "mean": raw("recommendationMean"),
                "analysts": raw("numberOfAnalystOpinions"),
                "target_mean": raw("targetMeanPrice"),
                "target_low": raw("targetLowPrice"),
                "target_high": raw("targetHighPrice"),
                "breakdown": {k: trend.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")},
            }
        break
    with _cache_lock:
        _cache[key] = (time.time(), result)
    return result


def fx_symbol(ccy):
    return None if ccy == "DKK" else f"{ccy}DKK=X"


# --------------------------------------------------------------------------- portfolio

def snapshot(include_history, exclude=(), pid=None):
    cfg = load_config(pid)
    txs, path = [], None
    if cfg.get("transactions_file"):
        path = HERE / os.path.expanduser(cfg["transactions_file"])
        txs = read_transactions(path)
    txs = merge_manual(txs, cfg.get("manual_transactions", []))
    positions, warnings = build_positions(txs, cfg.get("opening_positions", {}))
    tickers = cfg.get("tickers", {})

    for name in positions:
        if name not in tickers:
            warnings.append(f"No ticker for \"{name}\". Add it under \"tickers\" in config.json.")

    held = {n: p for n, p in positions.items() if p["shares"] > 0 and n in tickers}
    symbols = sorted({tickers[n] for n in held})

    with ThreadPoolExecutor(max_workers=8) as ex:
        quotes = dict(zip(symbols, ex.map(_safe(fetch_quote), symbols)))
        ccys = sorted({q["currency"] for q in quotes.values() if q} - {"DKK"})
        fx_quotes = dict(zip(ccys, ex.map(_safe(fetch_quote), [fx_symbol(c) for c in ccys])))
    fx = {"DKK": 1.0, **{c: q["price"] for c, q in fx_quotes.items() if q}}
    fx_prev = {"DKK": 1.0, **{c: q["prev_close"] or q["price"] for c, q in fx_quotes.items() if q}}

    rows = []
    for name, p in held.items():
        sym = tickers[name]
        q = quotes.get(sym)
        row = {
            "name": name, "symbol": sym, "shares": p["shares"],
            "cost": p["cost"] if p["cost_known"] else None,
            "realised": p["realised"], "dividends": p["dividends"],
            "since": p["since"].isoformat() if p["since"] else None,
            "lots": p["lots"],
            "avg_native": p["cost_native"] / p["shares"] if p["native_known"] and p["shares"] else None,
            "quote": q,
        }
        if q and q["currency"] in fx:
            rate = fx[q["currency"]]
            row["value"] = p["shares"] * q["price"] * rate
            if q["prev_close"]:
                # Shares bought today count from their purchase cost, not yesterday's close.
                today_iso = datetime.now().date().isoformat()
                new = [l for l in p["lots"] if l["date"] == today_iso and l["remaining"] > 0]
                new_qty = sum(l["remaining"] for l in new)
                new_cost = sum(l["cost"] * l["remaining"] / l["qty"] for l in new)
                row["value_prev"] = ((p["shares"] - new_qty) * q["prev_close"] * fx_prev[q["currency"]]
                                     + new_cost)
            row["fx"] = rate
        else:
            warnings.append(f"No live price for {name} ({sym}).")
        rows.append(row)

    closed = [{"name": n, "realised": p["realised"], "dividends": p["dividends"]}
              for n, p in positions.items() if n not in held and (p["realised"] or p["dividends"])]

    result = {
        "generated": time.time(),
        "refresh_seconds": cfg.get("refresh_seconds", 30),
        "rules": cfg.get("rules", {}),
        "screen": {k: v for k, v in cfg.get("screen", {}).items() if k in ("min_analysts", "show_top")},
        "timing": cfg.get("timing", {}),
        "core": cfg.get("core", []),
        "core_split": cfg.get("core_split") or {},
        "portfolio": cfg["_pid"],
        "portfolios": portfolio_list(load_config()),
        "sectors": cfg.get("sectors", {}),
        "source_file": Path(path).name if path else "manual trades",
        "source_modified": os.path.getmtime(path) if path else None,
        "holdings": sorted(rows, key=lambda r: -(r.get("value") or 0)),
        "closed": closed,
        "realised_total": sum(p["realised"] for p in positions.values()),
        "dividends_total": sum(p["dividends"] for p in positions.values()),
        "fx": fx,
        "warnings": warnings,
        "transactions": [{**t, "date": t["date"].isoformat()} for t in reversed(txs)],
    }
    if include_history:
        result["history"] = build_history(txs, positions, tickers, cfg.get("opening_positions", {}), set(exclude))
        result["signals"] = build_signals({n: tickers[n] for n in held},
                                          {n: positions[n]["since"] for n in held},
                                          cfg.get("timing", {}).get("trailing_stop_pct"))
        with ThreadPoolExecutor(max_workers=8) as ex:
            result["consensus"] = dict(zip(symbols, ex.map(_safe(fetch_consensus), symbols)))
        result["candidates"] = build_screen(cfg.get("screen", {}), set(symbols))
        result["fund_candidates"] = build_fund_screen(cfg.get("screen", {}).get("funds", {}), set(symbols))
    return result


# --------------------------------------------------------------------------- technical signals

def _sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _ema_series(xs, n):
    k = 2 / (n + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def _rsi(xs, n=14):
    """Wilder's RSI."""
    if len(xs) <= n:
        return None
    deltas = [b - a for a, b in zip(xs, xs[1:])]
    gain = sum(max(d, 0) for d in deltas[:n]) / n
    loss = sum(max(-d, 0) for d in deltas[:n]) / n
    for d in deltas[n:]:
        gain = (gain * (n - 1) + max(d, 0)) / n
        loss = (loss * (n - 1) + max(-d, 0)) / n
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def technicals(closes):
    """Rule-based indicators on daily closes (oldest first). Score > 0 is bullish."""
    if len(closes) < 60:
        return None
    price = closes[-1]
    sma50, sma200 = _sma(closes, 50), _sma(closes, 200)
    rsi = _rsi(closes)
    macd = [a - b for a, b in zip(_ema_series(closes, 12), _ema_series(closes, 26))]
    hist = [m - s for m, s in zip(macd, _ema_series(macd, 9))]
    year = closes[-252:]
    hi, lo = max(year), min(year)

    score, reasons = 0, []
    if sma200:
        if price > sma200:
            score += 1; reasons.append(("+", "Price above 200-day average (long-term uptrend)"))
        else:
            score -= 1; reasons.append(("−", "Price below 200-day average (long-term downtrend)"))
        prev50 = sum(closes[-60:-10]) / 50
        prev200 = sum(closes[-210:-10]) / 200 if len(closes) >= 210 else None
        if sma50 > sma200:
            score += 1
            if prev200 and prev50 <= prev200:
                reasons.append(("+", "Golden cross: 50-day crossed above 200-day in the last 2 weeks"))
            else:
                reasons.append(("+", "50-day average above 200-day"))
        else:
            score -= 1
            if prev200 and prev50 >= prev200:
                reasons.append(("−", "Death cross: 50-day crossed below 200-day in the last 2 weeks"))
            else:
                reasons.append(("−", "50-day average below 200-day"))
    elif sma50:
        if price > sma50:
            score += 1; reasons.append(("+", "Price above 50-day average"))
        else:
            score -= 1; reasons.append(("−", "Price below 50-day average"))

    recent_flip = any((hist[-i] > 0) != (hist[-i - 1] > 0) for i in range(1, 6))
    if hist[-1] > 0:
        score += 1
        reasons.append(("+", "MACD turned positive this week" if recent_flip else "MACD momentum positive"))
    else:
        score -= 1
        reasons.append(("−", "MACD turned negative this week" if recent_flip else "MACD momentum negative"))

    if rsi is not None:
        if rsi < 30:
            score += 1; reasons.append(("+", f"RSI {rsi:.0f}: oversold"))
        elif rsi > 70:
            score -= 1; reasons.append(("−", f"RSI {rsi:.0f}: overbought"))

    label = "Bullish" if score >= 2 else "Bearish" if score <= -2 else "Neutral"
    return {
        "label": label, "score": score, "reasons": reasons,
        "price": price, "sma50": sma50, "sma200": sma200, "rsi": rsi, "macd_hist": hist[-1],
        "high52": hi, "low52": lo, "range_pos": (price - lo) / (hi - lo) if hi > lo else None,
    }


def trailing_stop(hist, since, stop_pct):
    """Highest close since the holding was bought, the stop level below it, and how long price has been under it."""
    days = sorted(d for d in hist if since is None or d >= since)
    if not days:
        return {}
    peak, peak_day, last_ok = 0.0, None, None
    for i, d in enumerate(days):
        if hist[d] > peak:
            peak, peak_day = hist[d], d
        if stop_pct is None or hist[d] >= peak * (1 - stop_pct / 100):
            last_ok = i
    out = {"peak": peak, "peak_date": peak_day.isoformat(), "peak_since": days[0].isoformat()}
    if stop_pct is not None:
        out["stop"] = peak * (1 - stop_pct / 100)
        out["days_below_stop"] = len(days) - 1 - last_ok if last_ok is not None else len(days)
    return out


def build_screen(screen, held_symbols):
    """Run stocks/ETFs you don't own through the same consensus + technical checks."""
    names = {**screen.get("universe", {}), **screen.get("watchlist", {})}
    names = {n: s for n, s in names.items() if s not in held_symbols}
    if not names:
        return []
    start = date.today().replace(year=date.today().year - 2)

    def one(item):
        name, sym = item
        q = fetch_quote(sym)
        h = fetch_history(sym, start)
        t = technicals([h[d] for d in sorted(h)]) if h else None
        c = fetch_consensus(sym)
        return {"name": name, "symbol": sym, "watchlist": name in screen.get("watchlist", {}),
                "price": q["price"], "currency": q["currency"], "consensus": c, "signal": t}

    with ThreadPoolExecutor(max_workers=8) as ex:
        return [r for r in ex.map(_safe(one), names.items()) if r]


def fetch_fund_profile(symbol):
    """Yearly fee (TER) and fund size from Yahoo. None if unavailable."""
    key = (symbol, "fund")
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < 24 * 3600:
            return hit[1]
    result = None
    for attempt in range(2):
        op, crumb = _yahoo_crumb(refresh=attempt > 0)
        url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(symbol)}"
               f"?modules=fundProfile,summaryDetail,defaultKeyStatistics&crumb={urllib.parse.quote(crumb)}")
        try:
            d = json.load(op.open(url, timeout=10))["quoteSummary"]["result"][0]
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue
            if e.code == 404:
                break
            raise
        raw = lambda o, k: (o.get(k) or {}).get("raw") if isinstance(o.get(k), dict) else o.get(k)
        fp = d.get("fundProfile") or {}
        ter = raw(fp.get("feesExpensesInvestment") or {}, "annualReportExpenseRatio")
        assets = raw(d.get("summaryDetail") or {}, "totalAssets") or raw(d.get("defaultKeyStatistics") or {}, "totalAssets")
        result = {"ter": ter, "assets": assets}
        break
    with _cache_lock:
        _cache[key] = (time.time(), result)
    return result


def risk_stats(closes):
    """Annualised volatility and worst peak-to-trough fall over the last 3 years of daily closes."""
    window = closes[-756:]
    if len(window) < 120:
        return {}
    rets = [b / a - 1 for a, b in zip(window, window[1:])]
    mean = sum(rets) / len(rets)
    vol = (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 * 252 ** 0.5
    peak, max_dd = window[0], 0.0
    for c in window:
        peak = max(peak, c)
        max_dd = min(max_dd, c / peak - 1)
    return {"volatility": vol, "max_drawdown": max_dd, "risk_years": round(len(window) / 252, 1)}


def build_fund_screen(funds, held_symbols):
    """Broad ETFs you don't own: price, trend, fee and size."""
    funds = {n: f for n, f in funds.items() if isinstance(f, dict) and f.get("symbol") not in held_symbols}
    start = date.today().replace(year=date.today().year - 5)

    def one(item):
        name, f = item
        sym = f["symbol"]
        q = fetch_quote(sym)
        h = fetch_history(sym, start)
        t = technicals([h[d] for d in sorted(h)]) if h else None
        p = _safe(fetch_fund_profile)(sym) or {}
        ter = f.get("ter_pct") / 100 if f.get("ter_pct") is not None else p.get("ter")
        closes = [h[d] for d in sorted(h)] if h else []
        year = closes[-252:]
        return {"name": name, "symbol": sym, "category": f.get("category"), "gap": f.get("gap"),
                "sector": f.get("sector"), "price": q["price"], "currency": q["currency"],
                "ter": ter, "assets": p.get("assets"), "signal": t,
                "return_1y": year[-1] / year[0] - 1 if len(year) > 200 else None,
                "isin": f.get("isin"), "morningstar_stars": f.get("morningstar_stars"),
                **risk_stats(closes)}

    with ThreadPoolExecutor(max_workers=8) as ex:
        return [r for r in ex.map(_safe(one), funds.items()) if r]


def build_signals(symbols_by_name, since_by_name=None, stop_pct=None):
    start = date.today().replace(year=date.today().year - 2)
    with ThreadPoolExecutor(max_workers=8) as ex:
        hists = dict(zip(symbols_by_name,
                         ex.map(_safe(lambda s: fetch_history(s, start)), symbols_by_name.values())))
    out = {}
    for name, sym in symbols_by_name.items():
        h = hists.get(name)
        if h:
            t = technicals([h[d] for d in sorted(h)])
            if t:
                t.update(trailing_stop(h, (since_by_name or {}).get(name), stop_pct))
                out[sym] = t
    return out


def build_history(txs, positions, tickers, opening, exclude=frozenset()):
    """Daily portfolio value and cost basis in DKK, for positions with a known cost."""
    trades = [t for t in txs if t["type"] != DIVIDEND and t["qty"] and t["name"] in tickers and t["name"] not in exclude
              and positions[t["name"]]["cost_known"]]
    if not trades:
        return None
    start = trades[0]["date"]
    # Opening positions with a known cost are held from the first day.
    held_at_start = {n: o for n, o in opening.items()
                     if n in tickers and n not in exclude and o.get("cost_dkk") is not None and o.get("shares")}
    names = sorted({t["name"] for t in trades} | set(held_at_start))
    symbols = {n: tickers[n] for n in names}

    with ThreadPoolExecutor(max_workers=8) as ex:
        hist = dict(zip(symbols.values(),
                        ex.map(_safe(lambda s: fetch_history(s, start)), symbols.values())))
        live = {s: _safe(fetch_quote)(s) or {} for s in symbols.values()}
        meta_ccy = {s: live[s].get("currency", "DKK") for s in symbols.values()}
        ccys = sorted(set(meta_ccy.values()) - {"DKK"})
        fx_hist = dict(zip(ccys, ex.map(_safe(lambda c: fetch_history(fx_symbol(c), start)), ccys)))
        fx_live = {c: (_safe(fetch_quote)(fx_symbol(c)) or {}).get("price") for c in ccys}

    # Daily closes can lag a day behind, so also include every trade date and today
    # (today is valued at live prices); missing closes carry forward from the day before.
    today = date.today()
    days = sorted({d for h in hist.values() if h for d in h if d >= start}
                  | {t["date"] for t in trades} | {today})
    last = {}
    shares = {n: float(held_at_start[n]["shares"]) if n in held_at_start else 0.0 for n in names}
    cost = {n: float(held_at_start[n]["cost_dkk"]) if n in held_at_start else 0.0 for n in names}
    ti = 0
    out = []
    for d in days:
        while ti < len(trades) and trades[ti]["date"] <= d:
            t = trades[ti]
            if t["qty"] > 0:
                shares[t["name"]] += t["qty"]
                cost[t["name"]] += -t["amount"]
            else:
                avg = cost[t["name"]] / shares[t["name"]] if shares[t["name"]] else 0
                cost[t["name"]] -= avg * -t["qty"]
                shares[t["name"]] += t["qty"]
            ti += 1
        value = 0.0
        for n in names:
            s = symbols[n]
            if hist.get(s) and d in hist[s]:
                last[s] = hist[s][d]
            ccy = meta_ccy[s]
            if ccy != "DKK" and fx_hist.get(ccy) and d in fx_hist[ccy]:
                last[ccy] = fx_hist[ccy][d]
            if d == today:
                if live[s].get("price"):
                    last[s] = live[s]["price"]
                if ccy != "DKK" and fx_live.get(ccy):
                    last[ccy] = fx_live[ccy]
            rate = 1.0 if ccy == "DKK" else last.get(ccy)
            if shares[n] and s in last and rate:
                value += shares[n] * last[s] * rate
        out.append({"date": d.isoformat(), "value": round(value, 2),
                    "cost": round(sum(cost.values()), 2)})
    excluded = [n for n, p in positions.items() if not p["cost_known"]]
    return {"points": out, "excluded": excluded}


def _safe(fn):
    def wrapped(arg):
        try:
            return fn(arg)
        except Exception as e:  # network hiccups shouldn't take the page down
            print(f"  ! {arg}: {e}", file=sys.stderr)
            return None
    return wrapped


# --------------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", (HERE / "dashboard.html").read_bytes())
        elif url.path == "/manifest.json":
            self._send(200, "application/manifest+json", (HERE / "manifest.json").read_bytes())
        elif url.path in ("/icons/icon-180.png", "/icons/icon-192.png", "/icons/icon-512.png"):
            self._send(200, "image/png", (HERE / url.path.lstrip("/")).read_bytes())
        elif url.path == "/api/portfolio":
            qs = urllib.parse.parse_qs(url.query)
            try:
                exclude = [n for v in qs.get("exclude", []) for n in v.split("|") if n]
                body = json.dumps(snapshot("history" in qs, exclude, (qs.get("p") or [None])[0]), default=str).encode()
                self._send(200, "application/json", body)
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode())
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main():
    cfg = load_config()
    port = int(os.environ.get("PORT", cfg.get("port", 8765)))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Portfolio running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

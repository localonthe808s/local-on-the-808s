#!/usr/bin/env python3
"""What the market actually charged, hour by hour, on every settled day.

WHY THIS EXISTS.  The panel said "bet between 8 AM and noon" and the honest
footnote underneath admitted it was an argument, not a measurement: our accuracy
is flat by hour, prices are assumed to harden through the day, therefore the
morning. The price half had never been measured because a note in my own
records said Kalshi's candlestick endpoint 404s and historical prices were
unrecoverable.

That note was wrong. The PATH was wrong:

    /markets/{ticker}/candlesticks                      -> 404
    /series/{series}/markets/{ticker}/candlesticks      -> 200, and it carries
                                                           yes_bid and yes_ask
                                                           per period

It is public, not even gated behind the API key. So every settled market has a
full hourly price history sitting there, and the timing question can be
answered today instead of after a fortnight of collecting it forward.

WHAT IT MEASURES.  For each settled day and each hour, it replays this model as
of that hour -- using only data that existed then -- and puts our probability
next to the market's real bid/ask at the same moment. From that:

  * our Brier by hour, and the MARKET's Brier by hour, on the same days
  * the best +EV bet available at that hour, at the price actually quoted
  * what that bet RETURNED, since the outcome is known

The last is the answer. Not "when are we most accurate" but "when did betting
make the most money", which is a different question and the one that matters.

    python3 _kalshi/price_study.py            # every configured market
    python3 _kalshi/price_study.py --city ny  # one
"""
import collections
import calendar
import datetime
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kalshi_daily as K                                   # noqa: E402

OUT = os.path.join(HERE, 'price_study.json')
CACHE = os.path.join(HERE, '.price_cache.json')
HOURS = list(range(7, 23))


# ------------------------------------------------------------- the market ----
def settled_events(cfg):
    """{date: [market dicts]} for every settled event Kalshi still lists."""
    out = collections.defaultdict(list)
    cursor = None
    for _ in range(12):
        u = ('https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=%s'
             '&status=settled&limit=200' % cfg['series']) + (
                 ('&cursor=' + cursor) if cursor else '')
        j = K.get_json(u, timeout=45)
        for m in j.get('markets') or []:
            ev = m.get('event_ticker') or ''
            try:
                d = datetime.datetime.strptime(ev.split('-')[1], '%y%b%d').date()
            except Exception:
                continue
            out[d.isoformat()].append(m)
        cursor = j.get('cursor')
        if not cursor:
            break
    return out


def bounds(m):
    """Kalshi states open bounds strictly -- the same reading fetch_market uses."""
    f, c, st = m.get('floor_strike'), m.get('cap_strike'), m.get('strike_type')
    if st == 'between':
        return float(f), float(c)
    if st in ('less', 'less_or_equal'):
        hi = float(c if c is not None else f)
        return None, hi - (1.0 if st == 'less' else 0.0)
    lo = float(f)
    return lo + (1.0 if st == 'greater' else 0.0), None


def candles(cfg, ticker, day, tzoff):
    """{local hour: (bid, ask, open_interest)} across the trading day.

    Open interest is the pool: every open contract settles at a dollar, so the
    count is the money riding on that rung at that hour. It answers a question
    the price cannot -- WHEN the market fills up -- which is what decides
    whether an edge found at 8 AM can actually be bought.
    """
    start = calendar.timegm(datetime.datetime(day.year, day.month, day.day).timetuple()) \
        + tzoff * 3600
    u = ('https://api.elections.kalshi.com/trade-api/v2/series/%s/markets/%s'
         '/candlesticks?period_interval=60&start_ts=%d&end_ts=%d'
         % (cfg['series'], ticker, start, start + 30 * 3600))
    out = {}
    try:
        for x in K.get_json(u, timeout=45).get('candlesticks') or []:
            t = datetime.datetime.utcfromtimestamp(
                x['end_period_ts']) - datetime.timedelta(hours=tzoff)
            b = (x.get('yes_bid') or {}).get('close_dollars')
            a = (x.get('yes_ask') or {}).get('close_dollars')
            if b is None and a is None:
                continue
            out[t.hour] = (float(b) if b is not None else None,
                           float(a) if a is not None else None,
                           float(x.get('open_interest_fp') or 0))
    except Exception:
        pass
    return out


# -------------------------------------------------------------- the model ----
def replay(cfg, dates):
    """{date: {hour: (pred, sd, floor)}} -- this model as of each hour, using
    only what existed then. Same helpers as the live job, so a difference here
    would be a difference in the job."""
    today = K.local_now(cfg).date()
    span = K.RESID_M + K.BIAS_K + 6
    fcm = {m: v for m, v in K.forecast_runs(cfg, span + 40, K.models_for(cfg)).items() if v}
    daily = K.daily_series(cfg, today - datetime.timedelta(days=span + 60), today)
    obh = K.obs_hourly_range(cfg, today - datetime.timedelta(days=span + 60),
                             today + datetime.timedelta(days=1))
    bias_of = K.biases_factory(fcm, daily, cfg.get('skill', True))
    h0_of = lambda k: K.climate_day_start(                                # noqa: E731
        cfg, datetime.date(*map(int, k.split('-'))))
    fc = fcm.get(K.MODELS[0]) or list(fcm.values())[0]

    out = {}
    for k in sorted(dates):
        prior = sorted(x for x in fc if x < k and x in daily
                       and len(fc[x]) >= 20)[-K.BIAS_K:]
        if len(prior) < 8:
            continue
        biases = bias_of(prior)
        yday = daily.get((datetime.date(*map(int, k.split('-')))
                          - datetime.timedelta(days=1)).isoformat())
        h0 = h0_of(k)
        per = {}
        for h in HOURS:
            r = K.running_max(obh, k, h, h0)
            fl = (r + K.HOURLY_PEAK_OFFSET) if r is not None else None
            pf = K.point_forecast(fcm, biases, k, h, yday)
            cand = [x for x in (fl, pf) if x is not None]
            if not cand:
                continue
            pred = max(cand)
            has = bool(fc.get(k))
            over = has and not [x for x in (fc.get(k) or {}) if x >= h]
            bind = fl is not None and ((pf is not None and fl >= pf) or over)
            sd, _ = K.spread(K.residuals(fcm, bias_of, daily, obh, h, k, h0_of), h, bind)
            if bind:
                sd = min(sd, K.OFFSET_SD)
            per[h] = (pred, sd, fl)
        if per:
            out[k] = per
    return out


# --------------------------------------------------------------- the study ---
def study(cfg):
    tzoff = 4 if 3 <= datetime.date.today().month <= 11 else 5
    ev = settled_events(cfg)
    print('%s: %d settled days' % (cfg['key'], len(ev)))
    model = replay(cfg, ev.keys())
    print('  replayed %d of them' % len(model))

    rows = []
    for i, (day, ms) in enumerate(sorted(ev.items())):
        if day not in model:
            continue
        truth = next((m for m in ms if m.get('result') == 'yes'), None)
        if not truth:
            continue
        d = datetime.date(*map(int, day.split('-')))
        lad = []
        for m in ms:
            lo, hi = bounds(m)
            lad.append({'label': m.get('yes_sub_title') or m['ticker'].split('-')[-1],
                        'lo': lo, 'hi': hi, 'ticker': m['ticker'],
                        'won': m.get('result') == 'yes'})
        lad.sort(key=lambda r: (r['lo'] if r['lo'] is not None else -999))
        px = {r['ticker']: candles(cfg, r['ticker'], d, tzoff) for r in lad}
        for h, (pred, sd, fl) in sorted(model[day].items()):
            ps = K.distribution(lad, pred, sd, fl)
            quote = [px[r['ticker']].get(h) for r in lad]
            if sum(1 for q in quote if q and q[1] is not None) < 3:
                continue
            mids = [((q[0] + q[1]) / 2) if q and q[0] is not None and q[1] is not None
                    else None for q in quote]
            tot = sum(x for x in mids if x is not None) or 1.0
            best = None
            for r, p, q in zip(lad, ps, quote):
                if not q:
                    continue
                ask, bid = q[1], q[0]
                for side, price, prob_, wins in (
                        ('yes', ask, p, r['won']),
                        ('no', None if bid is None else round(1 - bid, 2), 1 - p, not r['won'])):
                    if price is None or not (0 < price < 1):
                        continue
                    e = prob_ - price - K.fee_of(price)
                    if best is None or e > best['ev']:
                        cost = price + K.fee_of(price)
                        best = {'ev': e, 'price': price, 'side': side, 'wins': wins,
                                'ret': ((1 - cost) / cost) if wins else -1.0}
            pool = sum((q[2] if q and len(q) > 2 and q[2] else 0) for q in quote)
            rows.append({
                'date': day, 'hour': h, 'pool': round(pool, 1),
                'ours': [round(x, 4) for x in ps],
                'mkt': [None if x is None else round(x / tot, 4) for x in mids],
                'truth': [bool(r['won']) for r in lad],
                'best': best,
            })
        if (i + 1) % 15 == 0:
            print('  %d/%d days, %d hour-rows' % (i + 1, len(ev), len(rows)))
    return rows


def summarise(rows):
    by = collections.defaultdict(lambda: {'n': 0, 'ob': [], 'mb': [], 'ev': [],
                                          'ret': [], 'won': 0, 'bets': 0, 'pool': []})
    for r in rows:
        e = by[r['hour']]
        e['n'] += 1
        if r.get('pool'):
            e['pool'].append(r['pool'])
        for p, m, t in zip(r['ours'], r['mkt'], r['truth']):
            o = 1 if t else 0
            e['ob'].append((p - o) ** 2)
            if m is not None:
                e['mb'].append((m - o) ** 2)
        b = r['best']
        if b and b['ev'] > 0.005:
            e['bets'] += 1
            e['ev'].append(b['ev'])
            e['ret'].append(b['ret'])
            e['won'] += 1 if b['wins'] else 0
    out = []
    for h in sorted(by):
        e = by[h]
        out.append({
            'h': h, 'days': e['n'],
            'ours_brier': round(statistics.mean(e['ob']), 4) if e['ob'] else None,
            'mkt_brier': round(statistics.mean(e['mb']), 4) if e['mb'] else None,
            'bets': e['bets'],
            'edge': round(statistics.mean(e['ev']), 4) if e['ev'] else None,
            'ret': round(statistics.mean(e['ret']), 4) if e['ret'] else None,
            'winrate': round(e['won'] / e['bets'], 3) if e['bets'] else None,
            # the median, not the mean: one enormous day would otherwise draw a
            # curve no ordinary day resembles
            'pool': round(statistics.median(e['pool'])) if e['pool'] else None,
        })
    return out


def main():
    only = None
    if '--city' in sys.argv:
        only = sys.argv[sys.argv.index('--city') + 1]
    all_rows, per = [], {}
    for cfg in K.MARKETS:
        if only and not cfg['key'].startswith(only):
            continue
        K.HOURLY_PEAK_OFFSET, K.OFFSET_SD = K.OFFSET_DEFAULT, K.OFFSET_SD_DEFAULT
        try:
            r = study(cfg)
        except Exception as e:
            print('%s FAILED: %s: %s' % (cfg['key'], type(e).__name__, e))
            continue
        per[cfg['key']] = summarise(r)
        all_rows += r
        print('  -> %d hour-rows | %s' % (len(r), K.timing_report()))
    doc = {'built': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'),
           'hour_rows': len(all_rows), 'by_market': per,
           'pooled': summarise(all_rows)}
    with open(os.path.join(HERE, 'price_rows.json'), 'w') as f:
        json.dump(all_rows, f, separators=(',', ':'))
    print('kept %d raw rows for later analysis' % len(all_rows))
    with open(OUT, 'w') as f:
        json.dump(doc, f, indent=1)
    print('\nwrote %s' % OUT)
    print('\n hour  days   ours   market    bets   edge   return   win%')
    for e in doc['pooled']:
        print('  %2d   %4d  %.4f  %s   %4d  %s  %s  %s' % (
            e['h'], e['days'], e['ours_brier'] or 0,
            ('%.4f' % e['mkt_brier']) if e['mkt_brier'] else '   -  ',
            e['bets'],
            ('%+.3f' % e['edge']) if e['edge'] is not None else '  -   ',
            ('%+.3f' % e['ret']) if e['ret'] is not None else '  -   ',
            ('%.0f%%' % (100 * e['winrate'])) if e['winrate'] is not None else '-'))
    return 0


if __name__ == '__main__':
    sys.exit(main())

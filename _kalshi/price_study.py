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
    bias_of = K.biases_factory(fcm, daily, cfg.get('skill', True), cfg.get('bias_hl'))
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
                                'ret': ((1 - cost) / cost) if wins else -1.0,
                                'i': lad.index(r)}
            pool = sum((q[2] if q and len(q) > 2 and q[2] else 0) for q in quote)
            rows.append({
                'city': cfg['key'],
                'date': day, 'hour': h, 'pool': round(pool, 1),
                'ours': [round(x, 4) for x in ps],
                'mkt': [None if x is None else round(x / tot, 4) for x in mids],
                'truth': [bool(r['won']) for r in lad],
                'best': best,
                # the raw quote per rung at this hour, so a bet struck earlier
                # can be valued here: (yes bid, yes ask)
                'q': [[None, None] if not q else [q[0], q[1]] for q in quote],
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


def exit_policy(rows, entries=(8, 9, 10, 11), exits=(12, 13, 14, 15, 16, 17)):
    """Hold to settlement, or close early? Every morning bet at its real quote,
    valued at each later hour's real bid/ask. Answers the question a losing
    afternoon asks -- "should I have got out?" -- with the whole history
    rather than one day. Needs rows carrying 'q' and best['i']."""
    by = collections.defaultdict(dict)
    for r in rows:
        by[r['date']][r['hour']] = r

    def value(b, r):
        i = b['i']; bid, ask = r['q'][i]
        cost = b['price'] + K.fee_of(b['price'])
        out = bid if b['side'] == 'yes' else (None if ask is None else 1 - ask)
        if out is None:
            return None
        out -= K.fee_of(out) if 0 < out < 1 else 0
        return (out - cost) / cost

    res = collections.defaultdict(list)
    up_n = up_lost = 0
    for d, hrs in by.items():
        for e in entries:
            r0 = hrs.get(e)
            b = r0.get('best') if r0 else None
            if not b or b.get('ev', 0) <= 0.03 or 'i' not in b or 'q' not in r0:
                continue
            if not (K.MIN_PRICE <= b['price'] <= 1 - K.MIN_PRICE):
                continue
            hold = b['ret']
            res['hold'].append(hold)
            vals = {}
            for x in exits:
                rx = hrs.get(x)
                v = value(b, rx) if rx and 'q' in rx else None
                vals[x] = v
                res['exit%d' % x].append(v if v is not None else hold)
            gain = (1 - (b['price'] + K.fee_of(b['price']))) / (b['price'] + K.fee_of(b['price']))
            tp = next((v for x in exits if (v := vals.get(x)) is not None and v >= 0.5 * gain), hold)
            sl = next((v for x in exits if (v := vals.get(x)) is not None and v <= -0.4), hold)
            res['take_profit'].append(tp)
            res['stop_loss'].append(sl)
            if e == 9 and vals.get(15) is not None and vals[15] > 0.3:
                up_n += 1
                up_lost += 1 if hold < 0 else 0
    if not res['hold']:
        return None
    out = {k: {'ret': round(statistics.mean(v), 3),
               'win': round(sum(1 for x in v if x > 0) / len(v), 3)} for k, v in res.items()}
    out['n'] = len(res['hold'])
    out['up_at_15'] = [up_lost, up_n]
    return out


def calibration_bands(rows, hours=(7, 13), min_n=150):
    """Said-versus-happened by probability band, betting hours only.

    Feeds a sizing haircut: where a band has enough rows and says more than
    happens, Kelly is computed from what happens. New York's morning bands on
    2026-09-06: 0.6 and 0.7 UNDER-confident, 0.8 over by 17 points on 38 rows,
    nothing with 150 rows -- so the haircut is inert until the data exists.
    The all-hours "6-7 points overconfident at 70-90%" was the evening talking."""
    bands = collections.defaultdict(lambda: [0.0, 0.0, 0])
    for r in rows:
        if not (hours[0] <= r['hour'] <= hours[1]):
            continue
        for p, t in zip(r['ours'], r['truth']):
            lo = min(int(p * 10) / 10.0, 0.9)
            b = bands[lo]
            b[0] += p; b[1] += (1.0 if t else 0.0); b[2] += 1
    out = {}
    for lo, (sp, hp, n) in bands.items():
        out['%.1f' % lo] = {'said': round(sp / n, 3), 'happened': round(hp / n, 3), 'n': n,
                            'haircut': round(max(0.0, sp / n - hp / n), 3) if n >= min_n else 0.0}
    return out


EV_BINS = ((0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, 1.01))
FLOOR_PRICE = 0.30


def edge_floor(rows, hours=(7, 13), need_ret=0.15, min_n_priced=100, min_n_cheap=60):
    """THE EDGE FLOOR, MEASURED (2026-09-07). The bake's floor -- 20c of edge,
    or 10c once the price is 30c or more -- was read off this table by hand and
    typed into kalshi_daily.py. This re-reads it every night from the same rows
    so it moves with the record and nobody has to. Per price class, the floor
    is the lowest edge bin from which that bin AND every bin above it return at
    least +0.15 per $1 on enough rows; a lone good bin under a bad one does not
    count. Clamped to a sane range; the bake applies it only past a row count
    (see kalshi_daily.measured_floor) and keeps the typed values otherwise."""
    tab = {}
    for r in rows:
        b = r.get('best')
        if not b or b.get('ret') is None or not (hours[0] <= r['hour'] <= hours[1]):
            continue
        cls = 'priced' if b['price'] >= FLOOR_PRICE else 'cheap'
        for lo, hi in EV_BINS:
            if lo <= b['ev'] < hi:
                tab.setdefault((cls, lo), []).append(b['ret'])
                break
    out = {'price': FLOOR_PRICE, 'need_ret': need_ret, 'n': sum(len(v) for v in tab.values()), 'bins': {}}
    for cls, min_n, lo_c, hi_c, default in (('priced', min_n_priced, 0.08, 0.30, 0.10), ('cheap', min_n_cheap, 0.15, 0.40, 0.20)):
        bins = [(lo, tab.get((cls, lo), [])) for lo, _ in EV_BINS]
        out['bins'][cls] = {('%.2f' % lo): {'n': len(v), 'ret': round(statistics.mean(v), 3) if v else None} for lo, v in bins}
        chosen = None
        for i, (lo, v) in enumerate(bins):
            above = [(l2, v2) for l2, v2 in bins[i:]]
            good = all(len(v2) >= min_n and statistics.mean(v2) >= need_ret for l2, v2 in above if l2 < 0.30) \
                and len(v) >= min_n and statistics.mean(v) >= need_ret
            if good:
                chosen = lo
                break
        out[cls] = round(min(hi_c, max(lo_c, chosen if chosen is not None else default)), 2)
        out[cls + '_measured'] = chosen is not None
    # the bake's names: min = any price, priced = at or above the price line
    out['min'] = out['cheap']
    return out


GAP_BINS = (0.3, 0.4, 0.5, 0.6, 0.7)


def disagree_cap(rows, hours=(7, 13), min_n=60):
    """THE DISAGREEMENT CAP, MEASURED (2026-09-07). The bake refuses a rung
    where our probability and the market's differ by more than MAX_DISAGREE
    (typed: 50 points). Measured here from the same replayed rows: the return
    of the plan's bet by how far we stood from the market on it, morning
    window. The cap is the lower edge of the first gap bin, ascending from 30
    points, that either LOSES on average or has too few rows to say -- so it
    tightens the moment wide disagreements start losing, and loosens only on
    sixty-plus rows of them paying. Clamped and rate-limited in the bake."""
    tab = {lo: [] for lo in GAP_BINS}
    n = 0
    for r in rows:
        b = r.get('best')
        if not b or b.get('ret') is None or not (hours[0] <= r['hour'] <= hours[1]):
            continue
        i = b['i']
        ours, mkt = r['ours'][i], r['mkt'][i]
        if b['side'] == 'no':
            ours, mkt = 1 - ours, 1 - mkt
        g = abs(ours - mkt)
        n += 1
        for lo in GAP_BINS:
            if lo <= g < lo + 0.1:
                tab[lo].append(b['ret'])
                break
    cap = None
    for lo in GAP_BINS:
        v = tab[lo]
        if len(v) < min_n or statistics.mean(v) < 0:
            cap = lo
            break
    if cap is None:
        cap = GAP_BINS[-1] + 0.1
    return {'cap': round(cap, 2), 'n': n, 'min_n': min_n,
            'bins': {('%.1f' % lo): {'n': len(v), 'ret': round(statistics.mean(v), 3) if v else None} for lo, v in tab.items()}}


def main():
    only = None
    if '--city' in sys.argv:
        # a comma list: --city las,aus. Markets NOT named keep their existing
        # rows (the New York study is hours of fetching; a Las Vegas run must
        # not throw it away, which overwriting price_rows.json used to do).
        only = [x.strip() for x in sys.argv[sys.argv.index('--city') + 1].split(',') if x.strip()]
    all_rows, per = [], {}
    if only:
        try:
            with open(os.path.join(HERE, 'price_rows.json')) as f:
                all_rows = [r for r in json.load(f)
                            if r.get('city') and not any(r['city'].startswith(o) for o in only)]
            print('kept %d rows from markets not in this run' % len(all_rows))
        except Exception:
            all_rows = []
    for cfg in K.MARKETS:
        if only and not any(cfg['key'].startswith(o) for o in only):
            continue
        K.HOURLY_PEAK_OFFSET, K.OFFSET_SD = K.OFFSET_DEFAULT, K.OFFSET_SD_DEFAULT
        try:
            r = study(cfg)
        except Exception as e:
            print('%s FAILED: %s: %s' % (cfg['key'], type(e).__name__, e))
            continue
        all_rows += r
        print('  -> %d hour-rows | %s' % (len(r), K.timing_report()))
    for k in sorted(set(r.get('city') for r in all_rows if r.get('city'))):
        per[k] = summarise([r for r in all_rows if r.get('city') == k])
    doc = {'built': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'),
           'hour_rows': len(all_rows), 'by_market': per,
           'pooled': summarise(all_rows),
           'exit': exit_policy(all_rows),
           'exit_by_market': {k: exit_policy([r for r in all_rows if r.get('city') == k])
                              for k in per},
           'calib': calibration_bands(all_rows),
           'calib_by_market': {k: calibration_bands([r for r in all_rows if r.get('city') == k])
                               for k in per},
           'blend_by_market': {k: blend_by_hour([r for r in all_rows if r.get('city') == k])
                               for k in per},
           'flip_by_market': {k: pick_flip([r for r in all_rows if r.get('city') == k])
                              for k in per},
           'edge_floor': edge_floor(all_rows),
           'disagree_cap': disagree_cap(all_rows)}
    with open(os.path.join(HERE, 'price_rows.json'), 'w') as f:
        json.dump(all_rows, f, separators=(',', ':'))
    print('kept %d raw rows for later analysis' % len(all_rows))
    with open(OUT, 'w') as f:
        json.dump(doc, f, indent=1)
    print('\nwrote %s' % OUT)
    if doc.get('exit'):
        ex = doc['exit']
        print('\n exit policy on %d morning bets: hold %+.3f | close 1pm %+.3f | 4pm %+.3f | take-profit %+.3f | stop-loss %+.3f | up at 3pm then lost %d/%d'
              % (ex['n'], ex['hold']['ret'], ex['exit13']['ret'], ex['exit16']['ret'],
                 ex['take_profit']['ret'], ex['stop_loss']['ret'], ex['up_at_15'][0], ex['up_at_15'][1]))
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


def pick_flip(rows, h_early=8, h_late=11):
    """How often the best bet at h_late is a different bet from h_early (the
    mid-morning runs moved the pick), and what each hour's bet returned on the
    days it did and did not. Measured 2026-09-07: New York's pick moved on 25
    of 52 days; on those the 8 AM bet paid +0.20 and the 11 AM bet +0.50, while
    on the steady days 8 AM had the better price for the same return."""
    by = collections.defaultdict(dict)
    for r in rows:
        b = r.get('best')
        if b and (b.get('ev') or 0) >= 0.04:
            by[r['date']][r['hour']] = b
    same, diff = [], []
    for d in by.values():
        if h_early in d and h_late in d:
            a, b = d[h_early], d[h_late]
            (same if (a['i'] == b['i'] and a['side'] == b['side']) else diff).append((a['ret'], b['ret'], a['price'], b['price']))
    def m(xs, i):
        return round(statistics.mean(x[i] for x in xs), 3) if xs else None
    n = len(same) + len(diff)
    return {'n': n, 'moved': len(diff), 'rate': round(len(diff) / n, 3) if n else None,
            'moved_ret_early': m(diff, 0), 'moved_ret_late': m(diff, 1),
            'steady_ret_early': m(same, 0), 'steady_ret_late': m(same, 1),
            'steady_price_early': m(same, 2), 'steady_price_late': m(same, 3),
            'h_early': h_early, 'h_late': h_late}


def blend_by_hour(rows):
    """For each hour, the weight on OUR probabilities (the rest on the market's)
    that minimises Brier over the rows. Measured 2026-09-06: New York goes to
    the market by 2 PM, Las Vegas leans ~60/40 to the market from 3 PM, Austin
    stays ~70/30 on ours until 5 PM. Used for the afternoon sell/hold verdict,
    never for placing a bet (a blended edge is not an edge)."""
    def ok(r):
        return (r.get('mkt') and r.get('truth') and r.get('ours')
                and all(x is not None for x in r['mkt']) and all(x is not None for x in r['ours']))
    def brier(p, t):
        return sum((pi - (1.0 if ti else 0.0)) ** 2 for pi, ti in zip(p, t))
    byh = collections.defaultdict(list)
    for r in rows:
        if ok(r):
            byh[r['hour']].append(r)
    out = {}
    for h, rs in byh.items():
        if len(rs) < 20:
            continue
        best = None
        for k in range(0, 21):
            w = k / 20.0
            b = sum(brier([w * o + (1 - w) * m for o, m in zip(r['ours'], r['mkt'])], r['truth']) for r in rs) / len(rs)
            if best is None or b < best[1] - 1e-9:
                best = (w, b)
        bo = sum(brier(r['ours'], r['truth']) for r in rs) / len(rs)
        bm = sum(brier(r['mkt'], r['truth']) for r in rs) / len(rs)
        out[str(h)] = {'w': best[0], 'brier': round(best[1], 4), 'ours': round(bo, 4), 'mkt': round(bm, 4), 'n': len(rs)}
    return out


def refit_from_rows():
    """Rebuild price_study.json's exit block from the saved rows, no fetching."""
    with open(os.path.join(HERE, 'price_rows.json')) as f:
        rows = json.load(f)
    with open(OUT) as f:
        doc = json.load(f)
    doc['exit'] = exit_policy(rows)
    keys = sorted(set(r.get('city') for r in rows if r.get('city')))
    doc['exit_by_market'] = {k: exit_policy([r for r in rows if r.get('city') == k]) for k in keys}
    doc['calib'] = calibration_bands(rows)
    doc['calib_by_market'] = {k: calibration_bands([r for r in rows if r.get('city') == k]) for k in keys}
    doc['blend_by_market'] = {k: blend_by_hour([r for r in rows if r.get('city') == k]) for k in keys}
    with open(OUT, 'w') as f:
        json.dump(doc, f, indent=1)
    print('exit block rebuilt from %d rows: %s' % (len(rows), json.dumps(doc['exit'])))
    return 0


if __name__ == '__main__':
    sys.exit(refit_from_rows() if '--exit-only' in sys.argv else main())

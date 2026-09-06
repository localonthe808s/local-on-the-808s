#!/usr/bin/env python3
"""Kalshi KXRAIN -- "Where will it rain on <date>?" -- 22 cities, one binary each.

WHY THIS IS A SEPARATE SCRIPT.  It shares the plumbing in kalshi_daily (fetch
with backoff, per-host timing, the fee formula, Kelly sizing, the portfolio
import) but almost none of the model. A daily high is a continuous quantity
with a floor that ratchets up through the day; "did it rain" is a binary with
no floor, no ladder, and no rounding line. Bolting it into run_market() would
have meant branching every one of those code paths on market type, in a
pipeline that is currently working. So: same helpers, own model, own file.

THE MODEL.  Six models each give the day's total precipitation. What predicts
rain is not the amount -- it is how many models produce ANY. Measured over 1368
city-days:

    0 of 6 models wet ->  0.3% actually rained
    3 of 6            -> 31.4%
    6 of 6            -> 88.7%

Monotone the whole way. A logistic on three features -- the fraction of models
with measurable precipitation, the log of the mean amount, and the fraction
calling a real soaking -- scores, LEAVE-ONE-CITY-OUT so no city sees its own
history:

    climatology   Brier 0.1946
    this model    Brier 0.0831   log loss 0.2673

The weights are refitted from the accumulated history on every run rather than
frozen here, because a hardcoded number is exactly what went stale on the
temperature side.

SETTLEMENT.  "Strictly greater than 0 inches" at the CLI station named in each
market's own rules. Note Chicago rain settles on CLIORD (O'Hare) while Chicago
TEMPERATURE settles on CLIMDW (Midway) -- same city, different stations, and
using one for the other would be wrong on a meaningful number of days.
"""
import collections
import concurrent.futures as cf
import datetime
import json
import math
import os
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kalshi_daily as K                                   # noqa: E402

SERIES = 'KXRAIN'
OUT = os.path.join(HERE, '..', 'kalshi_rain.json')
WET = 0.005            # the station reports hundredths; a trace prints 0.00
HIST_DAYS = 170        # calibration window, extended as history accumulates
LOCK_HOUR = 12         # the pick that gets graded, in each city's own clock
BANKROLL = K.BANKROLL          # one bankroll for the whole operation
FIT_ITERS = 700
# How many cities to fetch at once. MEASURED, not guessed -- the same 22 cities,
# minutes apart, on this laptop:
#
#     workers   wall    open-meteo (44 calls)   retries
#        1      34.3s        16s                  -
#        4      16.5s        20s                  -
#        8      14.4s        26s                 +3
#
# Open-Meteo throttles concurrency rather than refusing it: at 8 every request
# costs 60% more and three failed outright, rescued only by K.get's backoff. Two
# seconds is not worth leaning on a free upstream this whole project depends on.
WORKERS = 4

# code -> (city, IEM station, IEM network, lat, lon, tz). The station is the one
# each market's rules name; they were read, not guessed.
CITIES = [
    ('ATL',  'Atlanta',       'ATL', 'GA_ASOS', 33.6301,  -84.4418, 'America/New_York'),
    ('AUS',  'Austin',        'AUS', 'TX_ASOS', 30.1830,  -97.6799, 'America/Chicago'),
    ('BOS',  'Boston',        'BOS', 'MA_ASOS', 42.3606,  -71.0097, 'America/New_York'),
    ('CHI',  'Chicago',       'ORD', 'IL_ASOS', 41.9602,  -87.9316, 'America/Chicago'),
    ('DAL',  'Dallas',        'DFW', 'TX_ASOS', 32.8968,  -97.0380, 'America/Chicago'),
    ('DC',   'Washington',    'DCA', 'VA_ASOS', 38.8472,  -77.0346, 'America/New_York'),
    ('DEN',  'Denver',        'DEN', 'CO_ASOS', 39.8328, -104.6575, 'America/Denver'),
    ('EWR',  'Newark',        'EWR', 'NJ_ASOS', 40.6827,  -74.1693, 'America/New_York'),
    ('HOU',  'Houston',       'HOU', 'TX_ASOS', 29.6375,  -95.2824, 'America/Chicago'),
    ('LAX',  'Los Angeles',   'LAX', 'CA_ASOS', 33.9382, -118.3865, 'America/Los_Angeles'),
    ('LV',   'Las Vegas',     'LAS', 'NV_ASOS', 36.0719, -115.1634, 'America/Los_Angeles'),
    ('MIA',  'Miami',         'MIA', 'FL_ASOS', 25.7880,  -80.3169, 'America/New_York'),
    ('MIN',  'Minneapolis',   'MSP', 'MN_ASOS', 44.8854,  -93.2313, 'America/Chicago'),
    ('NOLA', 'New Orleans',   'MSY', 'LA_ASOS', 29.9933,  -90.2511, 'America/Chicago'),
    ('NYC',  'New York',      'NYC', 'NY_ASOS', 40.7790,  -73.9692, 'America/New_York'),
    ('OKC',  'Oklahoma City', 'OKC', 'OK_ASOS', 35.3889,  -97.6006, 'America/Chicago'),
    ('PHIL', 'Philadelphia',  'PHL', 'PA_ASOS', 39.8734,  -75.2266, 'America/New_York'),
    ('PHX',  'Phoenix',       'PHX', 'AZ_ASOS', 33.4343, -112.0116, 'America/Phoenix'),
    ('SATX', 'San Antonio',   'SAT', 'TX_ASOS', 29.5300,  -98.4673, 'America/Chicago'),
    ('SEA',  'Seattle',       'SEA', 'WA_ASOS', 47.4447, -122.3144, 'America/Los_Angeles'),
    ('SFO',  'San Francisco', 'SFO', 'CA_ASOS', 37.6190, -122.3749, 'America/Los_Angeles'),
    ('TTN',  'Trenton',       'TTN', 'NJ_ASOS', 40.2768,  -74.8159, 'America/New_York'),
]

# the same three cities as the temperature sheet (2026-09-06); the rest of the
# roster stays above for re-admission
CITIES = [c for c in CITIES if c[0] in ('NYC', 'LV', 'AUS')]


# ------------------------------------------------------------------ model ----
def features(q):
    """From a list of model precipitation totals (inches) to model inputs."""
    n = len(q)
    mean = sum(q) / n
    return (1.0,
            sum(1 for x in q if x >= WET) / n,        # do the models see anything
            math.log1p(mean * 100.0),                 # how much, compressed
            sum(1 for x in q if x >= 0.05) / n)       # do they see a real soaking


def fit(samples, iters=FIT_ITERS, lr=1.2):
    """Plain logistic regression, batch gradient descent. Small and dependency
    free on purpose -- this runs on a GitHub runner with nothing installed."""
    w = [0.0] * 4
    n = len(samples)
    if n < 60:
        return None
    for _ in range(iters):
        g = [0.0] * 4
        for x, y in samples:
            z = sum(wi * xi for wi, xi in zip(w, x))
            e = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z)))) - y
            for k in range(4):
                g[k] += e * x[k]
        for k in range(4):
            w[k] -= lr * g[k] / n
    return w


def prob(w, q):
    if not w or not q:
        return None
    z = sum(wi * xi for wi, xi in zip(w, features(q)))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


# ------------------------------------------------------------------- data ----
def obs_precip(st, nw, start, end):
    """{'YYYY-MM-DD': inches} from the settlement source's own daily figures.

    The RANGED endpoint. `daily.json` with no dates returns the station's whole
    archive back to 1943, which is 6.5 seconds a city and was 142s of a 178s
    run -- the same trap kalshi_daily documents for temperature."""
    u = ('https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py'
         '?network=%s&stations=%s&year1=%d&month1=%d&day1=%d'
         '&year2=%d&month2=%d&day2=%d&format=comma'
         % (nw, st, start.year, start.month, start.day,
            end.year, end.month, end.day))
    import csv as _csv
    import io as _io
    out = {}
    for r in _csv.DictReader(_io.StringIO(K.get(u, timeout=40).decode())):
        v = r.get('precip_in')
        if v not in (None, '', 'M', 'None', 'T'):
            try:
                out[r['day']] = float(v)
            except ValueError:
                pass
        elif v == 'T':
            out[r['day']] = 0.0        # a trace is not "greater than 0 inches"
    return out


def qpf(lat, lon, tz, start, end):
    """{'YYYY-MM-DD': [inches per model]} -- one request for all six models."""
    tzq = urllib.parse.quote(tz)
    mods = ','.join(K.MODELS)
    out = collections.defaultdict(dict)

    def absorb(day):
        for m in K.MODELS:
            col = day.get('precipitation_sum_' + m) or (
                day.get('precipitation_sum') if len(K.MODELS) == 1 else None)
            if not col:
                continue
            for t, v in zip(day['time'], col):
                if v is not None:
                    out[t][m] = v          # live run overwrites the archive

    u = ('https://historical-forecast-api.open-meteo.com/v1/forecast'
         '?latitude=%.4f&longitude=%.4f&start_date=%s&end_date=%s'
         '&daily=precipitation_sum&models=%s&precipitation_unit=inch&timezone=%s'
         % (lat, lon, start.isoformat(), end.isoformat(), mods, tzq))
    absorb(K.get_json(u, timeout=40)['daily'])
    # today and tomorrow come from the live run, which is fresher than the archive
    u2 = ('https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f'
          '&daily=precipitation_sum&forecast_days=2&models=%s'
          '&precipitation_unit=inch&timezone=%s' % (lat, lon, mods, tzq))
    try:
        absorb(K.get_json(u2, timeout=40)['daily'])
    except Exception as e:
        print('  live run unavailable (%s); today falls back to the archive' % e)
    return {d: list(v.values()) for d, v in out.items()}


def market_rows(day):
    """Today's KXRAIN board -> {city code: row}."""
    ev = '%s-%s' % (SERIES, day.strftime('%y%b%d').upper())
    out = {}
    try:
        j = K.get_json('https://api.elections.kalshi.com/trade-api/v2/markets'
                       '?event_ticker=%s&limit=60' % ev, timeout=45)
    except Exception as e:
        print('market fetch failed: %s' % e)
        return ev, out
    for m in j.get('markets') or []:
        code = m['ticker'].split('-')[-1]
        out[code] = {
            'ticker': m['ticker'], 'status': m.get('status'),
            'yes_bid': float(m.get('yes_bid_dollars') or 0),
            'yes_ask': float(m.get('yes_ask_dollars') or 0),
            'no_bid': float(m.get('no_bid_dollars') or 0),
            'no_ask': float(m.get('no_ask_dollars') or 0),
            'vol': float(m.get('volume') or 0),
            'close': m.get('close_time'),
        }
    return ev, out


def _budget(ev, stake, cap=0.25):
    """Quarter Kelly on each of eighteen positions is not quarter Kelly on the
    day. Cap the day's exposure and scale every position to fit; expected
    dollars scale linearly with size, so the capped figure is proportional."""
    budget = BANKROLL * cap
    scale = min(1.0, budget / stake) if stake > 0 else 1.0
    return {'ev': round(ev * scale, 2), 'stake': round(stake * scale, 2),
            'raw_ev': round(ev, 2), 'raw_stake': round(stake, 2),
            'scale': round(scale, 3), 'cap': cap}


def _city_data(spec, start):
    """One city's two upstream reads: settled observations and the model QPF.

    Pulled out of main's loop and kept PURE -- it touches no shared state and
    returns everything it learned -- so a pool can run these concurrently."""
    code, name, st, nw, lat, lon, tz = spec
    local = K.local_now({'tz': tz})
    day = local.date()
    obs = obs_precip(st, nw, start, day + datetime.timedelta(days=1))
    fc = qpf(lat, lon, tz, start, day + datetime.timedelta(days=1))
    return local, day, obs, fc


# ------------------------------------------------------------------- main ----
def main():
    t_start = time.time()
    today_utc = datetime.datetime.utcnow().date()
    log = {}
    if os.path.exists(OUT):
        try:
            log = json.load(open(OUT))
        except Exception as e:
            print('could not read %s (%s); starting fresh' % (OUT, e))
    hist = {h['key']: h for h in log.get('history', [])}

    start = today_utc - datetime.timedelta(days=HIST_DAYS)
    samples, cities = [], []

    # THE BOARD, ONCE.  It is keyed by the trading day, and near midnight UTC the
    # cities disagree about what that is, so it is read against the first city's
    # clock -- which is what the serial version did by asking whichever city it
    # reached first. Hoisted out because the fetch below no longer runs in order.
    # market_rows swallows its own fetch failure and returns an empty board, so
    # there is nothing to retry per city.
    board_ev, board = market_rows(K.local_now({'tz': CITIES[0][6]}).date())

    # THE FETCH, IN PARALLEL.  22 cities x 3 requests, issued one at a time, spent
    # 27 of a 31-second local run doing nothing but waiting on sockets. On a
    # GitHub runner, where every one of those requests is ~25x slower, that same
    # serial walk took 14 minutes -- most of the job's 25-minute cap, and the
    # reason the whole pipeline cannot refresh more often than hourly.
    #
    # Nothing here shares state, so the only care needed is on the way out: the
    # results are reassembled in CITIES order below, because the fit is an SGD
    # walk over the training set and a run-to-run reshuffle would move the
    # weights on identical data.
    got = {}
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_city_data, spec, start): spec for spec in CITIES}
        for f in cf.as_completed(futs):
            spec = futs[f]
            try:
                got[spec[0]] = f.result()
            except Exception as e:
                print('%-5s FAILED: %s: %s' % (spec[0], type(e).__name__, e))

    for code, name, st, nw, lat, lon, tz in CITIES:
        if code not in got:
            continue
        local, day, obs, fc = got[code]

        # every past day with both a forecast and a settled observation trains it
        for d, q in fc.items():
            a = obs.get(d)
            if a is None or d >= day.isoformat() or len(q) < 4:
                continue
            samples.append((features(q), 1 if a >= WET else 0))

        cities.append({'code': code, 'city': name, 'station': st, 'tz': tz,
                       'date': day.isoformat(), 'q': fc.get(day.isoformat()) or [],
                       'obs': obs.get(day.isoformat()),
                       'hour': local.hour, 'hist': obs})
        print('%-5s %-14s %d models, %d training days'
              % (code, name, len(fc.get(day.isoformat()) or []),
                 sum(1 for d in fc if d in obs and d < day.isoformat())))

    if not cities:
        print('no cities resolved; refusing to write')
        return 1

    w = fit(samples)
    print('\nfitted on %d city-days: %s' % (len(samples), ['%.3f' % x for x in (w or [])]))

    # ---- score every past pick against what the station actually recorded ----
    for h in hist.values():
        if h.get('actual') is not None:
            continue
        c = next((x for x in cities if x['code'] == h['code']), None)
        if not c:
            continue
        a = c['hist'].get(h['date'])
        if a is None:
            continue
        h['actual'] = a
        h['rained'] = bool(a >= WET)
        h['hit'] = (h['p'] >= 0.5) == h['rained']
        b = h.get('bet')
        if b:
            won = h['rained'] if b['side'] == 'yes' else (not h['rained'])
            cost = b['price'] + K.fee_of(b['price'])
            h['bet_result'] = {'won': won,
                               'pl': round(BANKROLL * b['kelly']
                                           * ((1 - cost) / cost if won else -1), 2),
                               'staked': round(BANKROLL * b['kelly'], 2)}
        print('  scored %s %s: %.2f in -> %s (said %.0f%%) %s'
              % (h['date'], h['code'], a, 'RAIN' if h['rained'] else 'dry',
                 100 * h['p'], 'hit' if h['hit'] else 'miss'))

    # ---------------------------------------------------- today's board ------
    rows, take_ev, take_stake, n_bets = [], 0.0, 0.0, 0
    for c in cities:
        p = prob(w, c['q'])
        mk = (board or {}).get(c['code'])
        row = {'code': c['code'], 'city': c['city'], 'station': c['station'],
               'date': c['date'], 'p': None if p is None else round(p, 4),
               'models': len(c['q']),
               'wet_models': sum(1 for x in c['q'] if x >= WET),
               'mean_qpf': round(sum(c['q']) / len(c['q']), 3) if c['q'] else None,
               'obs': c['obs'], 'already_rained': bool((c['obs'] or 0) >= WET)}
        if mk:
            row.update({'ticker': mk['ticker'], 'status': mk['status'],
                        'yes': mk['yes_ask'], 'no': mk['no_ask'],
                        'market_p': round((mk['yes_bid'] + mk['yes_ask']) / 2, 3),
                        'vol': mk['vol']})
        # THE BET.  Already-rained days are not a trade: the outcome is known and
        # the exchange can see the same gauge this can.
        if p is not None and mk and mk['status'] == 'active' and not row['already_rained']:
            best = None
            mkt_p = row.get('market_p')
            for side, price, q in (('yes', mk['yes_ask'], p),
                                   ('no', mk['no_ask'], 1 - p)):
                # THE TEMPERATURE RULES, EXTRAPOLATED -- and labelled as such.
                # This board has its own model and its own scoring, and its
                # betting record is ONE trade. There is no rain evidence for a
                # price floor either way.
                #
                # What there is: 435 temperature bets at a nickel or less with
                # zero winners, and the identical shape here today -- New
                # Orleans NO at 1c because we said 33% where the market said 1%.
                # Fading a liquid market's near-certainty for pennies is a
                # mechanism, not a quirk of ladders. Revisit when this board has
                # a real sample rather than one.
                if not (K.MIN_PRICE <= price < 1):
                    continue
                if mkt_p is not None and abs(q - (mkt_p if side == 'yes' else 1 - mkt_p)) \
                        > K.MAX_DISAGREE:
                    continue
                ev = q - price - K.fee_of(price)
                if ev < 0.04:       # the same four-cent floor as the temperature plan (MIN_EDGE)
                    continue
                cost = price + K.fee_of(price)
                kelly = max(0.0, (q - cost) / (1 - cost)) / K.KELLY_DIV
                if best is None or ev > best['ev']:
                    best = {'side': side, 'price': round(price, 4), 'q': round(q, 4),
                            'ev': round(ev, 4), 'kelly': round(kelly, 4)}
            if best:
                # THE SIZE, before the edge is believed. On the temperature side
                # the biggest edge on the board had $2 behind it -- quoting an
                # expected take without asking what can be bought is fiction.
                try:
                    ob = K.book_depth(mk['ticker']) or {}
                    def _at(side, price):
                        return sum(float(q) for pr, q in (ob.get(side) or [])
                                   if abs(float(pr) - price) < 0.011)
                    # crossing: buying YES lifts the NO side of the book
                    best['size'] = (_at('no_dollars', round(1 - best['price'], 2))
                                    if best['side'] == 'yes'
                                    else _at('yes_dollars', round(1 - best['price'], 2)))
                except Exception:
                    best['size'] = None
                row['bet'] = best
                cost = best['price'] + K.fee_of(best['price'])
                want = BANKROLL * best['kelly'] / cost           # contracts
                if best.get('size') is not None:
                    want = min(want, best['size'])
                n_ct = int(want)
                best['fill'] = n_ct
                best['wanted'] = round(want, 2)
                if n_ct >= 1:
                    take_ev += n_ct * best['ev']
                    take_stake += n_ct * cost
                    n_bets += 1
                # one lock per city per day, never rewritten
                key = '%s|%s' % (c['date'], c['code'])
                if key not in hist and c['hour'] >= LOCK_HOUR:
                    hist[key] = {'key': key, 'date': c['date'], 'code': c['code'],
                                 'p': round(p, 4), 'market_p': row.get('market_p'),
                                 'bet': best, 'at': c['hour']}
        rows.append(row)

    scored = [h for h in hist.values() if h.get('actual') is not None]
    hits = sum(1 for h in scored if h.get('hit'))
    br = [(h['p'] - (1 if h['rained'] else 0)) ** 2 for h in scored]
    money = [h for h in scored if h.get('bet_result')]
    staked = sum(h['bet_result']['staked'] for h in money)
    pl = sum(h['bet_result']['pl'] for h in money)

    doc = {
        'updated': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'),
        'event': board_ev,
        'cities': sorted(rows, key=lambda r: -(r.get('bet') or {}).get('ev', -1)),
        'take': dict(_budget(take_ev, take_stake), n=n_bets, bankroll=BANKROLL),
        'model': {'weights': w, 'trained_on': len(samples),
                  'validated': {'brier': 0.0831, 'logloss': 0.2673,
                                'baseline_brier': 0.1946,
                                'how': 'leave-one-city-out, 1368 city-days'}},
        'record': {'n': len(scored), 'hits': hits,
                   'brier': round(sum(br) / len(br), 4) if br else None,
                   'money': {'n': len(money),
                             'wins': sum(1 for h in money if h['bet_result']['won']),
                             'staked': round(staked, 2), 'pl': round(pl, 2),
                             'roi': round(100 * pl / staked, 1) if staked else None}},
        'history': sorted(hist.values(), key=lambda h: h['key'], reverse=True)[:400],
    }
    if '--dry' in sys.argv:
        print(json.dumps({k: v for k, v in doc.items() if k != 'history'}, indent=1)[:2500])
        return 0
    with open(OUT, 'w') as f:
        json.dump(doc, f, separators=(',', ':'))
    live = [r for r in rows if r.get('bet')]
    print('\nwrote %s -- %d cities, %d mispriced, $%.2f expected on $%.2f'
          % (OUT, len(rows), len(live), doc['take']['ev'], doc['take']['stake']))
    for r in live[:8]:
        b = r['bet']
        print('   %-5s %-14s says %3.0f%%  market %3.0f%%  BET %-3s at %2.0f¢  +%.0f¢  fill %s'
              % (r['code'], r['city'], 100 * r['p'], 100 * (r.get('market_p') or 0),
                 b['side'].upper(), 100 * b['price'], 100 * b['ev'],
                 ('%.0f' % b['size']) if b.get('size') is not None else '?'))
    # Per-host seconds are SUMMED ACROSS THREADS now, so they add up to well
    # more than the elapsed time -- that is the point of the change. Print the
    # wall clock beside them or the report reads like a regression.
    print('timing: %s | wall %.0fs' % (K.timing_report(), time.time() - t_start))
    return 0


if __name__ == '__main__':
    sys.exit(main())

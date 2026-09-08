#!/usr/bin/env python3
"""Daily NYC-high forecast scored against the Kalshi KXHIGHNY market.

Writes /kalshi_ny.json for the Belvedere Castle popup on the CITY map.

Why a scheduled job rather than a browser fetch: Kalshi answers 403 to any
request carrying an Origin header, and our proxy worker is 403'd too, so the
page cannot reach the market live. It reads our own repo instead.

THE MODEL

    pred = max( observed_high_so_far,  damped( consensus_peak - per_model_bias ) )

  * FIVE models are averaged (NBM, ECMWF, GFS, ICON, GEM), each with its own
    rolling bias -- the mean (that model's peak - actual) over the previous 21
    scored days.  Every member beat our old single source and the average beat
    every member: MAE 1.66 -> 1.44, Brier 0.559 -> 0.491 on the real ladders.
    Removing each model's warm bias is still most of the skill.
  * the observed-so-far term is a hard physical floor.  It is one-sided, so it
    deliberately introduces a positive bias (+0.79 degF at noon) -- without it
    the forecast is near-unbiased at -0.15 but MAE is far worse, 2.64 vs 1.98.
    Do NOT "correct" that bias away; the floor is a constraint, not an error,
    and the distribution is truncated at it instead.
  * the spread is measured from the last 45 days of this model's own residuals
    at the current hour, recomputed every run.  A fixed table cannot work: the
    spread is strongly seasonal, SD 3.79 in March against 2.06 in August.
  * REJECTED, do not re-add: an intraday nudge carrying the morning's forecast
    error into the afternoon (MAE 3.07 -> 3.54; the morning error does not
    persist to the peak), and a second-stage mean correction on the residuals
    (worse in 4 of 5 months at full strength).

  * warm-ups are damped -- see point_forecast(), the strongest usable signal.

  TWO LOCKS, both well before the 11:59pm close.  Noon is the honest forecast
  and what the skill record scores.  18:00 is the call worth acting on -- the
  day has largely resolved by then and backtested accuracy jumps from 40/68 to
  53/68 (MAE 1.44 -> 0.68).

  Verified by replaying this exact model on the 68 real Kalshi ladders and
  scoring against the settlements themselves (2026-06-28..09-03):
  39/68 brackets, Brier 0.559 against 0.833 for a uniform guess.  Each fix
  compounded: the spread alone took Brier 0.628 -> 0.592 and log loss
  1.309 -> 1.134, putting reliability on the diagonal (stated 38% -> 38%
  actual, 70% -> 71%); damping warm-ups then took MAE 1.83 -> 1.66,
  bias +0.78 -> +0.20 and Brier -> 0.559.

SETTLEMENT: Central Park (CLINYC), reported in WHOLE degrees -- 1096 of 1114
observations are integers.  So "84 to 85" means the reported integer is 84 or
85, i.e. the true temperature lies in [83.5, 85.5).  Bracket edges are offset
by half a degree accordingly; getting this wrong shifts every probability.

  AND IT IS READ DIRECTLY -- see twc_today().  The rulebook says "according to
  The Weather Company", and for a long time that sentence was in the output
  string while the number was never fetched: the floor was built from the
  station's hourly METAR plus a fitted offset, a proxy that agreed on settled
  days because settled days are the ones where it had already agreed.  It is
  not a small difference and it is not a constant.  On 2026-09-05 the station
  peaked at 77, the offset was +0.71, and this model put 85.6% on "78 or below"
  and called the 1c ask an 85c edge -- while TWC held 79 and the market held
  0.5%.  With TWC in the floor the same afternoon reads 0.0% against the
  market's 0.5%.  The proxy was never checkable in the only window that pays.

Kalshi prices live in the *_dollars fields.  The legacy integer-cent fields
(yes_bid, last_price) are present but always null -- do not read them.
"""

import json, math, os, statistics, sys, urllib.request, urllib.error
import io, csv, re, base64, collections, datetime, time
import threading, concurrent.futures as cf
RUN_STARTED = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

HERE = os.path.dirname(os.path.abspath(__file__))

# Kalshi lists ~369 weather series (other cities' highs and lows, rain, snow).
# Adding one is a dict here: its series ticker, the station that settles it, the
# co-ordinates to forecast for, and the file the page reads. Everything below is
# written against a market config, never against NYC specifically. Each market
# runs isolated, so one broken feed cannot take the others down.
# Every Kalshi daily-high city market. Skill per market is unchanged by adding
# more of them, but the number of MISPRICED ranges per day scales with the count
# -- the one lever that does not require getting smarter. Each runs
# independently and writes its own file; one city's outage cannot stop the rest.
#
# The station is whatever the market's own rules name. Read them, do not guess:
# Chicago settles on CLIMDW, which is MIDWAY, not O'Hare -- the two routinely
# differ by a degree and O'Hare would have been wrong all season.
def _mkt(key, series, city, station, net, lat, lon, tz, tzl, cli, slug, wfo=None,
         skill=True, twc_geocode=False, bias_hl=None, sd_mult=1.0):
    return {'key': key, 'series': series, 'label': city + ' daily high',
            'wfo': wfo, 'skill': skill, 'twc_geocode': twc_geocode, 'bias_hl': bias_hl,
            'sd_mult': sd_mult,
            'station': station, 'network': net, 'lat': lat, 'lon': lon,
            'field': 'max_temp_f', 'tz': tz, 'tzlabel': tzl,
            'city': city, 'cli': cli,
            'url': 'https://kalshi.com/markets/%s/%s' % (series.lower(), slug),
            'out': 'kalshi_%s.json' % key.split('_')[0]}


MARKETS = [
    # NEW YORK KEEPS THE EQUAL-WEIGHT MEAN. The skill weights that lift the
    # twenty-city pool (see biases_factory) lose here on every morning hour of
    # the 67 settled days: 9 AM hit 72% equal against 64% weighted, MAE 1.05
    # against 1.09, Brier .459 against .475, and the same order at noon and
    # 2 PM. Sixty-seven days is a small court, but the equal mean is also the
    # form the 607-day refit validated, so it is the conservative choice for
    # the one market that matters most. Revisit with the live trail.
    _mkt('ny_high',  'KXHIGHNY',   'New York',     'NYC', 'NY_ASOS', 40.7789, -73.9692,
         'America/New_York',    'ET', 'CLINYC', 'highest-temperature-in-nyc', 'OKX',
         skill=False, twc_geocode=True, bias_hl=7),
    _mkt('chi_high', 'KXHIGHCHI',  'Chicago',      'MDW', 'IL_ASOS', 41.786,  -87.752,
         'America/Chicago',     'CT', 'CLIMDW', 'highest-temperature-in-chicago', 'LOT'),
    _mkt('mia_high', 'KXHIGHMIA',  'Miami',        'MIA', 'FL_ASOS', 25.791,  -80.316,
         'America/New_York',    'ET', 'CLIMIA', 'highest-temperature-in-miami', 'MFL'),
    # AUSTIN KEEPS THE EQUAL-WEIGHT MEAN, like New York: on 67 replayed days
    # the skill weights scored Brier .495 / MAE .79 against .470 / .70 with
    # every model weighted 1 (2026-09-06).
    _mkt('aus_high', 'KXHIGHAUS',  'Austin',       'AUS', 'TX_ASOS', 30.183,  -97.680,
         'America/Chicago',     'CT', 'CLIAUS', 'highest-temperature-in-austin', 'EWX',
         skill=False),
    _mkt('den_high', 'KXHIGHDEN',  'Denver',       'DEN', 'CO_ASOS', 39.847, -104.656,
         'America/Denver',      'MT', 'CLIDEN', 'highest-temperature-in-denver', 'BOU'),
    _mkt('lax_high', 'KXHIGHLAX',  'Los Angeles',  'LAX', 'CA_ASOS', 33.938, -118.389,
         'America/Los_Angeles', 'PT', 'CLILAX', 'highest-temperature-in-los-angeles', 'LOX'),
    _mkt('phl_high', 'KXHIGHPHIL', 'Philadelphia', 'PHL', 'PA_ASOS', 39.873,  -75.227,
         'America/New_York',    'ET', 'CLIPHL', 'highest-temperature-in-philadelphia', 'PHI'),
    # THIRTEEN MORE, ADDED 2026-09-06. Kalshi's newer KXHIGHT* family. Every one
    # was checked the same way before going in: the station is the one the
    # market's own rules_primary names; IEM's daily max for that station equals
    # Kalshi's published expiration_value on 8 of 8 recent settled days in all
    # thirteen; and TWC's history endpoint resolves to that station and not a
    # neighbour (obs_name checked -- the KNYC=LaGuardia trap does not recur).
    # Houston is HOBBY (CLIHOU), not Intercontinental. Dallas is DFW. Phoenix
    # keeps standard time all year, hence its own zone.
    _mkt('phx_high', 'KXHIGHTPHX',  'Phoenix',       'PHX', 'AZ_ASOS', 33.4343, -112.0116,
         'America/Phoenix',     'MST', 'CLIPHX', 'phoenix-high-temperature-daily', 'PSR'),
    _mkt('sfo_high', 'KXHIGHTSFO',  'San Francisco', 'SFO', 'CA_ASOS', 37.6190, -122.3749,
         'America/Los_Angeles', 'PT', 'CLISFO', 'san-francisco-high-temperature-daily', 'MTR'),
    _mkt('sea_high', 'KXHIGHTSEA',  'Seattle',       'SEA', 'WA_ASOS', 47.4447, -122.3144,
         'America/Los_Angeles', 'PT', 'CLISEA', 'seattle-maximum-temperature-daily', 'SEW'),
    # LAS VEGAS IS UNDERCONFIDENT: on 66 replayed days the Brier score falls
    # monotonically as the residual spread is tightened (x1.0 .333, x0.85
    # .314, x0.7 .301) with the hit rate unchanged; the calibration bands said
    # the same (a 55% call came true 65% of the time). 0.75, not the best
    # tested, because the test is in-sample and a rain day punishes
    # overconfidence (2026-09-06).
    _mkt('las_high', 'KXHIGHTLV',   'Las Vegas',     'LAS', 'NV_ASOS', 36.0719, -115.1634,
         'America/Los_Angeles', 'PT', 'CLILAS', 'las-vegas-max-daily-temperature', 'VEF',
         sd_mult=0.75),
    _mkt('bos_high', 'KXHIGHTBOS',  'Boston',        'BOS', 'MA_ASOS', 42.3606,  -71.0097,
         'America/New_York',    'ET', 'CLIBOS', 'boston-maximum-daily-temperature', 'BOX'),
    _mkt('dfw_high', 'KXHIGHTDAL',  'Dallas',        'DFW', 'TX_ASOS', 32.8968,  -97.0380,
         'America/Chicago',     'CT', 'CLIDFW', 'dallas-maximum-temperature', 'FWD'),
    _mkt('msp_high', 'KXHIGHTMIN',  'Minneapolis',   'MSP', 'MN_ASOS', 44.8854,  -93.2313,
         'America/Chicago',     'CT', 'CLIMSP', 'minneapolis-daily-high-temperature', 'MPX'),
    _mkt('hou_high', 'KXHIGHTHOU',  'Houston',       'HOU', 'TX_ASOS', 29.6375,  -95.2824,
         'America/Chicago',     'CT', 'CLIHOU', 'daily-high-temperature-houston', 'HGX'),
    _mkt('sat_high', 'KXHIGHTSATX', 'San Antonio',   'SAT', 'TX_ASOS', 29.5300,  -98.4673,
         'America/Chicago',     'CT', 'CLISAT', 'san-antonio-daily-maximum-temperature', 'EWX'),
    _mkt('okc_high', 'KXHIGHTOKC',  'Oklahoma City', 'OKC', 'OK_ASOS', 35.3889,  -97.6006,
         'America/Chicago',     'CT', 'CLIOKC', 'oklahoma-city-maximum-high-temperature', 'OUN'),
    _mkt('atl_high', 'KXHIGHTATL',  'Atlanta',       'ATL', 'GA_ASOS', 33.6301,  -84.4418,
         'America/New_York',    'ET', 'CLIATL', 'atlanta-max-temperature', 'FFC'),
    _mkt('dca_high', 'KXHIGHTDC',   'Washington',    'DCA', 'VA_ASOS', 38.8472,  -77.0346,
         'America/New_York',    'ET', 'CLIDCA', 'washington-dc-daily-max-temp', 'LWX'),
    _mkt('msy_high', 'KXHIGHTNOLA', 'New Orleans',   'MSY', 'LA_ASOS', 29.9933,  -90.2511,
         'America/Chicago',     'CT', 'CLIMSY', 'new-orleans-max-temp-daily', 'LIX'),
]

# THREE CITIES, MASTERED ONE DAY AT A TIME (user, 2026-09-06: "drop all the
# cities except for the 3 we have now ... drop the rest of the baggage"). The
# other seventeen configs stay above so any can be re-admitted by adding its
# key here; their files stopped updating on 2026-09-06.
ACTIVE = ('ny_high', 'las_high', 'aus_high')
MARKETS = [m for m in MARKETS if m['key'] in ACTIVE]

WORKERS = 4                               # markets in flight at once (see main)
BIAS_K  = int(os.environ.get('BV_BIAS_K', '30') or 30)   # days in the rolling bias window (BV_BIAS_K for backlog tests) (21 -> 30 on 2026-09-06: Brier .499 -> .491 with the skill weights, 20 cities)
BIAS_MIN = 7                              # need this many before trusting it
LOCK_HOUR = 12                            # noon ET: morning obs in hand, peak ahead
EVE_HOUR = 17                             # the overnight plan is logged from 5 PM local
FINAL_HOUR = 18                           # the actionable call, still 6 h before close

# Five-model consensus.  Every one of these beats our old single source, and
# averaging them beats every individual member: on the real ladders MAE 1.66 ->
# 1.44 and Brier 0.559 -> 0.491.  Chosen a priori as the major global/regional
# runs rather than picked by score, so there is no selection built in.  NBM is
# NOAA's own blend, the closest public thing to what the market's providers use.
# meteofrance_seamless is deliberately excluded -- clearly worst at MAE 2.19.
MODELS = ['ncep_hrrr_conus', 'ncep_nbm_conus', 'ecmwf_ifs025', 'gfs_seamless',
          'icon_seamless', 'gem_seamless']
# NOAA's two run over CONUS only and answer HTTP 400 anywhere else. The per-model
# guard would catch that, but it would burn three retries with backoff on a
# request that can never succeed, every run, for any market outside the US.
CONUS_ONLY = {'ncep_hrrr_conus', 'ncep_nbm_conus'}


def models_for(cfg):
    """The models that actually cover this market's location."""
    inside = (20.0 <= cfg['lat'] <= 55.0) and (-130.0 <= cfg['lon'] <= -60.0)
    return [m for m in MODELS if inside or m not in CONUS_ONLY]

# Spread on a call made the day before, measured over 601 days: bias -0.09,
# MAE 1.64, SD 2.19. Wider than any same-day figure because there is no
# observed floor yet and the run is a day older -- tomorrow is a genuine
# forecast, not a half-settled fact.
TOMORROW_SD = 2.19
SWING_DAMP = 0.05         # see point_forecast(): models overdo warm-ups
RESID_M = 45              # days of recent residuals behind the spread estimate
SD_FLOOR = 0.25
# a multiplier on the residual spread, for calibration studies (BV_SD_MULT);
# 1.0 in production until a backlog test says otherwise
SD_MULT = float(os.environ.get('BV_SD_MULT', '1.0') or 1.0)
# Fallback only, for the first runs before enough residuals accumulate. These
# came from a 173-day fit; the live model prefers its own rolling estimate
# because the spread is strongly seasonal (SD 3.8 in March, 2.1 in August), so
# any fixed table is wrong for half the year.
# Refit 2026-09-04 on 607 days spanning all twelve months, on the five-model
# consensus. The previous table came from ONE model over 173 spring-summer days
# and was far too wide -- 2.64 at noon where the consensus achieves 1.76.
# Bracket accuracy by hour on fixed 2 degF bins. REFIT on the fresh product --
# every constant here was originally fitted on the day-old run, and after that
# switch the whole table was wrong, understating the morning by 13 points. The
# shape changed too: with a same-day run the morning is already strong (57%) and
# the curve is nearly flat, so there is no longer a decisive "wait until noon"
# step. What still improves through the day is certainty, not accuracy.
HOUR_ACC = {8:57, 9:57, 10:57, 11:57, 12:59, 13:58, 14:55, 15:55, 16:56,
            17:56, 18:58, 19:58, 20:58, 21:58, 22:58}
SD_FALLBACK = {8:1.20, 9:1.20, 10:1.20, 11:1.17, 12:1.10, 13:1.01, 14:0.92,
               15:0.82, 16:0.78, 17:0.72, 18:0.66, 19:0.64, 20:0.61, 21:0.59, 22:0.58}


# Where the wall clock goes, by host. The job runs in ~70s on a laptop and hit a
# 15-minute cap on a GitHub runner at the same work, so the bottleneck is one
# upstream throttling that network and not the code. Guessing which would be
# guessing; this measures it.
# PER THREAD, since 2026-09-06: markets run concurrently (see main), and a
# process-wide table would blend one city's Open-Meteo stall into another's
# summary line. Each thread keeps its own; the report reads the caller's.
_TL = threading.local()


def _timing():
    t = getattr(_TL, 'timing', None)
    if t is None:
        t = _TL.timing = collections.defaultdict(lambda: [0.0, 0, 0])
    return t


def timing_report(reset=True):
    TIMING = _timing()
    if not TIMING:
        return ''
    parts = []
    for host, (secs, calls, retries) in sorted(TIMING.items(), key=lambda kv: -kv[1][0]):
        parts.append('%s %.0fs/%d%s' % (host, secs, calls,
                                        (' +%dretry' % retries) if retries else ''))
    out = ' | '.join(parts)
    if reset:
        TIMING.clear()
    return out


# THE LOG, ONE MARKET AT A TIME. Twenty markets printing from four threads at
# once would interleave into something no one could read back after a bad
# day, and the per-city lines are how every bug so far has been found. So a
# worker thread's print goes to its own buffer, and main writes each market's
# block out whole the moment that market finishes. Shadowing the builtin at
# module level covers every print in this file without touching them.
_print = print


def print(*a, **k):                                   # noqa: A001
    buf = getattr(_TL, 'log', None)
    if buf is None:
        return _print(*a, **k)
    buf.append(k.get('sep', ' ').join(str(x) for x in a))


# PER-HOST CONCURRENCY. With four markets in flight the first thing each one
# does is ask kalshi.com for its ladder, and four of those at once was enough:
# Chicago's very first request answered 429 three times inside eleven seconds
# and the whole market failed for the run (2026-09-06, first parallel bake).
# Two in flight per host is well inside every limit met so far and costs
# nothing measurable; the hosts not listed keep the pool's own width.
_HOST_LIMIT = {'kalshi.com': 2, 'weather.com': 2}
_HOST_SEM = {}
_SEM_LOCK = threading.Lock()


def _host_sem(host):
    with _SEM_LOCK:
        sem = _HOST_SEM.get(host)
        if sem is None:
            sem = _HOST_SEM[host] = threading.Semaphore(_HOST_LIMIT.get(host, WORKERS))
        return sem


def get(url, timeout=90, tries=3):
    """Fetch with backoff. These feeds are all third-party and all flaky at some
    point in a day; a bare retry loop hammers a struggling host instead of
    letting it recover.

    A 429 is a different failure from a timeout: the host is saying "not yet",
    and answering it with the same 1.5 s pause that a dropped socket gets is
    how a whole market disappears from a run. It gets longer waits, the host's
    own Retry-After when it sends one, and up to six attempts."""
    host = urllib.parse.urlsplit(url).netloc.split('.')[-2:]
    host = '.'.join(host)
    t0 = time.time()
    last = None
    a = 0
    while True:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'bluishvoid/1.0'})
            with _host_sem(host):
                body = urllib.request.urlopen(req, timeout=timeout).read()
            e = _timing()[host]
            e[0] += time.time() - t0
            e[1] += 1
            e[2] += a
            return body
        except Exception as e:
            last = e
            code = getattr(e, 'code', None)
            limit = 6 if code == 429 else tries
            if a + 1 >= limit:
                break
            if code == 429:
                wait = None
                try:
                    wait = float(e.headers.get('Retry-After'))
                except Exception:
                    pass
                time.sleep(wait if wait else 3.0 * (2 ** a))
            else:
                time.sleep(1.5 * (2 ** a))
            a += 1
    e = _timing()[host]
    e[0] += time.time() - t0
    e[1] += 1
    e[2] += a
    raise last


def get_json(url, **kw):
    return json.loads(get(url, **kw))


def local_now(cfg):
    """Wall-clock time where the market settles.

    These markets settle on the local calendar day, so every hour comparison --
    the noon lock, the 6pm call, which forecast hours are still ahead -- has to
    be in the station's own zone, not ours. Falls back to a fixed US-Eastern
    offset only if the tz database is unavailable.
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo(cfg.get('tz', 'America/New_York'))).replace(tzinfo=None)
    except Exception:
        u = datetime.datetime.utcnow()
        return u - datetime.timedelta(hours=4 if 3 <= u.month <= 11 else 5)


# ---------------------------------------------------------------- market ----
def event_ticker(cfg, d):
    return '%s-%s' % (cfg['series'], d.strftime('%y%b%d').upper())


def market_state(cfg, rows, now):
    """Where a day's market is in its life: not open yet, trading, or closed.

    Each day's ladder is listed at 10am local the day before and trades until
    about 1am the night after -- a 15 hour overlap, not the 10 first assumed --
    so between 1am and 10am only ONE of the two is live. Saying which is the difference between 'no bet' meaning we see no
    value and 'no bet' meaning the market does not exist yet.
    """
    if not rows:
        # the ladder for a day is listed at 10am local the day before
        return {'status': 'not_open', 'opens': '10 AM'}
    def local(iso):
        if not iso:
            return None
        try:
            u = datetime.datetime.strptime(iso[:19], '%Y-%m-%dT%H:%M:%S')
        except Exception:
            return None
        off = 4 if 3 <= u.month <= 11 else 5      # the tz label's own offset
        return u - datetime.timedelta(hours=off)
    o, c = local(rows[0].get('open')), local(rows[0].get('close'))
    st = 'open'
    if o and now < o:
        st = 'not_open'
    elif c and now >= c:
        st = 'closed'
    return {'status': st,
            'opens': o.strftime('%a %-I %p').upper() if o else None,
            'closes': c.strftime('%a %-I %p').upper() if c else None}


def book_depth(ticker):
    """Contracts available at the best price, each way -> (yes_size, no_size).

    Sizing advice is worthless if the book cannot fill it, and this book is
    thin: often under a hundred contracts at the touch. Note the crossing --
    the size you can BUY yes at sits on the no side of the book, and the other
    way round.
    """
    try:
        # depth=1 returns the LOWEST level, not the best -- the levels come back
        # ascending, so the touch is the last one. Ask for enough to contain it.
        o = (get_json('https://api.elections.kalshi.com/trade-api/v2/markets/%s'
                      '/orderbook?depth=20' % ticker, timeout=45).get('orderbook_fp') or {})
    except Exception:
        return None, None
    return o


def fetch_market(cfg, ev):
    d = get_json('https://api.elections.kalshi.com/trade-api/v2/markets'
                 '?series_ticker=%s&status=open&limit=60' % cfg['series'])
    out = []
    for m in d.get('markets', []):
        if m.get('event_ticker') != ev:
            continue
        f, c = m.get('floor_strike'), m.get('cap_strike')
        st = m.get('strike_type')
        # Kalshi states the OPEN bounds as strict: cap_strike 82 on a `less`
        # market is "below 82", i.e. the integer 81 or under; floor_strike 89
        # on `greater` is "above 89", i.e. 90 or over.  Reading those strikes
        # literally shifts both tails a whole degree.
        if st == 'between':
            lo, hi = float(f), float(c)
        elif st in ('less', 'less_or_equal'):
            hi = float(c if c is not None else f)
            lo, hi = None, hi - (1.0 if st == 'less' else 0.0)
        else:                                     # greater / greater_or_equal
            lo = float(f)
            lo, hi = lo + (1.0 if st == 'greater' else 0.0), None
        bid = float(m.get('yes_bid_dollars') or 0)
        ask = float(m.get('yes_ask_dollars') or 0)
        # the other half of the instrument: you can also buy NO, which pays out
        # when this range does NOT happen. On a six-way ladder that is usually
        # where the value is -- there are five ways to be right instead of one.
        nbid = float(m.get('no_bid_dollars') or 0)
        nask = float(m.get('no_ask_dollars') or 0)
        # how much can actually be bought at those prices
        ysz = nsz = None
        ob = book_depth(m['ticker'])
        if ob:
            def _at(side, price):
                return sum(float(q) for pr, q in (ob.get(side) or [])
                           if abs(float(pr) - price) < 0.011)
            ysz = _at('no_dollars', round(1 - ask, 2)) if 0 < ask < 1 else 0
            nsz = _at('yes_dollars', round(1 - nask, 2)) if 0 < nask < 1 else 0
        out.append({
            'ticker': m['ticker'], 'label': m.get('yes_sub_title') or '',
            'lo': lo, 'hi': hi, 'bid': bid, 'ask': ask,
            'nbid': nbid, 'nask': nask, 'ysize': ysz, 'nsize': nsz,
            'mid': round((bid + ask) / 2, 4),
            'vol': float(m.get('volume_fp') or 0),
            # OPEN INTEREST is the pool: contracts currently held, each of which
            # settles at a dollar, so the count IS the money riding on this rung.
            # Volume counts every trade including the ones already closed out, so
            # it says how busy the market has been rather than how much is on it.
            'oi': float(m.get('open_interest_fp') or m.get('open_interest') or 0),
            'close': m.get('close_time'), 'open': m.get('open_time'),
        })
    out.sort(key=lambda r: (r['lo'] if r['lo'] is not None else -999))
    return out


# ------------------------------------------------------------- observed ----
# TRUTH SOURCE.  IEM's daily.json max_tmpf, rounded, landed in the bracket
# Kalshi actually settled on **68 of 68** settled markets (2026-06-28..09-03).
# It is also a RUNNING max during the current day, so the same field serves as
# both the settlement truth and today's floor.
#
# Do NOT compute the max from the hourly asos.py stream: the routine METAR
# misses the intra-hour peak and reads 1-2 degF LOW on 22 of 34 days (adding
# report_type=1..4 does not fix it).  An earlier calibration built on that
# stream reported MAE 1.54 / 56% when the truth was really MAE 1.93 / 49%.
# The hourly stream misses the intra-hour peak, so a running max taken from it
# needs lifting. Use the MEAN gap (0.62), not the median (1.0): this is an
# estimate of the true running max, not a bound on it, and the median overshot.
# With 1.0 the prediction sat +0.29 degF above the eventual high on days the
# floor was binding, which pushed real probability into brackets the day had
# already walked past -- 6% on a bracket that empirically never happened.
OFFSET_DEFAULT = 0.70          # fallback until a market has 40 days of its own
HOURLY_PEAK_OFFSET = OFFSET_DEFAULT
# Spread of that same gap. The residuals are measured against a floor built as
# hourly + offset, so they carry this noise; today's floor is the exact running
# max and carries none of it. On days the floor is binding the measured spread
# (0.75-0.88) is almost entirely this term -- deconvolving leaves ~0.3, which is
# what matches the empirical record: the high never rose 1.5 degF further after
# the floor was set, yet an un-deconvolved spread put 6% on a bracket that far
# out. Only applied when the floor is both binding and exact.
OFFSET_SD_DEFAULT = 0.65
OFFSET_SD = OFFSET_SD_DEFAULT
EXACT_FLOOR_SD_MIN = 0.30


def climate_day_start(cfg, day):
    """First hour of the NWS climate day, in the station's LOCAL clock.

    Settlement is the next-morning NWS Climate Report (CLI), whose period is
    12:00 AM - 11:59 PM **Local Standard Time**. Under daylight saving that is
    1:00 AM - 12:59 AM on the local clock, so the midnight hour belongs to the
    PREVIOUS climate day. Ignoring this inflates the morning floor on 23% of
    days by ~1.6 degF, and changes the day's max outright on 10 of 187 days --
    five of them in March-May, when a warm front can put the peak just after
    midnight. Summer hides it: 0 of 77 settled days diverged.
    """
    try:
        from zoneinfo import ZoneInfo
        z = ZoneInfo(cfg.get('tz', 'America/New_York'))
        noon = datetime.datetime(day.year, day.month, day.day, 12, tzinfo=z)
        return 1 if (noon.dst() or datetime.timedelta(0)) else 0
    except Exception:
        return 1 if 3 <= day.month <= 11 else 0


def running_max(obh, key, hour, h0):
    """Warmest reading so far on the climate day, from the hourly stream."""
    v = [x for h, x in (obh.get(key) or {}).items() if h0 <= h <= hour]
    return max(v) if v else None


def daily_series(cfg, start, end):
    """Settling temperature per day -> {'YYYY-MM-DD': degF}, including today's
    running value.

    Uses the date-ranged endpoint, not `daily.json` with no date: that returns
    the station's whole archive back to 1943 -- **8.8 MB on every run**, twenty
    times a day, for the ~75 days actually needed. The ranged form is 15 KB and
    was checked to agree with it exactly, today's running max included.
    """
    u = ('https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py'
         '?network=%s&stations=%s&year1=%d&month1=%d&day1=%d'
         '&year2=%d&month2=%d&day2=%d&format=comma'
         % (cfg['network'], cfg['station'], start.year, start.month, start.day,
            end.year, end.month, end.day))
    out = {}
    for r in csv.DictReader(io.StringIO(get(u, timeout=120).decode())):
        v = r.get(cfg['field'])
        if v not in (None, '', 'M', 'None'):
            try:
                out[r['day']] = float(v)
            except ValueError:
                pass
    return out


# THE LIVE OBSERVATION, AHEAD OF IEM.
#
# IEM is the archive and the settlement truth, but it publishes each :51 report
# late -- 10-15 minutes on a good afternoon, and on 2026-09-05 it stalled for
# over two hours. The market reads METAR directly and reprices within three
# minutes of the report (measured: minute :54 carries 4.6x the movement of an
# average minute), so a bake on IEM alone is trading against people who can see
# an observation it cannot.
#
# That gap cost something real. At 13:19 the panel recommended betting against
# a position, off an 11:51 reading, while the market -- holding the 12:51 -- had
# moved its odds forty points. The disagreement it was selling was not an edge,
# it was the hour it was blind for.
#
# So: aviationweather.gov first, IEM behind it. Keyless, no CORS problem for a
# job that runs server-side, and it had the 15:51 report five minutes after it
# was taken. IEM stays the archive and stays the settlement truth -- this only
# fills in TODAY's most recent hours, and only where it is actually newer.
def metar_today(cfg, day):
    """{hour: degF} for `day` from the live METAR feed, or {} if unavailable.

    reportTime in this API is rounded UP to the hour, so it cannot be used to
    place an observation: a :51 report comes back stamped as the next hour. The
    raw METAR carries its own DDHHMMZ group and that is what is parsed here.
    """
    from zoneinfo import ZoneInfo          # imported locally, as elsewhere here
    icao = cfg.get('icao') or ('K' + cfg['station'])
    try:
        j = get_json('https://aviationweather.gov/api/data/metar?ids=%s&format=json&hours=12'
                     % icao, timeout=45)
    except Exception as e:
        print('metar: %s unavailable (%s)' % (icao, e))
        return {}
    out = {}
    for m in (j or []):
        t = m.get('temp')
        if t is None:
            continue
        raw = str(m.get('rawOb') or '')
        stamp = None
        for tok in raw.split():
            if len(tok) == 7 and tok.endswith('Z') and tok[:6].isdigit():
                stamp = tok
                break
        if not stamp:
            continue
        try:
            utc = datetime.datetime(day.year, day.month, day.day,
                                    int(stamp[2:4]), int(stamp[4:6]),
                                    tzinfo=datetime.timezone.utc)
            # the day-of-month in the group settles which UTC date it belongs to
            utc = utc.replace(day=int(stamp[0:2]))
        except Exception:
            continue
        loc = utc.astimezone(ZoneInfo(cfg.get('tz', 'America/New_York')))
        if loc.date() != day:
            continue
        # TWO DECIMALS, AND THEY ARE EXACT. The ASOS reports Celsius to a
        # tenth, and a tenth of a C is 0.18 F -- so every reading lands on a
        # hundredth of a degree F and never on a whole one. Rounding to a
        # tenth here printed 2026-09-08's peak 26.1 C as 79.0 when it was
        # 78.98, and the panel then showed it as "79" -- a settled-looking
        # number that hid how far the 80 bracket really was (26.4 C).
        f = round(float(t) * 9.0 / 5.0 + 32.0, 2)
        out[loc.hour] = max(out.get(loc.hour, -99.0), f)
        # the day's latest report also gives the regime the trail logs (2026-09-07):
        # wind, present weather, sky cover -- the rain-day blind spot's raw material
        if utc > _METAR_LATEST.get(icao, {}).get('_utc', datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)):
            cl = m.get('clouds') or []
            _METAR_LATEST[icao] = {'_utc': utc, 'at': loc.strftime('%H:%M'), 'wdir': m.get('wdir'), 'wspd': m.get('wspd'),
                                   'wx': m.get('wxString') or None, 'cover': (cl[-1].get('cover') if cl else None),
                                   'rain': bool(m.get('wxString') and ('RA' in str(m.get('wxString')) or 'TS' in str(m.get('wxString')) or 'DZ' in str(m.get('wxString'))))}
    return out


_METAR_LATEST = {}
def metar_regime(cfg):
    icao = cfg.get('icao') or ('K' + cfg['station'])
    r = dict(_METAR_LATEST.get(icao) or {})
    r.pop('_utc', None)
    return r or None


# THE SETTLEMENT FEED ITSELF, NOT A PROXY FOR IT.
#
# Everything else here measures the weather. This reads the number the market
# pays on. The rulebook settles on the official daily figure "according to The
# Weather Company", and TWC publishes its own running maximum for the day --
# temperatureMaxSince7Am -- on the same public endpoint weather.com serves. That
# is the settled quantity, live, the night before the CLI report exists.
#
# It is NOT the station's METAR, and the gap is not a constant an offset can
# absorb. TWC minus the day's METAR peak, all seven markets, 16:40 ET on
# 2026-09-05:
#
#     New York      +2.0        Denver         -0.0
#     Chicago       +4.0        Los Angeles    -1.1
#     Miami         -1.0        Philadelphia   -0.0
#     Austin        +0.9
#
# New York that afternoon is the whole argument. METAR peaked at 77, the fitted
# offset was +0.71, and "78 or below" traded at 85c on the strength of it. TWC
# printed 79; the market repriced to 2c inside a minute. Every ladder built on
# the hourly stream was on the wrong side of a bracket edge, and no amount of
# freshness in that stream would have helped -- it was the wrong measurement.
#
# Whole degrees, like the settlement, so 79 means [78.5, 79.5) and the
# half-degree bracket convention above applies unchanged.
#
# `language` is a REQUIRED parameter. Without it the endpoint answers HTTP 400
# with every field null, which reads exactly like a station outage rather than a
# malformed request -- it cost an hour here. `icaoCode` pins the station the
# market's rules name; `geocode` snaps to a nearest-station blend and disagreed
# on Denver and Los Angeles in that same sample.
TWC_KEY = 'e1f10a1e78da46f5b10a1e78da96f525'   # the key weather.com's own site ships


# The running max is the right quantity but the WRONG FIELD ON ITS OWN.
# temperatureMaxSince7Am is what weather.com prints, and on 2026-09-05 it read
# 86 for Chicago while TWC's own observation history for the same station
# peaked at 83 and the market sat at 99% on "83 to 84". Trusting it alone put
# the floor four degrees high -- which is worse than the proxy it replaced,
# because a floor that is too HIGH deletes the bracket the day actually lands
# in. New York the same afternoon: max7 79, history 79, market 79-80. The field
# is right when it agrees with the history and unusable when it does not.
#
# So the history is the spine and max7 is corroboration. The history is TWC's
# own observation series -- hourly plus specials, the same shape as METAR, and
# therefore sampled: it can miss a peak between reports, which is exactly what
# HOURLY_PEAK_OFFSET exists for. max7 is a true running max and should sit at or
# a little above it. A little above is the intra-hour peak and is believed; far
# above is a broken aggregate and is dropped.
TWC_MAX7_TOL = 1.5     # degF above the history that an intra-hour peak can explain

# THE CLIMATE DAY, UNRESOLVED AND DELIBERATELY NOT GUESSED AT.
# climate_day_start() drops the midnight hour, because the NWS climate day runs
# 1am-12:59am local STANDARD time. Chicago on 2026-09-05 read 83 at 00:53 CT and
# the market settled that day on 83-84, which is the calendar day including it.
# One day is not enough to overturn a rule fitted on 187, so this window is used
# for the TWC floor only and climate_day_start() is left exactly as it was.
def twc_history(cfg, day):
    """TWC's own observations for `day`, local calendar day -> [degF].

    v1 rather than v3: it is the only form that returns the series instead of a
    single current reading, and the series is what makes max7 checkable.
    """
    from zoneinfo import ZoneInfo
    icao = cfg.get('icao') or ('K' + cfg['station'])
    z = ZoneInfo(cfg.get('tz', 'America/New_York'))
    # THE KNYC LOCATION KEY RETURNS LAGUARDIA. `v1/location/KNYC:9:US` answers
    # with obs_name "New York/LaGuardia", key KLGA -- re-confirmed 2026-09-06 --
    # so for the one market that matters the "TWC history" on the panel was a
    # different airport (it read 79 on 2026-09-05 only because LaGuardia and
    # the park happened to peak alike; IEM had LGA 80). The geocode form of
    # the same endpoint resolves to "New York/Central Park", key KNYC. Used
    # only where a market says so: elsewhere the ICAO form is right and the
    # geocode form can snap to a neighbour.
    if cfg.get('twc_geocode'):
        u = ('https://api.weather.com/v1/geocode/%.4f/%.4f/observations/'
             'historical.json?apiKey=%s&units=e&startDate=%s'
             % (cfg['lat'], cfg['lon'], TWC_KEY, day.strftime('%Y%m%d')))
    else:
        u = ('https://api.weather.com/v1/location/%s:9:US/observations/'
             'historical.json?apiKey=%s&units=e&startDate=%s'
             % (icao, TWC_KEY, day.strftime('%Y%m%d')))
    try:
        j = get_json(u, timeout=45)
    except Exception as e:
        print('twc history: %s unavailable (%s)' % (icao, e))
        return []
    out = []
    for o in (j or {}).get('observations') or []:
        t, v = o.get('temp'), o.get('valid_time_gmt')
        if not isinstance(t, (int, float)) or not isinstance(v, (int, float)):
            continue
        if datetime.datetime.fromtimestamp(v, datetime.timezone.utc)\
                            .astimezone(z).date() == day:
            out.append(float(t))
    return out


def twc_today(cfg, day):
    """The settlement feed's own view of today -> {'max','now','hist','max7'}.

    'max' is the figure to use: the history's peak, raised to max7 only when
    max7 is close enough to be a real intra-hour peak rather than a bad
    aggregate. Returns {} when nothing is available -- this is an additional
    floor, never a precondition, and every market must survive it missing.
    """
    icao = cfg.get('icao') or ('K' + cfg['station'])
    cur = {}
    try:
        j = get_json('https://api.weather.com/v3/wx/observations/current'
                     '?icaoCode=%s&units=e&language=en-US&format=json&apiKey=%s'
                     % (icao, TWC_KEY), timeout=45)
        if isinstance(j, dict):
            cur = j
    except Exception as e:
        print('twc: %s unavailable (%s)' % (icao, e))
    hist = twc_history(cfg, day)
    hmax = max(hist) if hist else None
    m7 = cur.get('temperatureMaxSince7Am')
    m7 = float(m7) if isinstance(m7, (int, float)) else None
    # BEFORE 7 AM THE FIELD IS YESTERDAY'S. "Since 7 AM" has not reset yet, so
    # at 00:56 on 2026-09-06 New York read max7 79 -- the previous afternoon's
    # peak -- with no history for the new day to check it against, and the
    # panel printed 79 as today's TWC reading on a day forecast to reach 75.
    # Nothing to corroborate it with means nothing to use it for.
    if m7 is not None and local_now(cfg).hour < 7:
        m7 = None
    now = cur.get('temperature')
    now = float(now) if isinstance(now, (int, float)) else None

    use = hmax
    if m7 is not None:
        if hmax is None:
            use = m7                      # nothing to check it against
        elif m7 <= hmax + TWC_MAX7_TOL:
            use = max(hmax, m7)
        else:
            print('twc: %s max7 %.0f exceeds its own history %.0f by more than '
                  '%.1f -- dropped' % (icao, m7, hmax, TWC_MAX7_TOL))
    out = {}
    if use is not None:
        out['max'] = use
    if now is not None:
        out['now'] = now
    if hmax is not None:
        out['hist'] = hmax
    if m7 is not None:
        out['max7'] = m7
    return out


# THE STATION'S OWN SIX-HOUR MAXIMUM, WHICH THE HOURLY REPORT THROWS AWAY.
#
# A METAR carries the temperature at :51 and nothing else, so a peak that falls
# between two reports is simply gone from that stream -- 2026-09-05 Central Park
# topped out at 79 at 2:33 PM and the 2:51 report already read 25.0C = 77.0F.
# But the ASOS also computes its own six-hour maximum and appends it as the
# 1sTTT remark group in the observation nearest 00/06/12/18Z. That group is the
# instrument's max over the window, intra-hour peaks included.
#
# Scored against Kalshi's own expiration_value over 322 settled market-days,
# seven cities, reconstructed from these groups alone:
#
#     exact (to the settled whole degree)   98.1%
#     MAE                                   0.12 degF
#     mean error                           -0.06 degF
#
# The mean is NEGATIVE, which is what makes this safe as a floor: it is a
# maximum over a window inside the day, so it cannot structurally exceed the
# day's maximum, and the measurement agrees.
#
# WHY BOTHER WHEN IEM DAILY IS ALSO 96-98% EXACT: because it is independent of
# IEM and lands earlier. The 17:51Z group closes 12-18Z at 1:51 PM local ET, so
# a day that peaks before 2 PM has an exact figure in hand two hours before the
# preliminary climate report. 2026-09-04 is the case: the group read 84.0 at
# 1:51 PM, the exchange settled 84.00, and IEM daily still says 83.0 today --
# settle_corrected has been fixing that day after the fact ever since. This
# catches that class of error while the market is still open.
#
# WINDOW DISCIPLINE. Each group covers the six hours ENDING at its synoptic
# hour, so a group is only usable when that whole window falls inside the local
# climate day (climate_day_start, which starts at 1am LST under DST). In ET that
# admits the 06-12Z, 12-18Z and 18-00Z groups -- 2am to 8pm local -- and drops
# both 00-06Z groups, which straddle midnight and would import the previous
# evening. The uncovered hours are night, which cannot hold a daily maximum.
# THE SETTLING PARTY'S OWN FORECAST, AND THE NWS'S -- LOGGED, NOT USED.
# Kalshi pays on The Weather Company's figure. TWC also publishes a forecast
# for the same station on the same endpoint family, and on 2026-09-06 at
# 3 AM it said 73 for New York while this model's consensus said 75.2 and
# the market leaned 73-74. Whether TWC's forecast is a better predictor of
# TWC's observation than the raw models is exactly the kind of thing that
# cannot be backtested (no archive of either), so both go on the hourly
# trail from today and get judged forward. Nothing here feeds the pick.
NWS_GRID = {}


def twc_forecast(cfg, day):
    """TWC's forecast max for `day` at the market's station -> degF, or None."""
    icao = cfg.get('icao') or ('K' + cfg['station'])
    try:
        j = get_json('https://api.weather.com/v3/wx/forecast/daily/3day?icaoCode=%s'
                     '&units=e&language=en-US&format=json&apiKey=%s' % (icao, TWC_KEY),
                     timeout=30)
    except Exception as e:
        print('twc forecast: %s unavailable (%s)' % (icao, e))
        return None
    times = j.get('validTimeLocal') or []
    for i, t in enumerate(times):
        if str(t)[:10] != day.isoformat():
            continue
        # the day part goes null once TWC considers the daytime over; the
        # calendar-day figure stays
        for fld in ('temperatureMax', 'calendarDayTemperatureMax'):
            col = j.get(fld) or []
            if i < len(col) and isinstance(col[i], (int, float)):
                return float(col[i])
    return None


def nws_forecast(cfg, day):
    """The NWS gridpoint forecast max for `day` -> degF, or None."""
    from zoneinfo import ZoneInfo
    try:
        url = NWS_GRID.get(cfg['key'])
        if not url:
            j = get_json('https://api.weather.gov/points/%.4f,%.4f' % (cfg['lat'], cfg['lon']),
                         timeout=30)
            url = NWS_GRID[cfg['key']] = j['properties']['forecastGridData']
        j = get_json(url, timeout=45)
        z = ZoneInfo(cfg.get('tz', 'America/New_York'))
        best = None
        for v in j['properties']['maxTemperature']['values']:
            st, dur = v['validTime'].split('/')
            t = datetime.datetime.fromisoformat(st.replace('Z', '+00:00')).astimezone(z)
            if t.date() != day or v.get('value') is None:
                continue
            m = re.match(r'P(?:(\d+)D)?T?(?:(\d+)H)?', dur)
            hours = (int(m.group(1) or 0) * 24 + int(m.group(2) or 0)) if m else 0
            if best is None or hours > best[0]:
                best = (hours, float(v['value']))
        return round(best[1] * 9.0 / 5.0 + 32.0, 1) if best else None
    except Exception as e:
        print('nws forecast: %s unavailable (%s)' % (cfg['key'], e))
        return None


# THE SETTLEMENT, READ WHERE KALSHI READS IT. weather.com/kalshi is the page
# the rules name, and the page calls a plain JSON route for every station it
# lists (found 2026-09-06 in its own bundle; no key, any user agent):
#
#     /kalshi/api/climate/primary?date=YYYY-MM-DD
#
# One response carries all 37 US stations, each with a status -- no_report,
# preliminary, official -- and the report's max. The official value equalled
# Kalshi's expiration_value on 15 of 15 New York days checked. It is what the
# exchange settles on, hours before the exchange posts the settlement, so it
# scores a day at ~3 AM instead of ~3 PM, and it is the arbiter the panel
# should be showing. One fetch per date per run serves every market.
PORTAL_URL = 'https://weather.com/kalshi/api/climate/primary?date=%s'
_PORTAL = {}
_PORTAL_LOCK = threading.Lock()


def portal_day(cfg, day):
    """The portal's row for this market's station and `day`
    -> {'status', 'max', 'official'} or {} when it has nothing."""
    key = day.isoformat()
    with _PORTAL_LOCK:
        j = _PORTAL.get(key)
        if j is None:
            try:
                j = get_json(PORTAL_URL % key, timeout=30)
            except Exception as e:
                print('portal: %s unavailable (%s)' % (key, e))
                j = {}
            _PORTAL[key] = j
    icao = cfg.get('icao') or ('K' + cfg['station'])
    for r in j.get('results') or []:
        if (r.get('station') or {}).get('icao') == icao:
            d = r.get('data') or {}
            v = d.get('maxTemp')
            return {'status': r.get('status'), 'max': float(v) if isinstance(v, (int, float)) else None,
                    'official': bool(d.get('isOfficial'))}
    return {}


SIX_WINDOW = {}          # per market: [start hour, end hour] of the group that holds six_max

# THE SETTLEMENT SENSOR'S OWN 5-MINUTE FEED. Central Park has none in public,
# but Las Vegas settles on Harry Reid (KLAS) and Austin on Bergstrom (KAUS):
# FAA airports whose 5-minute observations are on Synoptic, logged by the
# cron worker every five minutes into its public summary. The running maximum
# of those readings since 7 AM is an exact floor on the day's high -- the same
# sensor the climate report is computed from, sampled every five minutes
# instead of every hour (2026-09-06).
OWN5 = {'las_high': 'KLAS', 'aus_high': 'KAUS'}
LEAD_URL = 'https://bluish-void-kalshi-cron.junkyjunkjunkjunkjunk.workers.dev/obs/lead'
_LEAD = []


# PEAK BEHIND US, measured (peak_stats.py, from the one-minute archive): per
# city and local hour, how often the day's high had already happened, plain
# and given the reading had fallen a degree off the running max. Plus the
# mean gap between the true one-minute peak and the best 5-minute sample,
# which is the allowance the settlement sensor's own feed still needs.
_PEAK = []


def peak_stats(cfg):
    if not _PEAK:
        try:
            _PEAK.append(json.load(open(os.path.join(HERE, 'peak_stats.json'))))
        except Exception:
            _PEAK.append(None)
    d = _PEAK[0] or {}
    return (d.get('cities') or {}).get(cfg['key']) or {}


def own5_row(cfg, day):
    """{max7, last, at} from the worker's summary for the settlement sensor."""
    st = OWN5.get(cfg['key'])
    if not st:
        return None
    if not _LEAD:
        try:
            _LEAD.append(get_json(LEAD_URL, timeout=20))
        except Exception as e:
            print('own5: worker summary unavailable (%s)' % e)
            _LEAD.append(None)
    d = _LEAD[0] or {}
    row = (d.get('days') or {}).get('%s|%s' % (cfg['key'], day.isoformat())) or {}
    a = ((row.get('apt5') or {}).get('s') or {}).get(st) or {}
    return a if isinstance(a.get('max7'), (int, float)) else None


def apt_max(cfg, day):
    """The highest 'since 7 AM' reading across the market's 5-minute stations
    that are NOT the settlement sensor (New York's three airports)."""
    if not _LEAD:
        try:
            _LEAD.append(get_json(LEAD_URL, timeout=20))
        except Exception as e:
            print('apt: worker summary unavailable (%s)' % e)
            _LEAD.append(None)
    d = _LEAD[0] or {}
    row = (d.get('days') or {}).get('%s|%s' % (cfg['key'], day.isoformat())) or {}
    best = None
    for st, a in (((row.get('apt5') or {}).get('s') or {}).items()):
        if st == OWN5.get(cfg['key']):
            continue
        v = (a or {}).get('max7')
        if isinstance(v, (int, float)) and (best is None or v > best):
            best = float(v)
    return best


def own5_max(cfg, day):
    st = OWN5.get(cfg['key'])
    if not st:
        return None
    if not _LEAD:
        try:
            _LEAD.append(get_json(LEAD_URL, timeout=20))
        except Exception as e:
            print('own5: worker summary unavailable (%s)' % e)
            _LEAD.append(None)
    d = _LEAD[0] or {}
    row = (d.get('days') or {}).get('%s|%s' % (cfg['key'], day.isoformat())) or {}
    a = ((row.get('apt5') or {}).get('s') or {}).get(st) or {}
    v = a.get('max7')
    return float(v) if isinstance(v, (int, float)) else None


def metar_six_max(cfg, day):
    """The day's max from the ASOS six-hourly groups, or None.

    Own fetch rather than sharing metar_today()'s: that one asks for 12 hours,
    and the 18-00Z group lands in the 23:51Z report, which is outside that
    window for most of the runs in a day.
    """
    from zoneinfo import ZoneInfo
    icao = cfg.get('icao') or ('K' + cfg['station'])
    try:
        j = get_json('https://aviationweather.gov/api/data/metar?ids=%s&format=json&hours=36'
                     % icao, timeout=45)
    except Exception as e:
        print('six-hourly: %s unavailable (%s)' % (icao, e))
        return None
    z = ZoneInfo(cfg.get('tz', 'America/New_York'))
    h0 = climate_day_start(cfg, day)
    lo = datetime.datetime(day.year, day.month, day.day, h0, tzinfo=z)
    hi = (datetime.datetime(day.year, day.month, day.day, 23, 59, tzinfo=z)
          + datetime.timedelta(minutes=1) + (datetime.timedelta(hours=1) if h0 == 1
                                             else datetime.timedelta(0)))
    best = None
    for m in (j or []):
        raw = str(m.get('rawOb') or '')
        g = re.search(r'\b1([01])(\d{3})\b', raw)
        if not g:
            continue
        stamp = next((t for t in raw.split()
                      if len(t) == 7 and t.endswith('Z') and t[:6].isdigit()), None)
        if not stamp:
            continue
        try:
            rep = datetime.datetime(day.year, day.month, int(stamp[0:2]),
                                    int(stamp[2:4]), int(stamp[4:6]),
                                    tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        # the group is stamped on the :51 report; its window ends at the hour
        end = rep.replace(minute=0, second=0) + (datetime.timedelta(hours=1)
                                                 if rep.minute > 30 else datetime.timedelta(0))
        st = end - datetime.timedelta(hours=6)
        if not (st.astimezone(z) >= lo and end.astimezone(z) <= hi):
            continue
        c = (int(g.group(2)) / 10.0) * (-1 if g.group(1) == '1' else 1)
        f = round(c * 9.0 / 5.0 + 32.0, 2)   # exact; see the hourly reader
        if best is None or f > best:
            best = f
            # the window that carried it, in local hours, so the panel can say
            # "2-8 AM" against "8 AM-2 PM" -- the second is the one that matters
            SIX_WINDOW[cfg['key']] = [st.astimezone(z).hour, end.astimezone(z).hour]
    return best


# THE INSTRUMENT THE MARKET ACTUALLY SETTLES ON.
#
# The rules say "according to The Weather Company", and that names the PUBLISHER,
# not a different number. Scored against Kalshi's own expiration_value -- the
# settled temperature, which the exchange publishes -- over 22 days x 2 cities:
#
#     NWS CLI, FINAL      44/44        <-- governs
#     NWS CLI, prelim     37/44
#     IEM daily           41/44
#
# The aggregate is not the proof; the disagreements are. Every day the candidates
# diverged, settlement followed the CLI final:
#
#     New York 2026-09-03   settled 84   CLI final 84   CLI prelim 83   IEM 83.0
#     New York 2026-09-04   settled 84   CLI final 84                   IEM 83.0
#     Chicago  2026-09-04   settled 93   CLI final 93                   IEM 92.0
#
# So TWC's blended fields are irrelevant here. They govern Kalshi's HOURLY
# temperature markets, which are a different product; this reads the climate
# report, which is free, keyless, and the actual settlement instrument.
#
# PUBLICATION: a preliminary run in the afternoon (~4:43 PM ET for New York,
# "VALID TODAY AS OF 0400 PM"), sometimes a morning run as well, and the FINAL
# for the day at roughly 2:30 AM local the next morning.
#
# THE PRELIMINARY IS NOT THE ANSWER, and how wrong it is depends on the city.
# Its window closes at 4 PM local and New York frequently peaks later: 15/22
# there, against 22/22 for Chicago, which peaks earlier in its day. 2026-09-03 is
# the shape of it -- prelim 83 at 2:59 PM, final 84 at 4:58 PM, settled 84. So a
# preliminary is used ONLY as a floor for today, never to score a past day.
CLI_CACHE = os.path.join(HERE, 'cli_cache.json')
# Markets now run in parallel and each one rewrites this file. Without a lock,
# two cities finishing together would each write the copy they loaded and one
# station's reports would vanish until the next run refetched them. The save
# re-reads the file under the lock and merges only its own station in.
CLI_LOCK = threading.Lock()


def _cli_parse(text):
    """One CLI product -> {'day','max','final'} or None.

    THREE THINGS THAT BREAK A NAIVE PARSER, all found live before this shipped:
      * "MM" means MISSING, not a number. Miami's 4pm run on 2026-09-05 carried
        `MAXIMUM MM MM` -- reading that as present would poison the record.
      * FINAL is the ABSENCE of a valid-as-of line, not the presence of some
        phrase. Denver says "VALID AS OF 0600 AM LOCAL TIME" where New York says
        "VALID TODAY AS OF 0400 PM" -- matching on "VALID TODAY" called Denver's
        6:30am partial a final and would have recorded an overnight 73 as the
        day's high against an actual 92.
      * offices issue two or three products a day, so a version index is not a
        day index.
    """
    d = re.search(r'SUMMARY FOR\s+(\w+)\s+(\d+)\s+(\d{4})', text)
    if not d:
        return None
    try:
        day = datetime.datetime.strptime(
            '%s %s %s' % (d.group(1)[:3], d.group(2), d.group(3)), '%b %d %Y').date()
    except ValueError:
        return None
    # the first MAXIMUM after the temperature block, so PRECIPITATION's own
    # columns and the record/normal values cannot be mistaken for it
    seg = text.split('TEMPERATURE', 1)
    if len(seg) < 2:
        return None
    m = re.search(r'MAXIMUM\s+(MM|-?\d+)\s+(\d{1,2}:?\d{2}\s*(?:AM|PM))?', seg[1])
    if not m:
        return None
    val = None if m.group(1) == 'MM' else float(m.group(1))
    at = (m.group(2) or '').replace(' ', '')
    if at and ':' not in at:                    # "233PM" -> "2:33PM"
        at = at[:-4].lstrip('0') + ':' + at[-4:-2] + at[-2:]
    # the WMO header's DDHHMM decides WHICH final came first, and that is the
    # whole ballgame -- see cli_read()
    w = re.search(r'CDUS\d+\s+\w+\s+(\d{6})', text)
    return {'day': day.isoformat(), 'max': val, 'issued': w.group(1) if w else None,
            'at': at or None,
            'final': not re.search(r'VALID.{0,12}AS OF', text)}


def cli_read(cfg, deep=False):
    """{'YYYY-MM-DD': {'max': degF, 'final': bool}} for this market's station.

    Cached on disk and topped up a few versions at a time: the products are
    immutable once issued, and refetching forty of them per city per run would
    add minutes to a job that runs every quarter hour. `deep` backfills.
    """
    wfo = cfg.get('wfo')
    if not wfo:
        return {}
    try:
        with open(CLI_CACHE) as fh:
            cache = json.load(fh)
        if not isinstance(cache, dict):
            cache = {}
    except Exception:
        cache = {}
    mine = cache.setdefault(cfg['cli'], {})
    # a station with little history gets the deep pass once; after that the two
    # or three newest products are all that can have appeared since last run
    n = 40 if (deep or len(mine) < 10) else 4
    fresh = 0
    for v in range(1, n + 1):
        try:
            h = get('https://forecast.weather.gov/product.php?site=%s&issuedby=%s'
                    '&product=CLI&format=CI&version=%d&glossary=0'
                    % (wfo, cfg['cli'][3:], v), timeout=45).decode('utf-8', 'replace')
        except Exception:
            break
        m = re.search(r'<pre[^>]*>(.*?)</pre>', h, re.S)
        if not m:
            break
        p = _cli_parse(re.sub(r'<[^>]*>', '', m.group(1)))
        if not p or p['max'] is None:
            continue
        old = mine.get(p['day'])
        # KEEP THE *FIRST* NON-PRELIMINARY REPORT, NOT THE NEWEST ONE.
        #
        # Kalshi settles on "the first official non-preliminary report" and
        # explicitly ignores later revisions. The NWS does revise, sometimes
        # within the hour, and when it does the exchange does NOT follow.
        #
        # Miami 2026-08-29 is the case and it cost a wrong conclusion before it
        # was found. Two finals for that day:
        #
        #     04:24 AM Aug 30   MAXIMUM 90 at 3:25 PM   <-- the exchange paid 90
        #     05:10 AM Aug 30   MAXIMUM 85 at 5:11 AM   <-- corrected, 46 min later
        #
        # Every instrument agrees the true value was 85 -- the corrected report
        # even moves the time of the max from an afternoon 3:25 PM to an
        # early-morning 5:11 AM, which is what the METARs show on a day with
        # 0.62in of rain. It does not matter. The rulebook settles on the first
        # one, so THAT is the quantity this model has to predict, and a record
        # holding 85 would be scoring against a number the exchange never used.
        #
        # Versions come back newest-first, so an older product is seen later;
        # the issuance stamp is compared rather than trusting iteration order,
        # because an incremental run only fetches the newest few.
        better = old is None
        if not better and p['final'] and not old.get('final'):
            better = True                       # any final beats a preliminary
        elif not better and p['final'] and old.get('final'):
            oi, ni = old.get('issued'), p.get('issued')
            better = bool(oi and ni and ni < oi)  # an EARLIER final wins
        if better:
            mine[p['day']] = {'max': p['max'], 'final': p['final'],
                              'issued': p.get('issued'), 'at': p.get('at')}
            fresh += 1
    if fresh:
        try:
            with CLI_LOCK:
                try:
                    with open(CLI_CACHE) as fh:
                        disk = json.load(fh)
                    if not isinstance(disk, dict):
                        disk = {}
                except Exception:
                    disk = {}
                disk[cfg['cli']] = mine
                with open(CLI_CACHE, 'w') as fh:
                    json.dump(disk, fh, indent=0, sort_keys=True)
        except Exception as e:
            print('cli cache not written (%s)' % e)
    return mine


def obs_hourly_range(cfg, start, end, sink=None):
    """Hourly obs -> {'YYYY-MM-DD': {hour: degF}}, for historic running maxima.

    Each hour holds that hour's MAXIMUM, which is what a running peak needs. The
    latest actual reading is a different thing -- it is the temperature right
    now, which can sit a degree under the hour it belongs to -- so a caller can
    pass `sink` to also receive the final row as (timestamp, degF)."""
    u = ('https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=%s'
         '&data=tmpf&year1=%d&month1=%d&day1=%d&year2=%d&month2=%d&day2=%d'
         '&tz=%s&format=onlycomma&missing=empty&trace=empty'
         % (cfg['station'], start.year, start.month, start.day,
            end.year, end.month, end.day,
            urllib.parse.quote(cfg.get('tz', 'America/New_York'))))
    out = collections.defaultdict(dict)
    last = None
    for r in csv.DictReader(io.StringIO(get(u, timeout=180).decode())):
        if r.get('tmpf'):
            d, hh = r['valid'][:10], int(r['valid'][11:13])
            out[d][hh] = max(out[d].get(hh, -99.0), float(r['tmpf']))
            last = (r['valid'], float(r['tmpf']))
    if sink is not None and last:
        sink.append(last)
    return out


# ------------------------------------------------------------- forecast ----
def forecast_runs(cfg, past_days, model=None, today=None):
    """Hourly forecast per day -> {'YYYY-MM-DD': {hour: degF}}.

    USE THE FRESHEST RUN AVAILABLE.  This used to read `temperature_2m_previous_day1`
    -- the run issued ~24 h earlier -- purely because that was the product with a
    long archive to calibrate against. That was a full day of model lead thrown
    away, and it was the single largest thing holding the odds down. Measured on
    600 matched days, same pipeline, only the product swapped:

        hour    day-ahead            same-day run
        08:00   43%  MAE 1.52        56%  MAE 1.00
        12:00   50%  MAE 1.28        58%  MAE 0.91     <- the noon lock
        16:00   51%  MAE 0.90        55%  MAE 0.73

    History comes from the historical-forecast archive and today from the live
    forecast API; the two were verified to return identical values for the same
    day, and they carry real forecast error (86.9 against an actual 84 on
    2026-09-04), so this is a forecast and not hindsight.

    ONE REQUEST, ALL MODELS.  `&models=a,b,c` returns a column per model
    (`temperature_2m_gfs_seamless` and so on), so six models cost one call
    rather than six. This mattered: the job ran in ~70s on a laptop and timed
    out at 15 minutes on a GitHub runner doing identical work, because 13
    Open-Meteo calls per market times seven markets is 91 requests from a
    datacentre IP. Batched it is three.

    Pass `models` as a list to get {model: {date: {hour: degF}}}; the old
    single-model form still works and returns just {date: {hour: degF}}.
    """
    many = isinstance(model, (list, tuple))
    mods = list(model) if many else ([model] if model else [])
    tz = urllib.parse.quote(cfg.get('tz', 'America/New_York'))
    mq = ('&models=' + ','.join(mods)) if mods else ''
    out = {m: collections.defaultdict(dict) for m in mods} if many \
        else collections.defaultdict(dict)

    def absorb(h):
        for m in (mods if many else [None]):
            # a single-model request labels the column plainly; a multi-model
            # one suffixes it with the model name
            col = h.get('temperature_2m_' + m) if (many and m) else h.get('temperature_2m')
            if col is None:
                continue
            tgt = out[m] if many else out
            for t, v in zip(h['time'], col):
                if v is not None:
                    tgt[t[:10]][int(t[11:13])] = v

    if today is None:
        today = local_now(cfg).date()
    start = today - datetime.timedelta(days=past_days)
    u = ('https://historical-forecast-api.open-meteo.com/v1/forecast'
         '?latitude=%.4f&longitude=%.4f&start_date=%s&end_date=%s&hourly=temperature_2m'
         '&temperature_unit=fahrenheit&timezone=%s'
         % (cfg['lat'], cfg['lon'], start.isoformat(),
            (today - datetime.timedelta(days=1)).isoformat(), tz) + mq)
    # 40 s, not 180: on the runner these sockets hang and then succeed at once on
    # retry (13:35Z run: 183 s and 215 s on three cities). The timeout is the cost.
    absorb(get_json(u, timeout=40)['hourly'])
    # today comes from the live run, which is fresher than anything archived
    # two days: today drives the live call, tomorrow drives the plan
    u2 = ('https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f'
          '&hourly=temperature_2m&forecast_days=2&temperature_unit=fahrenheit'
          '&timezone=%s' % (cfg['lat'], cfg['lon'], tz) + mq)
    try:
        absorb(get_json(u2, timeout=40)['hourly'])
    except Exception as e:
        print('live run unavailable (%s); today falls back to archive' % e)
    return out


def fresh_runs(cfg, hour):
    """The DAY-AHEAD runs for today -> {model: peak degF over remaining hours}.

    The roles are now the other way round. The forecast runs on the freshest
    available run (see forecast_runs); this records what the day-old run said,
    so the comparison keeps being measured on live data rather than resting on
    the backtest. The archive says the switch is worth ~21 points of bracket
    accuracy, which is a big enough claim to keep checking.
    """
    u = ('https://previous-runs-api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f'
         '&hourly=temperature_2m_previous_day1&past_days=1&forecast_days=1'
         '&temperature_unit=fahrenheit&timezone=%s&models=%s'
         % (cfg['lat'], cfg['lon'],
            urllib.parse.quote(cfg.get('tz', 'America/New_York')), ','.join(models_for(cfg))))
    # 30 s, not 90. On the runner this call hangs outright and then succeeds
    # at once on retry: the 2026-09-06 06:58Z log shows "open-meteo.com 93s/3
    # +1retry" on nine of twenty cities and 186s on two -- every one of them
    # exactly the 90 s timeout plus the backoff, never a slow answer. A hung
    # socket costs whatever the timeout is, so the timeout is the cost.
    h = get_json(u, timeout=30)['hourly']
    today = local_now(cfg).date().isoformat()
    out = {}
    for m in models_for(cfg):
        key = 'temperature_2m_previous_day1_' + m
        col = h.get(key) or h.get('temperature_2m_previous_day1') or []
        v = [x for i, x in enumerate(col)
             if x is not None and h['time'][i][:10] == today
             and int(h['time'][i][11:13]) >= hour]
        if v:
            out[m] = round(max(v), 2)
    return out


# SKILL WEIGHTS, MEASURED 2026-09-06 ON 1,330 SETTLED CITY-DAYS (20 cities).
# The equal-weight consensus was kept after a 68-day NYC test called
# inverse-MAE weighting a wash. Twenty cities say otherwise, and at every hour:
#
#     local hour            6h     9h    12h    14h    16h    18h
#     equal weights, hit%  59.5   59.6   63.8   69.3   72.8   79.2
#     1/MAE^2 weights      61.8   61.9   66.1   72.0   74.5   79.4
#     MAE  equal / skill   0.99/0.90     0.91/0.84   0.65/0.61
#     Brier equal / skill  .529/.491     .493/.458   .395/.382
#
# Walk-forward by construction (the window is the same trailing one the bias
# uses), and the same gain shows with power 1, 2 or 4 and windows of 21-45
# days, so it is not a knife-edge. Why it works: in the Open-Meteo archive
# NBM and ECMWF are poor at station level (alone: 40% and 44% at 9h, against
# GFS's 62%), and an equal mean lets them pull. The weights find that per
# city, per month, without naming a model. Also found on the way: the
# archive's ncep_hrrr_conus column is GFS (identical on 2,240/2,240
# city-days), so "six models" was five with GFS counted twice -- which is why
# dropping HRRR *loses* 2 points. Left in; the weights make it harmless.
SKILL_POWER = 2
SKILL_MAE_FLOOR = 0.3         # degF; below this a model's weight stops growing


# HOW LONG THE BIAS REMEMBERS. A flat 30-day mean was fitted on 67 summer
# days. On 539 New York days from January 2025 (hourly obs, floor included,
# walk-forward) a shorter memory wins at every hour and in every season:
#
#     9 AM, 539 days     MAE    within 1F   Brier
#     flat 30 days       0.929    64.2%     .559
#     flat 21 days       0.920    67.7%     .555
#     flat 10 days       0.903    65.1%     .552
#     EWMA half-life 7   0.906    67.2%     .551   (spring +4, summer +4.4,
#                                                   autumn +1, winter level)
#     EWMA half-life 4..10 all within noise of 7; 14 is back to the flat 30.
#
# The model's error drifts with the regime on a scale of a week or two, and a
# month-long mean averages across regimes. An exponential window keeps the
# whole 90 days for stability but lets the last fortnight dominate.
BIAS_SPAN = 90                # days an EWMA bias looks back


def biases_factory(fcm, daily, skill=True, half_life=None):
    """-> f(prior_days) giving each model's bias (peak - actual) over them,
    plus its skill weight under '__w__' (see SKILL_POWER above). With
    skill=False every weight is 1 -- the plain mean, which New York keeps.
    With half_life set, the bias is an exponentially weighted mean over the
    last BIAS_SPAN days ending at the last prior day, instead of a flat mean
    over `prior`."""
    any_fc = fcm[sorted(fcm)[0]] if fcm else {}
    all_keys = sorted(k for k in any_fc if k in daily and len(any_fc[k]) >= 20)

    def f(prior):
        out, w = {}, {}
        if half_life and prior:
            last = max(prior)
            span = [k for k in all_keys if k <= last][-BIAS_SPAN:]
            lam = 0.5 ** (1.0 / half_life)
            for m, fc in fcm.items():
                pts = [(max(fc[k].values()) - daily[k], lam ** j)
                       for j, k in enumerate(reversed(span))
                       if k in fc and len(fc[k]) >= 20]
                if len(pts) < BIAS_MIN:
                    out[m] = None
                    continue
                ws = sum(wt for _, wt in pts)
                out[m] = sum(e * wt for e, wt in pts) / ws
                if skill:
                    mae = sum(abs(e) * wt for e, wt in pts) / ws
                    w[m] = 1.0 / max(mae, SKILL_MAE_FLOOR) ** SKILL_POWER
            out['__w__'] = w
            return out
        for m, fc in fcm.items():
            e = [max(fc[p].values()) - daily[p]
                 for p in prior if p in fc and p in daily and len(fc[p]) >= 20]
            out[m] = statistics.mean(e) if len(e) >= BIAS_MIN else None
            if out[m] is not None and skill:
                mae = statistics.mean(abs(x) for x in e)
                w[m] = 1.0 / max(mae, SKILL_MAE_FLOOR) ** SKILL_POWER
        out['__w__'] = w
        return out
    return f


def rolling_bias(fc, daily, today_key):
    """mean(forecast peak - actual) over prior scored days. None if too few."""
    keys = sorted(k for k in fc if k < today_key)[-BIAS_K:]
    errs = []
    for k in keys:
        if len(fc[k]) < 20:
            continue
        a = daily.get(k)
        if a is None:
            continue
        errs.append(max(fc[k].values()) - a)
    if len(errs) < BIAS_MIN:
        return None, len(errs)
    return statistics.mean(errs), len(errs)


def point_forecast(fcm, biases, key, hour, yday):
    """Consensus of the models' bias-corrected peaks, with warm-ups damped.

    The single strongest usable predictor of a bust is how big a day-to-day
    RISE the run is calling for: on days forecast to climb more than 4 degF
    above yesterday the error runs MAE 2.68 / bias +1.43, against 1.26-1.96
    elsewhere (r = +0.22 with |error|, beating cloud, rain and wind at
    0.05-0.18). The model overdoes warm advection, so a quarter of the
    forecast rise is taken back. Damping 0.15-0.40 all help, so this is not a
    knife-edge: at 0.25, MAE 1.83 -> 1.66, bias +0.78 -> +0.20, brackets
    37/68 -> 39/68, Brier 0.591 -> 0.559.
    """
    vals, ws = [], []
    wt = biases.get('__w__') or {}
    for m, fc in fcm.items():
        day = fc.get(key)
        if not day or biases.get(m) is None:
            continue
        rest = [v for h, v in day.items() if h >= hour]
        if rest:
            vals.append(max(rest) - biases[m])
            ws.append(wt.get(m, 1.0))
    if not vals:
        return None
    # skill-weighted, not equal: see biases_factory
    p = sum(v * w for v, w in zip(vals, ws)) / sum(ws)
    if yday is not None:
        p -= SWING_DAMP * max(0.0, p - yday)
    return p


def residuals(fcm, biases_of, daily, obh, hour, today_key, h0_of):
    """Replay the model on past days at `hour` -> [(date, pred - actual)].

    This is what the spread is measured from, so it is recomputed every run and
    tracks the season on its own.  Historic floors come from the hourly stream
    plus HOURLY_PEAK_OFFSET, since that stream reads low against the daily max
    the model is actually scored on.
    """
    any_fc = fcm[sorted(fcm)[0]]
    keys = sorted(k for k in any_fc if k < today_key and k in daily
                  and len(any_fc[k]) >= 20 and len(obh.get(k, {})) >= 18)
    out = []
    for i, k in enumerate(keys):
        prior = keys[max(0, i - BIAS_K):i]
        if len(prior) < BIAS_MIN:
            continue
        b = biases_of(prior)
        yk = (datetime.date(*map(int, k.split('-')))
              - datetime.timedelta(days=1)).isoformat()
        p = point_forecast(fcm, b, k, hour, daily.get(yk))
        if p is None:
            continue
        run = running_max(obh, k, hour, h0_of(k))
        floor = run + HOURLY_PEAK_OFFSET if run is not None else -99.0
        # record WHICH regime the day was in: once the observed high exceeds the
        # forecast, the only question left is how much further it can climb, and
        # that is a far tighter distribution than a day still being forecast
        out.append((k, max(floor, p) - daily[k], floor >= p))
    return out


def spread(res, hour, binding=None):
    """Spread of the recent residuals, conditioned on the regime.

    Measured over 174 days, the two regimes are nothing alike: with the floor
    binding the spread runs ~0.75 degF at any afternoon hour, without it 1.5-2.5.
    Blending them overstates the uncertainty on exactly the days the answer is
    already known -- which invents value in brackets the day has walked past --
    and understates it on the days still genuinely open.
    """
    pool = res
    if binding is not None:
        same = [r for r in res if len(r) > 2 and r[2] == binding]
        if len(same) >= 20:
            pool = same
    v = [r[1] for r in pool[-RESID_M:]]
    if len(v) < 20:
        return SD_FALLBACK.get(hour, 3.06 if hour < 8 else 0.89) * SD_MULT, len(v)
    return max(statistics.stdev(v) * SD_MULT, SD_FLOOR), len(v)


# ---------------------------------------------------------- probability ----
def _phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def distribution(rows, pred, sd, obs_floor):
    """P(reported integer high lands in each bracket).

    Brackets are integer-valued, so a bracket [lo, hi] covers the real
    interval [lo-0.5, hi+0.5).  Mass below the observed-so-far high is
    impossible; truncate there and renormalise.
    """
    sd = max(sd, 0.15)
    cut = (obs_floor - 0.5) if obs_floor is not None else None
    base = 1.0 - _phi((cut - pred) / sd) if cut is not None else 1.0
    if base < 1e-6:                     # obs already past everything the model knew
        base, cut = 1.0, None
    ps = []
    for r in rows:
        lo = (r['lo'] - 0.5) if r['lo'] is not None else -1e9
        hi = (r['hi'] + 0.5) if r['hi'] is not None else 1e9
        if cut is not None:
            lo, hi = max(lo, cut), max(hi, cut)
        ps.append(max(0.0, (_phi((hi - pred) / sd) - _phi((lo - pred) / sd)) / base))
    s = sum(ps) or 1.0
    return [p / s for p in ps]


# --------------------------------------------------------------- scoring ----
def which(rows, t):
    """Index of the bracket a reported temperature falls in."""
    r = math.floor(t + 0.5)
    for i, b in enumerate(rows):
        if (b['lo'] is None or r >= b['lo']) and (b['hi'] is None or r <= b['hi']):
            return i
    return None


def load_log(out):
    try:
        with open(out) as f:
            return json.load(f)
    except Exception:
        return {'history': []}


def fetch_settled(cfg, limit=400):
    """Past events -> their own bracket ladder plus which bracket settled yes.

    The ladder is re-centred by Kalshi every day (Sep 3 ran 83-84/85-86/87-88,
    Sep 4 ran 84-85/86-87/88-89), so a past day must be scored against the
    ladder it actually traded, never today's.
    """
    d = get_json('https://api.elections.kalshi.com/trade-api/v2/markets'
                 '?series_ticker=%s&status=settled&limit=%d' % (cfg['series'], limit))
    ev = collections.defaultdict(list)
    # THE EXCHANGE PUBLISHES THE FIGURE ITSELF, not merely which rung paid.
    # expiration_value carries the settled temperature to two decimals and is
    # present on every settled market checked -- 469 of 469 across the seven
    # cities, agreeing with the winning bracket on 466. It is stamped onto every
    # row of the event so settle_corrected() can restore the exact number rather
    # than nudging to a bracket edge, and so any future caller has it without a
    # second request.
    val = {}
    for m in d.get('markets', []):
        v = m.get('expiration_value')
        if v not in (None, ''):
            try:
                val[m['event_ticker']] = float(v)
            except ValueError:
                pass
    for m in d.get('markets', []):
        f, c, st = m.get('floor_strike'), m.get('cap_strike'), m.get('strike_type')
        if st == 'between':
            lo, hi = float(f), float(c)
        elif st in ('less', 'less_or_equal'):
            hi = float(c if c is not None else f)
            lo, hi = None, hi - (1.0 if st == 'less' else 0.0)
        else:
            lo = float(f)
            lo, hi = lo + (1.0 if st == 'greater' else 0.0), None
        ev[m['event_ticker']].append({'label': m.get('yes_sub_title') or '',
                                      'lo': lo, 'hi': hi,
                                      'yes': m.get('result') == 'yes',
                                      'value': val.get(m['event_ticker'])})
    for k in ev:
        ev[k].sort(key=lambda r: (r['lo'] if r['lo'] is not None else -999))
    return ev


def settle_corrected(daily, settled):
    """`daily` with every settled day forced to agree with the exchange.

    WHY THIS EXISTS.  Kalshi resolves on "the first official non-preliminary
    report" and explicitly ignores later revisions. IEM does not: its daily
    max_temp_f is revised after the fact. Measured 2026-09-05 across 466 settled
    markets, the two disagree on five -- and IEM is LOW on all five, because it
    has since corrected values downward:

        New York    2026-09-04   IEM now 83.0   exchange settled 84-85
        Chicago     2026-09-04   IEM now 92.0   exchange settled 93-94
        Miami       2026-09-04   IEM now 90.0   exchange settled 91-92
        Miami       2026-08-29   IEM now 85.0   exchange settled 90-91
        Los Angeles 2026-09-03   IEM now 77.0   exchange settled 78-79

    New York is the proof: the record STORED 84.0 that day, the exchange settled
    84-85, and IEM today says 83.0. It agreed at the time and drifted afterwards.

    So a re-fetch scores old days against numbers the exchange never used, and
    every fitted quantity that takes its actuals from `daily` -- the rolling
    bias, the peak offset, the residual spread -- inherits that drift.

    IT USED TO SAY a settled bracket is not a temperature, and nudged the value
    to the nearest edge of the bracket that paid. The exchange publishes the
    temperature: expiration_value, present on 469 of 469 settled markets across
    the seven cities. So the exact figure is restored where it exists, and the
    bracket-edge nudge survives only as the fallback for a day that somehow
    carries a result without a value.

    This matters beyond tidiness. Scored against those exact figures over 59 New
    York days, IEM's Central Park daily max is the settlement to 0.03degF MAE --
    97% exactly right, 100% within a degree. Nudging to a bracket edge threw
    away most of that precision on the days it fired; every fitted quantity that
    takes its actuals from `daily` now gets the number the exchange used.
    """
    if not settled:
        return daily, 0
    out = dict(daily)
    fixed = 0
    for evk, lad in settled.items():
        try:
            k = datetime.datetime.strptime(evk.split('-')[1], '%y%b%d').date().isoformat()
        except Exception:
            continue
        a = out.get(k)
        if a is None:
            continue
        win = next((r for r in lad if r.get('yes')), None)
        if not win:
            continue
        exact = win.get('value')
        if exact is not None:
            if abs(a - exact) < 1e-9:
                continue
            out[k] = exact
            how = 'exchange value'
        else:
            lo, hi = win.get('lo'), win.get('hi')
            if lo is not None and a < lo:
                out[k] = lo
            elif hi is not None and a > hi:
                out[k] = hi
            else:
                continue
            how = 'nearest edge of %s' % (win.get('label') or 'the settled rung')
        fixed += 1
        print('  settled override %s: IEM %.1f -> %.1f (%s)'
              % (k, a, out[k], how))
    return out, fixed


def backfill(fcm, bias_of, daily, obh, settled, sd_lock,
             cfg=None, h0_of=None, res=None):
    """What we WOULD have locked at noon on each past day, scored.

    Uses only information available at noon that day. Flagged backtest=true so
    it is never shown as a live lock. Contemporaneous market prices are not
    recoverable for these days, so they score us only.

    WALK-FORWARD, INCLUDING THE FITTED CONSTANTS.  The per-model bias was always
    fitted only on days before k, which is the honest way round. The other two
    numbers were not: HOURLY_PEAK_OFFSET is a mean over every day in the record
    and the spread came from every residual in it, and both were then used to
    score the very days they were measured on. Day k was helping to set its own
    yardstick.

    The leak is small -- one day is 1/71st of a mean, and the argmax bracket is
    driven by the prediction rather than the spread -- but "small" is a claim,
    and a backtest that grades itself has no standing to make it. Both are now
    refitted per day on days strictly before k, falling back to the defaults
    early on when there is not enough history, which is exactly what would have
    been known at the time.
    """
    by_date = {}
    for evk, lad in settled.items():
        try:
            d = datetime.datetime.strptime(evk.split('-')[1], '%y%b%d').date()
        except Exception:
            continue
        by_date[d.isoformat()] = lad

    def act(k):
        return daily.get(k)

    fc = fcm[MODELS[0]] if MODELS[0] in fcm else list(fcm.values())[0]
    keys = sorted(k for k in by_date if k in fc and len(fc[k]) >= 20)
    out = []
    for k in keys:
        a = act(k)
        if a is None:
            continue
        prior = [p for p in sorted(x for x in fc if x < k)[-BIAS_K:]
                 if len(fc[p]) >= 20 and act(p) is not None]
        if len(prior) < BIAS_MIN:
            continue
        b = bias_of(prior)
        oh = obh.get(k) or {}
        run = max([v for h, v in oh.items() if h <= LOCK_HOUR] or [-99.0])
        # the station offset as it would have been measured that morning
        off_k = HOURLY_PEAK_OFFSET
        if cfg is not None and h0_of is not None:
            m_k = measure_offset(cfg, {d: v for d, v in obh.items() if d < k},
                                 {d: v for d, v in daily.items() if d < k}, h0_of)
            off_k = m_k[0] if m_k else OFFSET_DEFAULT
        obs = run + off_k if run > -90 else None
        yk = (datetime.date(*map(int, k.split('-'))) - datetime.timedelta(days=1)).isoformat()
        fp = point_forecast(fcm, b, k, LOCK_HOUR, daily.get(yk))
        if fp is None:
            continue
        pred = max([x for x in (obs, fp) if x is not None])
        lad = by_date[k]
        # the spread as it would have looked that morning, not as it looks now
        sd_k = sd_lock
        if res is not None:
            past = [r for r in res if r[0] < k]
            if len(past) >= 20:
                sd_k, _ = spread(past, LOCK_HOUR)
        ps = distribution(lad, pred, sd_k, obs)
        bi = max(range(len(lad)), key=lambda i: ps[i])
        ai = which(lad, a)
        truth = next((r['label'] for r in lad if r['yes']), None) \
            or (lad[ai]['label'] if ai is not None else None)
        out.append({
            'date': k, 'event': 'KXHIGHNY', 'backtest': True,
            'actual': a, 'actual_bracket': truth,
            'hit': lad[bi]['label'] == truth,
            'err': round(pred - a, 2),
            'lock': {'at': k + 'T12:00 ET (backtest)', 'pick': lad[bi]['label'],
                     'p': round(ps[bi], 4), 'pred': round(pred, 2),
                     'sd': round(sd_k, 2),
                     'obs_at_lock': obs, 'market_pick': None, 'market_p': None,
                     'ladder': [{'label': r['label'], 'lo': r['lo'], 'hi': r['hi'],
                                 'ours': round(p, 4), 'market': None}
                                for r, p in zip(lad, ps)]},
        })
    return out


# ---------------------------------------------------------------- money ----
# A bracket hit-rate says nothing about profit: 80c to win 20c and 30c to win
# 70c both score one square.  What decides whether any of this is worth doing
# is what the recommended bet would have RETURNED, so each lock records the bet
# it would have placed -- side, price, fee, stake -- and settlement grades it.
#
# This can only ever start live.  Kalshi's candlestick endpoint 404s and a
# settled market quotes 0 or 100, so no historical entry price is recoverable;
# the backfilled days carry no prices at all and are excluded by construction.
# THE ACTUAL BANKROLL. Every stake, the daily cap and the P&L are quoted
# against this one number, and it is small on purpose: the user is starting with
# $10 and a suggestion of "8% of bankroll" means nothing if the arithmetic
# underneath assumed $500. At this size CONTRACTS are the real unit -- quarter
# Kelly on $10 buys one or two of them -- and Kalshi's fee rounds UP to the cent
# per ORDER, so a single contract pays about 2c where a hundred pay 1.35c each.
# fee_of() already computes the single-contract worst case, so the edge shown
# here is the one a small order actually gets.
BANKROLL = 10.0
KELLY_DIV = 4.0

def fee_of(price):
    """Kalshi's per-contract trading fee, charged on entry: ceil(0.07 p (1-p)
    * 100)/100, capped at 3.5c.  It peaks at a 50c contract, which is exactly
    where thin edges live, so an edge that ignores it is not an edge."""
    return min(0.035, math.ceil(0.07 * price * (1 - price) * 100) / 100.0)


# HOW FAR WE ARE ALLOWED TO DISAGREE, measured rather than chosen.
#
# _kalshi/market_study.py bins 1,065 hour-rows of real quotes by how far our
# probability sat from the market's, and asks who ended up closer to what
# happened. The answer is not monotonic and the last line is brutal:
#
#     gap          n     our brier   mkt brier
#     10-20 pts   961      0.0971      0.0852   market wins by 12%
#     20-30 pts   219      0.1349      0.1952   we win by 31%
#     30-50 pts   205      0.1979      0.2002   level
#     50+  pts    213      0.5595      0.1065   MARKET WINS BY 81%
#
# A Brier of 0.56 is not "slightly off". It is being confidently wrong, over and
# over. Past about half the board, a disagreement with a liquid market has never
# been an edge -- it has been this model breaking, and the panel was sizing real
# money against it. Miami's 88-89 rung this morning sat 52 points from the
# market and wanted 65% of the bankroll.
#
# So: rungs we disagree with by more than this are not traded. They are not
# hidden either -- they still render on the ladder, where a 50-point gap is
# worth looking at as a bug report.
MAX_DISAGREE = 0.50


# THE CHEAPEST CONTRACTS HAVE NEVER ONCE PAID.
#
# Every chosen bet in the record, 1,065 of them, split by what it cost:
#
#     price      bets   winrate   ret/$1   cumulative
#      0- 5c      435       0%     -0.94     -410.00
#      5-10c       51       6%     -0.28      -14.49
#     10-25c      106      27%     +0.67      +71.12
#     25-50c      178      55%     +0.44      +79.10
#     50-75c      141      87%     +0.40      +55.82
#     75c+        154      91%     +0.00       +0.33
#
# Four hundred and thirty-five bets at a nickel or less, and NOT ONE of them
# won. That is the whole loss: the strategy is -218 units overall and +206 with
# nothing under a dime in it.
#
# WHY, and it is not the obvious reason. The calibration is fine in the
# aggregate -- across all rung-hours we say 14% and it happens 15%. But bets are
# not a random sample of rung-hours, they are the subset where we disagree with
# the market MOST, and that is exactly where our number is most inflated. Buying
# only where we are the most out of line with a liquid market is the winner's
# curse with the safety off. It is the same finding as MAX_DISAGREE from the
# other end: there by how far we disagree, here by what the disagreement costs.
#
# The floor is a dime, not a quarter, deliberately: 10-25c is the single best
# band in the record at +0.67 per $1, and a quarter would throw it away.
MIN_PRICE = 0.10
# NO BET UNDER FOUR CENTS OF EDGE. The plan used to list anything above half
# a cent after the fee, so a 2c headline and a 1c "also" sat in the plan box
# looking like instructions. A cent is the tick; two cents is inside the
# noise of a quote; four is the floor below which nothing is worth typing.
MIN_EDGE = 0.04
# THE EDGE FLOOR IS PRICE-AWARE (2026-09-07). Measured on 3,237 replayed
# hour-rows at real quotes: under 20c of edge the 7 AM-1 PM bets return about
# nothing in every city and LOSE outright under a 30c price (New York -0.22
# to -0.42 per $1, Las Vegas -0.62 to -1.00, Austin -0.78 to -0.87); at 20c
# and over they return +0.7 to +1.7. The floor is 20c, or 10c once the price
# is 30c or more. On the record it keeps the total profit and lifts return
# per dollar from +0.44 / +0.35 / +0.17 to +0.68 / +0.60 / +0.61 on a third
# fewer bets. MIN_EDGE stays as the tick floor the panel's prose refers to.
EDGE_FLOOR = 0.20
EDGE_FLOOR_PRICED = 0.10
EDGE_PRICE = 0.30


def edge_ok(ev, price):
    """Whether an edge at this price clears the measured floor."""
    return ev >= EDGE_FLOOR or (price is not None and price >= EDGE_PRICE and ev >= EDGE_FLOOR_PRICED)


def measured_floor():
    """THE FLOOR LEARNS (2026-09-07). price_study.py measures the edge floor
    nightly (edge_floor in price_study.json); when it rests on enough rows the
    typed constants above give way to it. Guardrails: at least 800 scored
    hour-rows behind the table, both classes inside their clamps, and a move
    of at most 5c a night from the values in force, so one odd week cannot
    swing the floor. Returns what was applied, for the log and the lock."""
    global EDGE_FLOOR, EDGE_FLOOR_PRICED, EDGE_PRICE
    try:
        d = json.load(open(os.path.join(HERE, 'price_study.json'))).get('edge_floor') or {}
    except Exception:
        return None
    if (d.get('n') or 0) < 800 or d.get('min') is None or d.get('priced') is None:
        return None
    step = 0.05
    new_min = min(EDGE_FLOOR + step, max(EDGE_FLOOR - step, float(d['min'])))
    new_pr = min(EDGE_FLOOR_PRICED + step, max(EDGE_FLOOR_PRICED - step, float(d['priced'])))
    if not (0.15 <= new_min <= 0.40 and 0.08 <= new_pr <= 0.30):
        return None
    changed = (abs(new_min - EDGE_FLOOR) > 1e-9) or (abs(new_pr - EDGE_FLOOR_PRICED) > 1e-9)
    EDGE_FLOOR, EDGE_FLOOR_PRICED = round(new_min, 2), round(new_pr, 2)
    if d.get('price'):
        EDGE_PRICE = float(d['price'])
    return {'min': EDGE_FLOOR, 'priced': EDGE_FLOOR_PRICED, 'price': EDGE_PRICE, 'n': d.get('n'),
            'measured': bool(d.get('cheap_measured')) and bool(d.get('priced_measured')), 'changed': changed}


def _wild(q, market_p):
    """True when our number is too far from the market's to be believed."""
    return market_p is not None and abs(q - market_p) > MAX_DISAGREE


def measured_cap():
    """THE CAP LEARNS (2026-09-07), the same way the edge floor does:
    price_study.py measures it nightly (disagree_cap); the bake applies it on
    800+ morning rows, clamped to 30-70 points, moving at most 5 points a
    night from the value in force. Typed value kept otherwise."""
    global MAX_DISAGREE
    try:
        d = json.load(open(os.path.join(HERE, 'price_study.json'))).get('disagree_cap') or {}
    except Exception:
        return None
    if (d.get('n') or 0) < 800 or d.get('cap') is None:
        return None
    new = min(MAX_DISAGREE + 0.05, max(MAX_DISAGREE - 0.05, float(d['cap'])))
    if not (0.30 <= new <= 0.70):
        return None
    changed = abs(new - MAX_DISAGREE) > 1e-9
    MAX_DISAGREE = round(new, 2)
    return {'cap': MAX_DISAGREE, 'n': d.get('n'), 'changed': changed}




def best_bet(rows, ps):
    """The largest gap between our probability and what a side actually costs,
    after the fee.  Both directions: on a six-way ladder buying NO is usually
    where the value sits, because there are five ways to be right."""
    best = None
    for r, p in zip(rows, ps):
        for side, price, q in (('for', r.get('ask'), p),
                               ('against', r.get('nask'), 1.0 - p)):
            if price is None or not (MIN_PRICE <= price < 1):
                continue
            # our q for this side vs the market's own number for the same side
            mp = r.get('market')
            if mp is not None and side == 'against':
                mp = 1.0 - mp
            if _wild(q, mp):
                continue
            ev = q - price - fee_of(price)
            if not edge_ok(ev, price):
                continue
            if best is None or ev > best['ev']:
                cost = price + fee_of(price)
                best = {'dir': side, 'label': r['label'], 'price': round(price, 4),
                        'q': round(q, 4), 'ev': round(ev, 4),
                        'fee': round(fee_of(price), 4),
                        'kelly': round(max(0.0, (q_sized(q, getattr(_TL, 'cfg', None)) - cost)
                                           / (1 - cost)) / KELLY_DIV, 4)
                                 if cost < 1 else 0.0,
                        'size': r.get('nsize') if side == 'against' else r.get('ysize')}
    return best


def lock_book(rows, ps):
    """Every bet the plan names at lock -- the headline and the ALSOs -- so the
    record can grade the whole plan, not just its first line. 2026-09-05's plan
    was NO 79-80 and YES 78-or-below; both lost; only the first was scored."""
    out = []
    for r, p in zip(rows, ps):
        for side, price, q in (('for', r.get('ask'), p), ('against', r.get('nask'), 1.0 - p)):
            if price is None or not (MIN_PRICE <= price < 1):
                continue
            mp = r.get('market')
            if mp is not None and side == 'against':
                mp = 1.0 - mp
            if _wild(q, mp):
                continue
            ev = q - price - fee_of(price)
            cost = price + fee_of(price)
            if not edge_ok(ev, price) or cost >= 1:
                continue
            out.append({'dir': side, 'label': r['label'], 'price': round(price, 4),
                        'q': round(q, 4), 'ev': round(ev, 4), 'fee': round(fee_of(price), 4),
                        'kelly': round(max(0.0, (q_sized(q, getattr(_TL, 'cfg', None)) - cost)
                                           / (1 - cost)) / KELLY_DIV, 4)})
    out.sort(key=lambda b: -b['ev'])
    return out


def book_value(rows, ps, bankroll=None):
    """Expected dollars from today's whole book, capped by what is on offer.

    Edge per contract is exactly `ev` -- pay cost, receive $1 with probability
    q, so the expectation is q - cost. Multiply by the number of contracts that
    can actually be bought and it is dollars, not basis points.

    This is the number the strategy is really trying to maximise. Accuracy
    without size is a hobby: the largest edge on the board tonight was 41c with
    $2 behind it, while the deepest market had $349 and a 3c edge.
    """
    bankroll = bankroll or BANKROLL
    ev = stake = 0.0
    n = 0
    for r, p in zip(rows, ps):
        for side, price, q, size in (('for', r.get('ask'), p, r.get('ysize')),
                                     ('against', r.get('nask'), 1.0 - p, r.get('nsize'))):
            if price is None or not (MIN_PRICE <= price < 1):
                continue
            mp = r.get('market')
            if mp is not None and side == 'against':
                mp = 1.0 - mp
            if _wild(q, mp):
                continue
            e = q - price - fee_of(price)
            if not edge_ok(e, price):
                continue
            cost = price + fee_of(price)
            if cost >= 1:
                continue
            f = max(0.0, (q_sized(q, getattr(_TL, 'cfg', None)) - cost) / (1 - cost)) / KELLY_DIV
            want = (bankroll * f) / cost
            fill = want if size is None else min(want, float(size))
            if fill <= 0:
                continue
            ev += fill * e
            stake += fill * cost
            n += 1
    return {'ev': round(ev, 2), 'stake': round(stake, 2), 'n': n}


# ----------------------------------------------------------- real trades ----
# The P&L above is hypothetical: quarter Kelly, filled at the ask. What was
# actually done is a different number and the more useful one, so it is kept in
# a plain file the user appends to (_kalshi/trades.csv) and scored here against
# the same settlements. Deliberately tolerant about how a strike is written --
# a ledger that rejects "78-" because it wanted "78 or below" will not get kept.
TRADES = os.path.join(HERE, 'trades.csv')


def parse_strike(txt):
    """'84-85' / '78 or below' / '90+' / '<=78' -> (lo, hi), either may be None."""
    t = str(txt).lower().replace('\u00b0', '').replace('\u2264', '<=').replace('\u2265', '>=')
    nums = [int(x) for x in re.findall(r'\d+', t)]
    if len(nums) >= 2:
        return min(nums[0], nums[1]), max(nums[0], nums[1])
    if not nums:
        return None
    n = nums[0]
    if any(w in t for w in ('below', 'under', 'less', '<')) or t.rstrip().endswith('-'):
        return None, n
    if any(w in t for w in ('above', 'over', 'greater', '>')) or t.rstrip().endswith('+'):
        return n, None
    return n, n            # a bare number: treat as its own bracket


def load_trades(market_key):
    """Rows for one market, as dicts. A bad line is reported, never fatal."""
    out = []
    if not os.path.exists(TRADES):
        return out
    short = market_key.split('_')[0]
    try:
        with open(TRADES) as f:
            body = [l for l in f if l.strip() and not l.lstrip().startswith('#')]
        for i, row in enumerate(csv.DictReader(body)):
            if not row.get('date') or not row.get('market'):
                continue
            if row['market'].strip().lower() not in (short, market_key):
                continue
            try:
                price = float(row['price'])
                if price > 1.5:              # written in cents
                    price /= 100.0
                side = row['side'].strip().lower()
                bounds = parse_strike(row['strike'])
                if bounds is None or side not in ('yes', 'no'):
                    raise ValueError('side or strike not understood')
                out.append({'date': row['date'].strip(), 'side': side,
                            'lo': bounds[0], 'hi': bounds[1], 'price': price,
                            'contracts': float(row['contracts']),
                            'fee': (float(row['fee']) / 100.0) if row.get('fee') else None,
                            'note': (row.get('note') or '').strip()})
            except Exception as e:
                print('trades.csv line %d ignored (%s): %s' % (i + 2, e, row))
    except Exception as e:
        print('could not read trades.csv: %s' % e)
    return out


def score_trades(trades, hist):
    """Real P&L: cost is price plus fee, a winner pays $1, a loser pays nothing."""
    done, open_ = [], []
    for t in trades:
        h = hist.get(t['date'])
        if not h or h.get('actual_bracket') is None:
            open_.append(t)
            continue
        # match by BOUNDS, not by label: the ladder re-centres daily and the
        # user writes "78-", not "78\u00b0 or below"
        lad = (h.get('lock') or {}).get('ladder') or []
        mine = None
        for r in lad:
            if r.get('lo') == t['lo'] and r.get('hi') == t['hi']:
                mine = r['label']
                break
        if mine is None:
            print('trade %s: no range with those bounds that day, skipped' % t['date'])
            continue
        won = (mine == h['actual_bracket']) if t['side'] == 'yes' \
              else (mine != h['actual_bracket'])
        fee = t['fee'] if t['fee'] is not None else fee_of(t['price'])
        cost = t['price'] + fee
        pl = t['contracts'] * ((1 - cost) if won else -cost)
        done.append(dict(t, bracket=mine, settled=h['actual_bracket'], won=won,
                         cost=round(cost, 4), pl=round(pl, 2),
                         provisional=bool(h.get('provisional'))))
    return done, open_


# ------------------------------------------------------- portfolio import ----
# Real fills, straight from the exchange, so the ledger needs no typing. Kalshi
# authenticates with an RSA key: every request carries the key id, a millisecond
# timestamp, and an RSA-PSS/SHA256 signature over `timestamp + METHOD + path`.
#
# THE KEY NEVER TOUCHES THIS REPO. Both values are read from the environment and
# come from GitHub Actions secrets, which the account owner sets themselves:
#   KALSHI_API_KEY_ID    the key's uuid
#   KALSHI_PRIVATE_KEY   the PEM, newlines and all
# With either missing this whole path no-ops and trades.csv remains the ledger.
# BANKROLL IS NOT PUBLISHED. kalshi_*.json is served by GitHub Pages, so every
# field in it is world-readable -- `curl` on the URL returns it with no browser
# and no session. The account's live cash balance sat in `today.bankroll` for
# anyone who looked. It is now served privately by the cron worker's /positions
# endpoint instead, behind a token.
#
# The number is still READ here, because sizing has to divide by something. It
# just does not go in the file.
#
# HONEST RESIDUAL: the contract counts that remain are derived from it, and the
# quantities they are derived from (our q, the price) are published, so a
# determined reader can still work the pot out to within a rounding. Closing
# that means moving sizing into the page against the private feed, which is the
# next step rather than this one.
KALSHI_BASE = 'https://api.elections.kalshi.com'


def _signer():
    kid = os.environ.get('KALSHI_API_KEY_ID')
    pem = os.environ.get('KALSHI_PRIVATE_KEY')
    if not kid or not pem:
        return None
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        print('portfolio: cryptography not installed, skipping API import')
        return None
    try:
        key = serialization.load_pem_private_key(
            pem.replace('\\n', '\n').encode(), password=None)
    except Exception as e:
        print('portfolio: private key could not be loaded (%s)' % type(e).__name__)
        return None

    def sign(method, path):
        ts = str(int(time.time() * 1000))
        sig = key.sign((ts + method + path).encode(),
                       padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                   salt_length=padding.PSS.DIGEST_LENGTH),
                       hashes.SHA256())
        return {'KALSHI-ACCESS-KEY': kid,
                'KALSHI-ACCESS-TIMESTAMP': ts,
                'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode()}
    return sign


def portfolio_get(sign, path, params=''):
    """Signed GET. The signature covers the PATH ONLY -- query string excluded."""
    req = urllib.request.Request(KALSHI_BASE + path + params,
                                 headers=dict(sign('GET', path),
                                              **{'Accept': 'application/json',
                                                 'User-Agent': 'bluishvoid/1.0'}))
    return json.loads(urllib.request.urlopen(req, timeout=45).read())


def decode_ticker(tk, lookup):
    """KXHIGHNY-26SEP05-B85.5 -> (date, lo, hi). `B` carries its own bounds; `T`
    is a threshold whose direction the ticker does not record, so those are
    looked up once and cached."""
    parts = tk.split('-')
    if len(parts) < 3:
        return None
    try:
        d = datetime.datetime.strptime(parts[1], '%y%b%d').date().isoformat()
    except Exception:
        return None
    suf = parts[2]
    if suf.startswith('B'):
        try:
            mid = float(suf[1:])
            return d, int(math.floor(mid)), int(math.ceil(mid))
        except Exception:
            return None
    if tk not in lookup:
        try:
            m = json.loads(get('%s/trade-api/v2/markets/%s'
                               % (KALSHI_BASE, urllib.parse.quote(tk))))['market']
            lookup[tk] = (m.get('floor_strike'), m.get('cap_strike'), m.get('strike_type'))
        except Exception:
            lookup[tk] = None
    got = lookup[tk]
    if not got:
        return None
    lo, hi, st = got
    if st == 'less':
        return d, None, int(hi) - 1 if hi is not None else None
    if st == 'greater':
        return d, int(lo) + 1 if lo is not None else None, None
    return d, int(lo) if lo is not None else None, int(hi) if hi is not None else None


_FILLS_CACHE = []          # one page-through per process, shared by all markets
_FILLS_LOCK = threading.Lock()


def fetch_balance():
    """The real bankroll, from the exchange. Returns dollars, or None.

    Hardcoding it means editing source every time the pot changes, and the pot
    is meant to change -- that is the whole point. With the read-scoped key set
    this tracks the actual balance, so every stake on the panel is sized against
    what is really there. Without the key it falls back to BANKROLL.
    """
    sign = _signer()
    if not sign:
        return None
    try:
        j = portfolio_get(sign, '/trade-api/v2/portfolio/balance')
        # `balance` is in cents; newer responses also carry a _dollars form
        if j.get('balance_dollars') is not None:
            return float(j['balance_dollars'])
        if j.get('balance') is not None:
            return float(j['balance']) / 100.0
    except Exception as e:
        print('portfolio: balance unavailable (%s)' % e)
    return None


def _all_fills():
    """Every fill on the account, fetched ONCE. Called per market otherwise,
    which would page through the whole history seven times a run for no reason
    and for no thanks from the rate limiter."""
    with _FILLS_LOCK:
        return _all_fills_locked()


def _all_fills_locked():
    if _FILLS_CACHE:
        return _FILLS_CACHE[0]
    sign = _signer()
    if not sign:
        _FILLS_CACHE.append(None)
        return None
    got, cursor = [], None
    try:
        for _ in range(20):                       # 200 a page; plenty of history
            params = '?limit=200' + ('&cursor=' + cursor if cursor else '')
            j = portfolio_get(sign, '/trade-api/v2/portfolio/fills', params)
            got += j.get('fills') or []
            cursor = j.get('cursor')
            if not cursor:
                break
        print('portfolio: %d fills on the account' % len(got))
    except urllib.error.HTTPError as e:
        print('portfolio: HTTP %s -- check the key id, that the PEM matches it, '
              'and that the key has the read scope' % e.code)
        got = None
    except Exception as e:
        print('portfolio: %s: %s' % (type(e).__name__, e))
        got = None
    _FILLS_CACHE.append(got)
    return got


def fetch_fills(cfg, lookup):
    """This market's fills, as trade rows in the same shape as trades.csv."""
    fills = _all_fills()
    if fills is None:
        return None
    out = []
    try:
        if True:
            for f in fills:
                tk = f.get('ticker') or ''
                if not tk.startswith(cfg['series'] + '-'):
                    continue
                dec = decode_ticker(tk, lookup)
                if not dec:
                    continue
                d, lo, hi = dec
                # `side` is which contract was bought; `action` says buy or sell.
                # A sale is an exit, not a new position -- skip rather than
                # score it as a fresh bet in the opposite direction.
                if (f.get('action') or 'buy') != 'buy':
                    continue
                side = (f.get('side') or 'yes').lower()
                cents = f.get('yes_price') if side == 'yes' else f.get('no_price')
                if cents is None:
                    continue
                out.append({'date': d, 'side': side, 'lo': lo, 'hi': hi,
                            'price': float(cents) / 100.0,
                            'contracts': float(f.get('count') or 0),
                            'fee': None, 'note': 'api',
                            'at': f.get('created_time'),
                            'id': f.get('trade_id') or f.get('order_id')})
    except Exception as e:
        print('portfolio: %s: %s' % (type(e).__name__, e))
        return None
    if out:
        print('%s portfolio: %d fills' % (cfg['key'], len(out)))
    return out


def grade_bet(bet, truth):
    """Profit on a $100 bankroll staked at quarter Kelly.  Cost is price plus
    fee; a winning contract pays $1, a losing one pays nothing."""
    if not bet or truth is None or not bet.get('kelly'):
        return None
    won = (bet['label'] == truth) if bet['dir'] == 'for' else (bet['label'] != truth)
    cost = bet['price'] + bet['fee']
    stake = BANKROLL * bet['kelly']
    return {'won': won, 'staked': round(stake, 2),
            'pl': round(stake * ((1 - cost) / cost) if won else -stake, 2)}


def measure_offset(cfg, obh, daily, h0_of):
    """How far the hourly stream reads BELOW the day's true max, for this
    station specifically.

    The routine METAR misses the intra-hour peak, so a running max built from it
    reads low -- but by how much depends on the station's reporting cadence and
    its diurnal curve, which are not the same in Los Angeles as in Central Park.
    These were fitted once on NYC and applied to all seven cities, which is
    exactly the kind of borrowed constant that quietly costs accuracy.

    Direct measurement, not a fit: for each completed day, the published daily
    max minus the max of the hourly readings. Returns (mean, sd, n), or None
    when there is not enough history to be worth trusting.
    """
    gaps = []
    for k, hrs in obh.items():
        a = daily.get(k)
        if a is None or not hrs:
            continue
        try:
            h0 = h0_of(k)
        except Exception:
            continue
        vals = [v for h, v in hrs.items() if h >= h0]
        if len(vals) < 18:            # a day with big holes says nothing
            continue
        g = a - max(vals)
        if -3.0 < g < 5.0:            # a gap outside this is a data fault
            gaps.append(g)
    if len(gaps) < 40:
        return None
    return (round(statistics.mean(gaps), 3),
            round(max(0.20, statistics.pstdev(gaps)), 3),
            len(gaps))


TICKER_CACHE = {}


_STUDY = []


def measured_calib(cfg):
    """price_study.py's said-vs-happened bands for this market (or the pool)."""
    measured_hours(cfg)
    d = _STUDY[0] if _STUDY else None
    if not d:
        return None
    # A CITY BAND UNDER 150 ROWS TAKES THE POOLED HAIRCUT (2026-09-07). The
    # per-market tables existed for every city, so the pooled table -- which
    # already carried the long-shot haircuts (0.1: 5 pts, 0.2: 10, 0.3: 6) --
    # was never read, and the bands that lose money (outcomes we call 20-35%
    # land 0-20% in every city) went unprotected because none had 150 rows.
    pool = d.get('calib') or {}
    mine = (d.get('calib_by_market') or {}).get(cfg['key']) or {}
    out = dict(pool)
    for k, b in mine.items():
        if (b.get('n') or 0) >= 150:
            out[k] = b
    return out or None


_CALIB = {}


def q_sized(q, cfg=None):
    """The probability Kelly is sized from: q, less the measured haircut of its
    band when that band has enough rows and says more than happens. Inert
    until the data exists (see price_study.calibration_bands)."""
    cal = _CALIB.get((cfg or {}).get('key')) if cfg else None
    if not cal:
        return q
    b = cal.get('%.1f' % min(int(q * 10) / 10.0, 0.9)) or {}
    return max(0.0, q - (b.get('haircut') or 0.0))


def measured_blend(cfg, hour):
    """Weight on our probability against the market's for this hour (price_study
    blend_by_market), or None before the study has it."""
    if not _STUDY:
        measured_hours(cfg)
    d = _STUDY[0] if _STUDY else None
    if not d:
        return None
    e = ((d.get('blend_by_market') or {}).get(cfg['key']) or {}).get(str(int(hour)))
    return e.get('w') if e else None


def measured_exit(cfg):
    """price_study.py's hold-versus-close measurement, this market's own if it
    has one, else the pool. The panel quotes it beside an open position."""
    measured_hours(cfg)                     # loads the study file once
    d = _STUDY[0] if _STUDY else None
    if not d:
        return None
    return (d.get('exit_by_market') or {}).get(cfg['key']) or d.get('exit')


def measured_hours(cfg):
    """The hourly curve from price_study.py, this market's own if it has one.

    Replaces an ASSUMED timing story with a measured one. The panel used to say
    "bet in the morning" because our accuracy is flat and prices were assumed to
    harden; this is what betting at each hour actually returned.
    """
    if not _STUDY:
        f = os.path.join(HERE, 'price_study.json')
        try:
            with open(f) as fh:
                _STUDY.append(json.load(fh))
        except Exception:
            _STUDY.append(None)
    d = _STUDY[0]
    if not d:
        return None
    cur = (d.get('by_market') or {}).get(cfg['key']) or d.get('pooled')
    if not cur:
        return None
    out = []
    for e in cur:
        if e.get('ret') is None:
            continue
        out.append({'h': e['h'], 'ret': e['ret'], 'winrate': e.get('winrate'),
                    'pool': e.get('pool'),
                    'bets': e.get('bets'), 'ours': e.get('ours_brier'),
                    'mkt': e.get('mkt_brier'), 'days': e.get('days'),
                    # kept so the old chart still has something to draw
                    'acc': int(round(100 * (e.get('winrate') or 0))),
                    'sd': SD_FALLBACK.get(e['h'])})
    return out or None


def compose_review(cfg, hist, obh, cli, fills_by_day, now, record=None):
    """The latest settled day's takeaways, as short lines with a lesson each."""
    tkey = now.date().isoformat()
    cands = sorted(k for k, h in hist.items()
                   if k < tkey and h.get('actual') is not None and h.get('lock'))
    if not cands:
        return None
    k = cands[-1]
    h = hist[k]
    lk = h['lock']
    a = float(h['actual'])
    final = (h.get('truth_source') == 'settlement')
    L = []
    c = (cli or {}).get(k) or {}
    at = c.get('at')
    hit = bool(h.get('hit'))
    err = h.get('err')
    # 1. the outcome against the noon lean
    L.append('Settled <b>%.0f\u00B0</b>%s, in <b>%s</b>. The noon lean was <b>%s</b> at %.0f%% \u2014 %s%s.' % (
        a, (' at ' + at) if at else '', h.get('actual_bracket') or '?', lk.get('pick') or '?', 100 * (lk.get('p') or 0),
        '<b style="color:#2fbfa8;">held</b>' if hit else '<b style="color:#f2833f;">missed</b>',
        (' (forecast %.1f, off by %.1f\u00B0)' % (lk['pred'], abs(err))) if (err is not None and lk.get('pred') is not None) else ''))
    # 2. the market at noon
    mp, mh = lk.get('market_pick'), h.get('market_hit')
    if mp:
        if mp == lk.get('pick'):
            L.append('The market agreed at noon (%s at %.0f%%)%s.' % (mp, 100 * (lk.get('market_p') or 0),
                     ' and both were right' if hit else ' and both were wrong'))
        else:
            L.append('The market disagreed at noon, on <b>%s</b> \u2014 %s. Morning disagreements have favoured the forecast in the record; afternoon ones favour the market.' % (
                mp, 'the market was right' if mh else ('we were right' if hit else 'neither was right')))
    # 3. where the day was decided, and what the hourly stream showed
    hrs = obh.get(k) or {}
    hmax = max(hrs.values()) if hrs else None
    six_txt = ''
    # the six-hourly group is the 17:51Z observation everywhere, so its local
    # clock follows the city's zone: 1:51 PM ET, 12:51 PM CT, 10:51 AM PT
    try:
        _off = int(round(now.utcoffset().total_seconds() / 3600)) if now.utcoffset() is not None else -4
    except Exception:
        _off = -4
    six_h = (17 + _off) % 24
    six_lbl = '%d:51 %s' % ((six_h % 12) or 12, 'AM' if six_h < 12 else 'PM')
    if at:
        try:
            t = datetime.datetime.strptime(str(at).replace(' ', ''), '%I:%M%p')
            late = (t.hour * 60 + t.minute) > (six_h * 60 + 51)
            six_txt = (' \u2014 after the %s six-hour group' if late else ' \u2014 inside the %s six-hour group') % six_lbl
        except ValueError:
            pass
    if hmax is not None:
        gap = a - hmax
        L.append('The high was set%s%s; the hourly reports topped out at %.0f\u00B0%s.' % (
            (' at ' + at) if at else '', six_txt, hmax,
            (', <b>%.0f\u00B0 under the settlement</b> \u2014 a peak between reports that the 5-minute feed or a corroborated TWC maximum would have seen' % gap) if gap >= 1 else ''))
    elif at:
        L.append('The high was set at %s%s.' % (at, six_txt))
    # 4. the official forecasts
    pub = h.get('published') or {}
    fc = []
    if pub.get('twc') is not None:
        fc.append('TWC %.0f (off %.0f)' % (pub['twc'], abs(pub['twc'] - a)))
    if pub.get('nws') is not None:
        fc.append('NWS %.0f (off %.0f)' % (pub['nws'], abs(pub['nws'] - a)))
    if fc:
        L.append('Morning forecasts: ' + ', '.join(fc) + '; ours %.1f (off %.1f).' % (lk.get('pred') or 0, abs((lk.get('pred') or 0) - a)))
    # 5. the plan and the fills
    br = h.get('book_results') or []
    if br:
        won = sum(1 for b in br if b.get('won'))
        L.append('The noon book: %d of %d lines paid%s.' % (won, len(br),
                 (', <b>%+.2f per $1</b> held to settlement' % h['plan_ret']) if h.get('plan_ret') is not None else ''))
    elif h.get('bet_result'):
        b = h['bet_result']
        L.append('The noon bet %s (%+.2f on $%.2f).' % ('paid' if b.get('won') else 'lost', b.get('pl', 0), b.get('staked', 0)))
    f = (fills_by_day or {}).get(k)
    if f and f.get('staked'):
        L.append('Real fills that day: <b>%+.2f</b> on $%.2f.' % (f['pl'], f['staked']))
        if h.get('plan_ret') is not None and h['plan_ret'] > 0 and f['pl'] < 0:
            L.append('<b>Lesson:</b> the plan paid and the fills did not \u2014 the loss came from stepping off the plan, not from the forecast.')
    # 6. the model lesson
    if err is not None:
        if abs(err) >= 1.0:
            L.append('<b>Lesson:</b> the runs ran %s by %.1f\u00B0; the %s bias window pulls that in from tomorrow.' % (
                'cold' if err < 0 else 'warm', abs(err), 'recency-weighted' if cfg.get('bias_hl') else ('%d-day' % BIAS_K)))
        elif not hit:
            L.append('<b>Lesson:</b> the forecast was within a degree and still missed the range \u2014 a rounding-line day; the plan\u2019s coin-flip warning is the right read on those.')
        else:
            L.append('<b>Lesson:</b> the forecast was within %.1f\u00B0 and the range held; nothing to change.' % abs(err))
    day = [x for x in L if not x.startswith('<b>Lesson:</b>')]
    lessons = [x.replace('<b>Lesson:</b> ', '') for x in L if x.startswith('<b>Lesson:</b>')]
    lessons = [x[:1].upper() + x[1:] for x in lessons]
    return {'date': k, 'final': final, 'lines': L, 'day': day, 'lessons': lessons,
            'run': compose_run_review(cfg, record or {}),
            'built': now.strftime('%Y-%m-%dT%H:%M')}


def compose_run_review(cfg, record):
    """THE LONG VIEW, in words: what the settled record says about the
    system and what it changes. Read by the panel beside the day's review
    (user, 2026-09-06: "longterm and shortterm reviews and reflection").
    Every line is a measured thing from `record`, never an opinion."""
    R = []
    bt = record.get('backtest') or {}
    lv = record.get('live') or {}
    # the tiles already carry the hit counts; these lines say what the counts
    # do not: a bias the runs carry, and a live record parting from the backtest
    if bt.get('n') and abs(bt.get('bias') or 0) >= 0.2:
        R.append('The runs read <b>%.2f\u00B0 %s</b> on average over %d settled days; the bias window takes it out.' % (
            abs(bt['bias']), 'warm' if bt['bias'] > 0 else 'cold', bt['n']))
    if lv.get('n', 0) >= 2 and bt.get('n'):
        exp = bt.get('hits', 0) / float(bt['n'])
        got = lv.get('hits', 0) / float(lv['n'])
        if got < exp - 0.2:
            R.append('Live is running behind the record: <b>%d of %d</b> right against %d%% expected, mean miss %.2f\u00B0 \u2014 the grade and the stake are cut in half until it recovers.' % (
                lv.get('hits', 0), lv['n'], round(100 * exp), lv.get('mae') or 0))
        elif lv['n'] >= 5 and got > exp + 0.15:
            R.append('Live is running ahead of the record: <b>%d of %d</b> right against %d%% expected.' % (
                lv.get('hits', 0), lv['n'], round(100 * exp)))
    cal = record.get('calibration') or {}
    bins = cal.get('bins') or []
    if cal.get('gap') is not None and bins:
        tot = sum(b['n'] for b in bins) or 1
        signed = sum(b['n'] * (b['said'] - b['happened']) for b in bins) / tot
        if abs(signed) > 0.01:
            R.append('The stated odds run <b>%s</b>%s.' % (
                'a little high' if signed > 0 else 'a little low',
                ' \u2014 confident calls are landing more often than they are priced, so the plan under-sizes them' if signed < 0
                else ' \u2014 the plan sizes on the stated odds, so a high reading is money bet that is not there'))
    D = record.get('discipline') or {}
    mn = record.get('money') or {}
    if D.get('days') and D.get('plan_ret') is not None:
        line = 'The noon plan held to settlement returned <b>%+.2f per $1</b> over %d days (%d won).' % (D['plan_ret'], D['days'], D.get('plan_wins', 0))
        if mn.get('n'):
            line += ' Real fills: <b>%+.2f</b> on $%.2f.' % (mn['pl'], mn.get('staked', 0))
            if D['plan_ret'] > 0 and mn['pl'] < 0:
                line += ' The gap between the two is discipline, not forecasting: the plan pays when it is left alone.'
        R.append(line)
    lh = record.get('live_hours') or {}
    if lh:
        hs = sorted(int(h) for h in lh)
        ok = sum(lh[str(h)][0] for h in hs); tot = sum(lh[str(h)][1] for h in hs)
        worst = min(hs, key=lambda h: (lh[str(h)][0] / lh[str(h)][1]) if lh[str(h)][1] else 1)
        wr = lh[str(worst)]
        # name the weak hour only when the hours actually differ
        weak = (tot and wr[1] and wr[0] < wr[1] and ok > 0 and (wr[0] / wr[1]) < (ok / tot) - 0.05)
        if weak:
            R.append('The live lean is weakest at <b>%d %s</b> (%d of %d), so a bet placed then leans on less.' % (
                (worst % 12) or 12, 'AM' if worst < 12 else 'PM', wr[0], wr[1]))
    pub = record.get('published') or {}
    if pub.get('n', 0) >= 5:
        parts = []
        for key, name in (('ours', 'ours'), ('twc', 'TWC'), ('nws', 'NWS')):
            o = pub.get(key) or {}
            if o.get('mae') is not None:
                parts.append('%s %.2f\u00B0' % (name, o['mae']))
        if parts:
            R.append('Morning forecasts over %d days, mean miss: %s.' % (pub['n'], ', '.join(parts)))
    vm = record.get('vs_market') or {}
    if vm.get('n', 0) >= 3:
        R.append('Head to head at noon over %d priced days: ours %d, the market %d.' % (vm['n'], vm.get('ours', 0), vm.get('market', 0)))
    return R


TRAIL_URL = 'https://cdn.bluishvoid.com/kalshi/trail/%s_%s.jsonl'
_TRAIL_CACHE = {}
def trail_rows(key, day):
    """the day's bake rows from R2 (see trail_row), or [] before the trail existed"""
    k = (key, day)
    if k in _TRAIL_CACHE:
        return _TRAIL_CACHE[k]
    rows = []
    try:
        req = urllib.request.Request(TRAIL_URL % (key, day), headers={'User-Agent': 'bluishvoid.com bake'})
        txt = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'replace')
        for line in txt.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        rows.sort(key=lambda r: r.get('t') or '')
    except Exception:
        rows = []
    _TRAIL_CACHE[k] = rows
    return rows


def execution_score(cfg, fills, label_of):
    """EXECUTION AGAINST THE PLAN (2026-09-07): each fill matched to the bake that
    was on screen when it was struck. Was it the plan's line, what did the plan
    say to pay, how long after the plan first showed it was it placed. Only fills
    since the trail began can be scored; older ones are simply not counted."""
    out = []
    for t in fills:
        at = t.get('at')
        if not at or not t.get('date'):
            continue
        rows = trail_rows(cfg['key'], t['date'])
        if not rows:
            continue
        T = str(at)[:19].replace('T', ' ')
        before = [r for r in rows if (r.get('t') or '')[:19].replace('T', ' ') <= T]
        if not before:
            continue
        row = before[-1]
        b = row.get('bet')
        lab = label_of(t['lo'], t['hi'])
        dirn = 'against' if t['side'] == 'no' else 'for'
        in_plan = bool(b and b.get('label') == lab and b.get('dir') == dirn)
        first = next((r for r in rows if r.get('bet') and r['bet'].get('label') == lab and r['bet'].get('dir') == dirn), None)
        delay = None
        if first:
            try:
                t0 = datetime.datetime.strptime((first['t'] or '')[:19], '%Y-%m-%dT%H:%M:%S')
                t1 = datetime.datetime.strptime(T, '%Y-%m-%d %H:%M:%S')
                delay = round((t1 - t0).total_seconds() / 60)
            except Exception:
                delay = None
        out.append({'date': t['date'], 'at': at, 'side': t['side'], 'label': lab, 'price': t['price'],
                    'contracts': t.get('contracts'), 'in_plan': in_plan,
                    'plan_price': (b.get('price') if in_plan else None),
                    'slip_c': (round(100 * (t['price'] - b['price']), 1) if in_plan and b.get('price') is not None else None),
                    'delay_min': delay, 'bake_at': row.get('lt')})
    if not out:
        return None
    sl = [x['slip_c'] for x in out if x['slip_c'] is not None]
    dl = [x['delay_min'] for x in out if x['delay_min'] is not None]
    return {'n': len(out), 'in_plan': sum(1 for x in out if x['in_plan']),
            'slip_mean_c': round(statistics.mean(sl), 1) if sl else None,
            'delay_median_min': int(statistics.median(dl)) if dl else None,
            'recent': out[-6:]}


def trail_row(doc, now):
    """THE BAKE TRAIL (2026-09-07): one compact row per bake per city -- what the
    sheet said (bet, price, size, grade inputs), what was on offer (every rung's
    ask and depth), and what the readings were (hourly max, 5-minute sensor, TWC,
    six-hour group) with their clocks. Appended locally per day; the workflow
    appends the day's file to R2 (kalshi/trail/<key>_<date>.jsonl, public on the
    cdn). Mids can be rebuilt from Kalshi's candles later; depth and the plan
    cannot, which is why this exists. No bankroll figures in here."""
    T = doc.get('today') or {}
    b = T.get('bet') or None
    tm = T.get('tomorrow') or {}
    o5 = T.get('own5_row') or {}
    pk = T.get('peak_done') or {}
    return {
        't': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'lt': now.strftime('%H:%M'), 'date': T.get('date'), 'k': doc.get('key') or T.get('key'),
        'pred': T.get('pred'), 'sd': round(T.get('sd') or 0, 3), 'p': T.get('p'), 'pick': T.get('pick'),
        'mpick': T.get('market_pick'), 'mp': T.get('market_p'), 'agree': T.get('agree'),
        'bet': ({'dir': b['dir'], 'label': b['label'], 'price': b['price'], 'ev': b['ev'], 'q': b.get('q'),
                 'kelly': b.get('kelly'), 'size': b.get('size')} if b else None),
        'locked': bool(T.get('locked')), 'over': bool(T.get('day_over')), 'decided': bool(T.get('day_decided')),
        'blend_w': T.get('blend_w'), 'peak_p': pk.get('p') if isinstance(pk, dict) else None,
        'obs': {'hmax': T.get('obs_so_far'), 'now': T.get('now_temp'), 'now_at': T.get('now_at'),
                'own5': T.get('own5_max'), 'own5_last': o5.get('last') if isinstance(o5, dict) else None,
                'own5_at': o5.get('at') if isinstance(o5, dict) else None,
                'twc': T.get('twc_max'), 'twc_now': T.get('twc_now'), 'twc_corr': bool(T.get('twc_corroborated')),
                'apt': T.get('apt_max'), 'six': T.get('six_max'), 'six_win': T.get('six_window'),
                'cli': T.get('cli_max'), 'cli_at': T.get('cli_at'), 'cli_final': bool(T.get('cli_final'))},
        'models': T.get('models') or None,
        'rg': T.get('regime') or None,
        'ladder': [[r.get('label'), r.get('ask'), r.get('ysize'), r.get('nask'), r.get('nsize'), r.get('market'), r.get('ours')]
                   for r in (T.get('ladder') or [])],
        'tom': ({'pick': tm.get('pick'), 'p': tm.get('p'), 'pred': tm.get('pred'),
                 'bet': ({'dir': tm['bet']['dir'], 'label': tm['bet']['label'], 'price': tm['bet']['price'], 'ev': tm['bet']['ev']} if tm.get('bet') else None)}
                if tm else None),
    }


# THE TUNABLE CONSTANTS (2026-09-07). Three module-level knobs the weekly
# tuner may now set PER CITY: cfg['_globals'] carries them, run_market puts
# them in force for that city's run and restores them after, and every lock
# stamps them in params. Their typed values remain the defaults.
TUNABLE_GLOBALS = {'bias_k': 'BIAS_K', 'swing_damp': 'SWING_DAMP', 'sd_floor': 'SD_FLOOR'}


def apply_globals(cfg):
    saved = {}
    for k, name in TUNABLE_GLOBALS.items():
        if k in (cfg.get('_globals') or {}) and cfg['_globals'][k] is not None:
            saved[name] = globals()[name]
            globals()[name] = type(globals()[name])(cfg['_globals'][k])
    return saved


def restore_globals(saved):
    for name, v in saved.items():
        globals()[name] = v


def params_of(cfg):
    p = {'skill': bool(cfg.get('skill', True)), 'bias_hl': cfg.get('bias_hl'), 'sd_mult': cfg.get('sd_mult', 1.0)}
    p.update({k: v for k, v in (cfg.get('_globals') or {}).items() if k in TUNABLE_GLOBALS})
    return p


def run_market(cfg, ticker_cache=TICKER_CACHE):
    saved = apply_globals(cfg)
    try:
        return _run_market(cfg, ticker_cache)
    finally:
        restore_globals(saved)


def _run_market(cfg, ticker_cache=TICKER_CACHE):
    dry = '--dry' in sys.argv
    _TL.cfg = cfg
    _CALIB[cfg['key']] = measured_calib(cfg) or {}
    now = local_now(cfg)
    print('--- %s (%s) ---' % (cfg['key'], cfg.get('city', '')))
    today = now.date()
    tkey = today.isoformat()
    OUT = os.path.join(HERE, '..', cfg['out'])

    rows = fetch_market(cfg, event_ticker(cfg, today))
    if not rows:
        print('%s: no open market for %s' % (cfg['key'], event_ticker(cfg, today)))
        return 0

    span = RESID_M + BIAS_K + 6
    want = models_for(cfg)
    try:
        fcm = {m: v for m, v in forecast_runs(cfg, span, want).items() if v}
        for m in want:
            if m not in fcm:
                print('model %s returned nothing' % m)
    except Exception as e:
        print('batched forecast failed (%s); falling back to one call per model' % e)
        fcm = {}
        for m in want:
            try:
                fcm[m] = forecast_runs(cfg, span, m)
            except Exception as e2:
                print('model %s unavailable: %s' % (m, e2))
    if not fcm:
        print('no forecast models available')
        return 0
    fc = fcm.get(MODELS[0]) or list(fcm.values())[0]
    # the observation window is deliberately wider than the model window: it also
    # has to score history entries (kept 120 days) and backfill against every
    # settled market Kalshi still lists. At 15 KB the extra span is free.
    daily = daily_series(cfg, today - datetime.timedelta(days=200), today)
    # THE EXCHANGE OUTRANKS THE FEED. Fetched once here and reused for the
    # pending-day scoring below, so this costs no extra call. Everything fitted
    # downstream -- the rolling bias, the peak offset, the residual spread, the
    # backfill -- now takes its actuals from a series that agrees with what
    # actually paid, rather than from whatever IEM has revised since.
    # THREE SOURCES FOR `daily`, APPLIED LEAST AUTHORITATIVE FIRST.
    #
    #   IEM daily          the feed, and it REVISES -- 41/44 against settlement
    #   NWS CLI, final     the settlement instrument itself -- 111/112
    #   expiration_value   what the exchange actually paid -- true by definition
    #
    # Each overwrites the one before it, so a day ends up carrying the best
    # figure available for it: the exchange's own number once it has settled,
    # the climate report before that, the feed only where neither exists.
    #
    # The CLI layer is what fills the gap that used to hurt. A day is FINAL in
    # the climate report at about 2:30 AM but does not settle until 7 or 8 AM,
    # and IEM may never agree with it at all -- New York 2026-09-03 and 09-04
    # both settled 84 while IEM still says 83.0 today. Those days feed the
    # rolling bias, the peak offset and the residual spread, so scoring them
    # against a feed that drifted is a slow leak into every fitted quantity.
    #
    # PRELIMINARIES ARE NOT USED HERE. Their window closes at 4 PM local and New
    # York peaks later often enough to make them wrong 1 day in 3. They are used
    # only as a floor for TODAY, below, where being a lower bound is all that is
    # asked of them.
    _cli = {}
    _portal_today = portal_day(cfg, today)
    _portal_yday = portal_day(cfg, today - datetime.timedelta(days=1))
    if _portal_today or _portal_yday:
        print('%s portal: today %s, yesterday %s' % (cfg['key'],
              _portal_today.get('status'), (_portal_yday.get('status'), _portal_yday.get('max'))))
    try:
        _cli = cli_read(cfg)
    except Exception as e:
        print('%s cli unavailable (%s)' % (cfg['key'], e))

    # THE FIRST NON-PRELIMINARY CLIMATE REPORT, WHICH IS THE TARGET VARIABLE.
    #
    # This model exists to predict what the exchange will PAY, and the rulebook
    # pays on the first official non-preliminary report, explicitly ignoring
    # later revisions. So that -- not the physically-corrected figure -- is the
    # quantity `daily` has to carry. cli_read() keeps the first final for exactly
    # this reason; see the note there on Miami 2026-08-29, where the NWS issued
    # 90 at 4:24 AM and corrected it to 85 forty-six minutes later, and the
    # exchange paid 90.
    #
    # Applied BEFORE settle_corrected, because expiration_value is what actually
    # paid and is therefore the last word on a settled day. This layer's job is
    # the days in between: a day is final in the climate report at ~2:30 AM but
    # does not settle until 7 or 8 AM, and IEM may never agree with either --
    # New York 09-03 and 09-04 both paid 84 while IEM still says 83.0 today.
    # Those days feed the rolling bias, the peak offset and the residual spread.
    #
    # A disagreement bigger than a rounding step is NAMED rather than absorbed.
    # With the first-final rule in place there should be none; if one appears it
    # means either the rule has changed or a report was reissued in a way this
    # does not model, and that is worth seeing rather than smoothing.
    _ncli = 0
    for _k, _v in _cli.items():
        if not _v.get('final') or _v.get('max') is None:
            continue
        _was = daily.get(_k)
        if _was is not None and abs(_was - _v['max']) > 1e-9:
            _ncli += 1
            if abs(_was - _v['max']) > 1.5:
                print('  LARGE climate-report correction %s: %.1f -> %.1f'
                      % (_k, _was, _v['max']))
        daily[_k] = _v['max']
    if _ncli:
        print('%s: %d day(s) set from the first final climate report' % (cfg['key'], _ncli))

    _settled = {}
    try:
        _settled = fetch_settled(cfg)
    except Exception as e:
        print('settled markets unavailable (%s); scoring on the feed alone' % e)
    daily, _nfix = settle_corrected(daily, _settled)
    if _nfix:
        print('%s: %d day(s) corrected to the settlement' % (cfg['key'], _nfix))

    # end one day AHEAD: asos.py treats the end date as the cut-off, so asking
    # for `today` returns almost nothing for today
    ob_last = []
    obh = obs_hourly_range(cfg, today - datetime.timedelta(days=span),
                           today + datetime.timedelta(days=1), sink=ob_last)

    # LIVE METAR OVER THE TOP OF TODAY. IEM stays the archive and the settlement
    # truth; this only fills in hours it has not published yet, and only where
    # the reading is actually higher or missing. A running peak can only go up,
    # so max() is the safe merge -- it can never talk the floor down.
    # THE BOUNDARY, AND IT MATTERS MORE THAN THE FRESHNESS.
    #
    # The rulebook settles on the official daily figure -- CLINYC via The
    # Weather Company -- and IEM's daily max_temp_f is the proxy that has
    # matched it on every settled market checked. That is the truth this record
    # is scored against, and METAR IS NOT IT. An hourly observation is a
    # different measurement: it misses the intra-hour peak, which is the entire
    # reason HOURLY_PEAK_OFFSET exists.
    #
    # So this merge writes obh (the forecast's running floor) and ob_last (the
    # reading shown as "now"). It must never write `daily`. A model that is
    # fresher is worth having; a model scored against a number the exchange does
    # not use is worth nothing, however fresh.
    #
    # The offset stays honest too: it is fitted on PAST days, where obh is still
    # IEM-only, so today's METAR rows cannot drift the calibration.
    _mt = metar_today(cfg, today)
    if _mt:
        _cur = obh.setdefault(tkey, {})
        _new = sorted(h for h in _mt if h not in _cur)
        # IEM PUBLISHES WHOLE DEGREES F, AND max() TREATS ITS ROUNDING AS A
        # HIGHER READING. Both feeds carry the same observation -- the ASOS's
        # Celsius tenth -- but IEM rounds it first, so on 2026-09-08 hour 13
        # IEM's 79.00 beat the METAR's exact 78.98 and the panel printed a
        # settled-looking 79. When the two are within half a degree they ARE
        # the same reading and the unrounded one is the true value; only a
        # genuinely higher IEM figure (a different or later observation) still
        # wins. The running peak can still only rise on new information.
        for _h, _v in _mt.items():
            _old = _cur.get(_h)
            if _old is None or _v > _old or abs(_old - _v) < 0.5:
                _cur[_h] = _v
        # and the reading the panel prints as "now": prefer the newer one
        _lh = max(_mt)
        _iem_h = int(ob_last[0][0][11:13]) if ob_last else -1
        _iem_d = ob_last[0][0][:10] if ob_last else None
        # same rule for the reading printed as "now": the METAR wins its own
        # hour outright, not only when it is an hour IEM has not reached
        if (not ob_last or _iem_d != tkey or _lh > _iem_h
                or (_lh == _iem_h and abs(ob_last[0][1] - _mt[_lh]) < 0.5)):
            ob_last[:] = [('%s %02d:51' % (tkey, _lh), _mt[_lh])]
        print('%s metar: %d hours, %s ahead of IEM%s'
              % (cfg['key'], len(_mt),
                 ('%d' % len(_new)) if _new else 'none',
                 (' (' + ', '.join('%d:00' % h for h in _new) + ')') if _new else ''))

    # AND THE SETTLEMENT FEED'S OWN RUNNING MAX, WHICH OUTRANKS BOTH OF THEM.
    # METAR and IEM measure the station; this is the figure the rulebook names.
    # It is folded into `live` below rather than into obh, because it is a daily
    # maximum and not an hourly observation -- the same shape as daily.py's
    # running figure, and used in exactly the same place.
    _twc = twc_today(cfg, today)
    if _twc.get('max') is not None:
        print('%s twc: max %.0f (history %s, max7 %s), now %s'
              % (cfg['key'], _twc['max'],
                 ('%.0f' % _twc['hist']) if _twc.get('hist') is not None else '-',
                 ('%.0f' % _twc['max7']) if _twc.get('max7') is not None else '-',
                 ('%.0f' % _twc['now']) if _twc.get('now') is not None else '-'))
    else:
        print('%s twc: no reading; floor falls back to the station' % cfg['key'])

    # AND THE STATION'S OWN SIX-HOUR MAXIMUM, which unlike TWC is measured
    # rather than published: 98.1% exact against the exchange's settled figure
    # over 322 market-days, mean error -0.06degF. This one does go in the floor.
    _six = metar_six_max(cfg, today)
    if _six is not None:
        print('%s six-hourly: %.1f degF from the ASOS max groups' % (cfg['key'], _six))
    _own5 = own5_max(cfg, today)
    if _own5 is not None:
        print('%s own 5-minute feed: %.1f degF since 7 AM at %s' % (cfg['key'], _own5, OWN5[cfg['key']]))
    # the two published forecasts, for the trail (see twc_forecast)
    _twcf = twc_forecast(cfg, today)
    _nwsf = nws_forecast(cfg, today)
    if _twcf is not None or _nwsf is not None:
        print('%s published forecasts: TWC %s, NWS %s' % (
            cfg['key'], ('%.0f' % _twcf) if _twcf is not None else '-',
            ('%.0f' % _nwsf) if _nwsf is not None else '-'))


    bias, nb = rolling_bias(fc, daily, tkey)
    if bias is None:
        print('not enough scored history for a bias (%d days)' % nb)
        return 0
    bias_of = biases_factory(fcm, daily, cfg.get('skill', True), cfg.get('bias_hl'))
    h0_of = lambda k: climate_day_start(
        cfg, datetime.date(*map(int, k.split('-'))))

    # Per-station offset, measured from this market's own history. Assigned to
    # the module globals because the constant is read from every layer of the
    # calculation -- the floor, the residuals, the backfill -- and threading it
    # through all of them would be a large refactor for no behavioural gain.
    # Markets run sequentially, so each sets its own before computing anything.
    global HOURLY_PEAK_OFFSET, OFFSET_SD, SD_MULT
    HOURLY_PEAK_OFFSET, OFFSET_SD = OFFSET_DEFAULT, OFFSET_SD_DEFAULT
    # the market's own spread multiplier (an env override wins, for backlog tests)
    SD_MULT = float(os.environ.get('BV_SD_MULT') or cfg.get('sd_mult', 1.0) or 1.0)
    off_n = 0
    _m = measure_offset(cfg, obh, daily, h0_of)
    if _m:
        HOURLY_PEAK_OFFSET, OFFSET_SD, off_n = _m
        print('%s offset measured: +%.2f degF, sd %.2f, over %d days (defaults %.2f/%.2f)'
              % (cfg['key'], HOURLY_PEAK_OFFSET, OFFSET_SD, off_n,
                 OFFSET_DEFAULT, OFFSET_SD_DEFAULT))
    prior_days = sorted(k for k in fc if k < tkey and k in daily
                        and len(fc[k]) >= 20)[-BIAS_K:]
    biases = bias_of(prior_days)

    # TODAY'S FLOOR USES THE BEST DATA AVAILABLE, not the same estimate history
    # is stuck with. daily.json carries the station's true running max, which is
    # the settlement source's own figure; the hourly stream plus an offset is
    # only an approximation of it, and approximating when the real number is in
    # hand put 22% on a bracket the day had already passed. History has no
    # choice -- there is no archived running max -- so the residuals keep using
    # the estimate, and the resulting spread is mildly conservative. Take
    # whichever is higher: both are lower bounds on where the day ends up.
    h0 = climate_day_start(cfg, today)
    rmax = running_max(obh, tkey, now.hour, h0)
    est = (rmax + HOURLY_PEAK_OFFSET) if rmax is not None else None
    # THE SETTLEMENT SENSOR'S OWN 5-MINUTE MAXIMUM, plus the measured gap to the
    # true one-minute peak (0.2-0.4 degF), is a better estimate of where the
    # day already stands than the hourly report plus its 0.8 allowance. The
    # hourly stream hides 1.1-1.3 degF on an average day; the 5-minute feed
    # hides a third of that (one-minute archive, 2026-09-06).
    _pk = peak_stats(cfg)
    _gap5 = _pk.get('gap5_mean') if _pk.get('gap5_mean') is not None else 0.3
    _gap5_sd = _pk.get('gap5_sd') if _pk.get('gap5_sd') is not None else 0.45
    _est5 = (_own5 + _gap5) if _own5 is not None else None
    if _est5 is not None and (est is None or _est5 > est):
        est = _est5
    live = daily.get(tkey)
    # TWC IS SHOWN, NOT TRUSTED. It was briefly folded into this floor and that
    # was a mistake, caught by backtest before it could cost anything.
    #
    # Scored on 315 settled market-days -- all seven cities, 45 days each --
    # against the bracket that actually PAID:
    #
    #     raw IEM daily              310/315   98.4%
    #     TWC obs, calendar day      185/315   58.7%
    #     TWC obs, climate day       185/315   58.7%
    #
    # The two windows score IDENTICALLY, so the midnight-hour question that
    # prompted this is answered and it is not the problem: dropping that hour
    # changes no day in 315. The feed is.
    #
    # Six of the seven cities are never HIGH -- Chicago, Miami, Austin, Denver,
    # Los Angeles and Philadelphia run 0.6 to 1.1 degF LOW with a hard ceiling
    # at 0.0, the exact signature of hourly spot sampling missing an intra-hour
    # peak. That is the same deficiency as the METAR this already reads, so TWC
    # adds nothing there.
    #
    # NEW YORK IS THE OUTLIER, AND IT IS THE MARKET THAT MATTERS MOST HERE:
    # mean +1.18 degF, high on 32 of 45 days, and the tail is not small --
    #
    #     2026-08-10   TWC 91   IEM 85.0   settled "88 or below"
    #     2026-08-03   TWC 84   IEM 80.0   settled 80-81
    #     2026-09-03   TWC 87   IEM 83.0   settled 83-84
    #
    # A floor above the settled bracket does not mis-weight the winning rung, it
    # DELETES it. Fitting each city's own mean bias and subtracting it makes
    # things WORSE, not better -- 147/315, 46.7% -- so the error is not an
    # offset to calibrate away, it is noise with a fat upper tail on the one
    # station we care about most.
    #
    # So the feed carries spurious highs and cannot be a lower bound. What it is
    # good for is TIMING -- on 2026-09-05 it published 79 around 16:25 ET, the
    # market repriced within minutes, and IEM daily did not carry 79 until an
    # hour later. That is worth seeing on the panel, and it is worth nothing in
    # the arithmetic until the spikes can be told from the scoops.
    #
    # THE REAL FAULT THAT DAY WAS IEM DAILY'S LATENCY, not its accuracy.

    # TODAY'S CLIMATE REPORT IS A FLOOR TOO, preliminary or not. Its value is a
    # maximum measured over a window inside the day, so it cannot structurally
    # exceed the day's maximum -- the same argument that makes the six-hourly
    # group safe, and the reason a preliminary that is unfit for SCORING is
    # perfectly fit for BOUNDING. On 2026-09-05 New York's 4:43 PM run already
    # carried 79 at 2:33 PM, three hours before the six-hourly group covering
    # that peak was published at 7:51 PM.
    _ctoday = (_cli.get(tkey) or {}).get('max')
    if _ctoday is not None:
        live = _ctoday if live is None else max(live, _ctoday)
        print('%s climate report today: %.1f%s' % (cfg['key'], _ctoday,
              '' if (_cli.get(tkey) or {}).get('final') else ' (preliminary)'))

    # SO THIS IS THE ONE THAT GOES IN. The six-hourly group is the same class of
    # thing as `live` -- an exact running maximum for the day, not an estimate
    # needing HOURLY_PEAK_OFFSET -- and it is independent of IEM, so it also
    # catches the days IEM has plain wrong. max() because both are lower bounds.
    #
    # Everything downstream reads `live`: the floor, the exactness test that
    # collapses the spread, and snapshot()'s locks. Folding it in here is the
    # whole change, exactly as it was for TWC -- the difference is that this one
    # earned it on 322 days instead of being assumed.
    if _six is not None:
        live = _six if live is None else max(live, _six)
    # the settlement sensor's own 5-minute maximum: exact, and fresher than
    # the hourly report by up to 55 minutes
    if _own5 is not None:
        live = _own5 if live is None else max(live, _own5)
    # TWC'S RUNNING MAXIMUM, CORROBORATED. Alone it is a warning (Chicago read
    # 86 against a settled 83 on 2026-09-05). But on 2026-09-06 it printed 75
    # for Central Park at 3:05 PM with LaGuardia's 5-minute feed already at
    # 75.2 since 2:30, and the 3:51 report read 75: two independent sources
    # agreeing is a different thing from one blended field. When an airport's
    # own maximum has reached TWC's figure, the figure goes into the floor.
    _twcmax = (_twc or {}).get('max') if isinstance(_twc, dict) else None
    _aptmax = apt_max(cfg, today) if OWN5.get(cfg['key']) is None else None
    # and never a spike: the one-minute archive puts the true peak at most 4 F
    # above the hourly maximum in New York (90th percentile 3); Chicago's bad
    # 86 sat 3 above its hourly 83. A corroborated figure more than 2 above the
    # hourly stream is still treated as a warning. Newark runs warm, so the
    # airport check alone is not enough.
    _twc_corr = (_twcmax is not None and _aptmax is not None and _aptmax >= _twcmax - 1e-9
                 and rmax is not None and _twcmax <= rmax + 2.0 + 1e-9)
    if _twc_corr:
        live = _twcmax if live is None else max(live, _twcmax)
        print('%s TWC running max %.0f corroborated by an airport at %.1f: counted as a floor' % (cfg['key'], _twcmax, _aptmax))
    cands_fl = [x for x in (est, live) if x is not None]
    obs_far = max(cands_fl) if cands_fl else None
    obs_hr = max(obh.get(tkey) or {0: 0}) if obh.get(tkey) else None
    # WHEN the day's highest reading came, not just through which hour the
    # running maximum was computed: "PEAK · BY 9 AM" read as if the peak had
    # been at 9 when it was the 1 AM reading. The hour of the warmest hourly
    # reading inside the climate day, and the reading itself, so the panel can
    # say "69.1 read at 1 AM" beside the offset-corrected estimate.
    obs_peak_hour, obs_peak_read = None, None
    _day_obs = {h: v for h, v in (obh.get(tkey) or {}).items() if h0 <= h <= now.hour}
    if _day_obs:
        obs_peak_hour = max(_day_obs, key=lambda h: (_day_obs[h], -h))
        obs_peak_read = _day_obs[obs_peak_hour]
    yday = daily.get((today - datetime.timedelta(days=1)).isoformat())
    # the remaining-hours cut-off is the CLOCK hour, matching residuals(), not
    # whichever hour last reported -- otherwise the spread is measured for one
    # horizon and applied to another
    hr0 = now.hour
    rest = [v for h, v in (fc.get(tkey) or {}).items() if h >= hr0]
    fpeak = max(rest) if rest else None
    fadj = point_forecast(fcm, biases, tkey, hr0, yday)
    # WHEN the runs put the peak. At or after 4 PM local it lands after the
    # 1:51 PM six-hour group and the 4 PM preliminary report, so every intraday
    # reading understates the final. The panel says so in the briefing.
    _ph = []
    for _m, _fc in fcm.items():
        _day = _fc.get(tkey) or {}
        _late = [(v, h) for h, v in _day.items() if h >= hr0]
        if _late:
            _ph.append(max(_late)[1])
    peak_hour = int(statistics.median(_ph)) if _ph else None
    # MODEL VERSUS SENSOR, RIGHT NOW. The runs' expected temperature at the
    # latest observed hour against what the station actually read. On
    # 2026-09-06 Las Vegas had a monsoon cell over the airport at 1 PM (78 in
    # rain) while the runs expected the high 80s and the bake held 67% on
    # 86-87; nothing on the sheet said the runs were eight degrees warm. This
    # does not move the forecast (a nudge toward the current reading measured
    # WORSE on New York); it makes the gap visible so the plan can be doubted.
    model_resid = None
    _hobs = {h: v for h, v in (obh.get(tkey) or {}).items() if h0 <= h <= now.hour and v is not None}
    if _hobs:
        _hh = max(_hobs)
        _exp = [x for x in ((fc.get(tkey) or {}).get(_hh) for fc in fcm.values()) if x is not None]
        if _exp:
            _e = statistics.median(_exp)
            model_resid = {'hour': _hh, 'expected': round(_e, 1), 'observed': round(_hobs[_hh], 1),
                           'diff': round(_hobs[_hh] - _e, 1)}

    # TWC'S OWN FORECAST JOINS WHEN THE RECORD SAYS SO (2026-09-07). Its daily
    # maximum has been logged against ours every morning since 09-06; once
    # twenty graded mornings show it beating the consensus by 0.15 F of mean
    # miss in this city, the two are averaged. Measured, per city, from the
    # previous bake's record.published -- never switched on by hand.
    twc_in = False
    try:
        _pub = ((load_log(OUT).get('record') or {}).get('published') or {})
        _po, _pt = (_pub.get('ours') or {}), (_pub.get('twc') or {})
        if fadj is not None and _twcf is not None and (_pt.get('n') or 0) >= 20 \
                and _po.get('mae') is not None and _pt.get('mae') is not None and _pt['mae'] <= _po['mae'] - 0.15:
            fadj = round(0.5 * fadj + 0.5 * float(_twcf), 2)
            twc_in = True
            print('%s TWC forecast joins the consensus (%.2f vs ours %.2f over %d mornings)' % (cfg['key'], _pt['mae'], _po['mae'], _pt['n']))
    except Exception as e:
        print('twc roster check skipped (%s)' % e)
    cands = [x for x in (obs_far, fadj) if x is not None]
    if not cands:
        print('no forecast and no observations yet')
        return 0
    pred = max(cands)
    if not (-40.0 < pred < 130.0):
        print('%s: implausible prediction %.1f -- refusing to write' % (cfg['key'], pred))
        return 0
    res = residuals(fcm, bias_of, daily, obh, now.hour, tkey, h0_of)
    # same rule as snapshot(): no forecast hour left in the day is the strongest
    # binding case, and `obs_far >= fadj` failed against a None fadj -- so a
    # finished day kept a full forecast spread. Distinguish "the day is over"
    # from "the forecast never arrived", which must not collapse anything.
    over_now = bool(fc.get(tkey)) and not [h for h in (fc.get(tkey) or {}) if h >= hr0]
    binding_now = (obs_far is not None
                   and ((fadj is not None and obs_far >= fadj) or over_now))
    sd, nsd = spread(res, now.hour, binding_now)
    if binding_now:
        exact_now = (live is not None and obs_far <= live + 1e-9)
        sd = min(sd, max(EXACT_FLOOR_SD_MIN,
                         math.sqrt(max(sd * sd - OFFSET_SD * OFFSET_SD, 0.0)))
                     if exact_now else OFFSET_SD)
        if _est5 is not None and _est5 >= (rmax + HOURLY_PEAK_OFFSET if rmax is not None else -99):
            # the floor came from the 5-minute feed: what is hidden is the
            # 5-minute gap, not the hourly one
            sd = min(sd, max(EXACT_FLOOR_SD_MIN, _gap5_sd))
    # PEAK BEHIND US: how likely the day's high has already happened at this
    # hour, from the one-minute archive, conditioned on cooling when the
    # latest reading sits a degree under the running max. Reported for the
    # sheet; and when it is near-certain AND the 5-minute feed is in hand, the
    # remaining spread is the 5-minute gap even if the runs still hope for more.
    peak_done = None
    _bhs = _pk.get('by_hour') or {}
    # the archive table runs 10:00-21:00; past its last hour the day is only
    # more decided, so the last row stands in (New York at 10 PM had no row,
    # so no peak-behind-us figure and no day-decided collapse)
    _bh = _bhs.get(str(now.hour)) or (_bhs.get(str(max(int(h) for h in _bhs))) if _bhs and now.hour > max(int(h) for h in _bhs) else None)
    if _bh:
        _lastv = None
        _hobs2 = {h: v for h, v in (obh.get(tkey) or {}).items() if h0 <= h <= now.hour and v is not None}
        if _hobs2:
            _lastv = _hobs2[max(_hobs2)]
        _o5 = own5_row(cfg, today) if _own5 is not None else None
        if _o5 and isinstance(_o5.get('last'), (int, float)):
            _lastv = _o5['last']
        _runmax = max(x for x in (rmax, _own5) if x is not None) if (rmax is not None or _own5 is not None) else None
        cooling = (_lastv is not None and _runmax is not None and _lastv <= _runmax - 1.0)
        pd = _bh.get('p_done_cooling') if (cooling and _bh.get('p_done_cooling') is not None and (_bh.get('n_cooling') or 0) >= 8) else _bh.get('p_done')
        peak_done = {'p': pd, 'cooling': cooling, 'hour': now.hour, 'n': _pk.get('n_days'),
                     'last': _lastv, 'run_max': _runmax}
        if pd is not None and pd >= 0.85 and _est5 is not None and not binding_now:
            sd = min(sd, max(EXACT_FLOOR_SD_MIN, _gap5_sd + 0.15))
            print('%s peak behind us %.0f%% at %d:00 with the 5-minute feed in hand: spread %.2f' % (cfg['key'], 100 * pd, now.hour, sd))
    # THE DAY IS DECIDED. The six-hour group is the ASOS's own maximum over
    # its window -- exact, not sampled -- so once the group that carried the
    # day's high has landed, every hourly reading since has stayed a degree
    # under it, and the one-minute archive puts the peak behind us, what is
    # left is the rounding of that exact figure. New York 2026-09-06 at
    # 10 PM: the 2-8 PM group read 75.9, the station 66 and falling, and the
    # sheet still said 79% on 75-76 with a fifth of the mass above 76.5, the
    # hidden-peak spread applied to a peak that was no longer hidden.
    day_decided = False
    _sw = SIX_WINDOW.get(cfg['key'])
    if _six is not None and _sw and now.hour >= _sw[1] and peak_done and peak_done.get('cooling') \
            and (peak_done.get('p') or 0) >= 0.95:
        _after = [v for h, v in (obh.get(tkey) or {}).items() if h > _sw[1] and v is not None]
        _since_ok = all(v <= _six - 0.5 for v in _after)
        _hourly_ok = (rmax is None or rmax <= _six + 0.5)
        if _since_ok and _hourly_ok:
            day_decided = True
            # the exact figure: the group, or the settlement sensor's own 5-minute
            # maximum where that runs higher (the group is whole-degree Celsius)
            _exact = max(x for x in (_six, _own5) if x is not None)
            pred, obs_far, sd = _exact, _exact, 0.15
            print('%s day decided: the %d-%d group read %.1f, readings since a degree under, peak behind us %.0f%% -- spread %.2f'
                  % (cfg['key'], _sw[0], _sw[1], _six, 100 * (peak_done.get('p') or 0), sd))
    res_lock = residuals(fcm, bias_of, daily, obh, LOCK_HOUR, tkey, h0_of)
    sd_lock, _ = spread(res_lock, LOCK_HOUR, binding_now)

    try:
        fresh_peaks = fresh_runs(cfg, hr0)
    except Exception as e:
        fresh_peaks = None
        print('fresh runs unavailable: %s' % e)

    ps = distribution(rows, pred, sd, obs_far)
    best = max(range(len(rows)), key=lambda i: ps[i])
    mbest = max(range(len(rows)), key=lambda i: rows[i]['mid'])

    # ---- TOMORROW: the plan, not the call -----------------------------------
    # Kalshi opens the next day's ladder in the afternoon, so most evenings there
    # is already a market to look at. Worth showing because the ladder is placed
    # off a model forecast and inherits its warm bias, which is exactly where a
    # disagreement worth acting on tends to sit.
    tom = None
    try:
        tdate = today + datetime.timedelta(days=1)
        tkey2 = tdate.isoformat()
        trows = fetch_market(cfg, event_ticker(cfg, tdate))
        tp = point_forecast(fcm, biases, tkey2, 0, daily.get(tkey) or obs_far)
        # THE PREP, WHETHER OR NOT THE LADDER EXISTS. Before ~10 AM there is no
        # market for tomorrow and the column stood empty. What a reader can
        # prepare against without a ladder: our number, each run's own peak,
        # the two official forecasts, and when the peak comes.
        t_models, t_ph = {}, []
        for _m, _fc in fcm.items():
            _day = _fc.get(tkey2) or {}
            if len(_day) >= 20 and biases.get(_m) is not None:
                _pk = max(_day.items(), key=lambda kv: kv[1])
                t_models[_m] = round(_pk[1] - biases[_m], 1)
                t_ph.append(_pk[0])
        t_prep = {'pred': round(tp, 2) if tp is not None else None, 'sd': TOMORROW_SD,
                  'models': t_models,
                  'peak_hour': int(statistics.median(t_ph)) if t_ph else None,
                  'twc_fc': twc_forecast(cfg, tdate), 'nws_fc': nws_forecast(cfg, tdate)}
        if trows and tp is not None and -40.0 < tp < 130.0:
            tps = distribution(trows, tp, TOMORROW_SD, None)
            tb = max(range(len(trows)), key=lambda i: tps[i])
            tm = max(range(len(trows)), key=lambda i: trows[i]['mid'])
            tom = {
                'date': tkey2, 'event': event_ticker(cfg, tdate),
                'state': market_state(cfg, trows, now),
                'pred': round(tp, 2), 'sd': TOMORROW_SD,
                'pick': trows[tb]['label'], 'p': round(tps[tb], 4),
                'market_pick': trows[tm]['label'], 'market_p': trows[tm]['mid'],
                'agree': tb == tm,
                'ladder': [{'label': r['label'], 'lo': r['lo'], 'hi': r['hi'],
                            'ticker': r.get('ticker'),
                            'ours': round(pp, 4), 'market': r['mid'],
                            'bid': r['bid'], 'ask': r['ask'],
                            'nbid': r['nbid'], 'nask': r['nask'],
                            'ysize': r['ysize'], 'nsize': r['nsize'],
                            'vol': r['vol']}
                           for r, pp in zip(trows, tps)],
                'link': (cfg['url'] + '/' + event_ticker(cfg, tdate).lower())
                        if cfg.get('url') else None,
                # the bet and the book the panel used to compute for itself, so
                # the overnight plan is written down and can be graded
                'bet': best_bet(trows, tps),
                'book': lock_book(trows, tps),
            }
            tom.update({k: v for k, v in t_prep.items() if k not in ('pred', 'sd')})
        elif not trows:
            tom = {'date': tkey2, 'event': event_ticker(cfg, tdate),
                   'state': market_state(cfg, [], now), 'ladder': []}
            tom.update(t_prep)
    except Exception as e:
        print('tomorrow unavailable: %s' % e)

    log = load_log(OUT)
    hist = {h['date']: h for h in log.get('history', [])}

    # THE OVERNIGHT PLAN, LOGGED (2026-09-07). Tomorrow's bet was arithmetic in
    # the panel and never written down, so nothing could say whether the
    # evening draft pays or how often the morning reverses it. The first bake
    # at or after EVE_HOUR local writes it once into tomorrow's entry; the
    # day's scoring grades it beside the noon lock (eve_result, eve_hit).
    if tom and tom.get('bet') and now.hour >= EVE_HOUR:
        e2 = hist.get(tom['date']) or {'date': tom['date'], 'event': tom['event']}
        if 'eve' not in e2:
            e2['eve'] = {'at': now.strftime('%H:%M'), 'pred': tom.get('pred'), 'sd': tom.get('sd'),
                         'pick': tom.get('pick'), 'p': tom.get('p'),
                         'market_pick': tom.get('market_pick'), 'market_p': tom.get('market_p'),
                         'bet': tom['bet'], 'book': tom.get('book')}
            hist[tom['date']] = e2
            print('%s overnight plan logged for %s: %s %s at %.2f' % (
                cfg['key'], tom['date'], tom['bet']['dir'], tom['bet']['label'], tom['bet']['price']))

    if '--backfill' in sys.argv:
        added = 0
        for h in backfill(fcm, bias_of, daily, obh, (_settled or fetch_settled(cfg)), sd_lock,
                          cfg=cfg, h0_of=h0_of, res=res_lock):
            if h['date'] not in hist:
                hist[h['date']] = h
                added += 1
        print('backfilled %d day(s)' % added)

    # ---- lock one decision per day, at or after noon ET, never overwritten
    entry = hist.get(tkey)
    if entry is None:
        entry = {'date': tkey, 'event': event_ticker(cfg, today)}
        hist[tkey] = entry
    def snapshot(hour):
        """Our call AS OF `hour` today, however late the job actually runs.

        Cron slots get delayed or skipped, and a noon lock computed from 2pm
        data would quietly be hindsight rather than a forecast. So the lock is
        rebuilt for its own hour: the forecast uses only hours from then on,
        and the floor is the running max through then, taken from the hourly
        stream (+ offset) rather than today's live daily max.
        """
        r = running_max(obh, tkey, min(hour, now.hour), h0)
        fl = (r + HOURLY_PEAK_OFFSET) if r is not None else None
        # For an hour already past, the hourly stream only ESTIMATES the running
        # max (it misses the intra-hour peak). But if the hourly series already
        # peaked at or before this hour, the day's true max had been reached by
        # then, so the exact figure applies rather than the estimate. That is
        # not hindsight -- it uses only WHEN the observed peak happened. Without
        # it a 6pm call on a day that peaked at 3pm was built on 83.62 when 84.0
        # was known, landing the estimate a tenth of a degree off a bracket edge.
        oh = obh.get(tkey) or {}
        peak_h = max(oh, key=lambda k: oh[k]) if oh else None
        if live is not None and ((hour >= now.hour)
                                 or (peak_h is not None and peak_h <= hour)):
            fl = live if fl is None else max(fl, live)
        pf = point_forecast(fcm, biases, tkey, hour, yday)
        cand = [x for x in (fl, pf) if x is not None]
        if not cand:
            return None
        pr = max(cand)
        # NO REMAINING HOURS IS THE STRONGEST BINDING CASE, NOT A MISSING ONE.
        # point_forecast returns None once no forecast hour is left in the day,
        # and `fl >= pf` then failed against None -- so the most finished day
        # possible was treated as wide open. Distinguish that from a forecast
        # that never arrived, which must NOT collapse the spread.
        day_has_fc = bool(fc.get(tkey))
        hours_left = len([h for h in (fc.get(tkey) or {}) if h >= hour])
        over = day_has_fc and hours_left == 0
        bind = (fl is not None and ((pf is not None and fl >= pf) or over))
        sdh, _ = spread(residuals(fcm, bias_of, daily, obh, hour, tkey, h0_of), hour, bind)
        # ONCE THE FLOOR BINDS, THE DAY IS OVER.  `bind` means no remaining hour
        # is forecast above what the station has already recorded, so the high is
        # not going to move: the only live question is whether the true peak sat
        # a little above the samples we have. That is the offset's uncertainty,
        # nothing like a forecast's.
        #
        # This used to apply only when daily.json had published an exact figure,
        # and daily.json lags. On a day where it had not, the full forecast
        # spread survived to midnight -- Denver 2026-09-04 closed at 92.7 with
        # the market at 99.5% on 92-93, and this model still put 23% on 94-95
        # and called buying it at 1c a 21c edge. It was betting on a day that had
        # already happened.
        if bind and fl is not None:
            exact = (live is not None and fl <= live + 1e-9)
            sdh = min(sdh, max(EXACT_FLOOR_SD_MIN,
                               math.sqrt(max(sdh * sdh - OFFSET_SD * OFFSET_SD, 0.0)))
                           if exact else OFFSET_SD)
        return pr, sdh, distribution(rows, pr, sdh, fl), fl

    def make_lock(hour):
        snap = snapshot(hour)
        if snap is None:
            return None
        pred, sd, ps, fl = snap
        best = max(range(len(rows)), key=lambda i: ps[i])
        fresh = fresh_peaks or {}
        fadj_fresh = None
        if fresh:
            v = [fresh[m] - biases[m] for m in fresh if biases.get(m) is not None]
            if v:
                fadj_fresh = statistics.mean(v)
                if yday is not None:
                    fadj_fresh -= SWING_DAMP * max(0.0, fadj_fresh - yday)
                if fl is not None:
                    fadj_fresh = max(fadj_fresh, fl)
        return {
            'at': now.strftime('%Y-%m-%dT%H:%M') + ' ET',
            'pick': rows[best]['label'], 'ticker': rows[best]['ticker'],
            'p': round(ps[best], 4), 'pred': round(pred, 2), 'sd': round(sd, 2),
            'as_of': '%02d:00 %s' % (hour, cfg.get('tzlabel', 'ET')),
            'params': params_of(cfg),
            # the hour the PRICES were read. A lock is rebuilt as-of its own
            # hour, but the market quotes can only ever be live ones -- so if the
            # run happens well after the lock hour the forecast is honest and the
            # prices are not, and the head-to-head must skip that day.
            'priced_at': now.hour,
            'bias': round(bias, 2),
            'obs_at_lock': fl,
            'market_pick': rows[mbest]['label'], 'market_p': rows[mbest]['mid'],
            # the day-old run's answer, recorded for comparison -- see fresh_runs()
            'dayahead_peaks': fresh or None,
            'dayahead_pred': round(fadj_fresh, 2) if fadj_fresh is not None else None,
            # bounds are stored with the lock so a past day can be scored even
            # if the live ladder has since changed shape
            'ladder': [{'label': r['label'], 'lo': r['lo'], 'hi': r['hi'],
                        'ours': round(p, 4), 'market': r['mid']}
                       for r, p in zip(rows, ps)],
            # what would actually have been staked, at the prices on the screen
            # at this moment. Only counted later if those prices were live -- see
            # priced_on_time().
            'bet': best_bet(rows, ps),
            'book': lock_book(rows, ps),
        }

    # INTRADAY PRICE TRAIL.  Kalshi's candlestick endpoint 404s, so there is no
    # way to recover how a day's prices moved -- which means the question "when
    # is the market slowest to update, i.e. when is our edge biggest" cannot be
    # answered from history.  So record it going forward: one row per hour with
    # the market's mids, our probabilities and the state of the day.  After a
    # few weeks this is the dataset that answers when to act.
    trail = entry.setdefault('trail', [])
    if not trail or trail[-1].get('h') != now.hour:
        trail.append({
            'h': now.hour,
            'pred': round(pred, 2), 'sd': round(sd, 2),
            'obs': obs_far,
            'ours': [round(p, 3) for p in ps],
            'mkt': [r['mid'] for r in rows],
            # the size of the best available edge at this hour. Prices alone
            # cannot answer "when should the bet go on" -- this can.
            'edge': (lambda b: round(b['ev'], 4) if b else None)(best_bet(rows, ps)),
            # the published forecasts as of this hour, judged forward
            'twc_fc': _twcf, 'nws_fc': _nwsf,
        })
        del trail[:-24]

    # TWO locks, both well before the 11:59pm close, because they answer
    # different questions.  The noon lock is the honest forecast -- the peak is
    # still hours away -- and it is what the skill record scores.  The 18:00
    # lock is the call worth acting on: by then the day has largely resolved
    # and backtested bracket accuracy jumps from 40/68 to 53/68.
    if 'lock' not in entry and now.hour >= LOCK_HOUR:
        L = make_lock(LOCK_HOUR)
        if L:
            entry['lock'] = L
            print('%s LOCKED %s (as of %s): %s (%.0f%%), market %s (%.0f%%)'
                  % (cfg['key'], tkey, L['as_of'], L['pick'], 100 * L['p'],
                     L['market_pick'], 100 * L['market_p']))
    if 'final' not in entry and now.hour >= FINAL_HOUR:
        F = make_lock(FINAL_HOUR)
        if F:
            entry['final'] = F
            print('FINAL %s (as of %s): %s (%.0f%%)'
                  % (tkey, F['as_of'], F['pick'], 100 * F['p']))

    # ---- score any past locked day whose actual has since been published
    #
    # TRUTH BY DEFINITION.  The settled market itself says which bracket paid,
    # so use that rather than re-deriving it from an observation feed. Kalshi
    # resolves on the next-morning NWS Climate Report (CLI, 12:00-11:59 LST);
    # our observation source agrees with it on all 68 settled days checked, but
    # a 1 degF disagreement hides inside a 2 degF bracket, so agreement at the
    # bracket level is weaker evidence than it looks. Scoring on the settlement
    # is exact, and comparing our own figure against it turns any future drift
    # in the feed into a visible flag instead of a silent wrong record.
    # "pending" is any day not yet scored FROM A SETTLEMENT -- not merely a day
    # with no score. Built only for unscored days, the lookup came back empty on
    # every run once the record was full, so the re-examination loop below had
    # nothing to compare against and two wrong legacy days stood for weeks.
    pending = [k for k, h in hist.items()
               if k < tkey and 'lock' in h
               and (h.get('actual') is None or h.get('truth_source') != 'settlement')]
    settled_by_date = {}
    portal_official = {}
    if pending:
        try:
            for evk, lad in (_settled or fetch_settled(cfg)).items():
                try:
                    dk = datetime.datetime.strptime(evk.split('-')[1], '%y%b%d').date()
                except Exception:
                    continue
                w = next((r['label'] for r in lad if r.get('yes')), None)
                if w:
                    settled_by_date[dk.isoformat()] = w
            # THE PORTAL FIRST. For the last few pending days the exchange has
            # not settled, the portal's OFFICIAL figure is the same number
            # hours earlier. It scores the day through the day's own ladder;
            # the row stays re-examinable until the exchange's own settlement
            # confirms it (which it has on every day checked).
            for k in sorted(pending)[-4:]:
                if k in settled_by_date:
                    continue
                pd_ = portal_day(cfg, datetime.date(*map(int, k.split('-'))))
                if pd_.get('official') and pd_.get('max') is not None:
                    lad = (hist[k].get('lock') or {}).get('ladder') or []
                    ai = which(lad, pd_['max']) if lad and lad[0].get('lo', 'x') != 'x' else None
                    if ai is not None:
                        settled_by_date[k] = lad[ai]['label']
                        portal_official[k] = pd_['max']
                        daily[k] = pd_['max']
                        print('portal: %s official %.0f -> %s (exchange not yet settled)'
                              % (k, pd_['max'], lad[ai]['label']))
        except Exception as e:
            print('settled lookup failed (%s) -- falling back to observations' % e)

    # PROVISIONAL SCORES HAVE TO BE REVISITED.  The rulebook resolves on "the
    # first official non-preliminary report", and revisions after that are
    # explicitly not counted -- but a day is often scored here hours before
    # Kalshi settles it, off an observation that can still be revised. Freezing
    # that on first sight, which is what this loop did, meant a provisional
    # score stood forever even once the exchange said otherwise.
    #
    # So: a day scored from a SETTLEMENT is final and never touched again. A day
    # scored from an OBSERVATION is provisional and is re-examined every run
    # until the settlement appears, then rewritten to agree with it.
    # LEGACY ROWS TOO. Days scored before `truth_source` existed carry no
    # flag at all and were treated as final -- and two of them were wrong:
    # 2026-09-02 stood at 70 and 2026-09-03 at 83 while the exchange paid 71
    # and 84 (found 2026-09-06 by diffing the record against every settled
    # expiration_value). A row is final only once it has been scored from a
    # settlement; anything else is re-examined while a settlement exists.
    for k, h in hist.items():
        if k >= tkey or 'lock' not in h:
            continue
        was = h.get('actual')
        if was is not None and not (h.get('truth_source') != 'settlement'
                                    and settled_by_date.get(k)):
            continue
        a = daily.get(k)
        lad = h['lock'].get('ladder') or []
        if not lad or lad[0].get('lo', 'x') == 'x':
            continue                       # pre-bounds lock; nothing to score against
        truth = settled_by_date.get(k)
        ours = None
        if a is not None:
            ai = which(lad, a)
            ours = lad[ai]['label'] if ai is not None else None
        if truth is None:
            if ours is None:
                continue                   # neither settlement nor observation yet
            truth, h['truth_source'] = ours, 'observed'
            h['provisional'] = True
        else:
            # 'portal' = the settlement source's official figure, before the
            # exchange has posted; it is re-examined until the exchange agrees
            h['truth_source'] = 'portal' if k in portal_official else 'settlement'
            h.pop('provisional', None)
            if ours is not None and ours != truth:
                h['feed_mismatch'] = {'observed': a, 'observed_bracket': ours}
                print('FEED MISMATCH %s: settled %s but our observation %.1f says %s'
                      % (k, truth, a, ours))
        # what an upgrade actually changed, kept so a corrected day is visible
        # rather than silently different from what the record showed yesterday
        if was is not None:
            if h.get('actual_bracket') != truth or (a is not None and a != was):
                h['revised'] = {'from_bracket': h.get('actual_bracket'),
                                'from_actual': was, 'was_hit': h.get('hit')}
                print('REVISED %s: provisional %s (%.1f) -> settled %s (%.1f)'
                      % (k, h.get('actual_bracket'), was, truth,
                         a if a is not None else float('nan')))
            else:
                print('confirmed %s: settlement agrees with the provisional score' % k)
        h['actual'] = a
        h['actual_bracket'] = truth
        h['hit'] = (h['lock']['pick'] == h['actual_bracket'])
        h['market_hit'] = (h['lock']['market_pick'] == h['actual_bracket'])
        if h.get('final'):
            h['final_hit'] = (h['final']['pick'] == h['actual_bracket'])
        h['err'] = round(h['lock']['pred'] - a, 2) if a is not None else None
        g = grade_bet(h['lock'].get('bet'), truth)
        if g:
            h['bet_result'] = g
        if h.get('eve') and h['eve'].get('bet'):
            ge = grade_bet(h['eve']['bet'], truth)
            if ge:
                h['eve_result'] = ge
            h['eve_hit'] = (h['eve'].get('pick') == truth)
            h['eve_reversed'] = (h['eve'].get('pick') != h['lock'].get('pick'))
        if h['lock'].get('book'):
            h['book_results'] = []
            _num = _den = 0.0
            for b in h['lock']['book']:
                gb = grade_bet(b, truth)
                if gb:
                    h['book_results'].append(dict(gb, dir=b['dir'], label=b['label'],
                                                  price=b['price'], ev=b['ev']))
                    # what a dollar on this line, held to settlement, returned
                    w = float(b.get('kelly') or 0) or 1.0
                    _num += w * (((1.0 - b['price']) / b['price']) if gb.get('won') else -1.0)
                    _den += w
            if _den:
                h['plan_ret'] = round(_num / _den, 3)
            print('  bet %s %s at %.0fc -> %s, %+.2f on $%d'
                  % (h['lock']['bet']['dir'], h['lock']['bet']['label'],
                     100 * h['lock']['bet']['price'],
                     'WON' if g['won'] else 'lost', g['pl'], BANKROLL))
        print('scored %s: actual %.0f -> %s | ours %s %s | market %s %s'
              % (k, a, h['actual_bracket'], h['lock']['pick'],
                 'HIT' if h['hit'] else 'miss', h['lock']['market_pick'],
                 'HIT' if h['market_hit'] else 'miss'))

    # normalise the flag across days scored before it existed, so "could this
    # still change" is answerable from the record alone
    for h in hist.values():
        if h.get('actual') is None or h.get('backtest'):
            continue
        if h.get('truth_source') == 'observed':
            h['provisional'] = True
        else:
            h.pop('provisional', None)

    scored = [h for h in hist.values() if h.get('actual') is not None and 'lock' in h]

    def is_interior(h):
        """Did the day settle in a bounded 2-degree bracket rather than an
        open-ended tail?  Tails are far easier to hit and flatter the score,
        so they are counted separately."""
        for r in (h.get('lock', {}).get('ladder') or []):
            if r['label'] == h.get('actual_bracket'):
                return r.get('lo') is not None and r.get('hi') is not None
        return False

    def tally(rows):
        e = [h['err'] for h in rows if h.get('err') is not None]
        inner = [h for h in rows if is_interior(h)]
        return {'n': len(rows), 'hits': sum(1 for h in rows if h.get('hit')),
                'mae': round(statistics.mean(abs(x) for x in e), 2) if e else None,
                'bias': round(statistics.mean(e), 2) if e else None,
                'interior_n': len(inner),
                'interior_hits': sum(1 for h in inner if h.get('hit'))}

    live = [h for h in scored if not h.get('backtest')]
    record = tally(scored)
    # live days are the honest score: a decision written down before the fact.
    # backtested days reconstruct what the same model would have picked, from
    # the day-ahead run and a bias fitted only on days already past.
    record['live'] = tally(live)
    record['backtest'] = tally([h for h in scored if h.get('backtest')])
    # THE DRIFT BRAKE (2026-09-07): the last fourteen live days against the
    # backtest's hit rate. A city running a fifth behind sizes at half, a tenth
    # behind at three quarters, until it recovers. Needs five live days to say
    # anything; the panel's two-day gate covers the first week.
    try:
        cutoff = (now.date() - datetime.timedelta(days=14)).isoformat()
        l14 = [h for h in live if h.get('date', '') >= cutoff]
        bt = record['backtest']
        if len(l14) >= 5 and bt.get('n'):
            lh = sum(1 for h in l14 if h.get('hit')) / float(len(l14))
            bh = bt['hits'] / float(bt['n'])
            gap = bh - lh
            record['brake'] = {'f': 0.5 if gap >= 0.2 else 0.75 if gap >= 0.1 else 1.0,
                               'n': len(l14), 'live_hit': round(lh, 3), 'backtest_hit': round(bh, 3)}
    except Exception as e:
        print('brake: skipped (%s)' % e)
    # head-to-head only exists where a contemporaneous market price was captured
    def priced_on_time(h):
        L = h.get('lock') or {}
        pa = L.get('priced_at')
        if pa is None:
            return False              # older locks: provenance unknown, don't count
        try:
            return abs(int(pa) - int(str(L.get('as_of', '')).split(':')[0])) <= 1
        except Exception:
            return False
    h2h = [h for h in live if h.get('market_hit') is not None and priced_on_time(h)]
    record['vs_market'] = {'n': len(h2h),
                           'ours': sum(1 for h in h2h if h.get('hit')),
                           'market': sum(1 for h in h2h if h.get('market_hit'))}
    # P&L, on the same on-time-price rule as the head-to-head: a bet is only
    # real if the quote it was struck at was live when the lock was written.
    # a provisional day's outcome can still be rewritten by the settlement, and
    # the P&L is the one number that should never move backwards, so it counts
    # only days the exchange has actually resolved
    money = [h for h in live
             if h.get('bet_result') and priced_on_time(h) and not h.get('provisional')]
    record['money'] = {
        'n': len(money),
        'wins': sum(1 for h in money if h['bet_result']['won']),
        'staked': round(sum(h['bet_result']['staked'] for h in money), 2),
        'pl': round(sum(h['bet_result']['pl'] for h in money), 2),
    }
    st = record['money']['staked']
    record['money']['roi'] = round(100.0 * record['money']['pl'] / st, 1) if st else None
    # the overnight plans, graded: does the evening draft pay, and how often
    # does the morning reverse it
    _eve = [h for h in hist.values() if h.get('eve_result') and h.get('actual') is not None]
    if _eve:
        _es = sum(h['eve_result']['staked'] for h in _eve)
        record['eve'] = {'n': len(_eve), 'wins': sum(1 for h in _eve if h['eve_result']['won']),
                         'hits': sum(1 for h in _eve if h.get('eve_hit')),
                         'reversed': sum(1 for h in _eve if h.get('eve_reversed')),
                         'ret': round(sum(h['eve_result']['pl'] for h in _eve) / _es, 3) if _es else None}

    # THE PUBLISHED FORECASTS, SCORED. Same days, same truth, same hour: our
    # morning number against TWC's own forecast and the NWS point forecast.
    # This is the forward study that no archive could run -- it measures
    # itself every run, and the panel prints it, so nobody has to remember.
    pub = [h for h in scored if h.get('published') and h.get('actual') is not None
           and h['published'].get('ours') is not None]
    def _mae(key):
        v = [abs(h['published'][key] - h['actual']) for h in pub if h['published'].get(key) is not None]
        return (round(statistics.mean(v), 2), len(v)) if v else (None, 0)
    def _hit(key):
        n = w = 0
        for h in pub:
            v = h['published'].get(key)
            if v is None:
                continue
            lad = (h.get('lock') or {}).get('ladder') or []
            if not lad or lad[0].get('lo', 'x') == 'x':
                continue
            i = which(lad, v)
            n += 1
            w += 1 if (i is not None and lad[i]['label'] == h.get('actual_bracket')) else 0
        return (w, n)
    lh = {}
    for h in scored:
        if h.get('backtest') or not h.get('live_picks') or not h.get('actual_bracket'):
            continue
        for hh, pick in h['live_picks'].items():
            e = lh.setdefault(hh, [0, 0])
            e[1] += 1
            e[0] += 1 if pick == h['actual_bracket'] else 0
    record['live_hours'] = lh
    record['published'] = {
        'n': len(pub),
        'ours': {'mae': _mae('ours')[0], 'hit': _hit('ours')},
        'twc': {'mae': _mae('twc')[0], 'n': _mae('twc')[1], 'hit': _hit('twc')},
        'nws': {'mae': _mae('nws')[0], 'n': _mae('nws')[1], 'hit': _hit('nws')},
        'since': min((h['date'] for h in pub), default=None),
    }

    # CALIBRATION.  The hit rate says how often the top pick lands. It does not
    # say whether the PROBABILITIES can be trusted -- and every bet on this panel
    # is sized off those probabilities, so calibration, not accuracy, is what has
    # to hold for the money to work. A stated 45% that happens 45% of the time is
    # a profitable number even though it loses more often than it wins.
    #
    # Measured over every cell of every scored ladder, not just the pick, because
    # the pick alone is 66 samples clustered at the confident end.
    cal_b = {}
    for h in scored:
        tb = h.get('actual_bracket')
        for r in (h.get('lock', {}).get('ladder') or []):
            p = r.get('ours')
            if p is None:
                continue
            b = min(9, int(p * 10))
            e = cal_b.setdefault(b, [0, 0, 0.0])
            e[0] += 1
            e[1] += 1 if r['label'] == tb else 0
            e[2] += p
    record['calibration'] = {
        'bins': [{'lo': round(b / 10.0, 1), 'n': v[0],
                  'said': round(v[2] / v[0], 3), 'happened': round(v[1] / v[0], 3)}
                 for b, v in sorted(cal_b.items())],
        # one number for the panel: how far the stated odds sit from reality,
        # weighted by how often each level is quoted
        'gap': round(sum(v[0] * abs(v[2] / v[0] - v[1] / v[0]) for v in cal_b.values())
                     / max(1, sum(v[0] for v in cal_b.values())), 3),
    }

    fin = [h for h in scored if h.get('final_hit') is not None]
    record['final'] = {'n': len(fin), 'hits': sum(1 for h in fin if h['final_hit']),
                       'hour': FINAL_HOUR}
    # days scored off an observation because Kalshi had not settled them yet.
    # They are in the accuracy tally but can still be rewritten.
    record['provisional'] = sum(1 for h in scored if h.get('provisional'))

    # WHAT WAS ACTUALLY DONE, as opposed to what was suggested.
    api_rows = fetch_fills(cfg, ticker_cache)
    manual = load_trades(cfg['key'])
    if api_rows is None:
        rows_in = manual                      # no credentials: the CSV is the ledger
    else:
        # both, deduped: a fill logged by hand and then imported should count once
        seen = set((r['date'], r['side'], r['lo'], r['hi'], round(r['price'], 2))
                   for r in api_rows)
        rows_in = api_rows + [r for r in manual
                              if (r['date'], r['side'], r['lo'], r['hi'],
                                  round(r['price'], 2)) not in seen]
    tr_done, tr_open = score_trades(rows_in, hist)
    # execution against the plan (needs the trail; fills before it are not counted)
    try:
        _lab = {}
        for r in rows:
            _lab[(r.get('lo'), r.get('hi'))] = r['label']
        for h in hist.values():
            for r in ((h.get('lock') or {}).get('ladder') or []):
                _lab.setdefault((r.get('lo'), r.get('hi')), r.get('label'))
        _ex = execution_score(cfg, rows_in, lambda lo, hi: _lab.get((lo, hi)))
        if _ex:
            record['execution'] = _ex
    except Exception as e:
        print('execution: skipped (%s)' % e)
    if tr_done or tr_open:
        staked = sum(t['contracts'] * t['cost'] for t in tr_done)
        pl = sum(t['pl'] for t in tr_done)
        record['real'] = {
            'n': len(tr_done), 'wins': sum(1 for t in tr_done if t['won']),
            'staked': round(staked, 2), 'pl': round(pl, 2),
            'roi': round(100 * pl / staked, 1) if staked else None,
            'open': len(tr_open),
            'recent': [{'date': t['date'], 'side': t['side'], 'range': t['bracket'],
                        'price': t['price'], 'contracts': t['contracts'],
                        'won': t['won'], 'pl': t['pl']}
                       for t in sorted(tr_done, key=lambda x: x['date'])[-8:]],
        }
        print('real trades: %d scored, %+.2f on $%.2f staked (%d still open)'
              % (len(tr_done), pl, staked, len(tr_open)))
    # DISCIPLINE: the noon plan held to settlement against what was actually
    # done. Both weekend losses (2026-09-05/06) came from acting against the
    # sheet's number, not from the number; this makes that a daily figure.
    try:
        _fills_by_day = {}
        for t in (tr_done if (tr_done or tr_open) else []):
            f = _fills_by_day.setdefault(t['date'], {'pl': 0.0, 'staked': 0.0})
            f['pl'] += t['pl']; f['staked'] += t['contracts'] * t['cost']
        _plan_days = sorted(k for k, h in hist.items() if h.get('plan_ret') is not None)
        if _plan_days or _fills_by_day:
            _by = []
            for k in sorted(set(_plan_days) | set(_fills_by_day))[-10:]:
                _by.append({'date': k, 'plan_ret': hist.get(k, {}).get('plan_ret'),
                            'fills_pl': round(_fills_by_day.get(k, {}).get('pl', 0.0), 2) if k in _fills_by_day else None,
                            'fills_staked': round(_fills_by_day.get(k, {}).get('staked', 0.0), 2) if k in _fills_by_day else None})
            record['discipline'] = {
                'days': len(_plan_days),
                'plan_ret': round(sum(hist[k]['plan_ret'] for k in _plan_days) / len(_plan_days), 3) if _plan_days else None,
                'plan_wins': sum(1 for k in _plan_days if hist[k]['plan_ret'] > 0),
                'by_day': _by,
            }
    except Exception as e:
        print('discipline: skipped (%s)' % e)
    # END OF DAY REVIEW. Written from the day's own numbers once it has settled
    # (or from the preliminary report after the local day is over), replaced
    # each night, shown at the bottom of RECORD (user, 2026-09-06: "critical
    # takeaways the system can learn from"). Rules, not opinions: every line
    # names a measured thing and what the model does about it.
    try:
        record['review'] = compose_review(cfg, hist, obh, _cli, _fills_by_day if '_fills_by_day' in dir() else {}, now, record)
    except Exception as e:
        print('review: skipped (%s)' % e)
    # These were hardcoded from a refit and went stale the moment the model
    # improved: the file advertised 40/68 and MAE 1.44 long after the fresh-run
    # switch had taken it to 48/66 and 1.04, understating itself by 14 points.
    # Compute them instead, from the same ladders the record is built on.
    br, ll = [], []
    for h in scored:
        tb = h.get('actual_bracket')
        for r in (h.get('lock', {}).get('ladder') or []):
            p = r.get('ours')
            if p is None:
                continue
            o = 1 if r['label'] == tb else 0
            br.append((p - o) ** 2)
            if o:
                ll.append(-math.log(max(p, 1e-9)))
    ds = sorted(h['date'] for h in scored)
    record['measured'] = {
        'brier': round(statistics.mean(br), 4) if br else None,
        'logloss': round(statistics.mean(ll), 3) if ll else None,
        'bracket': '%d/%d' % (record['hits'], record['n']),
        'mae': record['mae'],
        'window': ('%s..%s' % (ds[0], ds[-1])) if ds else None,
        'lock_hour': LOCK_HOUR,
        'offset': HOURLY_PEAK_OFFSET, 'offset_sd': OFFSET_SD, 'offset_n': off_n,
    }

    # trails are per-hour rows and only useful while recent; drop the old ones
    # so the file the page downloads does not grow without bound
    # THE PUBLISHED FORECASTS, KEPT PAST THE TRAIL. The trail lives 14 days;
    # the question "does TWC's own 8 AM forecast beat ours against TWC's own
    # settlement" needs months. So the first morning reading with a published
    # forecast (7-9 AM local) is stamped on the day's row permanently, three
    # numbers, and the record scores it below. Nothing here feeds the pick.
    # THE LIVE MORNING CURVE. The archive replay says accuracy is flat from
    # 7 to 11 AM, but the archive holds each day's best run; live, 8 AM sees
    # the overnight runs and 11 AM the morning ones. Each hour's top pick is
    # stamped from the trail before the trail is trimmed, and scored below.
    for k, h in hist.items():
        tr = h.get('trail') or []
        if not tr:
            continue
        lp = h.setdefault('live_picks', {})
        lad = (h.get('lock') or {}).get('ladder') or []
        for t in tr:
            hh = t.get('h')
            if hh is None or not (7 <= hh <= 13) or str(hh) in lp:
                continue
            ours = t.get('ours') or []
            if ours and lad and len(lad) == len(ours):
                lp[str(hh)] = lad[max(range(len(ours)), key=lambda i: ours[i])]['label']
    for k, h in hist.items():
        if 'published' in h or not h.get('trail'):
            continue
        for t in h['trail']:
            if 7 <= (t.get('h') or -1) <= 9 and (t.get('twc_fc') is not None or t.get('nws_fc') is not None):
                h['published'] = {'h': t['h'], 'twc': t.get('twc_fc'), 'nws': t.get('nws_fc'),
                                  'ours': t.get('pred')}
                break
    keep = sorted(hist, reverse=True)[:14]
    for k, h in hist.items():
        if k not in keep and 'trail' in h:
            del h['trail']

    doc = {
        # `now` is local_now(cfg) -- the clock where the market SETTLES, not
        # Eastern. This said ' ET' for every city, so Los Angeles stamped
        # "06:56 ET" at 09:56 ET and the file read three hours stale when it
        # had just been written. cfg['tzlabel'] is the right label and is
        # already used correctly two lines down.
        'updated': now.strftime('%Y-%m-%dT%H:%M') + ' ' + cfg.get('tzlabel', 'ET'),
        # ...and an unambiguous one beside it, so nothing downstream has to
        # infer an offset from a two-letter suffix to work out an age.
        'updated_utc': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        # when the run STARTED, so the panel can measure dispatch-to-landing
        'started_utc': RUN_STARTED,
        'today': {
            'date': tkey, 'event': event_ticker(cfg, today),
            'tz': cfg.get('tzlabel', 'ET'), 'market': cfg.get('label', ''),
            'close': rows[0]['close'],
            'settles': '%s (%s) via The Weather Company' % (cfg.get('city', ''), cfg.get('cli', '')),
            'state': market_state(cfg, rows, now),
            'pred': round(pred, 2), 'sd': sd, 'bias': round(bias, 2),
            'bias_days': nb, 'sd_days': nsd,
            'obs_so_far': obs_far, 'obs_through': obs_hr,
            'obs_peak_hour': obs_peak_hour, 'obs_peak_read': obs_peak_read,
            # today's hourly readings so far, [hour, degF], for the panel to draw
            # beside the replayed day's curve
            'obs_hours': [[h, round(v, 2)] for h, v in sorted(_day_obs.items())],
            # WHAT THE EXCHANGE IS SETTLING ON, next to what the station read.
            # The panel shows both, because the gap is the thing worth seeing
            # and it is not small: Chicago ran +4.0 the day this was wired in,
            # New York +2.0 on the afternoon that made the case for it.
            'six_max': _six, 'six_window': SIX_WINDOW.get(cfg['key']),
            'own5_max': _own5, 'own5_station': OWN5.get(cfg['key']),
            'apt_max': _aptmax, 'twc_corroborated': _twc_corr,
            'model_resid': model_resid,
            # the afternoon read: how much of the market's number to fold into
            # the sell/hold verdict at this hour (1 = ours alone)
            'blend_w': measured_blend(cfg, now.hour),
            # THE REPORT THE DAY IS ACTUALLY SETTLED ON. Verified against the
            # exchange's own expiration_value on 112 of 112 settled days. The
            # panel leads with this; everything else on screen is an estimate of
            # it. `cli_final` false means the preliminary, whose window closes at
            # 4 PM local and which is wrong about 1 New York day in 3.
            # the settlement source's own view of today and yesterday
            'portal': _portal_today, 'portal_yday': _portal_yday,
            'cli_max': (_cli.get(tkey) or {}).get('max'),
            'cli_final': (_cli.get(tkey) or {}).get('final'),
            'cli_at': (_cli.get(tkey) or {}).get('at'),
            'twc_max': _twc.get('max'), 'twc_now': _twc.get('now'),
            'twc_fc': _twcf, 'nws_fc': _nwsf,
            'twc_gap': (round(_twc['max'] - rmax, 1)
                        if _twc.get('max') is not None and rmax is not None
                        else None),
            # the day's warming is finished and the high is already on the board.
            # Whatever spread is left is only doubt about where between two METARs
            # the true peak fell -- and the exchange settles on the official
            # reading, which it can see and this cannot. There is no edge to have
            # on a day that has already happened, however the arithmetic looks.
            'day_over': bool(binding_now),
            # the temperature right now, as opposed to the day's peak so far
            'now_temp': round(ob_last[0][1], 2) if ob_last else None,
            'now_at': ob_last[0][0][11:16] if ob_last else None,
            'fc_peak': round(fpeak, 2) if fpeak is not None else None,
            'peak_hour': peak_hour,
            'peak_done': peak_done, 'day_decided': day_decided,
            'own5_gap': _gap5,
            'ours': [round(p, 4) for p in ps],
            'pick': rows[best]['label'], 'p': round(ps[best], 4),
            'market_pick': rows[mbest]['label'], 'market_p': rows[mbest]['mid'],
            'agree': best == mbest,
            # the rung's own ticker travels with it, so the panel can match a
            # holding to a row exactly. Without it the page would have to decode
            # KXHIGHNY-26SEP05-B79.5 and -T79 back into bounds, and the T form
            # does not record its direction -- a guess where an identity is
            # available. Public either way: it is the exchange's own name for a
            # market anyone can look up.
            'ladder': [{'label': r['label'], 'lo': r['lo'], 'hi': r['hi'],
                        'ticker': r.get('ticker'),
                        'bid': r['bid'], 'ask': r['ask'],
                        'nbid': r['nbid'], 'nask': r['nask'],
                        'ysize': r['ysize'], 'nsize': r['nsize'], 'market': r['mid'],
                        'ours': round(p, 4), 'vol': r['vol'], 'oi': r.get('oi')}
                       for r, p in zip(rows, ps)],
            'locked': entry.get('lock'), 'final': entry.get('final'),
            'final_hour': FINAL_HOUR,
            # what is measured about timing, and how much of the other half
            # (when the MARKET is slow) we have collected so far
            # MEASURED, when price_study.py has been run: what a bet placed at
            # each hour actually returned, against the market's own quotes on
            # settled days. Falls back to the assumed accuracy curve otherwise.
            'exit': measured_exit(cfg),
            'bet': best_bet(rows, ps),
            'calib': _CALIB.get(cfg['key']) or None,
            'edge_floor': {'min': EDGE_FLOOR, 'priced': EDGE_FLOOR_PRICED, 'price': EDGE_PRICE},
            'max_disagree': MAX_DISAGREE,
            'regime': metar_regime(cfg),
            'brake': record.get('brake'),
            'twc_in': twc_in,
            'params': params_of(cfg),
            # the live plan-stability figure once the bake trail has two weeks
            # (trail_study.py); the panel prefers it over the archive replay
            'flip_live': (lambda st: ({'rate': round(1 - st['stable_to_11'][0] / float(st['stable_to_11'][1]), 3),
                                       'n': st['stable_to_11'][1], 'days': st['n_days']}
                                      if st and st.get('n_days', 0) >= 14 and (st.get('stable_to_11') or [0, 0])[1] >= 10 else None))(
                (lambda f: (json.load(open(f)).get('cities', {}).get(cfg['key'], {}).get('summary') if os.path.exists(f) else None))(os.path.join(HERE, 'trail_study.json'))),
            # how often the mid-morning runs move the pick between 8 and 11,
            # and what each hour's bet returned on those days (price_study)
            'flip': (((_STUDY[0] if _STUDY else None) or {}).get('flip_by_market') or {}).get(cfg['key']),
            # whether the hour study above is this market's own or a fallback
            # (price_study by_market), so the panel can label it honestly
            'hours_own': bool(((_STUDY[0] if _STUDY else None) or {}).get('by_market', {}).get(cfg['key'])),
            'by_hour': measured_hours(cfg) or [
                {'h': h, 'acc': HOUR_ACC[h], 'sd': SD_FALLBACK.get(h)}
                for h in sorted(HOUR_ACC)],
            'lock_hour': LOCK_HOUR,
            # the panel renders these rather than hardcoding them: the prose had
            # already drifted from the constants twice after a refit
            'damp': SWING_DAMP, 'bias_days': BIAS_K, 'bias_hl': cfg.get('bias_hl'),
            'resid_days': RESID_M,
            # deep link straight to today's event, so the panel is one click
            # from actually placing the bet
            'link': (cfg['url'] + '/' + event_ticker(cfg, today).lower())
                    if cfg.get('url') else None,
            'n_models': len(models_for(cfg)),
            # each run's own peak for today, bias removed: the panel's timing
            # gate reads their spread (agreeing runs = a steady pick)
            'models': {m: round(max((fc.get(tkey) or {}).values()) - biases[m], 1)
                       for m, fc in fcm.items() if fc.get(tkey) and biases.get(m) is not None},
            'trail_days': sum(1 for h in hist.values() if h.get('trail')),
            'tomorrow': tom,
        },
        'record': record,
        'history': sorted(hist.values(), key=lambda h: h['date'], reverse=True)[:120],
    }

    if dry:
        print(json.dumps(doc['today'], indent=2)[:2600])
        print('\nrecord:', json.dumps(record))
        # BV_DRY_OUT=/path/KEY.json writes the whole document a dry run would
        # have published, so a change can be inspected (and previewed on the
        # panel) without touching the live files or sending alerts
        if os.environ.get('BV_DRY_OUT'):
            with open(os.environ['BV_DRY_OUT'].replace('KEY', cfg['key']), 'w') as f:
                json.dump(doc, f, separators=(',', ':'))
        return 0
    with open(OUT, 'w') as f:
        json.dump(doc, f, separators=(',', ':'))
    # the bake trail, appended for the workflow to push to R2
    try:
        row = trail_row(doc, now); row['k'] = cfg['key']
        tdir = os.path.join(HERE, 'trail_out'); os.makedirs(tdir, exist_ok=True)
        with open(os.path.join(tdir, '%s_%s.jsonl' % (cfg['key'], tkey)), 'a') as tf:
            tf.write(json.dumps(row, separators=(',', ':')) + '\n')
    except Exception as e:
        print('trail row skipped (%s)' % e)
    print('wrote %s (%d scored days) -- %s'
          % (OUT, record['n'], timing_report()))
    # a one-line digest for the panel's other-cities table, so seven markets
    # cost one fetch rather than seven
    t = doc['today']
    st_now = t.get('state') or {}
    decided = (t.get('p') or 0) >= 0.95 or (t.get('sd') is not None and t['sd'] <= 0.4) \
              or bool(t.get('day_over'))
    tradable = bool(rows) and st_now.get('status') == 'open' and not decided
    bb = best_bet(rows, ps) if tradable else None
    take = book_value(rows, ps) if tradable else {'ev': 0.0, 'stake': 0.0, 'n': 0}
    return {'key': cfg['key'], 'city': cfg.get('city', cfg['key']),
            'baked': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'tzlabel': cfg.get('tzlabel'), 'link': t.get('link'),
            'date': t.get('date'), 'state': t.get('state'),
            'pred': t.get('pred'), 'sd': t.get('sd'),
            'pick': t.get('pick'), 'p': t.get('p'),
            'market_pick': t.get('market_pick'), 'agree': t.get('agree'),
            'obs': t.get('obs_so_far'), 'now_temp': t.get('now_temp'),
            'day_over': t.get('day_over'),
            # what is riding on this market right now, across every rung
            'pool': round(sum((r.get('oi') or 0) for r in rows), 0),
            'vol': round(sum((r.get('vol') or 0) for r in rows), 0),
            'bet': bb, 'take': take, 'file': cfg['out']}


def only_markets():
    """`--only ny_high[,chi_high]` runs a subset: the New York fast lane bakes
    Central Park alone every five minutes between the full 15-minute runs.
    Its digest is merged into the head file's list; the others are kept."""
    if '--only' not in sys.argv:
        return None
    keys = sys.argv[sys.argv.index('--only') + 1].split(',')
    return [c for c in MARKETS if c['key'] in keys]


def main():
    """Run every configured market. One market's outage must not stop the rest,
    and a market that throws leaves its previous file untouched rather than
    writing something half-built.

    Afterwards the digests are folded into the first market's file, so the page
    can show every city's best bet from the one fetch it already makes."""
    global BANKROLL
    bal = fetch_balance()
    if bal is not None and bal > 0:
        BANKROLL = round(bal, 2)
        print('bankroll: $%.2f, read from the exchange' % BANKROLL)
    else:
        print('bankroll: $%.2f (no balance available, using the default)' % BANKROLL)
    bad = 0
    digests = []
    RUN = only_markets() or MARKETS
    # THE EDGE FLOOR, from last night's study when it has the rows (guarded)
    try:
        _mf = measured_floor()
        if _mf:
            print('edge floor %s: %sc any price, %sc from %sc (%d rows%s)' % (
                'measured' if _mf['measured'] else 'defaulted', int(round(100 * _mf['min'])), int(round(100 * _mf['priced'])),
                int(round(100 * _mf['price'])), _mf['n'] or 0, ', CHANGED from the typed values' if _mf['changed'] else ''))
    except Exception as e:
        print('edge floor: typed values kept (%s)' % e)
    try:
        _mc = measured_cap()
        if _mc:
            print('disagreement cap measured: %d points (%d rows%s)' % (int(round(100 * _mc['cap'])), _mc['n'] or 0, ', CHANGED from the typed value' if _mc['changed'] else ''))
    except Exception as e:
        print('disagreement cap: typed value kept (%s)' % e)
    # THE TUNED SETTINGS (tune.py, weekly): applied only where a guarded replay
    # chose them; every lock stamps what was in force
    try:
        _tuned = json.load(open(os.path.join(HERE, 'tuned.json')))
        for cfg in RUN:
            a = (_tuned.get(cfg['key']) or {}).get('active')
            if a and (_tuned.get(cfg['key']) or {}).get('n', 0) >= 45:
                cfg.update({k: a[k] for k in ('skill', 'bias_hl', 'sd_mult') if k in a})
                g = {k: a[k] for k in TUNABLE_GLOBALS if k in a}
                if g:
                    cfg['_globals'] = g
                print('%s tuned: %s' % (cfg['key'], a))
    except FileNotFoundError:
        pass
    except Exception as e:
        print('tuned.json skipped (%s)' % e)
    prev_markets = []
    if RUN is not MARKETS:
        print('fast lane: %s only' % ', '.join(c['key'] for c in RUN))
        # run_market rewrites the head file WITHOUT its markets list (main folds
        # that in afterwards), so the other cities' digests have to be read
        # before anything runs, or the fast lane folds in New York alone
        try:
            with open(os.path.join(HERE, '..', MARKETS[0]['out'])) as f:
                prev_markets = json.load(f).get('markets') or []
        except Exception:
            prev_markets = []

    # IN PARALLEL, since the roster went from seven markets to twenty. The work
    # is all waiting on sockets, and Open-Meteo from a GitHub runner stalls for
    # two or three minutes on a few cities every run (94s, 184s and 124s on the
    # 04:56Z run of 2026-09-06, seven cities, 7.5 minutes). Serial, twenty
    # cities would not fit the 15-minute cadence; four at a time they do.
    # Four, not twenty: the same host is behind most of the wait, and a burst
    # of sixty requests from one datacentre IP is how the stalls get longer.
    def one(cfg):
        _TL.log = []
        t0 = time.time()
        try:
            return run_market(cfg), None, _TL.log, time.time() - t0
        except Exception as e:
            return None, e, _TL.log, time.time() - t0
        finally:
            _TL.log = None

    got = {}
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(one, cfg): cfg['key'] for cfg in RUN}
        for f in cf.as_completed(futs):
            key = futs[f]
            d, err, lines, secs = f.result()
            _print('\n'.join(lines))
            if err is not None:
                _print('%s FAILED: %s: %s' % (key, type(err).__name__, err))
            _print('%s done in %.0fs' % (key, secs), flush=True)
            got[key] = (d, err)
    for cfg in RUN:
        d, err = got.get(cfg['key'], (None, RuntimeError('never ran')))
        if err is not None:
            bad += 1
        elif isinstance(d, dict):
            digests.append(d)
    # HOW INDEPENDENT ARE SEVEN CITIES, REALLY?
    # They share a model family and one bias method, so a bad synoptic day can
    # miss in several at once. Seven bets are only worth seven if their errors
    # are unrelated; measure it rather than assume it. Effective independent
    # bets = n / (1 + (n-1) * mean pairwise r), the standard correction for an
    # equicorrelated set.
    corr = None
    try:
        series = {}
        for d in digests:
            with open(os.path.join(HERE, '..', d['file'])) as f:
                hist = json.load(f).get('history') or []
            series[d['city']] = {h['date']: h['err'] for h in hist
                                 if h.get('err') is not None}
        rs = []
        names = sorted(series)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = series[names[i]], series[names[j]]
                both = sorted(set(a) & set(b))
                if len(both) < 30:
                    continue
                xs = [a[k] for k in both]
                ys = [b[k] for k in both]
                mx, my = statistics.mean(xs), statistics.mean(ys)
                sx = math.sqrt(sum((v - mx) ** 2 for v in xs))
                sy = math.sqrt(sum((v - my) ** 2 for v in ys))
                if sx > 0 and sy > 0:
                    rs.append(sum((xs[t] - mx) * (ys[t] - my)
                                  for t in range(len(xs))) / (sx * sy))
        if rs:
            rbar = statistics.mean(rs)
            n = len(names)
            eff = n / (1 + (n - 1) * max(rbar, 0.0)) if n > 1 else 1.0
            corr = {'pairs': len(rs), 'mean_r': round(rbar, 3),
                    'max_r': round(max(rs), 3), 'cities': n,
                    'effective_bets': round(eff, 2)}
            print('cross-city error correlation: mean r %.3f over %d pairs '
                  '-> %.1f independent bets, not %d'
                  % (rbar, len(rs), eff, n))
    except Exception as e:
        print('correlation check failed: %s' % e)

    if digests and '--dry' not in sys.argv:
        head = os.path.join(HERE, '..', MARKETS[0]['out'])
        try:
            with open(head) as f:
                doc = json.load(f)
            if RUN is not MARKETS:
                # a subset ran: keep every other market's digest as it stood
                had = {m.get('key'): m for m in prev_markets}
                for d in digests:
                    had[d['key']] = d
                digests = [had[c['key']] for c in MARKETS if c['key'] in had]
            doc['markets'] = digests
            if corr:
                doc['correlation'] = corr
            # the day's fillable expectation, which is the number the whole
            # exercise is actually trying to maximise
            # A DAILY RISK BUDGET.  Each position is sized at quarter Kelly of
            # the WHOLE bankroll, which is right for one bet and badly wrong for
            # nineteen: unscaled they wanted $745 of a $500 pot. Kelly assumes
            # independent opportunities and these are not -- same models, same
            # bias method, seven cities under one synoptic pattern. So cap the
            # day's total exposure and scale every position to fit. Expected
            # dollars scale linearly with size, so the capped figure is simply
            # proportional -- and it is the honest one.
            bank, cap_frac = BANKROLL, 0.25
            raw_ev = sum((d.get('take') or {}).get('ev', 0) for d in digests)
            raw_st = sum((d.get('take') or {}).get('stake', 0) for d in digests)
            budget = bank * cap_frac
            scale = min(1.0, budget / raw_st) if raw_st > 0 else 1.0
            doc['take'] = {
                'ev': round(raw_ev * scale, 2), 'stake': round(raw_st * scale, 2),
                'raw_ev': round(raw_ev, 2), 'raw_stake': round(raw_st, 2),
                'scale': round(scale, 3),
                'n': sum((d.get('take') or {}).get('n', 0) for d in digests),
                'cities': sum(1 for d in digests if (d.get('take') or {}).get('n')),
                'cap': cap_frac,
            }
            print('take: $%.2f expected on $%.2f staked (%d positions, %d cities)%s'
                  % (doc['take']['ev'], doc['take']['stake'], doc['take']['n'],
                     doc['take']['cities'],
                     '' if scale >= 1 else ' -- scaled to %.0f%% of a $%.0f budget'
                     % (100 * scale, budget)))
            with open(head, 'w') as f:
                json.dump(doc, f, separators=(',', ':'))
            print('folded %d market digests into %s' % (len(digests), MARKETS[0]['out']))
        except Exception as e:
            print('digest fold failed: %s' % e)
    return 1 if bad == len(RUN) else 0


if __name__ == '__main__':
    sys.exit(main())

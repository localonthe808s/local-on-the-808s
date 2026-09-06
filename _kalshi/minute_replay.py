#!/usr/bin/env python3
"""
MINUTE REPLAY -- the post-mortem tool.

Central Park's one-minute temperature record is not public in real time, but
it IS archived: NCEI's TD-6405 one-minute ASOS dataset, mirrored by the Iowa
Environmental Mesonet, runs several days behind the day it describes. That is
useless for a live bet and exactly right for a post-mortem. Once a day has
settled, this replays it minute by minute and answers the questions the
hourly stream cannot:

  * what minute did the day's high actually land, and was it BETWEEN the
    hourly :51 reports (the blind spot that lost 2026-09-05)?
  * did the 1:51 PM six-hourly maximum group see it, or did it come later?
  * did the airports (LaGuardia, Newark, JFK -- the ones with a public
    5-minute feed) reach their own high BEFORE the park did, and by how
    much? That is what decides whether the airports block on the panel is a
    real early warning.

Truth stays the climate report (CLINYC via cli_cache.json: max + time). The
archive is whole degrees, so its own daily high can differ from the report
by a degree; it is shown as the archive, never as the settlement.

Output: kalshi_minutes.json at the repo root (picked up by the workflow's
kalshi_*.json commit glob). Self-throttles: the archive moves once a day, so
a run within REFRESH_HOURS of the last check exits without a request.
"""
import json, os, sys, time, statistics, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, 'kalshi_minutes.json')
CLI = os.path.join(HERE, 'cli_cache.json')

IEM = 'https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py'
# one entry per panel tab: the settlement station in IEM's 1-minute archive,
# the nearby stations to compare against, the climate product, the zone
CITIES = {
    'ny_high':  {'park': 'NYC', 'apts': ['LGA', 'EWR', 'JFK'], 'cli': 'CLINYC', 'tz': 'America/New_York',    'utc_off': -4},
    'las_high': {'park': 'LAS', 'apts': ['VGT'],               'cli': 'CLILAS', 'tz': 'America/Los_Angeles', 'utc_off': -7},
    'aus_high': {'park': 'AUS', 'apts': ['ATT'],               'cli': 'CLIAUS', 'tz': 'America/Chicago',     'utc_off': -5},
}
NAMES = {'LGA': 'LaGuardia', 'EWR': 'Newark', 'JFK': 'JFK', 'VGT': 'North Las Vegas', 'ATT': 'Camp Mabry'}
WINDOW_DAYS = 45                          # how far back the replay reaches
REFRESH_HOURS = 6                         # the archive moves ~daily; do not hammer IEM
COMPLETE = 0.90                           # share of minutes present for a day to count
KEEP_CURVES = 3                           # newest complete days carry a 5-min curve
FORCE = '--force' in sys.argv

def log(*a):
    print('[minutes]', *a, flush=True)

def fetch(station, sts, ets, tz='America/New_York'):
    q = urllib.parse.urlencode({
        'station': station, 'vars': 'tmpf', 'sts': sts, 'ets': ets,
        'sample': '1min', 'what': 'view', 'tz': tz})
    req = urllib.request.Request(IEM + '?' + q, headers={'User-Agent': 'bluishvoid.com kalshi post-mortem'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                txt = r.read().decode('utf-8', 'replace')
            break
        except Exception as e:
            log(station, 'attempt', attempt + 1, 'failed:', e)
            time.sleep(5 * (attempt + 1))
    else:
        return {}
    days = {}
    for line in txt.splitlines()[1:]:
        p = line.split(',')
        if len(p) < 4:
            continue
        v = p[3].strip()
        if v in ('', 'M'):
            continue
        try:
            v = float(v)
        except ValueError:
            continue
        d, t = p[2].split(' ')
        days.setdefault(d, {})[t] = v
    return days

def mins(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)

def hhmm_12(hhmm):
    h, m = mins(hhmm) // 60, mins(hhmm) % 60
    return '%d:%02d %s' % ((h % 12) or 12, m, 'AM' if h < 12 else 'PM')

def cli_at_mins(s):
    """'2:33PM' -> minutes since midnight, or None."""
    if not s:
        return None
    try:
        t = datetime.strptime(s.replace(' ', ''), '%I:%M%p')
        return t.hour * 60 + t.minute
    except ValueError:
        return None

def first_reach(m, val):
    return min((t for t, v in m.items() if v >= val), key=mins)

def settlements(key, cli_key):
    """cli_cache carries max + time; before that the city file's graded
    history knows the settled value (no time)."""
    out = {}
    try:
        fn = 'kalshi_%s.json' % key.split('_')[0]
        h = json.load(open(os.path.join(ROOT, fn))).get('history') or []
        for r in h:
            if r.get('actual') is not None and r.get('date') and not r.get('backtest_only'):
                out[r['date']] = {'max': r['actual']}
    except Exception as e:
        log('no history:', e)
    try:
        for d, c in (json.load(open(CLI)).get(cli_key) or {}).items():
            if c.get('max') is not None:
                out[d] = {'max': c['max'], 'at': c.get('at')}
    except Exception as e:
        log('no cli cache:', e)
    return out

def replay_day(d, park, apts, cli, airports):
    m = park.get(d) or {}
    cov = len(m) / 1440.0
    row = {'date': d, 'coverage': round(cov, 3), 'complete': cov >= COMPLETE}
    if not m:
        return row
    hi = max(m.values())
    at = first_reach(m, hi)
    hourly = [v for t, v in m.items() if t.endswith(':51')]
    six = [v for t, v in m.items() if '07:51' <= t <= '13:51']
    row.update({
        'archive_max': hi, 'archive_at': at,
        'hourly_max': max(hourly) if hourly else None,
        'six_max': max(six) if six else None,
    })
    c = cli.get(d) or {}
    if c.get('max') is not None:
        row['cli_max'] = c['max']
        row['cli_at'] = c.get('at')
        cam = cli_at_mins(c.get('at'))
        row['cli_at_min'] = cam
        if row['hourly_max'] is not None:
            row['hidden'] = round(c['max'] - row['hourly_max'], 1)   # what the :51 stream never showed
        if cam is not None:
            row['after_six'] = cam > mins('13:51')                    # past the six-hourly group
    # the airports: when did each reach ITS OWN high, relative to the park's
    # settlement minute (or the archive minute when the report has no time)
    ref = row.get('cli_at_min')
    if ref is None:
        ref = mins(at)
    A = {}
    for s in airports:
        am = (apts.get(s) or {}).get(d) or {}
        if len(am) < 600:
            continue
        ah = max(am.values())
        aat = first_reach(am, ah)
        A[s] = {'max': ah, 'at': aat, 'lead_min': ref - mins(aat)}     # +: airport peaked first
    if A:
        row['airports'] = A
    return row

def curve(m):
    """5-minute samples of the day, [minute, tmpf], for the panel to draw."""
    out = []
    for k in range(0, 1440, 5):
        t = '%02d:%02d' % (k // 60, k % 60)
        if t in m:
            out.append([k, m[t]])
    return out

def summarize(rows, airports):
    done = [r for r in rows if r.get('complete') and r.get('cli_max') is not None]
    S = {'n': len(done)}
    if not done:
        return S
    ats = [r['cli_at_min'] for r in done if r.get('cli_at_min') is not None]
    if ats:
        med = int(statistics.median(ats))
        S['peak_median'] = '%02d:%02d' % (med // 60, med % 60)
        S['peak_median_12'] = hhmm_12(S['peak_median'])
        S['after_six_n'] = sum(1 for r in done if r.get('after_six'))
        S['timed_n'] = len(ats)
    hid = [r['hidden'] for r in done if r.get('hidden') is not None]
    if hid:
        S['hidden_n'] = sum(1 for h in hid if h >= 1)
        S['hidden_mean'] = round(sum(hid) / len(hid), 2)
        S['hidden_max'] = max(hid)
    six = [(r['cli_max'] - r['six_max']) for r in done if r.get('six_max') is not None]
    if six:
        S['six_missed_n'] = sum(1 for g in six if g >= 1)
        S['six_n'] = len(six)
    for s in airports:
        leads = [r['airports'][s]['lead_min'] for r in done
                 if r.get('airports', {}).get(s) and r['airports'][s].get('lead_min') is not None]
        if leads:
            S.setdefault('airports', {})[s] = {
                'n': len(leads),
                'led_n': sum(1 for l in leads if l > 0),
                'lead_median': int(statistics.median(leads)),
            }
    S['archive_off_by'] = sum(1 for r in done if abs(r['archive_max'] - r['cli_max']) >= 1)
    return S

def replay_city(key, c, now, today_by_tz):
    airports = c['apts']
    cli = settlements(key, c['cli'])
    start = (now - timedelta(days=WINDOW_DAYS)).strftime('%Y-%m-%dT04:00Z')
    end = (now + timedelta(days=1)).strftime('%Y-%m-%dT04:00Z')
    park = fetch(c['park'], start, end, c['tz'])
    log(key, 'park days in archive:', len(park), '| settled days known:', len(cli))
    apts = {st: fetch(st, start, end, c['tz']) for st in airports}
    today = now.astimezone(timezone(timedelta(hours=c['utc_off']))).strftime('%Y-%m-%d')
    rows = [replay_day(d, park, apts, cli, airports) for d in sorted(park) if d < today]
    complete = [r for r in rows if r.get('complete')]
    for r in complete[-KEEP_CURVES:]:
        m = park[r['date']]
        r['curve'] = curve(m)
        r['hourly'] = [[mins(t), v] for t, v in sorted(m.items()) if t.endswith(':51') or t.endswith(':56')]
    newest = complete[-1]['date'] if complete else None
    lag = None
    if newest:
        lag = (datetime.strptime(today, '%Y-%m-%d') - datetime.strptime(newest, '%Y-%m-%d')).days
    return {'station': 'K' + c['park'], 'newest_complete': newest, 'lag_days': lag,
            'days': rows, 'summary': summarize(rows, airports)}


def main():
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT))
        except Exception:
            prev = {}
    now = datetime.now(timezone.utc)
    last = prev.get('checked_utc')
    if last and not FORCE:
        try:
            age = (now - datetime.fromisoformat(last.replace('Z', '+00:00'))).total_seconds() / 3600
            if age < REFRESH_HOURS:
                log('checked %.1f h ago; the archive moves daily -- skipping' % age)
                return
        except ValueError:
            pass
    cities = {}
    for key, c in CITIES.items():
        try:
            cities[key] = replay_city(key, c, now, None)
        except Exception as e:
            log(key, 'FAILED:', e)
    out = {'checked_utc': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
           'source': 'NCEI TD-6405 one-minute ASOS via IEM',
           'cities': cities}
    # the New York block also sits at the top level, the shape the panel first read
    if 'ny_high' in cities:
        out.update(cities['ny_high'])
    json.dump(out, open(OUT, 'w'), separators=(',', ':'))
    for k, v in cities.items():
        log('%s: %d days, newest %s, lag %s' % (k, len(v['days']), v['newest_complete'], v['lag_days']))
    log('wrote', OUT)


if __name__ == '__main__':
    main()

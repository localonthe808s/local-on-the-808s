#!/usr/bin/env python3
"""
PEAK BEHIND US -- how often the day's high has already happened by each hour.

From the one-minute archive (NCEI TD-6405 via IEM), per city: for each local
hour H, the share of days whose highest minute came before H:00, both
unconditionally and given that the reading at H:00 had already fallen a degree
below the day's running maximum ("cooling"). Written to peak_stats.json; the
bake reads it to say, at any afternoon hour, how likely the peak is behind us
-- the measured basis for treating a day as decided before the climate
report (2026-09-06). Also the mean gap between the true one-minute peak and
the best 5-minute sample, which is what the settlement sensor's 5-minute feed
leaves unseen.
"""
import json, os, sys, urllib.parse, urllib.request, statistics, collections
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'peak_stats.json')
IEM = 'https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py'
CITIES = {
    'ny_high':  ('NYC', 'America/New_York'),
    'las_high': ('LAS', 'America/Los_Angeles'),
    'aus_high': ('AUS', 'America/Chicago'),
}
DAYS = 60
COOL = 1.0          # degF below the running max that counts as "cooling"


def fetch(st, tz):
    now = datetime.now(timezone.utc)
    q = urllib.parse.urlencode({
        'station': st, 'vars': 'tmpf', 'sample': '1min', 'what': 'view', 'tz': tz,
        'sts': (now - timedelta(days=DAYS)).strftime('%Y-%m-%dT04:00Z'),
        'ets': now.strftime('%Y-%m-%dT04:00Z')})
    req = urllib.request.Request(IEM + '?' + q, headers={'User-Agent': 'bluishvoid.com peak study'})
    txt = urllib.request.urlopen(req, timeout=240).read().decode('utf-8', 'replace')
    days = collections.defaultdict(dict)
    for line in txt.splitlines()[1:]:
        p = line.split(',')
        if len(p) < 4 or p[3] in ('', 'M'):
            continue
        d, t = p[2].split(' ')
        h, m = t.split(':')
        days[d][int(h) * 60 + int(m)] = float(p[3])
    return days


def study(days):
    full = {d: m for d, m in days.items() if len(m) >= 1300}
    out = {'n_days': len(full), 'by_hour': {}}
    if not full:
        return out
    gaps5 = []
    peaks = {}
    for d, m in full.items():
        mx = max(m.values())
        peaks[d] = min(k for k, v in m.items() if v >= mx)      # first minute at the max
        five = [v for k, v in m.items() if k % 5 == 0]
        if five:
            gaps5.append(mx - max(five))
    out['gap5_mean'] = round(statistics.mean(gaps5), 2) if gaps5 else None
    out['gap5_sd'] = round(statistics.pstdev(gaps5), 2) if len(gaps5) > 1 else None
    for H in range(10, 22):
        done = cool = cool_done = 0
        for d, m in full.items():
            before = peaks[d] < H * 60
            done += before
            run = max(v for k, v in m.items() if k < H * 60) if any(k < H * 60 for k in m) else None
            at = m.get(H * 60)
            if run is not None and at is not None and at <= run - COOL:
                cool += 1
                cool_done += before
        out['by_hour'][str(H)] = {
            'p_done': round(done / len(full), 3),
            'p_done_cooling': round(cool_done / cool, 3) if cool else None,
            'n_cooling': cool,
        }
    return out


def main():
    doc = {'built': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ'), 'days': DAYS, 'cool_deg': COOL, 'cities': {}}
    for key, (st, tz) in CITIES.items():
        try:
            doc['cities'][key] = study(fetch(st, tz))
            s = doc['cities'][key]
            print('%s: %d full days | 1-min peak minus best 5-min sample %.2f (sd %s)' % (key, s['n_days'], s.get('gap5_mean') or 0, s.get('gap5_sd')))
            for H in ('13', '14', '15', '16', '17', '18'):
                b = s['by_hour'].get(H, {})
                print('   by %s:00  peak behind %3.0f%%  | if cooling %s%% (n=%s)' % (
                    H, 100 * b.get('p_done', 0), ('%.0f' % (100 * b['p_done_cooling'])) if b.get('p_done_cooling') is not None else ' -', b.get('n_cooling')))
        except Exception as e:
            print(key, 'FAILED', e)
    json.dump(doc, open(OUT, 'w'), indent=1)
    print('wrote', OUT)


if __name__ == '__main__':
    main()

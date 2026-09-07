#!/usr/bin/env python3
"""
SELF-TUNING WITH GUARDRAILS (2026-09-07). The per-city settings -- skill-weighted
consensus or the equal mean, the recency window on the bias, the spread
multiplier -- and three constants that were typed by hand (the bias window
length, the warm-up damping, the spread floor) are replayed over the frozen
record with the current model and re-chosen every week. Written to tuned.json
for the bake to apply per city.

COORDINATE SEARCH, not a grid: with six knobs a full grid is hundreds of
replays. Each pass walks the knobs in turn, tries each alternative with the
others held at the incumbent, and keeps the best alternative only if it clears
the guardrails against the incumbent; a second pass catches interactions.
About twenty replays a city, all memoised.

The guardrails are the point, not the search. A candidate wins only when
  * the city has at least 45 scored days in common,
  * its ladder Brier beats the incumbent's by 0.006 or more,
  * it also wins on BOTH halves of the record (the older days and the recent
    days), so a summer fit cannot buy the whole year,
  * and it does not lose more than one bracket hit.
Otherwise the incumbent stands. Every change is logged with its numbers, and
the bake stamps the settings in force on every lock (lock.params), so a
regression is attributable to the week it happened.
"""
import copy, json, os, sys, datetime, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kalshi_daily as K          # noqa: E402
import rescore as R               # noqa: E402
OUT = os.path.join(HERE, 'tuned.json')
KNOBS = collections.OrderedDict([
    ('skill',      [False, True]),
    ('bias_hl',    [None, 7, 14]),
    ('sd_mult',    [0.75, 1.0, 1.25]),
    ('bias_k',     [21, 30, 45]),        # K.BIAS_K, days in the rolling bias window
    ('swing_damp', [0.0, 0.05, 0.10]),   # K.SWING_DAMP, how much of a warm-up the models overdo
    ('sd_floor',   [0.25, 0.40]),        # K.SD_FLOOR, the least spread the ladder may claim
])
CFG_KNOBS = ('skill', 'bias_hl', 'sd_mult')
MIN_DAYS = 45
MIN_GAIN = 0.006
PASSES = 2


def brier(h):
    lad = ((h.get('lock') or {}).get('ladder')) or []
    ab = h.get('actual_bracket')
    if not lad or ab is None:
        return None
    return sum((float(r.get('ours') or 0) - (1.0 if r.get('label') == ab else 0.0)) ** 2 for r in lad)


def score(hist, keys):
    rows = [hist[k] for k in keys if k in hist]
    b = [x for x in (brier(h) for h in rows) if x is not None]
    return {'n': len(rows), 'hits': sum(1 for h in rows if h.get('hit')),
            'brier': (sum(b) / len(b)) if b else None}


def defaults_of(cfg):
    return {'skill': bool(cfg.get('skill', True)), 'bias_hl': cfg.get('bias_hl'), 'sd_mult': float(cfg.get('sd_mult', 1.0)),
            'bias_k': K.BIAS_K, 'swing_damp': K.SWING_DAMP, 'sd_floor': K.SD_FLOOR}


def replay_with(cfg, st):
    c = copy.deepcopy(cfg)
    c.update({k: st[k] for k in CFG_KNOBS})
    c['_globals'] = {k: st[k] for k in st if k in K.TUNABLE_GLOBALS}
    if '--backfill' not in sys.argv:
        sys.argv.append('--backfill')
    doc = R.replay(c)
    return {h['date']: h for h in doc.get('history', []) if h.get('actual') is not None and 'lock' in h}


def key_of(st):
    return json.dumps(st, sort_keys=True)


def beats(cand, base):
    """The guardrails: (passes, gain, table) for a candidate against the incumbent."""
    days = sorted(set(cand) & set(base))
    if len(days) < MIN_DAYS:
        return False, None, {'why': 'only %d days' % len(days)}
    half = len(days) // 2
    old, new = days[:half], days[half:]
    c = {'all': score(cand, days), 'old': score(cand, old), 'new': score(cand, new)}
    b = {'all': score(base, days), 'old': score(base, old), 'new': score(base, new)}
    if c['all']['brier'] is None or b['all']['brier'] is None:
        return False, None, {'why': 'no brier'}
    gain = b['all']['brier'] - c['all']['brier']
    wins_both = c['old']['brier'] < b['old']['brier'] and c['new']['brier'] < b['new']['brier']
    hit_ok = c['all']['hits'] >= b['all']['hits'] - 1
    ok = gain >= MIN_GAIN and wins_both and hit_ok
    why = 'gain %.4f, wins both halves' % gain if ok else 'differs by %.4f (%s%s%s)' % (
        gain, 'small' if gain < MIN_GAIN else '', ' one half only' if not wins_both else '', ' hits' if not hit_ok else '')
    return ok, gain, {'why': why, 'n': len(days), 'cand': c['all'], 'base': b['all']}


def tune(cfg, prev):
    key = cfg['key']
    if not os.path.exists(os.path.join(HERE, '..', cfg['out'])):
        return {'active': None, 'why': 'no record'}
    cur = defaults_of(cfg)
    if prev.get(key, {}).get('active'):
        cur.update({k: v for k, v in prev[key]['active'].items() if k in KNOBS})
    memo, tested, changes = {}, {}, []

    def scored(st):
        k = key_of(st)
        if k not in memo:
            try:
                memo[k] = replay_with(cfg, st)
            except Exception as e:
                print('  %s %s failed: %s' % (key, st, e), flush=True)
                memo[k] = None
        return memo[k]

    base = scored(cur)
    if not base:
        return {'active': prev.get(key, {}).get('active'), 'why': 'incumbent replay failed'}
    if len(base) < MIN_DAYS:
        return {'active': prev.get(key, {}).get('active'), 'why': 'only %d days' % len(base), 'n': len(base)}
    for p in range(PASSES):
        moved = False
        for knob, vals in KNOBS.items():
            best = None
            for v in vals:
                if v == cur[knob]:
                    continue
                st = dict(cur); st[knob] = v
                h = scored(st)
                if not h:
                    continue
                ok, gain, info = beats(h, base)
                sc = score(h, sorted(h))
                tested[key_of(st)] = {'brier': round(sc['brier'], 4) if sc['brier'] is not None else None, 'hits': sc['hits'], 'vs': info.get('why')}
                if ok and (best is None or gain > best[2]):
                    best = (st, h, gain, info)
            if best:
                changes.append('%s %r -> %r (%s)' % (knob, cur[knob], best[0][knob], best[3]['why']))
                cur, base = best[0], best[1]
                moved = True
        if not moved:
            break
    sc = score(base, sorted(base))
    tested[key_of(cur)] = {'brier': round(sc['brier'], 4) if sc['brier'] is not None else None, 'hits': sc['hits'], 'vs': 'incumbent'}
    return {'active': cur, 'defaults': defaults_of(cfg), 'why': ('; '.join(changes) if changes else 'current stands'),
            'n': len(base), 'brier': round(sc['brier'], 4) if sc['brier'] is not None else None, 'hits': sc['hits'],
            'replays': len([v for v in memo.values() if v]), 'tested': tested,
            'chosen_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d') if changes else prev.get(key, {}).get('chosen_at')}


def main():
    prev = json.load(open(OUT)) if os.path.exists(OUT) else {}
    only = None
    if '--city' in sys.argv:
        only = sys.argv[sys.argv.index('--city') + 1].split(',')
    out = dict(prev)
    for cfg in K.MARKETS:
        if only and not any(cfg['key'].startswith(o) for o in only):
            continue
        print('=== %s' % cfg['key'], flush=True)
        r = tune(cfg, prev)
        out[cfg['key']] = r
        print('  ->', r.get('why'), '| active', r.get('active'), '| %s replays' % r.get('replays'), flush=True)
    out['_built'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    json.dump(out, open(OUT, 'w'), indent=1)
    print('wrote', OUT)


if __name__ == '__main__':
    main()

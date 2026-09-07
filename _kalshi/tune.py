#!/usr/bin/env python3
"""
SELF-TUNING WITH GUARDRAILS (2026-09-07). The per-city settings -- skill-weighted
consensus or the equal mean, the recency window on the bias, the spread
multiplier -- were set by hand from one-off studies and went stale the moment
the season turned. This replays the CURRENT model over the frozen record with
each setting flipped (the rescore harness, walk-forward), and writes the
winner to tuned.json for the bake to apply. Weekly, not nightly: eight replays
per city.

The guardrails are the point, not the search. A candidate wins only when
  * the city has at least 45 scored days,
  * its ladder Brier beats the current setting's by 0.006 or more,
  * it also wins on BOTH halves of the record (the older days and the recent
    days), so a summer fit cannot buy the whole year,
  * and it does not lose more than one bracket hit.
Otherwise the current setting stands. Every change is logged with its numbers,
and the bake stamps the settings in force on every lock (lock.params), so a
regression is attributable to the week it happened.
"""
import copy, json, os, sys, datetime, itertools
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kalshi_daily as K          # noqa: E402
import rescore as R               # noqa: E402
OUT = os.path.join(HERE, 'tuned.json')
GRID = {'skill': [False, True], 'bias_hl': [None, 7], 'sd_mult': [0.75, 1.0]}
MIN_DAYS = 45
MIN_GAIN = 0.006


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


def settings_of(cfg):
    return {'skill': bool(cfg.get('skill', True)), 'bias_hl': cfg.get('bias_hl'), 'sd_mult': float(cfg.get('sd_mult', 1.0))}


def replay_with(cfg, st):
    c = copy.deepcopy(cfg); c.update(st)
    # the replay only rescores the record when the bake is asked to backfill
    if '--backfill' not in sys.argv:
        sys.argv.append('--backfill')
    doc = R.replay(c)
    return {h['date']: h for h in doc.get('history', []) if h.get('actual') is not None and 'lock' in h}


def tune(cfg, prev):
    key = cfg['key']
    frozen_path = os.path.join(HERE, '..', cfg['out'])
    if not os.path.exists(frozen_path):
        return {'active': None, 'why': 'no record'}
    cur = settings_of(cfg)
    if prev.get(key, {}).get('active'):
        cur.update(prev[key]['active'])          # the tuned setting is the incumbent
    results = {}
    for combo in itertools.product(*GRID.values()):
        st = dict(zip(GRID.keys(), combo))
        try:
            results[json.dumps(st, sort_keys=True)] = replay_with(cfg, st)
        except Exception as e:
            print('  %s %s failed: %s' % (key, st, e))
    if not results:
        return {'active': prev.get(key, {}).get('active'), 'why': 'replays failed'}
    days = sorted(set.intersection(*[set(h) for h in results.values()]))
    if len(days) < MIN_DAYS:
        return {'active': prev.get(key, {}).get('active'), 'why': 'only %d days' % len(days), 'n': len(days)}
    half = len(days) // 2
    old, new = days[:half], days[half:]
    table = {}
    for k, hist in results.items():
        table[k] = {'all': score(hist, days), 'old': score(hist, old), 'new': score(hist, new)}
    ck = json.dumps(cur, sort_keys=True)
    base = table.get(ck) or min(table.values(), key=lambda t: t['all']['brier'])
    best_k, best = min(table.items(), key=lambda kv: kv[1]['all']['brier'])
    chosen = None; why = 'current stands'
    if best_k != ck:
        gain = base['all']['brier'] - best['all']['brier']
        wins_both = best['old']['brier'] < base['old']['brier'] and best['new']['brier'] < base['new']['brier']
        hit_ok = best['all']['hits'] >= base['all']['hits'] - 1
        if gain >= MIN_GAIN and wins_both and hit_ok:
            chosen = json.loads(best_k); why = 'gain %.4f, wins both halves' % gain
        else:
            why = 'best differs by %.4f (%s%s%s)' % (gain, 'small' if gain < MIN_GAIN else '', ' one half only' if not wins_both else '', ' hits' if not hit_ok else '')
    active = chosen if chosen is not None else prev.get(key, {}).get('active')
    return {'active': active, 'current': cur, 'why': why, 'n': len(days),
            'tested': {k: {'brier': round(v['all']['brier'], 4), 'hits': v['all']['hits'],
                           'old': round(v['old']['brier'], 4), 'new': round(v['new']['brier'], 4)} for k, v in table.items()},
            'chosen_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d') if chosen is not None else prev.get(key, {}).get('chosen_at')}


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
        print('  ->', r.get('why'), '| active', r.get('active'), flush=True)
    out['_built'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    json.dump(out, open(OUT, 'w'), indent=1)
    print('wrote', OUT)


if __name__ == '__main__':
    main()

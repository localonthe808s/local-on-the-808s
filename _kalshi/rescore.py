#!/usr/bin/env python3
"""Would today's model have done better on the days already scored?

THE RECORD IS FROZEN ON PURPOSE.  Each day's decision is written once and never
rewritten, and the scheduled job never runs backfill -- that is what makes the
record a forecast score instead of hindsight. A model that quietly re-marks its
own homework every time it changes will always look excellent.

But frozen also means the record answers a question about a model version that
may no longer exist. After a change, "did that help?" is unanswerable from it.

So: this replays the CURRENT model over the same historical days, in a scratch
file, and diffs it against what is stored. It writes nothing to the record and
is not wired into any schedule. Run it after a change; keep the number it gives
you next to the frozen one, never in place of it.

    python3 _kalshi/rescore.py            # New York
    python3 _kalshi/rescore.py --all      # all seven markets

A DIFFERENCE IS NOT NECESSARILY A CODE CHANGE.  The replay refetches its inputs,
and those move on their own: model archives get revised, and each day's bias is
fitted on the days before it, so one changed day can nudge its neighbours. On a
run with no code change at all, 5 of 453 day-markets picked a different bracket.
Treat single-digit differences as the noise floor, not as a result.

WHAT IT CANNOT TELL YOU.  The historical days are the days the model was tuned
on -- bias windows, the peak offset, the spread. A backtest over them is
in-sample and flatters itself, and this diff inherits that. It answers "is the
new version better than the old ON THESE DAYS", which is worth knowing and is
not the same as "is it better".
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kalshi_daily as K                                   # noqa: E402

SCRATCH = os.path.join(HERE, '_rescore')


def replay(cfg):
    """Run the current model over every settled day, into a scratch file."""
    os.makedirs(SCRATCH, exist_ok=True)
    c = copy.deepcopy(cfg)
    c['out'] = os.path.join('_kalshi', '_rescore', os.path.basename(cfg['out']))
    # a clean slate, so backfill recomputes every day rather than filling gaps
    real_load = K.load_log
    K.load_log = lambda out: {'history': []}
    try:
        K.run_market(c)
    finally:
        K.load_log = real_load
    return json.load(open(os.path.join(HERE, '..', c['out'])))


def line(tag, t):
    if not t or not t.get('n'):
        return '  %-9s --' % tag
    return ('  %-9s %3d days  %3d hit  %5.1f%%   mae %s'
            % (tag, t['n'], t['hits'], 100.0 * t['hits'] / t['n'],
               ('%.2f' % t['mae']) if t.get('mae') is not None else ' -  '))


def compare(cfg):
    out = os.path.join(HERE, '..', cfg['out'])
    if not os.path.exists(out):
        print('%s: no stored record' % cfg['key']); return None
    frozen = json.load(open(out))
    print('\n=== %s ===' % cfg.get('city', cfg['key']))
    fresh = replay(cfg)

    fh = {h['date']: h for h in frozen.get('history', [])
          if h.get('actual') is not None and 'lock' in h}
    nh = {h['date']: h for h in fresh.get('history', [])
          if h.get('actual') is not None and 'lock' in h}
    both = sorted(set(fh) & set(nh))
    if not both:
        print('  no overlapping scored days'); return None

    def tal(src, keys):
        rows = [src[k] for k in keys]
        e = [h['err'] for h in rows if h.get('err') is not None]
        return {'n': len(rows), 'hits': sum(1 for h in rows if h.get('hit')),
                'mae': (sum(abs(x) for x in e) / len(e)) if e else None}

    print('  on the %d days both versions scored:' % len(both))
    print(line('STORED', tal(fh, both)))
    print(line('TODAY', tal(nh, both)))

    # A FLIPPED VERDICT IS NOT AUTOMATICALLY A MODEL CHANGE, and reporting it
    # as one is how a tool like this starts lying. Chicago 2026-09-04 flipped to
    # a hit on the first run of this script with the pick completely unchanged:
    # the day had been scored provisionally from an observation and the market
    # has since settled a bracket higher. That is the record correcting itself,
    # and crediting it to the model would be exactly the self-flattery the
    # frozen record exists to prevent. So each flip is attributed.
    flips = [(k, fh[k], nh[k]) for k in both if bool(fh[k].get('hit')) != bool(nh[k].get('hit'))]
    model = [f for f in flips if f[1]['lock'].get('pick') != f[2]['lock'].get('pick')]
    truth = [f for f in flips if f[1]['lock'].get('pick') == f[2]['lock'].get('pick')]
    g = sum(1 for f in model if f[2].get('hit'))
    print('  %d verdicts moved: %d from a different PICK (%d gained, %d lost), '
          '%d from the TRUTH resolving' % (len(flips), len(model), g, len(model) - g,
                                           len(truth)))
    for k, o, n in model[:10]:
        print('    MODEL %s  actual %-5s  %-14s -> %-14s  %s'
              % (k, o.get('actual'), o['lock'].get('pick'), n['lock'].get('pick'),
                 'GAINED' if n.get('hit') else 'lost'))
    for k, o, n in truth[:10]:
        print('    TRUTH %s  actual %-5s  pick %-14s unchanged; bracket %s -> %s'
              % (k, o.get('actual'), o['lock'].get('pick'),
                 o.get('actual_bracket'), n.get('actual_bracket')))

    # the picks that did not flip a verdict can still have moved
    moved = sum(1 for k in both if fh[k]['lock'].get('pick') != nh[k]['lock'].get('pick'))
    print('  %d of %d picks differ at all' % (moved, len(both)))
    return {'city': cfg.get('city', cfg['key']), 'days': len(both),
            'was': tal(fh, both)['hits'], 'now': tal(nh, both)['hits'],
            'model_flips': len(model), 'gained': g, 'lost': len(model) - g,
            'truth_flips': len(truth), 'moved': moved}


def main():
    keys = [c for c in K.MARKETS] if '--all' in sys.argv else \
           [c for c in K.MARKETS if c['key'] == 'ny_high']
    # --city las   replays one market; BV_SKILL=0 / BV_BIAS_HL=7 override its
    # model settings so a variant can be scored without editing MARKETS
    if '--city' in sys.argv:
        only = sys.argv[sys.argv.index('--city') + 1]
        keys = [c for c in K.MARKETS if c['key'].startswith(only)]
    for c in keys:
        if os.environ.get('BV_SKILL') is not None:
            c['skill'] = os.environ['BV_SKILL'] not in ('0', 'false', 'no')
        if os.environ.get('BV_BIAS_HL'):
            c['bias_hl'] = float(os.environ['BV_BIAS_HL'])
        print('  %s: skill=%s bias_hl=%s' % (c['key'], c['skill'], c.get('bias_hl')))
    print('REPLAYING TODAY\'S MODEL OVER THE FROZEN RECORD')
    print('nothing here is written back; the record stays as it was decided.')
    tot = []
    for cfg in keys:
        try:
            r = compare(cfg)
            if r:
                tot.append(r)
        except Exception as e:
            print('  %s failed: %s: %s' % (cfg['key'], type(e).__name__, e))
    if not tot:
        return 1

    days = sum(t['days'] for t in tot)
    was = sum(t['was'] for t in tot)
    now = sum(t['now'] for t in tot)
    moved = sum(t['moved'] for t in tot)
    print('\n' + '=' * 58)
    print('OVERALL  %d markets, %d scored day-markets' % (len(tot), days))
    print('  stored  %d hits  %.1f%%' % (was, 100.0 * was / days))
    print('  today   %d hits  %.1f%%   (%+d)' % (now, 100.0 * now / days, now - was))
    print('  %d picks differ (%.1f%%), %d verdicts moved on a changed pick'
          % (moved, 100.0 * moved / days, sum(t['model_flips'] for t in tot)))
    # THE NOISE FLOOR IS REAL AND HAS BEEN MEASURED. With no code change at all,
    # a replay moved 5 of 453 picks -- the archives it refetches get revised,
    # and each day's bias is fitted on the days before it. Calling a two-day
    # improvement a win is how a model gets tuned into a backtest.
    verdict = ('NOISE -- below the measured floor of ~5 days; not a result'
               if abs(now - was) <= 5 else
               ('BETTER by %d days' % (now - was)) if now > was else
               ('WORSE by %d days' % (was - now)))
    print('  VERDICT: %s' % verdict)
    print('=' * 58)
    return 0


if __name__ == '__main__':
    if '--backfill' not in sys.argv:
        sys.argv.append('--backfill')          # the whole point of this script
    raise SystemExit(main())

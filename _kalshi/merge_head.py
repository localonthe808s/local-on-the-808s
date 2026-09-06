#!/usr/bin/env python3
"""Merge two copies of the head file (kalshi_ny.json) when a push is rejected.

Two jobs write it now: the 20-city bake every 15 minutes and the New York
fast lane every 5. When one's push lands while the other is running, the
loser used to lay its whole file over the winner's -- which, with two lanes,
would roll New York back by a few minutes several times an hour. The rule:
the body (today, record, history) comes from whichever file is newer, and
each market's digest comes from whichever copy baked it later.

    python3 _kalshi/merge_head.py <origin's copy> <our copy> <output>
"""
import json
import sys


def main(base_path, mine_path, out_path):
    with open(base_path) as f:
        base = json.load(f)
    with open(mine_path) as f:
        mine = json.load(f)
    newer, older = (mine, base) if (mine.get('updated_utc') or '') >= (base.get('updated_utc') or '') else (base, mine)
    doc = dict(newer)
    by = {}
    for src in (older, newer):
        for m in (src.get('markets') or []):
            k = m.get('key')
            if k and (k not in by or (m.get('baked') or '') >= (by[k].get('baked') or '')):
                by[k] = m
    order = [m.get('key') for m in (newer.get('markets') or [])] or list(by)
    doc['markets'] = [by[k] for k in order if k in by] + [by[k] for k in by if k not in order]
    with open(out_path, 'w') as f:
        json.dump(doc, f, separators=(',', ':'))
    print('merged: body from %s (%s), %d market digests, newest per market'
          % ('ours' if newer is mine else 'origin', doc.get('updated_utc'), len(doc['markets'])))


if __name__ == '__main__':
    main(*sys.argv[1:4])

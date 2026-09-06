// Cloudflare Worker: a reliable clock for the Kalshi daily job.
//
// WHY THIS EXISTS.  GitHub queues scheduled workflows on a best-effort basis and
// skips most of them under load: on the job's first day it fired 2 of ~10 slots,
// and never once in the 6pm window that writes the day's final call. Cloudflare
// cron triggers actually fire. So Cloudflare keeps the time and GitHub still does
// the work.
//
// WHY IT DOES NOT DO THE WORK ITSELF.  The forecast lives in
// _kalshi/kalshi_daily.py — five-model consensus, per-model rolling bias, a
// measured spread, the climate-day floor. Re-implementing that in JS would fork
// it, and the two copies would drift apart on the first change. This worker only
// presses the button.
//
// SETUP (all in your hands, no secrets in this repo):
//   1. Create a GitHub fine-grained personal access token
//        Settings -> Developer settings -> Personal access tokens -> Fine-grained
//        Repository access: only localonthe808s/bluish-void
//        Repository permissions: Actions = Read and write   (nothing else)
//   2. cd _kalshi/cron-worker && wrangler secret put GH_TOKEN
//        (paste the token when prompted; it is stored by Cloudflare, never here)
//   3. wrangler deploy
//
// Check it: GET the worker's URL for a status page. It never triggers anything —
// an open trigger endpoint is an invitation to abuse — so use `wrangler tail` or
// the Actions tab to watch the dispatches land.

const OWNER = 'localonthe808s';
const REPO = 'bluish-void';
const WORKFLOW = 'kalshi-nyc.yml';
const WORKFLOW_FAST = 'kalshi-nyc-fast.yml';   // Central Park alone, on the other five-minute marks
const REF = 'main';

async function dispatch(env, workflow) {
  if (!env.GH_TOKEN) {
    return { ok: false, status: 0, detail: 'GH_TOKEN secret is not set' };
  }
  const url = `https://api.github.com/repos/${OWNER}/${REPO}` +
              `/actions/workflows/${workflow || WORKFLOW}/dispatches`;
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.GH_TOKEN}`,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      // GitHub rejects API requests without one
      'User-Agent': 'bluishvoid-kalshi-cron'
    },
    body: JSON.stringify({ ref: REF })
  });
  // 204 No Content is success here; anything else carries a reason worth logging
  const detail = res.status === 204 ? '' : (await res.text()).slice(0, 300);
  return { ok: res.status === 204, status: res.status, detail };
}

// ---------------------------------------------------------------- PRIVATE ----
// GET /positions -- what the account actually holds, for the panel.
//
// WHY IT LIVES HERE AND NOT IN THE REPO. bluishvoid.com is GitHub Pages: every
// file it serves is world-readable, and a JS gate or an overlay on the popup is
// decoration -- `curl` never touches the page. Anything the browser can show
// without a server checking who is asking IS public. So positions are not baked
// into kalshi_*.json at all. They are fetched here, behind a token, by a worker
// that already holds secrets and sits on a domain we control.
//
// The threat this actually addresses is a stranger reading a public URL. A token
// in localStorage does not defend against someone using the owner's own browser,
// and is not claimed to.
//
// SETUP (three secrets, none of them in this repo):
// THE KEY MUST BE READ-ONLY. Kalshi scopes API keys, and this worker only ever
// reads: grant `read` and nothing else. `write::trade` and `write::transfer` are
// what would let a leaked PANEL_TOKEN place orders or move money, and nothing
// here needs them. Kalshi does not document a per-endpoint scope for
// /portfolio/positions; `read` is the parent of the read endpoints, so it is the
// right grant, and a 403 from this endpoint would be the signal it is not.
//
//   wrangler secret put KALSHI_API_KEY_ID     the key's uuid
//   wrangler secret put KALSHI_PRIVATE_KEY    the PEM, newlines and all
//   wrangler secret put PANEL_TOKEN           any long random string you invent
//
// Kalshi signs with RSA-PSS/SHA-256 over `timestamp + METHOD + path`, salt length
// equal to the digest (32). The query string is NOT covered -- the same rule the
// Python side documents, and getting it wrong returns a 401 that looks like a bad
// key.
const KALSHI = 'https://api.elections.kalshi.com';

function pemToDer(pem) {
  const txt = pem.replace(/\\n/g, '\n');
  // Kalshi hands out an RSA_PRIVATE_KEY, which is PKCS#1. Python's
  // load_pem_private_key takes either, so the GitHub job never noticed.
  // WebCrypto takes PKCS#8 ONLY, and rejects PKCS#1 with a DataError that
  // surfaces here as an unexplained 502. Say what is wrong instead.
  if (/BEGIN RSA PRIVATE KEY/.test(txt)) {
    throw new Error('private key is PKCS#1; WebCrypto needs PKCS#8. Convert it: '
      + 'openssl pkcs8 -topk8 -nocrypt -in kalshi-key.pem -out kalshi-key-pkcs8.pem');
  }
  const b64 = txt.replace(/-----[A-Z ]+-----/g, '').replace(/\s+/g, '');
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out.buffer;
}

async function kalshiGet(env, path, query) {
  const key = await crypto.subtle.importKey(
    'pkcs8', pemToDer(env.KALSHI_PRIVATE_KEY),
    { name: 'RSA-PSS', hash: 'SHA-256' }, false, ['sign']);
  const ts = String(Date.now());
  const sig = await crypto.subtle.sign(
    { name: 'RSA-PSS', saltLength: 32 }, key,
    new TextEncoder().encode(ts + 'GET' + path));
  const res = await fetch(KALSHI + path + (query || ''), {
    headers: {
      'KALSHI-ACCESS-KEY': env.KALSHI_API_KEY_ID,
      'KALSHI-ACCESS-TIMESTAMP': ts,
      'KALSHI-ACCESS-SIGNATURE': btoa(String.fromCharCode(...new Uint8Array(sig))),
      'Accept': 'application/json',
      'User-Agent': 'bluishvoid-kalshi-cron'
    }
  });
  if (!res.ok) throw new Error('kalshi ' + path + ' -> ' + res.status);
  return res.json();
}

// Constant-time-ish compare, so a failure does not leak the token by timing.
// BOTH SIDES ARE TRIMMED: a secret pasted into a dashboard field very often
// carries a trailing newline, and comparing lengths first turns that invisible
// character into a flat 401 that looks exactly like a wrong token.
function tokenOk(given, want) {
  given = String(given || '').trim();
  want = String(want || '').trim();
  if (!want || !given || given.length !== want.length) return false;
  let d = 0;
  for (let i = 0; i < given.length; i++) d |= given.charCodeAt(i) ^ want.charCodeAt(i);
  return d === 0;
}

function cors(origin) {
  return {
    'Access-Control-Allow-Origin': origin,
    'Access-Control-Allow-Headers': 'authorization',
    'Access-Control-Max-Age': '86400',
    'Vary': 'Origin'
  };
}
const ALLOWED = 'https://bluishvoid.com';

async function obsLead(request, env) {
  // PUBLIC, unlike /obs. This returns aggregates and no raw rows, it is one KV
  // get rather than a namespace listing, and the page has to reach it on a plain
  // load -- a token in client JS is not a token. The raw trail stays gated.
  if (!env.OBS) {
    return new Response(JSON.stringify({ error: 'no KV binding' }), {
      status: 503, headers: { 'content-type': 'application/json', ...cors(ALLOWED) } });
  }
  const sum = await env.OBS.get(LEAD_KEY, { type: 'json' });
  return new Response(JSON.stringify(sum || { days: {}, ticks: 0 }), {
    headers: {
      'content-type': 'application/json; charset=utf-8',
      // a tick is five minutes; there is no point re-asking sooner
      'cache-control': 'public, max-age=120',
      ...cors(ALLOWED)
    } });
}


async function obsDump(request, env) {
  // Behind the same token as /positions. The trail is not secret, but an open
  // endpoint that lists a KV namespace is a free way for anyone to burn the
  // day's read quota and blind the study.
  const url = new URL(request.url);
  const given = (request.headers.get('authorization') || '').replace(/^Bearer\s+/i, '')
                || url.searchParams.get('t') || '';
  if (!tokenOk(given, env.PANEL_TOKEN)) {
    return new Response(JSON.stringify({ error: 'unauthorized' }), {
      status: 401, headers: { 'content-type': 'application/json', ...cors(ALLOWED) } });
  }
  if (!env.OBS) {
    return new Response(JSON.stringify({ error: 'no KV binding' }), {
      status: 503, headers: { 'content-type': 'application/json', ...cors(ALLOWED) } });
  }
  // `since` is a plain ISO prefix, so obs_lead.py can pull only what it has not
  // seen. KV keys sort lexicographically and the timestamp does too.
  const since = url.searchParams.get('since') || '';
  const out = [];
  let cursor;
  do {
    const page = await env.OBS.list({ prefix: 'obs:', cursor, limit: 1000 });
    for (const k of page.keys) {
      if (since && k.name <= `obs:${since}`) continue;
      out.push(k.name);
    }
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor && out.length < 4000);
  out.sort();
  // JSONL, matching what obs_log.py writes locally, so one reader handles both.
  const body = [];
  for (const name of out.slice(0, 2000)) {
    const v = await env.OBS.get(name);
    if (!v) continue;
    try { for (const r of JSON.parse(v)) body.push(JSON.stringify(r)); } catch (e) { /* skip */ }
  }
  return new Response(body.join('\n') + '\n', {
    headers: { 'content-type': 'application/x-ndjson; charset=utf-8', ...cors(ALLOWED) } });
}


async function positions(request, env) {
  const url = new URL(request.url);
  const given = (request.headers.get('authorization') || '').replace(/^Bearer\s+/i, '')
                || url.searchParams.get('t') || '';
  if (!tokenOk(given, env.PANEL_TOKEN)) {
    return new Response(JSON.stringify({ error: 'unauthorized' }), {
      status: 401,
      headers: { 'content-type': 'application/json', ...cors(ALLOWED) }
    });
  }
  try {
    const [bal, pos] = await Promise.all([
      kalshiGet(env, '/trade-api/v2/portfolio/balance'),
      // count_filter=position asks the exchange for rows with a non-zero
      // position, which is the whole question here. settlement_status is NOT a
      // parameter of this endpoint -- it is on /portfolio/settlements -- and
      // sending it invites a 400 that reads like an auth failure.
      kalshiGet(env, '/trade-api/v2/portfolio/positions',
                '?count_filter=position&limit=500')
    ]);
    const cash = bal.balance_dollars != null
      ? Number(bal.balance_dollars) : Number(bal.balance || 0) / 100;
    // FIELD NAMES AND UNITS, READ FROM THE SCHEMA RATHER THAN GUESSED. The first
    // attempt used `position` and treated the money as cents; the endpoint
    // answered with nulls and zeroes rather than an error, which is the worst
    // kind of wrong. The real names are position_fp (signed: negative is NO) and
    // *_dollars, and the dollar fields are fixed-point STRINGS already in
    // dollars -- dividing by 100 was inventing a hundredfold error.
    const num = (v) => { const x = Number(v); return isFinite(x) ? x : 0; };
    const held = (pos.market_positions || [])
      .map((m) => {
        const n = num(m.position_fp !== undefined ? m.position_fp : m.position);
        return {
          ticker: m.ticker,
          side: n > 0 ? 'yes' : 'no',
          contracts: Math.abs(n),
          exposure: num(m.market_exposure_dollars),
          traded: num(m.total_traded_dollars),
          realized: num(m.realized_pnl_dollars),
          fees: num(m.fees_paid_dollars)
        };
      })
      .filter((h) => h.contracts !== 0);
    const exposure = held.reduce((a, h) => a + h.exposure, 0);
    return new Response(JSON.stringify({
      at: new Date().toISOString(),
      cash: Math.round(cash * 100) / 100,
      // AT COST, and named that way. market_exposure_dollars is what the
      // position cost, not what it is worth now -- calling the sum "equity"
      // said $28 while the same positions were worth about $71 on the screen.
      // Sizing wants market value, which needs live prices the panel already
      // holds: contracts x the current bid. That multiplication belongs there,
      // not here, so this returns the honest input and lets the page finish it.
      cost_basis: Math.round((cash + exposure) * 100) / 100,
      positions: held
    }), { headers: { 'content-type': 'application/json',
                     'cache-control': 'no-store', ...cors(ALLOWED) } });
  } catch (e) {
    return new Response(JSON.stringify({ error: String(e) }), {
      status: 502, headers: { 'content-type': 'application/json', ...cors(ALLOWED) }
    });
  }
}

// -------------------------------------------------------- observation log ----
//
// WHY THE WORKER AND NOT THE LAPTOP.  This records TWC's
// temperatureMaxSince7Am, a running maximum with intra-hour peaks in it. It is a
// CURRENT-ONLY field: there is no archive and no backfill. If nothing asks at
// 2:35 PM, that reading does not exist afterwards -- unlike IEM daily, the METAR
// and the six-hourly groups, which can all be re-fetched for any past day. So a
// logger that stops when a lid closes loses precisely the quantity it was built
// to measure, and permanently. A laptop cannot hold this job.
//
// WHY IT DOES THE WORK HERE, when the rest of this file deliberately does not:
// the thing being measured is LEAD IN MINUTES. Dispatching a GitHub runner adds
// thirty to sixty seconds of variable startup to every timestamp, which is noise
// laid directly on top of the signal. Four fetches and a KV write is not a
// forecast model, so there is no second copy to drift.
//
// 2026-09-05 is why: Central Park peaked at 79 at 2:33 PM, the 2:51 METAR read
// 77.0 because it had already fallen back, and the market repriced "78 or below"
// from 84c to 2c at about 4:25 PM -- before the 4:43 PM climate report. max7 held
// 79 the whole time. Whether that lead is real, and whether max7's spikes (that
// same day Chicago read 86 against IEM's 83 with the market 100% on 83-84, and it
// had not retracted hours later) make it unusable, is what this decides.
const OBS_MARKETS = [
  // key,      ICAO,   IEM network, IEM station -- the three cities the sheet
  // trades (2026-09-06); the other four were dropped with the rest
  ['ny_high',  'KNYC', 'NY_ASOS', 'NYC'],
  ['las_high', 'KLAS', 'NV_ASOS', 'LAS'],
  ['aus_high', 'KAUS', 'TX_ASOS', 'AUS']
];
// THE 5-MINUTE FEEDS, per market. New York's settlement sensor (Central Park)
// has no public 5-minute stream, so the three airports stand in for it. Las
// Vegas settles on Harry Reid (KLAS) and Austin on Bergstrom (KAUS) -- FAA
// airports, and their 5-minute observations ARE the settlement sensor's own.
const APT5 = {
  ny_high:  { stids: 'KLGA,KJFK,KEWR', tz: 'America/New_York' },
  las_high: { stids: 'KLAS,KVGT',      tz: 'America/Los_Angeles' },
  aus_high: { stids: 'KAUS,KATT',      tz: 'America/Chicago' }
};

// THE STATION TRAP, and it cost a whole 45-day study before it was found.
//   v3 /wx/observations/current?icaoCode=KNYC -> Central Park   RIGHT
//   v1 /location/KNYC:9:US/observations/...   -> LaGuardia      WRONG
// Same ICAO, two endpoints, two different stations three miles and two degrees
// apart. Only the v3 current form is used here. `language` is REQUIRED: without
// it the answer is HTTP 400 with every field null, which reads like a station
// outage rather than a malformed request.
const TWC_KEY = 'e1f10a1e78da46f5b10a1e78da96f525';

async function obsSnapshot(env) {
  const t = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  const rows = [];
  // One batched METAR call for all seven, rather than seven -- subrequests are
  // capped per invocation and this is the only field that batches.
  let metar = {};
  try {
    const ids = OBS_MARKETS.map((m) => m[1]).join(',');
    const r = await fetch(
      `https://aviationweather.gov/api/data/metar?ids=${ids}&format=json&hours=3`,
      { headers: { 'User-Agent': 'bluishvoid-obs-log' } });
    if (r.ok) {
      for (const m of await r.json()) {
        if (m && m.temp != null && m.icaoId) {
          const f = Math.round((m.temp * 9 / 5 + 32) * 10) / 10;
          const cur = metar[m.icaoId];
          if (!cur || m.reportTime > cur.at) metar[m.icaoId] = { at: m.reportTime, f };
        }
      }
    }
  } catch (e) { /* one dead source must not cost the tick */ }

  await Promise.all(OBS_MARKETS.map(async ([key, icao, net, stn]) => {
    const row = { t, key };
    const mt = metar[icao];
    if (mt) { row.metar = mt.f; row.metar_at = mt.at; }
    try {
      const r = await fetch('https://api.weather.com/v3/wx/observations/current'
        + `?icaoCode=${icao}&units=e&language=en-US&format=json&apiKey=${TWC_KEY}`);
      if (r.ok) {
        const j = await r.json();
        if (typeof j.temperatureMaxSince7Am === 'number') row.max7 = j.temperatureMaxSince7Am;
        if (typeof j.temperature === 'number') row.now = j.temperature;
        if (typeof j.temperatureMax24Hour === 'number') row.max24 = j.temperatureMax24Hour;
      } else { row.err_twc = `http ${r.status}`; }
    } catch (e) { row.err_twc = String(e).slice(0, 60); }
    try {
      // The station's LOCAL date, which is what IEM's daily row is keyed by --
      // asking UTC would request tomorrow for half the day in the west.
      const d = new Date().toLocaleDateString('en-CA', { timeZone: {
        ny_high: 'America/New_York', chi_high: 'America/Chicago',
        mia_high: 'America/New_York', aus_high: 'America/Chicago',
        den_high: 'America/Denver',   lax_high: 'America/Los_Angeles',
        phl_high: 'America/New_York' }[key] });
      row.day = d;
      const [Y, M, D] = d.split('-').map(Number);
      const r = await fetch('https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py'
        + `?network=${net}&stations=${stn}&year1=${Y}&month1=${M}&day1=${D}`
        + `&year2=${Y}&month2=${M}&day2=${D}&format=comma`);
      if (r.ok) {
        const txt = await r.text();
        const lines = txt.trim().split('\n');
        const head = lines[0].split(','); const col = head.indexOf('max_temp_f');
        if (col > 0 && lines.length > 1) {
          const v = parseFloat(lines[lines.length - 1].split(',')[col]);
          if (!isNaN(v)) row.iem = v;
        }
      } else { row.err_iem = `http ${r.status}`; }
    } catch (e) { row.err_iem = String(e).slice(0, 60); }
    rows.push(row);
  }));
  // THE AIRPORTS' 5-MINUTE READINGS, as a regional early warning for New York.
  // Central Park's own 5-minute stream is not public (Synoptic carries it for
  // LaGuardia, JFK and Newark, hourly only for the park -- checked 2026-09-06).
  // So the three airports' 5-minute maxima since 7 AM ride on the New York
  // row, to be judged against TWC's field and the settlement: when the region
  // is peaking between the hourly reports, the park usually is too.
  if (env && env.SYNOPTIC_TOKEN) {
    for (const key of Object.keys(APT5)) {
      const row = rows.find((x) => x.key === key);
      if (!row) continue;
      try {
        const r = await fetch(`https://api.synopticdata.com/v2/stations/timeseries?stid=${APT5[key].stids}` +
          `&recent=720&vars=air_temp&units=temp|F&obtimezone=local&token=${env.SYNOPTIC_TOKEN}`);
        if (!r.ok) continue;
        const j = await r.json();
        row.apt5 = {};
        // TODAY ONLY, in the market's own zone. The window reaches back twelve
        // hours, and filtering by the hour alone let yesterday evening's
        // readings count as "since 7 AM".
        const today = new Intl.DateTimeFormat('en-CA', { timeZone: APT5[key].tz,
          year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
        for (const st of (j.STATION || [])) {
          const ts = (st.OBSERVATIONS || {}).date_time || [], vs = (st.OBSERVATIONS || {}).air_temp_set_1 || [];
          let mx = null, last = null, lastAt = null;
          for (let i = 0; i < ts.length; i++) {
            const v = vs[i]; if (typeof v !== 'number') continue;
            const hh = Number(ts[i].slice(11, 13));
            if (ts[i].slice(0, 10) === today && hh >= 7 && (mx == null || v > mx)) mx = v;
            last = v; lastAt = ts[i].slice(11, 16);
          }
          row.apt5[st.STID] = { max7: mx, last, at: lastAt, n: ts.length };
        }
      } catch (e) { /* the row stands without it */ }
    }
  }
  return { t, rows };
}

// THE STUDY'S RUNNING ANSWER, FOLDED IN A TICK AT A TIME.
//
// The panel cannot scan a KV namespace on a page load, and the question does not
// need raw rows to answer -- it needs, per city-day, the peak each source
// reached and WHEN IT FIRST REACHED IT. That is a few hundred bytes and it can
// be maintained incrementally, so a page view costs one KV get.
//
// "First reached" is tracked against each source's OWN running peak: if a source
// later reads higher, its clock restarts, because the quantity of interest is
// when it arrived at the value the day ends on. A day is only reported once it
// has stopped moving -- while it is still climbing, whoever is merely EARLIEST
// reads as whoever is RIGHT, which is the exact mistake this study exists to
// avoid making twice.
const LEAD_KEY = 'lead:summary';
const LEAD_SOURCES = ['max7', 'iem', 'six'];

function foldLead(sum, snap) {
  sum = sum && typeof sum === 'object' ? sum : { days: {}, first: null };
  if (!sum.first) sum.first = snap.t;
  sum.last = snap.t;
  sum.ticks = (sum.ticks || 0) + 1;
  for (const r of snap.rows) {
    if (!r.day || !r.key) continue;
    const id = `${r.key}|${r.day}`;
    const d = (sum.days[id] = sum.days[id] || {});
    for (const src of LEAD_SOURCES) {
      const v = r[src];
      if (typeof v !== 'number') continue;
      const cur = d[src];
      // strictly higher restarts the clock; equal keeps the FIRST sighting
      if (!cur || v > cur.v + 1e-9) d[src] = { v, at: r.t };
    }
    d.seen = r.t;
    // the airports' 5-minute maxima ride along, latest reading per station,
    // so the public summary can show the regional picture without the raw trail
    if (r.apt5) d.apt5 = { at: r.t, s: r.apt5 };
  }
  // 30 days is far more than any analysis needs and keeps the value small
  const cutoff = new Date(Date.parse(snap.t) - 30 * 864e5).toISOString().slice(0, 10);
  for (const id of Object.keys(sum.days)) {
    if (id.split('|')[1] < cutoff) delete sum.days[id];
  }
  return sum;
}

async function logObs(env) {
  if (!env.OBS) return 'no KV binding';
  const snap = await obsSnapshot(env);
  try {
    const prev = await env.OBS.get(LEAD_KEY, { type: 'json' });
    await env.OBS.put(LEAD_KEY, JSON.stringify(foldLead(prev, snap)));
  } catch (e) { /* the trail itself still gets written below */ }
  // One key per tick. ~192 ticks a day against a 1000/day free write limit, and
  // the key sorts lexicographically because the timestamp does.
  await env.OBS.put(`obs:${snap.t}`, JSON.stringify(snap.rows), {
    expirationTtl: 60 * 60 * 24 * 120        // 120 days is far past any analysis
  });
  const ny = snap.rows.find((r) => r.key === 'ny_high');
  const apt = ny && ny.apt5 ? ' apt5 ' + Object.keys(ny.apt5).map((k) => `${k}:${ny.apt5[k].max7}`).join(' ') : ' apt5 none';
  const lead = snap.rows.filter((r) => r.max7 != null && r.iem != null && r.max7 > r.iem);
  return `${snap.rows.length} rows` + apt + (lead.length
    ? `, max7 above iem: ${lead.map((r) => `${r.key} +${(r.max7 - r.iem).toFixed(1)}`).join(' ')}`
    : '');
}

// ALERTS, so the day does not arrive as a loss. On 2026-09-05 the 79 that
// settled New York surfaced at 4:25 PM and the holder learned it from the
// balance. Every five-minute tick now looks at the things that change a held
// position and pushes a message through ntfy.sh -- a topic the phone
// subscribes to, no account, no key. Three triggers, New York only:
//   1. the climate portal's status for yesterday/today flips (preliminary,
//      official) -- the number that pays, the moment it exists;
//   2. TWC's running maximum crosses a range edge on a rung you hold;
//   3. the market on a rung you hold moves 25 points or more from where it
//      was when you were last told.
// SETUP:  wrangler secret put NTFY_TOPIC   (a long random string; subscribe to
//         https://ntfy.sh/<that string> in the ntfy app). No topic, no alerts.
// State lives in KV under alert:state; a 30-minute cool-down per message key
// stops a flapping market from paging you every five minutes.
const ALERT_KEY = 'alert:state';
const ALERT_SERIES = 'KXHIGHNY';   // kept for the status page
// the markets the alerts watch: series prefix -> the settlement station TWC
// reads (v3 current with the ICAO), its zone, and the name on the phone
const ALERT_MARKETS = [
  { series: 'KXHIGHNY',  icao: 'KNYC', tz: 'America/New_York',    name: 'Central Park' },
  { series: 'KXHIGHTLV', icao: 'KLAS', tz: 'America/Los_Angeles', name: 'Las Vegas' },
  { series: 'KXHIGHAUS', icao: 'KAUS', tz: 'America/Chicago',     name: 'Austin' }
];
const ALERT_COOLDOWN_MS = 30 * 60 * 1000;

function rungBounds(m) {
  // Kalshi states open bounds strictly: less/cap 79 = 78 or below.
  const f = m.floor_strike, c = m.cap_strike, st = m.strike_type;
  if (st === 'between') return [Number(f), Number(c)];
  if (st === 'less' || st === 'less_or_equal') {
    const hi = Number(c != null ? c : f); return [null, st === 'less' ? hi - 1 : hi];
  }
  const lo = Number(f); return [st === 'greater' ? lo + 1 : lo, null];
}
function rungLabel(b) {
  if (b[0] == null) return `${b[1]} or below`;
  if (b[1] == null) return `${b[0]} or above`;
  return `${b[0]} to ${b[1]}`;
}
function inRung(v, b) {
  const r = Math.round(v);
  return (b[0] == null || r >= b[0]) && (b[1] == null || r <= b[1]);
}
async function notify(env, state, key, title, body, priority) {
  const now = Date.now();
  const last = (state.sent || {})[key] || 0;
  if (now - last < ALERT_COOLDOWN_MS) return false;
  const r = await fetch(`https://ntfy.sh/${env.NTFY_TOPIC}`, {
    method: 'POST', body,
    headers: { 'Title': title, 'Priority': priority || 'default' }
  });
  state.sent = state.sent || {};
  state.sent[key] = now;
  return r.ok;
}
function localDate(offsetDays) {
  const d = new Date(Date.now() + offsetDays * 86400000);
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York',
    year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
}
async function alertTick(env) {
  if (!env.NTFY_TOPIC || !env.OBS) return 'alerts off';
  const state = (await env.OBS.get(ALERT_KEY, { type: 'json' })) || {};
  const before = JSON.stringify(state);
  const out = [];
  // 1. the portal, yesterday and today
  state.portal = state.portal || {};
  for (const off of [-1, 0]) {
    const day = localDate(off);
    try {
      const j = await (await fetch(`https://weather.com/kalshi/api/climate/primary?date=${day}`,
        { headers: { 'User-Agent': 'Mozilla/5.0 bluishvoid-alerts' } })).json();
      const row = (j.results || []).find((r) => r.station && r.station.icao === 'KNYC');
      if (!row) continue;
      const st = row.status, v = row.data && row.data.maxTemp;
      const was = state.portal[day];
      if (st !== 'no_report' && st !== was) {
        await notify(env, state, `portal:${day}:${st}`,
          `Central Park ${day}: ${st.toUpperCase()} ${v}°`,
          `weather.com/kalshi shows the ${day} high as ${v}° (${st}). ` +
          (st === 'official' ? 'This is the number that pays.' : 'Preliminary; the final comes ~3 AM.'),
          st === 'official' ? 'high' : 'default');
        out.push(`portal ${day} ${st} ${v}`);
      }
      state.portal[day] = st;
    } catch (e) { out.push(`portal ${day} err`); }
  }
  // 2+3 need the held rungs
  let held = [];
  try {
    const pos = await kalshiGet(env, '/trade-api/v2/portfolio/positions',
                                '?count_filter=position&limit=200');
    held = (pos.market_positions || [])
      .map((m) => ({ ticker: m.ticker, n: Number(m.position_fp != null ? m.position_fp : m.position) }))
      .map((h) => ({ ...h, am: ALERT_MARKETS.find((a) => h.ticker.startsWith(a.series + '-')) }))
      .filter((h) => h.n !== 0 && h.am);
  } catch (e) { out.push('positions err'); }
  if (!held.length) {
    state.mkt = {}; state.max7 = null; state.max7by = {};
    if (JSON.stringify(state) !== before) await env.OBS.put(ALERT_KEY, JSON.stringify(state));
    return out.concat(['no watched position']).join(', ');
  }
  // TWC's running max at each held market's settlement station (v3 current
  // with the ICAO is the station itself; before 7 AM local it is yesterday's)
  const max7by = {};
  for (const am of ALERT_MARKETS) {
    if (!held.some((h) => h.am === am)) continue;
    let v = null;
    try {
      const j = await (await fetch(`https://api.weather.com/v3/wx/observations/current?icaoCode=${am.icao}` +
        `&units=e&language=en-US&format=json&apiKey=${TWC_KEY}`)).json();
      if (typeof j.temperatureMaxSince7Am === 'number') v = j.temperatureMaxSince7Am;
    } catch (e) { /* no reading this tick */ }
    const lh = Number(new Intl.DateTimeFormat('en-US', { timeZone: am.tz, hour: 'numeric', hour12: false }).format(new Date()));
    max7by[am.series] = (lh < 7) ? null : v;
  }
  state.mkt = state.mkt || {};
  state.max7by = state.max7by || {};
  for (const h of held) {
    const max7 = max7by[h.am.series], prevMax7 = state.max7by[h.am.series];
    let m;
    try {
      m = (await (await fetch(`${KALSHI}/trade-api/v2/markets/${h.ticker}`,
        { headers: { 'Accept': 'application/json', 'User-Agent': 'bluishvoid-alerts' } })).json()).market;
    } catch (e) { out.push(`${h.ticker} err`); continue; }
    if (!m) continue;
    const b = rungBounds(m), label = rungLabel(b), side = h.n > 0 ? 'YES' : 'NO';
    const bid = Number(m.yes_bid_dollars), ask = Number(m.yes_ask_dollars);
    const mid = (isFinite(bid) && isFinite(ask) && ask > 0) ? (bid + ask) / 2 : null;
    // 3. the market moved on your rung
    if (mid != null) {
      const anchor = state.mkt[h.ticker];
      if (anchor != null && Math.abs(mid - anchor) >= 0.25) {
        const dir = mid > anchor ? 'up' : 'down';
        const good = (side === 'YES') === (mid > anchor);
        await notify(env, state, `mkt:${h.ticker}:${Math.round(mid * 4)}`,
          `${label}: market ${dir} ${Math.round(anchor * 100)}¢ → ${Math.round(mid * 100)}¢`,
          `You hold ${Math.abs(h.n)} ${side}. The YES price moved from ${Math.round(anchor * 100)}¢ to ` +
          `${Math.round(mid * 100)}¢ -- ${good ? 'in your favour' : 'against you'}. ` +
          `The afternoon market prices the hourly readings, not the peak; the plan is to hold.`,
          good ? 'default' : 'high');
        out.push(`${h.ticker} ${anchor}->${mid}`);
        state.mkt[h.ticker] = mid;
      } else if (anchor == null) {
        state.mkt[h.ticker] = mid;
      }
    }
    // 2. TWC's running max crossed an edge of your rung
    if (max7 != null && prevMax7 != null && max7 !== prevMax7) {
      const wasIn = inRung(prevMax7, b), nowIn = inRung(max7, b);
      const wasAbove = b[1] != null && prevMax7 > b[1] + 0.5, nowAbove = b[1] != null && max7 > b[1] + 0.5;
      if (wasIn !== nowIn || wasAbove !== nowAbove) {
        const bad = (side === 'YES') ? !nowIn : nowIn;
        await notify(env, state, `max7:${h.ticker}:${max7}`,
          `${h.am.name} running max ${max7}° (TWC)`,
          `TWC's temperatureMaxSince7Am went ${prevMax7}° → ${max7}°, ` +
          (nowIn ? `inside ${label}` : nowAbove ? `above ${label}` : `below ${label}`) +
          `. You hold ${Math.abs(h.n)} ${side} on ${label}${bad ? ' -- this is against you' : ''}. ` +
          `max7 is a blended field and has read high before; the climate report decides.`,
          bad ? 'high' : 'default');
        out.push(`max7 ${h.am.series} ${prevMax7}->${max7}`);
      }
    }
  }
  for (const k of Object.keys(max7by)) if (max7by[k] != null) state.max7by[k] = max7by[k];
  // KV allows 1,000 writes a day on this plan; a minute cadence is 1,020 ticks.
  // The state only changes when something happened, so only then is it written.
  if (JSON.stringify(state) !== before) await env.OBS.put(ALERT_KEY, JSON.stringify(state));
  return out.length ? out.join(', ') : `quiet (${held.length} watched position${held.length === 1 ? '' : 's'})`;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      try {
        console.log(`[alerts] ${new Date().toISOString()} ${await alertTick(env)}`);
      } catch (e) {
        console.log(`[alerts] FAILED ${e}`);
      }
    })());
    // EVERY tick logs; only the original four minutes dispatch. The cron went to
    // */5 for the observation trail, and the daily job must not suddenly run
    // twelve times an hour -- it takes ~5 minutes and the runs would overlap.
    // THE CRON IS EVERY MINUTE NOW, for the alerts: on 2026-09-05 the market
    // repriced New York in the minute ending 4:17 PM, and a five-minute tick
    // would have said so at 4:20. The observation log keeps its five-minute
    // cadence (one KV write per tick against a 1,000/day budget) and the
    // daily job its four minutes an hour.
    const minute = new Date().getUTCMinutes();
    if (minute % 5 === 0) {
      ctx.waitUntil((async () => {
        try {
          console.log(`[obs-log] ${new Date().toISOString()} ${await logObs(env)}`);
        } catch (e) {
          console.log(`[obs-log] FAILED ${e}`);
        }
      })());
    }
    // TWO LANES. The full 20-city bake on :05/:20/:35/:50; Central Park alone
    // on every other five-minute mark, so the New York sheet is never more
    // than a few minutes behind the newest report. Both merge into one file.
    if (minute % 5 !== 0) return;
    const wf = [5, 20, 35, 50].includes(minute) ? WORKFLOW : WORKFLOW_FAST;
    ctx.waitUntil((async () => {
      let r;
      try {
        r = await dispatch(env, wf);
      } catch (e) {
        r = { ok: false, status: 0, detail: String(e) };
      }
      // One retry: a transient GitHub 5xx should not cost the hour, and the job
      // is idempotent — a lock is written once and never rewritten.
      if (!r.ok && r.status >= 500) {
        await new Promise((s) => setTimeout(s, 4000));
        try {
          r = await dispatch(env, wf);
        } catch (e) {
          r = { ok: false, status: 0, detail: String(e) };
        }
      }
      console.log(`[kalshi-cron] ${event.cron} ${wf} -> ${r.ok ? 'dispatched' : 'FAILED'} ` +
                  `(http ${r.status}) ${r.detail}`);
    })());
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: cors(ALLOWED) });
    }
    if (url.pathname === '/positions') return positions(request, env);
    if (url.pathname === '/obs') return obsDump(request, env);
    if (url.pathname === '/obs/lead') return obsLead(request, env);

    // Status only. This deliberately cannot trigger a run: a public endpoint that
    // fires CI is an open invitation, and the cron is the point.
    //
    // The token is CHECKED, not merely counted. A token that has expired -- these
    // are issued with a fixed lifetime -- would still be "configured", so a
    // presence check would read healthy while every dispatch quietly 401s. One
    // read-only call against the workflow answers whether it actually works.
    let token = env.GH_TOKEN ? 'set, but unverified' : 'MISSING';
    let healthy = false;
    if (env.GH_TOKEN) {
      try {
        const r = await fetch(
          `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}`,
          { headers: {
              'Authorization': `Bearer ${env.GH_TOKEN}`,
              'Accept': 'application/vnd.github+json',
              'User-Agent': 'bluishvoid-kalshi-cron'
          } });
        healthy = r.ok;
        token = r.ok ? 'valid'
              : (r.status === 401 ? 'REJECTED - expired or revoked'
              : r.status === 404 ? 'REJECTED - no access to this repo/workflow'
              : `REJECTED - http ${r.status}`);
      } catch (e) {
        token = `could not be checked: ${e}`;
      }
    }
    const body = {
      worker: 'kalshi-cron',
      healthy,
      token,
      dispatches: `${OWNER}/${REPO} :: ${WORKFLOW} @ ${REF}`,
      // HAND-MAINTAINED, and it drifted: this still read the old hourly
      // schedule after the triggers went to every 15 minutes, so the status
      // page confidently reported a cadence the worker was not running.
      // Cloudflare does not expose a worker's own triggers to its code, so
      // this has to be kept in step with [triggers] in wrangler.toml by hand.
      schedule_utc: ['* 12-23 * * *', '* 0-4 * * *'],
      dispatch_minutes: [5, 20, 35, 50],
      fast_lane_minutes: [0, 10, 15, 25, 30, 40, 45, 55],
      obs_log: env.OBS ? 'KV bound' : 'NO KV BINDING - not logging',
      alerts: env.NTFY_TOPIC ? 'ntfy topic set; New York positions watched every 5 min' : 'off (no NTFY_TOPIC)',
      now_utc: new Date().toISOString(),
      note: 'Triggering is cron-only. Runs appear at github.com/' + OWNER + '/' + REPO + '/actions'
    };
    return new Response(JSON.stringify(body, null, 2), {
      status: healthy ? 200 : 503,
      headers: { 'content-type': 'application/json; charset=utf-8' }
    });
  }
};

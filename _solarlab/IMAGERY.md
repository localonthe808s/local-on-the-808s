# Public-domain / open-license space imagery — research for the solar system widget

Researched 2026-07-23. Goal: real mission photos in the map + popups, following the
site's established pattern (rehost stable copies on `cdn.bluishvoid.com` R2, keep
credits in code comments + visible where license requires).

## License landscape (verified)

| Source | License | Credit required | Notes |
|---|---|---|---|
| NASA (images.nasa.gov, JPL Photojournal, mission sites) | **Public domain** (US Gov work) | Requested, not required | Insignia/logo NOT PD; identifiable people = publicity rights caveat |
| STScI Hubble (hubblesite.org) | Public domain unless stated | NASA + STScI acknowledgement requested | |
| JWST (NASA-released) | Public domain | NASA/ESA/CSA/STScI credit requested | |
| ESA/Hubble + ESA/Webb (esahubble.org, esawebb.org) | CC BY 4.0 | **Required, visible** | Fine — site already carries CC-BY credits |
| ESA missions (esa.int) | CC BY-SA 3.0 IGO | **Required + ShareAlike** | ShareAlike is viral — prefer NASA equivalents where possible |
| ESO (eso.org) | CC BY 4.0 | **Required, visible** | Already used: milkyway.webp = ESO/S. Brunier panorama |
| NOAA/SWPC | Public domain | — | Already used for solar wind |

## What the lab already uses (keep)

- Planet discs: NASA/JPL Photojournal photos rehosted at `cdn.bluishvoid.com/planet-*.jpg`
  (PIA IDs documented in the site; see planet-imagery memory/notes).
- Popup hero photos: `images-assets.nasa.gov/image/PIA…/PIA…~medium.jpg` (PD, hotlinked).
- Repo astrophotos (alpha-baked webp, credits in index.html ~line 9263):
  - `milkyway.webp` — ESO/S. Brunier, CC BY 4.0
  - `andromeda.webp` — Adam Evans, CC BY 2.0
  - `orion_neb.webp` — NASA/ESA/M. Robberto HST, public domain
  - `pleiades.webp` — NASA/ESA/AURA/Caltech Palomar, public domain

## Applied to the lab now

- **M31 Andromeda** (`andromeda.webp`) at true chart position ecl lon 27.8°, lat +33.3°.
- **M45 Pleiades** (`pleiades.webp`) at ecl lon 59.9°, lat +4.1° (on the zodiac rim).
- **M42 Orion Nebula**: projects off-canvas (lat −28.7° → beyond the top edge) — unused.
- **Live Sun**: SDO AIA 171 Å latest frame (PD, courtesy NASA/SDO & AIA/EVE/HMI teams),
  hotlinked from `sdo.gsfc.nasa.gov/assets/img/latest/latest_512_0171.jpg`, clipped
  into the sun disc with the procedural glow kept as halo + onerror fallback.

## Spacecraft sprites in use (`_solarlab/sprites/`)

All public domain NASA work, alpha-cut and rehosted in the repo. `roman.png`
needed no keying — NASA publishes that illustration with transparency already.

| File | Source | Notes |
|---|---|---|
| `roman.png` | NASA, `assets.science.nasa.gov/.../rst/spacecraft-illustrations/Roman-transparent.png` (also Commons) | Official deployed illustration, 7680×4320 RGBA, trimmed on its own alpha to 520×351 |
| `jwst.png`, `dscovr.png`, `chandra.png`, `hst.png`, `iss.png`, `goes.png`, `gps.png`, `tiangong.png` | NASA spacecraft-model series via Commons | PD |
| `vanguard.png` | PD Commons photo | chroma-keyed |
| `agena.png` | NASA s66-46124 (Gemini X Agena) | luminance-keyed + manual erasers |

## For the widget build-out (per body / feature)

- **Sun**: SDO latest frames, several wavelengths: `latest_512_0171.jpg` (gold corona),
  `_0304` (red chromosphere), `_HMIIC` (visible photosphere). PD. NASA GSFC endpoints
  have a history of cert/outage issues (same reason the moon moved to R2) → snapshot
  via scheduled GitHub Action → `cdn.bluishvoid.com/sun/current.jpg`, like the moon.
  SOHO LASCO C2/C3 coronagraph (ESA/NASA joint — free with credit) for CME context.
- **Mercury**: MESSENGER MDIS global mosaic (PIA15162 etc.) — PD.
- **Venus**: Magellan radar composite (PIA00104, already used) or Mariner 10 reprocessed — PD.
- **Mars**: daily-ish global view — Mars Express VMC is ESA CC BY-SA (viral);
  prefer NASA MARCI weather maps (MSSS, PD) or stick with PIA00407 Viking mosaic.
- **Jupiter**: JunoCam processed images — PD (credit NASA/JPL-Caltech/SwRI/MSSS +
  citizen processor name; some processors assert rights — prefer NASA-released ones).
- **Saturn**: Cassini PIA06077 (used) + ring/moon closeups from Photojournal — PD.
- **Uranus/Neptune**: Voyager 2 (PIA18182 used / PIA01492 used); JWST 2023 ring
  portraits are stunning + PD-when-NASA-released.
- **Moons** (STRUCTURE tab expansion): Galilean moons (Galileo/Juno PD), Titan/Enceladus
  (Cassini PD), Triton (Voyager PD) — all on Photojournal.
- **Pluto/dwarf planets** (if added): New Horizons PIA19952 etc. — PD.
- **Milky Way band**: the repo's ESO/Brunier panorama could be warped along the
  projected galactic circle (segment-and-rotate like the hero uses), CC BY 4.0.
- **Comets/asteroids** (future): OSIRIS-REx Bennu (PD); Rosetta 67P is ESA CC BY-SA (viral).
  For the six BIG_ASTEROIDS specifically, see the audit below.

## Meteor showers (researched 2026-07-24)

For a hero photo in `openShowerPopup`, which currently has none. Every URL below was
HTTP-checked or pulled from the Commons/NASA APIs on 2026-07-24.

**Coverage is uneven — this is the headline.** Photos are plentiful for the famous
showers and near-absent for the rest, so the popup needs a graceful no-image path
rather than a photo slot that goes blank 4 times a year.

| Shower | Open imagery? | Best candidate |
|---|---|---|
| Quadrantids | thin (7 files) | none strong |
| Lyrids | **yes** | *Stunning Lyrids Over Earth at Night* — NASA/Don Pettit, **PD**, 3768×2832, shot from the ISS |
| Eta Aquariids | **yes** | ESO/P. Horálek Chilean Desert (`potw2227a/b`), **CC BY 4.0**, huge; + NASA All Sky Fireball Network mosaic (PD) |
| Delta Aquariids | **almost none** | see below |
| Perseids | **abundant** | NASA/Bill Ingalls `NHQ202508030001` + `NHQ202108100009` (**PD**); `ISS-44 Perseid meteor shower` (**PD**, from orbit) |
| Draconids | **none** (0 files) | historic *Draconids 1933, F. Quénisset* (PD) is the only thing |
| Orionids | **yes** | Mike Lewinski *Orionid meteor at dawn*, CC BY 4.0, 6000×4000 |
| Leonids | **yes** | Trouvelot *The November Meteors* (1889 lithograph of the 1833 storm), **PD**, 16975×23165 |
| Geminids | **abundant** | NOIRLab *Geminids over Gemini North* / *over Kitt Peak*, CC BY 4.0 — thematically perfect (Geminids over the Gemini telescope) |
| Ursids | **none** (1 file) | — |

### Delta Aquariids — the near-term one (peaks Jul 29)

Genuinely poorly served. Commons has **one** usable image and NASA's library has zero
(`images-api.nasa.gov?q=Delta Aquariid` → 0 hits). Options, honest ones first:

1. `Under the summer stars (54671841582).jpg` — bgwashburn, **CC BY 4.0**, 4456×5996.
   Caveat: the photographer's own caption says "a Delta Aquariid **or early Perseid**",
   so it cannot be captioned as a confirmed Delta Aquariid.
   `https://upload.wikimedia.org/wikipedia/commons/thumb/e/e2/Under_the_summer_stars_%2854671841582%29.jpg/1920px-Under_the_summer_stars_%2854671841582%29.jpg`
2. ESO/Horálek `potw2227b` — the *Eta* Aquariids, same Aquarius radiant region. Correct
   sky, wrong shower; only usable if the caption says which shower it actually shows.
3. No photo; let the radiant fan carry it.

### Parent bodies (the per-shower angle — mostly a dead end)

The popup already has a "Parent body" row, but only two of ten have real imagery:

- **3200 Phaethon** (Geminids): `PIA22185` Arecibo radar, **PD**, Arecibo/NASA/NSF; plus
  `Phaeton-trail-copy.jpg` (NASA/NRL, PD) showing its dust trail from STEREO.
- **1P/Halley** (Eta Aquariids + Orionids): the close-up nucleus is **ESA Giotto** —
  CC BY-SA, the viral licence this doc already says to avoid. Ground-based 1986 plates
  via NASA's IHW archive are the PD alternative.
- **109P/Swift-Tuttle**, **55P/Tempel-Tuttle**: 0 openly-licensed files on Commons.
- **96P/Machholz**: only CC BY-SA skymaps/orbit diagrams, no photograph.
- 2003 EH1, C/1861 G1 Thatcher, 21P, 8P: nothing usable.

Verdict: not viable as a consistent per-shower hero. Worth doing as a one-off for the
Geminids (Phaethon is a genuinely interesting "rock comet" story) but not systemwide.

### Licence notes specific to this batch

- Most amateur Commons astrophotos are **CC BY-SA**, not CC BY. ShareAlike is fine for a
  popup hero shown **as-is with visible credit** — it only bites when baked into a
  derived composite (unlike the alpha-baked hero night-sky assets, where it must stay out).
- NOIRLab / Gemini / KPNO images are CC BY 4.0 and consistently well-shot — the single
  best institutional source for shower photos after ESO.
- NASA APOD is **not** a source: the daily images are copyright the individual
  astrophotographers, not PD, despite the nasa.gov domain.
- On-pattern hotlink form (already used for planet popups, both verified 200):
  `https://images-assets.nasa.gov/image/<nasa_id>/<nasa_id>~medium.jpg`

## Serving guidance

- Prototype: hotlink NASA endpoints (PD, CORS-irrelevant for `<img>`/SVG `<image>`).
- Production: rehost stable copies on R2/cdn (existing rclone + GH Action pipeline)
  — NASA endpoints flake (SVS cert expiry precedent) and filenames change.
- Keep every non-PD credit visible or in code comments per site convention; CC BY-SA
  (ESA mission imagery) is ShareAlike — avoid baking it into derived composites.

## Moons — which have usable public photos (audited 2026-07-25)

All 23 named moons in `MOON_SYS` have a **NASA public-domain** photo in `MOON_IMG`,
and all 23 URLs return 200. They are now drawn on the map itself, not just in popups.

**Three sources were wrong** and were replaced — they were never portraits:

| moon | was | problem | now |
|---|---|---|---|
| Callisto | PIA03455 | two-panel science figure with a scale bar | **PIA03456** global view |
| Tethys | PIA07733 | flyby *coverage map* with axes, legend and title text | **PIA19636** "The Colors of Tethys I" |
| Dione | PIA12577 | flat surface mosaic strip, no disc | **PIA06163** global view |

Note PIA03456 has **no `~medium`** asset — older Photojournal items often don't;
use the collection.json manifest and fall back to `~orig`/`~small`.

**Disc coverage** — how much of each frame the moon actually fills. This matters
because these are science releases, not cutouts: a low number means mostly black sky,
which is why every image needs the measured crop in `MOON_CROP` rather than being
dropped straight into a circular clip.

| moon | disc fills | note |
|---|---|---|
| Dione | 100% | good portrait |
| Io | 99% | good portrait |
| Triton | 94% | good portrait |
| Callisto | 92% | good portrait |
| Moon | 88% | good portrait |
| Ariel | 86% | good portrait |
| Phobos | 86% | good portrait |
| Enceladus | 79% | good portrait |
| Ganymede | 79% | good portrait |
| Iapetus | 70% | good portrait |
| Europa | 67% | good portrait |
| Mimas | 61% | good portrait |
| Miranda | 56% | good portrait |
| Tethys | 50% | small in frame |
| Titan | 42% | small in frame |
| Rhea | 38% | small in frame |
| Titania | 31% | small in frame |
| Oberon | 29% | small in frame |
| Umbriel | 26% | small in frame |
| Hyperion | 19% | small in frame |
| Proteus | 17% | small in frame |
| Deimos | 16% | small in frame |
| Nereid | 4% | barely resolved — best that exists |

**Nereid is the honest floor**: Voyager 2 never resolved it, so its disc is 4% of the
frame — a bright speck. It is still the real photograph; there is no better one.
Deimos, Hyperion and Proteus are similarly small but genuinely resolved.

Regenerate the crop table with `python3 build_moon_crops.py` if any URL changes.

## Earth — the last body without a photo (added 2026-07-25)

Every other planet had a rehosted NASA portrait; Earth was still the procedural
`earthGrad` gradient. It now carries a **live full-disc photograph** from NASA's
**DSCOVR/EPIC** camera at the L1 point, which images the entire sunlit face once
a day. Public domain (NASA). Same live treatment the Sun gets from SDO and the
Moon gets from the R2 snapshot.

- Index: `https://epic.gsfc.nasa.gov/api/natural` — JSON, and **CORS-open**
  (`Access-Control-Allow-Origin: *`), so unlike SBDB/Horizons this can be
  fetched client-side rather than baked.
- Frame URL: `/archive/natural/YYYY/MM/DD/jpg/<image>.jpg`.
  **Use the jpg, not the png** — same 1080px disc at **218 KB** versus **3 MB**.
- Geometry (measured the same way as the Sun and the moons): the frame is
  square, disc centred at (0.497, 0.497), radius **0.392 of the width**, with a
  bbox solidity of 0.78 — i.e. a true disc (pi/4 = 0.785). The image is oversized
  by 1/0.392 so the limb lands exactly on the clip circle.
- EPIC lags a couple of days and the fetch can fail, so the gradient stays
  underneath as the fallback and the image only fades in on success.
- Production: snapshot to R2 on a schedule like the moon, rather than hotlinking
  GSFC — same reasoning as the Sun (`cdn.bluishvoid.com/sun/current.jpg`).

## Big asteroids — which have real photographs (audited 2026-07-26)

The split is simply which ones we have visited. Dawn orbited Vesta (2011) and
Ceres (2015); nobody has been to the other four.

| body | photo? | source | licence | in the map |
|---|---|---|---|---|
| Ceres | yes, sharp | Dawn **PIA21906** 1280², disc fills 100% | PD | **used**, rehosted |
| Vesta | yes, sharp | Dawn **PIA14317** 1024², fills 91% (no `~medium`) | PD | **used**, rehosted |
| Pallas | resolved blob | ESO VLT/SPHERE, in `eso2114a` mosaic | CC BY 4.0 | procedural rock |
| Hygiea | resolved blob | ESO `eso1918a` (own release) | CC BY 4.0 | procedural rock |
| Juno | resolved blob | `eso2114a` mosaic | CC BY 4.0 | procedural rock |
| Psyche | **none exists** | NASA holds only artist's concepts | — | procedural rock |

**Psyche has no photograph.** `images-api` returns 100 hits and every one is an
illustration (PIA24896, PIA24472, "A Metal-Rich World (Artist's Concept)"). The
spacecraft arrives 2029. These must never be used as photos.

The four un-visited ones stay procedural on purpose: a smeared 30-pixel
telescope blob beside a sharp Dawn portrait reads as a rendering fault rather
than as honesty about what has been seen. The ESO mosaic is also an
infographic — blue background, sizing ring, baked captions — so using it means
cropping and keying each panel, i.e. a derivative, allowed under CC BY 4.0 with
visible credit: *ESO/M. Kornmesser/Vernazza et al./MISTRAL algorithm (ONERA/CNRS)*.

**Two candidates that look right and are not** (same trap as Callisto's PIA03455):
`PIA22660` is Ceres with a cutaway interior diagram over it, and `PIA16632` is a
Vesta polar topographic map with axes and a colour bar. Both score high on
"disc fills frame".

Measured crops (same method as the moons):
Ceres fx .4986 fy .4986 fr .5000 ar 1 · Vesta fx .5389 fy .5056 fr .4931 ar 1.

Both Dawn frames are lit from +x, but `rockLit` puts +x AWAY from the Sun, so
the photo-backed pair carry a 180° offset. Rotating is the honest fix —
mirroring would put Occator's bright spots on the wrong side of Ceres.

### Rehosted (2026-07-26)

Both now served from R2 rather than hotlinked, per the serving guidance above:

    cdn.bluishvoid.com/asteroids/ceres.jpg   512x512, 61KB
    cdn.bluishvoid.com/asteroids/vesta.jpg   512x512, 64KB

Uploaded with `rclone copy <f> r2:bluishvoid-bg/asteroids/ --s3-no-check-bucket`
plus `--header-upload "Cache-Control: public, max-age=31536000, immutable"`.

512px is deliberate, not arbitrary: the map's zoom floor is MIN_W = 1000/16, so
Ceres tops out at 86px across (173px at 2x DPR) and Vesta at 64px. 512 leaves
3x headroom. The pair went 425KB -> 123KB.

Resizing does not disturb the crop table — fx/fy/fr are fractions of width, and
both re-measured identical after the downscale.

**The 23 moon photos are still hotlinked** from images-assets.nasa.gov and are
the obvious next candidates for the same treatment.

## Moons rehosted (2026-07-26) — and where each one came from

All 23 moved from images-assets.nasa.gov to `cdn.bluishvoid.com/moons/<name>.jpg`.
The NASA IDs lived only inside those URLs, so they are recorded here —
this table IS the provenance now. All public domain (NASA/JPL et al).

| moon | NASA id | served as |
|---|---|---|
| Moon | `GSFC_20171208_Archive_e001861` | `moons/moon.jpg` |
| Phobos | `PIA10368` | `moons/phobos.jpg` |
| Deimos | `PIA17350` | `moons/deimos.jpg` |
| Io | `PIA00583` | `moons/io.jpg` |
| Europa | `PIA19048` | `moons/europa.jpg` |
| Ganymede | `PIA24681` | `moons/ganymede.jpg` |
| Callisto | `PIA03456` | `moons/callisto.jpg` |
| Mimas | `PIA12570` | `moons/mimas.jpg` |
| Enceladus | `PIA07800` | `moons/enceladus.jpg` |
| Tethys | `PIA19636` | `moons/tethys.jpg` |
| Dione | `PIA06163` | `moons/dione.jpg` |
| Rhea | `PIA06578` | `moons/rhea.jpg` |
| Titan | `PIA06230` | `moons/titan.jpg` |
| Hyperion | `PIA08349` | `moons/hyperion.jpg` |
| Iapetus | `PIA08384` | `moons/iapetus.jpg` |
| Miranda | `PIA00042` | `moons/miranda.jpg` |
| Ariel | `PIA00041` | `moons/ariel.jpg` |
| Umbriel | `PIA00040` | `moons/umbriel.jpg` |
| Titania | `PIA00036` | `moons/titania.jpg` |
| Oberon | `PIA00034` | `moons/oberon.jpg` |
| Proteus | `PIA00062` | `moons/proteus.jpg` |
| Triton | `PIA00317` | `moons/triton.jpg` |
| Nereid | `PIA00054` | `moons/nereid.jpg` |

Each was downscaled to what the map can actually show rather than to a fixed
size: needed source width = (moon's drawn diameter at max zoom, 2x DPR) / (2·fr),
since a moon whose disc fills only part of its frame must start larger to end up
the same size on screen. Nereid needs the widest source (346px) despite being
the smallest moon, because its disc is 11% of its frame.

Sizes run 200–448px. **2426KB → 287KB, 88% smaller.** Uploaded with
`rclone copy . r2:bluishvoid-bg/moons/ --s3-no-check-bucket` plus
`--header-upload "Cache-Control: public, max-age=31536000, immutable"`.

`moon_crops.json` is unchanged and did not need to be: fx/fy/fr are fractions of
width. Every one of the 23 was re-measured after the downscale and drifted by at
most 0.003.

## Sun — deliberately NOT rehosted (decided 2026-07-26)

It was briefly mirrored to `cdn.bluishvoid.com/sun/current.jpg` on a 30-minute
job, and that was reverted. The Sun is the one image on the map whose value is
being live: a flare shows in AIA 171 within minutes, and the mirror put it up
to 45 minutes behind (30-min job + 15-min cache bucket). Outage resilience is
not worth that here. It stays hotlinked from
`sdo.gsfc.nasa.gov/assets/img/latest/latest_512_0171.jpg`.

Same reasoning applies to index.html's space-widget panels (PFSS, magnetogram,
white light): they poll every 5 minutes and are labelled "• LIVE". GitHub
Actions cron has a 5-minute floor and scheduled runs are routinely delayed
beyond it, so a snapshot job cannot honestly feed them. If those ever need
insulating from GSFC outages, the tool is the existing Cloudflare Worker
(proxy.bluishvoid.com) as a short-TTL pass-through cache — resilience without
staleness — not an R2 snapshot. They already fall back AIA → SOHO → PFSS.

### Worth keeping from the attempt: the zone raises short cache TTLs

Measured on 2026-07-26: R2 stored `Cache-Control: public, max-age=900` and the
edge served `max-age=14400`. The moon's `max-age=3600` is raised to 14400 the
same way, while the `immutable` assets (moons/, asteroids/) pass through
untouched. So anything that must refresh faster than 4 hours cannot rely on the
header alone — it needs a rotating query string or a zone change.

The site's hero moon was never affected: index.html already appends
`?v=<hour-of-year>` to `moon/current.jpg` for exactly this reason, and the
day-nav frames carry their own `_vq`. The lab's planet row did lack it and now
uses the same hourly index. (An earlier draft of this note claimed the hero
moon was stale — it was not.)

## Spacecraft images (mission layer + popups, 2026-07-26)

The spacecraft BODY as it looks in flight. First attempt used clean-room /
assembly photographs (PIA21732, KSC-97PC1350, ...) — REJECTED by the user:
"you can't scroll into the edge of the solar system to see a photo of a lab."
The porthole at the craft's position must show the machine itself.

Now: NASA's official transparent model renders from Wikimedia Commons (all
tagged Public domain, NASA source), trimmed to content, square-padded 12%,
≤800px webp with alpha, rehosted at `cdn.bluishvoid.com/craft/*.webp`.
Transparency matters — the map's own sky shows behind the craft inside the
clipped porthole. One file serves the popup hero and the zoomed map disc.

| mission | Commons file | note |
|---|---|---|
| voyager1/2 | Voyager spacecraft model.png | same render both — identical hardware |
| galileo | Galileo spacecraft model.png | |
| cassini | Cassini spacecraft model.png | |
| newhorizons | New Horizons spacecraft model 1.png | |
| juno | Juno spacecraft model 1.png | |
| pioneer10/11 | An artist's impression of a Pioneer spacecraft on its way to interstellar space.jpg | pre-CGI craft, no model render exists; classic NASA painting cropped square to the craft (1600,850)-(3000,2250), opaque |

The Pioneers are the honest exception to the no-artist's-concept rule: the
craft body is drawn accurately and nothing better exists — same reasoning as
Nereid's 4% disc, inverted. Lab code: CRAFT_IMG + craftPhoto()/
ensureCraftPhotos() (lazy, svg.zoomed gate); the colored dot swaps out at
zoom via `.craft-dot` so it doesn't peek through the transparent renders.

## Interstellar visitors (b96d3c6)

Porthole at the moving marker + popup hero, same lazy `data-craft-src` pattern
as the spacecraft. `cdn.bluishvoid.com/iso/*.webp`, 800px squares.

| object | source | license |
|---|---|---|
| 1i | ESO artist's impression (M. Kornmesser) — 'Oumuamua was NEVER resolved by any telescope; captioned as artist's impression in the popup footer | CC BY 4.0, credit required and present |
| 2i | Hubble, Dec 2019 (2019-53-4578, hubblesite) | public domain |
| 3i | Hubble, 21 Jul 2025 (heic2509a, esahubble) — the Nov 2025 frame was rejected: visible WFC3 mosaic seams | CC BY 4.0 (NASA/ESA/D. Jewitt) |

## Active-mission renders (FLYING NOW popups)

Same craft-body-render rule as the historical missions. All six are Public
domain on Commons — including JUICE and BepiColombo, whose usual ESA renders
are CC BY-SA (avoided): these two come from NASA's own "spacecraft icons"
toolkit renders instead. Files: Commons "X spacecraft model.png" for Parker /
Europa Clipper / Lucy / Psyche / JUICE / BepiColombo → trimmed, square-padded,
800px webp at cdn.bluishvoid.com/craft/{parker,clipper,lucy,psyche,juice,bepi}.webp.

## Famous junk photo sprites (EARTH VIEW, sprites/)

Real photographs of famous debris, alpha-cut and floating (not tappable)
among the junk layer at their real catalog orbits (`JUNK_IMG` in earth_lab).

| object | source | license |
|---|---|---|
| VANGUARD 1 (NORAD 5, baked by CATNR fetch — too faint for the visual group) | NASA/NRL Vanguard 1 satellite photo (Commons "Vanguard 1.jpg"), pale-blue backdrop chroma-keyed → sprites/vanguard.png | public domain |
| THOR AGENA D R/B | NASA s66-46124 — the Agena Target Docking Vehicle photographed from Gemini X (same Agena-D vehicle family), black sky luminance-keyed, Gemini nose/boom erased → sprites/agena.png | public domain |

Atlas Centaur 2 (1963, also in the bake) has no cleanly-keyable PD
photograph of the stage — it stays an anonymous streak on purpose.

## L-point sprites (EARTH VIEW, sprites/)

Zoom-gated renders replacing the L1/L2 diamonds, same gSprites stack.
Both from the Commons NASA spacecraft-model series (public domain,
pre-cut transparency): "JWST spacecraft model 1.png" → sprites/jwst.png,
"DSCOVR spacecraft model.png" → sprites/dscovr.png. Trimmed + resized only.

## Chandra (EARTH VIEW, sprites/)

Featured bird like Hubble; zoom sprite + popup hero both =
sprites/chandra.png, from Commons "Chandra X-ray Observatory spacecraft
model.png" (NASA model series, public domain, pre-cut transparency),
trimmed + resized only. Baked by CATNR=25867 (name "CXO", e~0.77 — the
high-e branch in junkXY runs 16 Kepler passes for it).

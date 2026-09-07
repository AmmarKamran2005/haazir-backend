/* Golden-file generator for the Python estimator port. Plan §12 Phase 4.
 *
 *   node tests/golden/generate_golden.js > tests/golden/engine_golden.json
 *
 * Phase 4's acceptance criterion is that the port "reproduces the prototype's numbers for a
 * fixed observation set to three decimal places". The only way to make that a real check
 * rather than a restatement of my own arithmetic is to run the actual prototype and record
 * what it says, so this loads `app/assets/js/data.js` and `app/assets/js/engine.js`
 * unmodified, under a stubbed `window`, and dumps their output.
 *
 * The simulated sensors are deliberately not exercised. Observations are injected directly
 * into the filter's state, because the point is to pin the ESTIMATOR, and the seeded PRNG
 * driving the fake sensors is the one part of the prototype that production deletes.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.resolve(__dirname, '../../../app/assets/js');

// `window` must BE the global object, not a property on it. In a browser
// `window.HZ = {}` also creates the bare global `HZ`, and data.js relies on that from its
// second line onward. A plain `{window: {}}` sandbox throws `HZ is not defined`.
const sandbox = { console, Math, Date, parseInt, parseFloat, String, Number, JSON };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

for (const file of ['data.js', 'engine.js']) {
  vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
}

const HZ = sandbox.HZ;
const E = HZ.engine;

// Full precision on purpose. Truncating here once put a value exactly on a three-decimal
// midpoint, and the comparison then disagreed with itself over a difference of 1.5e-7.
const round = (x) => (x === null || x === undefined ? null : x);

const out = {
  _readme:
    'Generated from app/assets/js/engine.js by tests/golden/generate_golden.js. ' +
    'Do not hand-edit: regenerate it, and treat a diff as a deliberate change to the model.',
  sources: E.SOURCES,
  weights: E.WEIGHTS,
};

/* ── wait curve ─────────────────────────────────────────────────────────────
   Every capacity band, across the whole occupancy range, including the kink at
   0.72 where the curve changes shape. */
out.wait_at = [];
for (const capacity of [60, 120, 420]) {
  for (let i = 0; i <= 100; i++) {
    const rho = i / 100;
    out.wait_at.push({
      capacity,
      rho: round(rho, 4),
      wait: round(E.waitAt(rho, { capacity })),
    });
  }
}

/* ── band thresholds ──────────────────────────────────────────────────────── */
out.state_band = [];
for (let i = 0; i <= 100; i++) {
  const x = i / 100;
  out.state_band.push({ x: round(x, 4), band: E.stateBand(x) });
}

/* ── the fusion step ──────────────────────────────────────────────────────
   Fixed observations, fixed clock, no simulation. Each case is a shape the
   filter has to get right: one source alone, a stale reading, a sharp staff
   tap against a vague prior, a payment reading with its own Poisson sigma. */
const FUSION_CASES = [
  {
    name: 'prior only',
    clock: 1200,
    src: { prior: { value: 0.42, sigma: 0.215, at: 1200 } },
  },
  {
    name: 'prior plus fresh staff tap',
    clock: 1200,
    src: {
      prior: { value: 0.42, sigma: 0.215, at: 1200 },
      staff: { value: 0.87, sigma: 0.05, at: 1198 },
    },
  },
  {
    name: 'staff tap gone stale',
    clock: 1200,
    src: {
      prior: { value: 0.42, sigma: 0.215, at: 1200 },
      staff: { value: 0.87, sigma: 0.05, at: 1100 },
    },
  },
  {
    name: 'all four sources',
    clock: 1200,
    src: {
      prior: { value: 0.42, sigma: 0.215, at: 1200 },
      staff: { value: 0.87, sigma: 0.05, at: 1195 },
      payment: { value: 0.71, sigma: 0.12, at: 1188 },
      checkin: { value: 0.64, sigma: 0.14, at: 1180 },
    },
  },
  {
    name: 'sharp payment reading at a large venue',
    clock: 600,
    src: {
      prior: { value: 0.55, sigma: 0.215, at: 600 },
      payment: { value: 0.68, sigma: 0.055, at: 599 },
    },
  },
  {
    name: 'vague payment reading at a small cash-heavy venue',
    clock: 600,
    src: {
      prior: { value: 0.55, sigma: 0.215, at: 600 },
      payment: { value: 0.68, sigma: 0.34, at: 599 },
    },
  },
];

const venueFor = (capacity) => ({ id: '__golden__', name: 'Golden', capacity });

out.fusion = FUSION_CASES.map((c) => {
  const capacity = c.capacity || 120;
  const v = venueFor(capacity);
  E.clock = c.clock;
  E.state[v.id] = {
    id: v.id, src: {}, history: [], staffPinged: false,
    lastCheckin: -999, lastPayment: -999, paymentTicks: 0, ticksThisHour: 0,
  };
  for (const k in c.src) E.state[v.id].src[k] = c.src[k];

  const f = E.fuse(v);
  return {
    name: c.name,
    clock: c.clock,
    capacity,
    input: c.src,
    occupancy: round(f.occupancy),
    sd: round(f.sd),
    confidence: round(f.confidence),
    band: f.band,
    wait: round(f.wait),
    wait_lo: round(f.waitLo),
    wait_hi: round(f.waitHi),
    wait_p90: round(f.waitP90),
    parts: f.parts.map((p) => ({
      source: p.source,
      age: round(p.age, 4),
      value: round(p.value),
      precision: round(p.precision),
      weight: round(p.weight),
    })),
  };
});

/* ── dish-time graph ──────────────────────────────────────────────────────── */
out.dish_quality = [];
HZ.venues.forEach((v) => {
  v.dishes.forEach((d) => {
    for (let h = 0; h < 24; h++) {
      out.dish_quality.push({
        venue: v.id,
        dish: d.id,
        hour: h,
        peak: d.peak,
        hi: d.hi,
        lo: d.lo,
        decay: d.decay,
        sellout: d.sellout || null,
        second_peak: d.secondPeak || null,
        second_hi: d.secondHi === undefined ? null : d.secondHi,
        quality: round(E.dishQualityAt(d, h)),
      });
    }
  });
});

/* ── trust ────────────────────────────────────────────────────────────────── */
out.trust = HZ.venues.map((v) => {
  const t = E.trustBreakdown(v);
  return {
    venue: v.id,
    total: t.total,
    components: t.components.map((c) => ({ key: c.key, pts: c.pts, max: c.max })),
  };
});

/* ── priors ──────────────────────────────────────────────────────────────── */
out.popularity = HZ.venues.map((v) => ({
  venue: v.id, rating: v.rating, reviews: v.reviews,
  popularity: round(E.popularity(v)),
}));

out.prior_at = [];
HZ.venues.slice(0, 6).forEach((v) => {
  for (let how = 0; how < 168; how += 7) {
    out.prior_at.push({ venue: v.id, archetype: v.prior, hour_of_week: how,
                        prior: round(E.priorAt(v, how * 60)) });
  }
});

process.stdout.write(JSON.stringify(out, null, 1));

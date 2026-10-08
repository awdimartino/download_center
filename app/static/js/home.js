"use strict";

/* --- home --------------------------------------------------------------
   The landing panel. It answers one question - what have you been listening
   to - because that is the question the thing this sits beside cannot
   answer at all: Navidrome keeps a cumulative total per track and the single
   most recent play date, so "how much did I listen to in March" has no home
   anywhere else.

   Everything drawn here is a door to the detail lower on this page, which
   the Listening panel used to hold on its own. Nothing on this page is a
   control, and nothing is edited here. */

import { apiFetch, setBanner } from "./core.js";
import { monthlyChart, artistBars, stat } from "./charts.js";
import { showView } from "./nav.js";
import { selectDates } from "./listening.js";

const homeGreeting = document.getElementById("home-greeting");
const homeHero = document.getElementById("home-hero");
const homeStats = document.getElementById("home-stats");
const homeChart = document.getElementById("home-chart");
const homeMonths = document.getElementById("home-months");
const homeArtists = document.getElementById("home-artists");
const homeListening = document.getElementById("home-listening");
const homeSnapshots = document.getElementById("home-snapshots");
const homeEmpty = document.getElementById("home-empty");
const homeCover = document.getElementById("home-cover");
const homeCoverArt = document.getElementById("home-cover-art");


function greeting() {
  const hour = new Date().getHours();
  if (hour < 5) return "Still up";
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

function heroFact(facts) {
  homeHero.hidden = !facts.length;
  if (!facts.length) return;
  // Picked here rather than on the server, so the headline changes on every
  // visit without another request, and the server stays cacheable.
  const fact = facts[Math.floor(Math.random() * facts.length)];
  homeHero.querySelector(".home-hero-lead").textContent = fact.lead;
  homeHero.querySelector(".home-hero-value").textContent = fact.value;
  homeHero.querySelector(".home-hero-tail").textContent = fact.tail;
}

const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

// RGB (0-255) to HSL, each channel 0-1.
function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h;
  if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  return [h / 6, s, l];
}

// The colours the page takes from a cover. The commonest colour in a cover
// is usually its darkest - shadow, a black sleeve - and a dark accent cannot
// carry a number on a dark page. So the pick is weighted toward brightness
// and saturation, with frequency as one input rather than the rule: a colour
// that is a little rarer but clearly bright wins over a muddy majority.
// Null when the canvas will not give up its pixels.
function coverPalette(img) {
  const size = 48;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, size, size);
  let pixels;
  try {
    pixels = ctx.getImageData(0, 0, size, size).data;
  } catch (err) {
    return null;
  }

  // Coarse buckets, averaged inside, so each candidate is a colour that is
  // actually in the art rather than a quantised step.
  const buckets = new Map();
  let counted = 0;
  for (let i = 0; i < pixels.length; i += 4) {
    if (pixels[i + 3] < 128) continue;
    counted += 1;
    const key = ((pixels[i] >> 4) << 8) | ((pixels[i + 1] >> 4) << 4) | (pixels[i + 2] >> 4);
    const b = buckets.get(key) || { n: 0, r: 0, g: 0, b: 0 };
    b.n += 1;
    b.r += pixels[i];
    b.g += pixels[i + 1];
    b.b += pixels[i + 2];
    buckets.set(key, b);
  }
  if (!counted) return null;

  let best = null;
  for (const b of buckets.values()) {
    const [h, s, l] = rgbToHsl(b.r / b.n, b.g / b.n, b.b / b.n);
    // Frequency counts, but as a root: it lifts a vivid minority over a
    // grey majority without letting a single pixel win.
    const score = Math.sqrt(b.n / counted) * s * (0.25 + l);
    if (!best || score > best.score) best = { score, h, s, l };
  }

  // A grey cover has no colour to take. A warm light neutral keeps the
  // page from going cold and blue-grey by default.
  if (best.s < 0.15) return paletteFrom(0.1, 0.4, 0.7);
  return paletteFrom(best.h, best.s, best.l);
}

// Three roles from one hue. The accent is pushed bright enough to read as a
// number on the dark page; the tint is the page itself, dark and only faintly
// of the same hue; the ink is what sits on a filled accent.
function paletteFrom(h, s, l) {
  const hsl = (sat, lig) => `hsl(${Math.round(h * 360)} ${Math.round(sat * 100)}% ${Math.round(lig * 100)}%)`;
  return {
    accent: hsl(clamp(s, 0.6, 1), clamp(l, 0.62, 0.76)),
    tint: hsl(clamp(s * 0.7, 0.25, 0.6), 0.12),
    on: hsl(0.5, 0.08),
  };
}

// The cover is one of the year's most played albums, picked at random on
// each visit, and the page takes its colour
// from the cover once the image has loaded. Either step failing leaves the
// page as it was: no cover shows the text alone, and no colour keeps the
// stylesheet's own background.
const PALETTE_VARS = ["--home-accent", "--home-tint", "--home-on"];
const PALETTE_KEY = "cover-palette";

// The whole site takes its accent from the cover, not just Home. The class
// on <body> is what tells the stylesheet to; with no palette every page keeps
// the app's own accent.
function applyPalette(palette) {
  const root = document.documentElement;
  document.body.classList.toggle("themed", Boolean(palette));
  if (!palette) {
    PALETTE_VARS.forEach((name) => root.style.removeProperty(name));
    return;
  }
  root.style.setProperty("--home-accent", palette.accent);
  root.style.setProperty("--home-tint", palette.tint);
  root.style.setProperty("--home-on", palette.on);
}

// Remembered between visits, so a page opened before Home has loaded (or a
// reload on Library) is already in the cover's colour rather than flashing
// the default blue first. A convenience only: storage can be unavailable.
function setPalette(palette) {
  applyPalette(palette);
  try {
    if (palette) localStorage.setItem(PALETTE_KEY, JSON.stringify(palette));
    else localStorage.removeItem(PALETTE_KEY);
  } catch (err) { /* private window or blocked storage */ }
}

try {
  const saved = JSON.parse(localStorage.getItem(PALETTE_KEY) || "null");
  if (saved && saved.accent) applyPalette(saved);
} catch (err) { /* nothing remembered */ }

// One album at random, so the header is not the same cover every time - but
// not evenly: the server weights what was played in the last two weeks and
// the last month above the year's favourites (overview.COVER_RECENT), so the
// cover is usually something you are listening to now. Picked here rather
// than on the server, the same as the headline fact. An album whose cover
// will not load (none embedded, none in the folder) is skipped for another;
// only when none load does the page fall back to text on the plain
// background.
function weightedPick(albums) {
  const total = albums.reduce((sum, album) => sum + (album.weight || 1), 0);
  let at = Math.random() * total;
  for (let i = 0; i < albums.length; i++) {
    at -= albums[i].weight || 1;
    if (at < 0) return i;
  }
  return albums.length - 1;
}

function setCover(albums) {
  const left = (albums || []).filter((album) => album.cover_track_id);

  const tryNext = () => {
    if (!left.length) {
      homeCover.classList.remove("has-art");
      setPalette(null);
      return;
    }
    const [album] = left.splice(weightedPick(left), 1);
    homeCoverArt.alt = album.album + " by " + album.artist;
    homeCoverArt.onload = () => setPalette(coverPalette(homeCoverArt));
    homeCoverArt.onerror = tryNext;
    homeCoverArt.src = "/api/library/art?id="
      + encodeURIComponent(album.cover_track_id) + "&size=600";
  };
  // The space is held from the start, so the copy does not jump down when
  // the image arrives.
  homeCover.classList.toggle("has-art", left.length > 0);
  tryNext();
}

function homeTiles(data) {
  const heard = data.listening || {};
  const year = heard.year || {};
  const held = data.collection || {};
  const hours = Math.round((year.seconds || 0) / 3600);

  // Against last month rather than an average: it is the comparison anyone
  // makes on their own anyway, and the average of a series this short is
  // mostly noise.
  const change = (heard.this_month || 0) - (heard.last_month || 0);
  const versus = heard.last_month
    ? (change >= 0 ? "+" : "−") + Math.abs(change).toLocaleString()
      + " on last month"
    : "no month before this one";

  const tiles = [
    // Its breakdown is the chart directly below: same by-month series,
    // this month's bar is the one on the right.
    stat("This month", (heard.this_month || 0).toLocaleString(), versus,
         () => homeChart.scrollIntoView({ behavior: "smooth", block: "start" })),
    // Opens the track list below on the same calendar year - the breakdown
    // of which tracks made up this total. Not the Year button, which is the
    // last 365 days and showed a different number.
    stat("In " + (year.year || new Date().getFullYear()),
         (year.tracks || 0).toLocaleString(),
         "different tracks, about " + hours.toLocaleString() + " hours",
         () => {
           const shown = year.year || new Date().getFullYear();
           selectDates(`${shown}-01-01`, `${shown}-12-31`);
           homeListening.scrollIntoView({ behavior: "smooth", block: "start" });
         }),
  ];
  if (held.available) {
    tiles.push(stat("Your library", (held.tracks || 0).toLocaleString(),
                    (held.albums || 0).toLocaleString() + " albums",
                    () => showView("library")));
  }
  homeStats.replaceChildren(...tiles);
}

export async function loadHome() {
  try {
    const data = await apiFetch("/api/overview").then((r) => r.json());
    if (data.detail) {
      homeEmpty.hidden = false;
      homeEmpty.textContent = data.detail;
      return;
    }
    const heard = data.listening || {};
    homeEmpty.hidden = true;
    homeGreeting.textContent = greeting() + ", " + data.username + ".";
    heroFact(data.highlights || []);
    setCover(heard.cover_albums);
    homeTiles(data);
    homeMonths.replaceChildren(monthlyChart(heard.months || []));
    homeArtists.replaceChildren(...artistBars(heard.top_artists || []));

    // Only when it is wrong. A tick saying the counts were read is noise on
    // a page whose job is to look calm; a job that stopped three weeks ago
    // is the one thing here worth interrupting for, because every day it
    // does not run is a day of listening nobody can recover.
    const snaps = data.snapshots || {};
    setBanner(homeSnapshots, heard.available === false
      ? "Your listening history could not be read just now, so these "
        + "numbers are not real. Try again shortly."
      : snaps.up_to_date === false
        ? "Play counts have not been read since "
          + (snaps.last_reading || "ever")
          + " — listening since then is not being recorded."
        : "", "warn");
  } catch (err) {
    homeEmpty.hidden = false;
    homeEmpty.textContent = "Could not load: " + err.message;
  }
}


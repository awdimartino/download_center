// Shared content for the four Home page mockups, so each direction is
// judged on the same numbers rather than on which one got the nicer stats.
// Nothing here talks to the app - it's invented but plausible: a real
// household's mixed library, not a demo-data placeholder set.

const ALBUMS = [
  { title: "In Rainbows", artist: "Radiohead", plays: 214 },
  { title: "Fetch the Bolt Cutters", artist: "Fiona Apple", plays: 188 },
  { title: "To Pimp a Butterfly", artist: "Kendrick Lamar", plays: 171 },
  { title: "the record", artist: "boygenius", plays: 156 },
  { title: "22, A Million", artist: "Bon Iver", plays: 142 },
  { title: "IGOR", artist: "Tyler, The Creator", plays: 139 },
  { title: "Rumours", artist: "Fleetwood Mac", plays: 128 },
  { title: "Blonde", artist: "Frank Ocean", plays: 121 },
];

const ARTISTS = [
  { name: "Radiohead", plays: 214 },
  { name: "Fiona Apple", plays: 188 },
  { name: "The National", plays: 179 },
  { name: "Kendrick Lamar", plays: 171 },
  { name: "boygenius", plays: 156 },
  { name: "Bon Iver", plays: 142 },
];

// Apr 2025 - Mar 2026, oldest first. Peaks in March, matching the
// "busiest month" fact below.
const MONTHS = [
  { label: "Apr", plays: 142 }, { label: "May", plays: 118 },
  { label: "Jun", plays: 96 },  { label: "Jul", plays: 88 },
  { label: "Aug", plays: 101 }, { label: "Sep", plays: 134 },
  { label: "Oct", plays: 176 }, { label: "Nov", plays: 203 },
  { label: "Dec", plays: 241 }, { label: "Jan", plays: 268 },
  { label: "Feb", plays: 224 }, { label: "Mar", plays: 289 },
];

const HOURLY = [2,1,0,0,0,1,3,6,10,14,18,22,26,20,16,19,24,30,34,29,21,14,8,4];

const FACTS = {
  year: 2026,
  tracksThisYear: 1842,
  hoursThisYear: 340,
  artistsHeard: 64,
  busiestMonth: "March",
  busiestMonthPlays: 289,
  topArtist: "Radiohead",
  topArtistPlays: 214,
  longestSessionHours: 8,
  longestSessionMinutes: 14,
  longestSessionTracks: 106,
  totalSessions: 4080,
  libraryTracks: 7807,
  libraryAlbums: 612,
};

function greeting() {
  const h = new Date().getHours();
  if (h < 5) return "Still up, Alex";
  if (h < 12) return "Good morning, Alex";
  if (h < 18) return "Good afternoon, Alex";
  return "Good evening, Alex";
}

// A small deterministic hash so the same title+artist always draws the
// same cover, without ever downloading or storing a real one.
function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) { h = (h << 5) - h + s.charCodeAt(i); h |= 0; }
  return Math.abs(h);
}

// The three colors `albumArt` would draw a given seed with, without
// rendering it - so a mockup can tint a gradient or a glow to match a
// cover it's showing elsewhere on the same page.
function pickColors(seed, palette) {
  const h = hashStr(seed);
  const bg = palette[h % palette.length];
  let fg = palette[(h >> 3) % palette.length];
  let fg2 = palette[(h >> 7) % palette.length];
  if (fg === bg) fg = palette[(palette.indexOf(fg) + 1) % palette.length];
  if (fg2 === bg || fg2 === fg) fg2 = palette[(palette.indexOf(fg2) + 2) % palette.length];
  return { bg, fg, fg2 };
}

// Renders an abstract, generated "sleeve" for an album as inline SVG
// markup, using only colors from the given palette. `variant` lets a
// mockup bias the pattern mix (e.g. the console skin prefers rings and
// bars over soft blends).
function albumArt(seed, palette, variant) {
  const h = hashStr(seed);
  const { bg, fg, fg2 } = pickColors(seed, palette);

  const pattern = (variant !== undefined) ? variant : h % 5;
  let shapes = "";

  if (pattern === 0) { // concentric rings
    for (let i = 5; i > 0; i--) {
      shapes += `<circle cx="100" cy="100" r="${i * 19}" fill="${i % 2 ? fg : fg2}"/>`;
    }
    shapes += `<circle cx="100" cy="100" r="8" fill="${bg}"/>`;
  } else if (pattern === 1) { // diagonal stripes
    for (let i = -2; i < 11; i++) {
      shapes += `<rect x="${i * 26 - 60}" y="-40" width="14" height="280" fill="${i % 2 ? fg : fg2}" transform="rotate(35 100 100)"/>`;
    }
  } else if (pattern === 2) { // halftone dot grid
    for (let x = 4; x < 200; x += 26) {
      for (let y = 4; y < 200; y += 26) {
        const r = 3 + (hashStr(seed + x + "," + y) % 9);
        shapes += `<circle cx="${x + 13}" cy="${y + 13}" r="${r}" fill="${(x + y) % 52 === 0 ? fg : fg2}"/>`;
      }
    }
  } else if (pattern === 3) { // sunburst
    const rays = 14;
    for (let i = 0; i < rays; i++) {
      const a = (360 / rays) * i;
      shapes += `<rect x="97" y="-10" width="6" height="120" fill="${i % 2 ? fg : fg2}" transform="rotate(${a} 100 100)"/>`;
    }
    shapes += `<circle cx="100" cy="100" r="30" fill="${bg}"/>`;
  } else { // quadrant blocks
    shapes = `<rect x="0" y="0" width="100" height="100" fill="${fg}"/>
      <rect x="100" y="0" width="100" height="100" fill="${fg2}"/>
      <rect x="0" y="100" width="100" height="100" fill="${fg2}"/>
      <rect x="100" y="100" width="100" height="100" fill="${fg}"/>`;
  }

  return `<svg viewBox="0 0 200 200" preserveAspectRatio="xMidYMid slice" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="${seed}">
    <rect width="200" height="200" fill="${bg}"/>${shapes}
  </svg>`;
}

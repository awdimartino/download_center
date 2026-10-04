"use strict";

import { el } from "./core.js";

const SVG_NS = "http://www.w3.org/2000/svg";

export function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    node.setAttribute(key, String(value));
  }
  return node;
}

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function monthLabel(month) {
  // Read off the string rather than through a Date: new Date("2026-09") is
  // UTC midnight, which reads as August everywhere west of Greenwich.
  return MONTH_NAMES[Number(String(month).slice(5, 7)) - 1] || "";
}

// The chart is drawn in its own coordinate space and scaled to fit by CSS,
// so these are ratios rather than pixels. Strokes opt out of the scaling.
const PLOT = { w: 720, h: 170, top: 14, bottom: 24, side: 6 };

export function monthlyChart(months) {
  const wrap = el("div", "chart-wrap");
  if (!months.length) return wrap;

  const most = Math.max(1, ...months.map((m) => m.plays));
  const inner = PLOT.w - PLOT.side * 2;
  const floor = PLOT.h - PLOT.bottom;
  const step = months.length > 1 ? inner / (months.length - 1) : 0;
  const x = (i) => PLOT.side + step * i;
  const y = (plays) => floor - (plays / most) * (floor - PLOT.top);

  const chart = svg("svg", {
    viewBox: "0 0 " + PLOT.w + " " + PLOT.h,
    class: "chart", preserveAspectRatio: "none", role: "img",
    "aria-label": "Plays by month over " + months.length
      + " months, peaking at " + most,
  });

  // Hairline and recessive. Three lines, so the eye has something to
  // measure against without the grid becoming the picture.
  for (const fraction of [0, 0.5, 1]) {
    const at = floor - fraction * (floor - PLOT.top);
    chart.append(svg("line", {
      x1: PLOT.side, x2: PLOT.w - PLOT.side, y1: at, y2: at,
      class: fraction ? "chart-grid" : "chart-base",
    }));
  }

  const spine = months.map((m, i) => x(i) + "," + y(m.plays)).join(" ");
  chart.append(svg("polygon", {
    class: "chart-fill",
    points: PLOT.side + "," + floor + " " + spine + " "
            + x(months.length - 1) + "," + floor,
  }));
  chart.append(svg("polyline", { class: "chart-line", points: spine }));

  // The peak, marked where it happened. One mark rather than twenty-four
  // numbers: the shape carries the rest, and the hover layer has the exact
  // figure for any month somebody actually wants.
  // Placed over the chart rather than inside it: the plot is stretched to
  // the panel width, and a circle drawn in that coordinate space comes out
  // an ellipse at every width but one.
  const peak = months.findIndex((m) => m.plays === most);
  const dot = el("span", "chart-peak");
  dot.hidden = !(months[peak] && months[peak].plays > 0);
  dot.style.left = (x(peak) / PLOT.w) * 100 + "%";
  dot.style.top = (y(most) / PLOT.h) * 100 + "%";

  const crosshair = svg("line", {
    class: "chart-crosshair", x1: 0, x2: 0, y1: PLOT.top - 8, y2: floor,
  });
  crosshair.style.opacity = "0";
  chart.append(crosshair);

  const tip = el("div", "chart-tip");
  tip.hidden = true;

  // One hit target per month, the full height of the plot. The line itself
  // is 2px and nobody can hover 2px on a trackpad.
  months.forEach((month, i) => {
    const width = Math.max(1, step || inner);
    const band = svg("rect", {
      class: "chart-hit", y: 0, height: PLOT.h, tabindex: 0,
      x: Math.max(0, x(i) - width / 2), width,
    });
    const show = () => {
      crosshair.setAttribute("x1", x(i));
      crosshair.setAttribute("x2", x(i));
      crosshair.style.opacity = "1";
      tip.hidden = false;
      tip.textContent = monthLabel(month.month) + " "
        + String(month.month).slice(0, 4) + " · "
        + month.plays.toLocaleString()
        + (month.plays === 1 ? " play" : " plays");
      // Kept inside the box at both ends, or the first and last months
      // push their own tooltip off the edge of the panel.
      const across = x(i) / PLOT.w;
      tip.style.left = Math.min(0.92, Math.max(0.08, across)) * 100 + "%";
    };
    band.addEventListener("pointerenter", show);
    band.addEventListener("focus", show);
    chart.append(band);
  });

  const hide = () => {
    crosshair.style.opacity = "0";
    tip.hidden = true;
  };
  chart.addEventListener("pointerleave", hide);
  chart.addEventListener("focusout", hide);

  const axis = el("div", "chart-axis");
  // First, last, and each January between them: enough to place the shape
  // in time without twenty-four labels fighting over the same inch.
  months.forEach((month, i) => {
    const last = i === months.length - 1;
    const january = String(month.month).endsWith("-01");
    if (i !== 0 && !last && !january) return;
    const label = el("span", "chart-axis-label",
      january && !last ? String(month.month).slice(0, 4)
                       : monthLabel(month.month));
    label.style.left = (x(i) / PLOT.w) * 100 + "%";
    axis.append(label);
  });

  // Everything positioned as a percentage of the plot goes inside the plot
  // - the wrapper is taller by the height of the axis strip, and a dot
  // placed against that lands below the line it is meant to sit on.
  const plot = el("div", "chart-plot");
  plot.append(chart, dot,
              el("span", "chart-peak-label", most.toLocaleString()), tip);
  wrap.append(plot, axis);
  return wrap;
}

// Shared by every "name against a plays count" list: the artist bars above,
// and top albums / top genres below - same shape, different label.
export function barRows(items, name, value, emptyText) {
  if (!items.length) return [el("p", "empty", emptyText)];
  const most = Math.max(1, ...items.map(value));
  return items.map((item) => {
    const row = el("div", "home-bar-row");
    const track = el("div", "home-bar");
    const fill = el("div", "home-bar-fill");
    fill.style.width = Math.max(1.5, (value(item) / most) * 100) + "%";
    track.append(fill);
    row.append(el("span", "home-bar-name", name(item)), track,
               el("span", "home-bar-value", value(item).toLocaleString()));
    return row;
  });
}

export function artistBars(artists) {
  return barRows(artists, (a) => a.artist, (a) => a.plays,
                 "Nothing played in the last year yet.");
}

export function albumBars(albums) {
  return barRows(albums,
                 (a) => a.artist ? `${a.album} — ${a.artist}` : a.album,
                 (a) => a.plays, "Nothing played in this window yet.");
}

export function genreBars(genres) {
  return barRows(genres, (g) => g.genre, (g) => g.plays,
                 "Nothing tagged with a genre in this window yet.");
}

// The columns sit in a row of their own inside `.hour-chart`. They used to
// sit in an unstyled div, so they stacked as blocks and every bar's
// percentage height resolved against nothing - the chart drew empty.
export function hourlyBars(hourly) {
  const wrap = el("div", "hour-cols");
  if (!hourly.some((h) => h.plays > 0)) {
    return el("p", "empty", "Nothing played in this window yet.");
  }
  const most = Math.max(1, ...hourly.map((h) => h.plays));
  hourly.forEach((h) => {
    const col = el("div", "hour-col");
    const bar = el("div", "hour-bar");
    bar.style.height = Math.max(2, Math.round(100 * h.plays / most)) + "%";
    col.title = `${String(h.hour).padStart(2, "0")}:00 · `
      + `${h.plays.toLocaleString()} play${h.plays === 1 ? "" : "s"}`;
    col.append(bar);
    if (h.hour % 6 === 0) {
      col.append(el("span", "hour-label", String(h.hour).padStart(2, "0")));
    }
    wrap.append(col);
  });
  return wrap;
}

export function formatDuration(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  if (hours && minutes) return `${hours}h ${minutes}m`;
  if (hours) return `${hours}h`;
  return `${minutes}m`;
}

export function sessionStats(longest) {
  if (!longest.count) {
    return [el("p", "empty", "Nothing played in this window yet.")];
  }
  return [
    stat("Longest session", formatDuration(longest.seconds),
         `${longest.tracks} track${longest.tracks === 1 ? "" : "s"}, `
         + `starting ${longest.start.replace("T", " ").slice(0, 16)}`),
    stat("Sessions in view", longest.count.toLocaleString(),
         "a gap of 30 minutes or more starts a new one"),
  ];
}

// A stat with somewhere to go is a button styled like the plain ones;
// one with nowhere to go stays a div, because a control that does nothing
// is worse than a number that never pretended to be one.
export function stat(label, value, detail, onClick) {
  const box = el(onClick ? "button" : "div", onClick ? "stat stat-link" : "stat");
  if (onClick) {
    box.type = "button";
    box.addEventListener("click", onClick);
  }
  box.append(el("div", "stat-label", label), el("div", "stat-value", value));
  if (detail) box.append(el("div", "stat-detail", detail));
  return box;
}

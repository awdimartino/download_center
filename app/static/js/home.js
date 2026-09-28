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

import { setBanner } from "./core.js";
import { monthlyChart, artistBars, stat } from "./charts.js";
import { showView } from "./nav.js";
import { selectRange } from "./listening.js";

const homeGreeting = document.getElementById("home-greeting");
const homeHero = document.getElementById("home-hero");
const homeStats = document.getElementById("home-stats");
const homeChart = document.getElementById("home-chart");
const homeMonths = document.getElementById("home-months");
const homeArtists = document.getElementById("home-artists");
const homeListening = document.getElementById("home-listening");
const homeSnapshots = document.getElementById("home-snapshots");
const homeEmpty = document.getElementById("home-empty");

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
    // "Year" is already the name of a range button on the track list below
    // - the breakdown of which tracks made up this total.
    stat("In " + (year.year || new Date().getFullYear()),
         (year.tracks || 0).toLocaleString(),
         "different tracks, about " + hours.toLocaleString() + " hours",
         () => { selectRange(365); homeListening.scrollIntoView(
           { behavior: "smooth", block: "start" }); }),
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
    const data = await fetch("/api/overview").then((r) => r.json());
    if (data.detail) {
      homeEmpty.hidden = false;
      homeEmpty.textContent = data.detail;
      return;
    }
    const heard = data.listening || {};
    homeEmpty.hidden = true;
    homeGreeting.textContent = greeting() + ", " + data.username + ".";
    heroFact(data.highlights || []);
    homeTiles(data);
    homeMonths.replaceChildren(monthlyChart(heard.months || []));
    homeArtists.replaceChildren(...artistBars(heard.top_artists || []));

    // Only when it is wrong. A tick saying the nightly job ran is noise on
    // a page whose job is to look calm; a job that stopped three weeks ago
    // is the one thing here worth interrupting for, because every day it
    // does not run is a day of listening nobody can recover.
    const snaps = data.snapshots || {};
    setBanner(homeSnapshots, snaps.up_to_date === false
      ? "Play counts have not been read since "
        + (snaps.last_reading || "ever")
        + " — listening since then is not being recorded."
      : "", "warn");
  } catch (err) {
    homeEmpty.hidden = false;
    homeEmpty.textContent = "Could not load: " + err.message;
  }
}

homeHero.addEventListener("click",
  () => homeListening.scrollIntoView({ behavior: "smooth", block: "start" }));

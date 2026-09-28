"use strict";

// A list of numbers that should be zero. The badge on the tab is the whole
// point: problems should be visible without anyone going looking, because
// everything this catches is the kind of thing that stays quiet for months.

import { el, setBanner, setNote } from "./core.js";
import { setBadge } from "./nav.js";
import { registerOperation, startOperation } from "./operations.js";

const healthEl = document.getElementById("health");
const healthEmpty = document.getElementById("health-empty");
const healthError = document.getElementById("health-error");
const healthBadge = document.getElementById("health-badge");

function renderCheck(check) {
  const row = el("div", `check ${check.status}${check.secondary ? " secondary" : ""}`);
  const label = el("span", "check-label", check.label);
  if (check.hint) label.title = check.hint;

  row.append(
    label,
    el("span", "check-value", String(check.value)),
    el("span", "check-detail", check.detail || "")
  );
  return row;
}

function renderHealth(report) {
  setBanner(healthError, report.navidrome_error
    ? `Navidrome database unreadable: ${report.navidrome_error}` : "", "warn");

  // Every row, always. The toggle hid the status rows behind a click that
  // had to be made on every visit to see the same panel, which is a worse
  // trade than a slightly longer list. `secondary` still dims a row and
  // still keeps it out of the badge: it says "this is status, not something
  // to act on", which is a different question from whether to show it.
  const blocks = [];
  report.sections.forEach((section) => {
    if (!section.checks.length) return;
    const block = el("section", "check-group");
    block.append(el("h2", "section-head", section.title));
    block.append(...section.checks.map(renderCheck));
    blocks.push(block);
  });

  healthEl.replaceChildren(...blocks);
  healthEmpty.hidden = blocks.length > 0;

  setBadge(healthBadge, report.problems);
}

export async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    if (!response.ok) throw new Error(await response.text());
    renderHealth(await response.json());
  } catch (err) {
    healthEmpty.textContent = `Could not run health checks: ${err.message}`;
    healthEmpty.hidden = false;
  }
}

registerOperation("audit", {
  button: "health-audit", idle: "Re-read files", busy: "Reading…",
  note: "health-op",
  onResult() {
    setNote("health-op", "");
    loadHealth();
  },
});

// Reading tags from every file takes long enough that it runs on a timer in
// the background; this is for when you have just fixed something and want the
// answer now rather than in six hours.
document.getElementById("health-audit").addEventListener("click", () => {
  startOperation("audit", "/api/health/audit");
});

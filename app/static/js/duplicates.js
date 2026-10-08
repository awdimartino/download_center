"use strict";

// Groups of files that look like the same recording. The keeper is chosen on
// quality alone, because stars are migrated onto it rather than protected in
// place - so the better file wins even when the worse one is the starred one.

import { action, apiFetch, el, getJSON, postJSON, setBanner, showError } from "./core.js";
import { registerOperation, startOperation } from "./operations.js";
import { setBadge } from "./nav.js";

const dupesEl = document.getElementById("dupes");
const dupesEmpty = document.getElementById("dupes-empty");
const dupeNote = document.getElementById("dupe-note");
const dupeBadge = document.getElementById("dupe-badge");
const dupeResult = document.getElementById("dupe-result");

let dupeGroups = [];

function describeCopy(copy) {
  const bits = [copy.suffix];
  if (copy.bit_rate) bits.push(`${copy.bit_rate}k`);
  bits.push(`${copy.duration}s`);
  if (copy.size) bits.push(`${(copy.size / 1e6).toFixed(1)}MB`);
  return bits.join(" · ");
}

// Titles differ within a group more often than the grouping admits: matching
// normalises away "feat." clauses, so two different collaborations of one
// song land together. Showing only the group's first title hid exactly the
// difference you need to see before removing one of them.
function titlesDiffer(copies) {
  return new Set(copies.map((c) => `${c.artist} — ${c.title}`)).size > 1;
}

function renderGroup(group) {
  const card = el("div", `dupe${group.confident ? " confident" : ""}`);
  const first = group.copies[0];
  const mixed = titlesDiffer(group.copies);

  const head = el("div", "dupe-head");
  head.append(
    el("span", "dupe-title", `${first.artist} — ${first.title}`),
    el("span", "dupe-reason", group.reason === "musicbrainz"
      ? "same MusicBrainz recording" : "same title and length")
  );
  if (group.why) head.append(el("span", "dupe-why", `keep ${group.why}`));
  card.append(head);
  if (mixed) {
    card.append(el("div", "dupe-warn",
      "These are not titled the same. Check they are the same recording " +
      "before removing either."));
  }

  const name = `dupe-${group.key}`;
  group.copies.forEach((copy) => {
    const row = el("label", "dupe-copy");
    const radio = el("input");
    radio.type = "radio";
    radio.name = name;
    radio.value = copy.id;
    radio.checked = copy.id === group.keeper;

    row.append(
      radio,
      // The per-copy title, always. It is the field the decision turns on.
      el("span", `dupe-copy-title${mixed ? " differs" : ""}`,
         `${copy.artist} — ${copy.title}`),
      el("span", "dupe-spec", describeCopy(copy)),
      el("span", "dupe-album", copy.album || "—"),
      el("span", "dupe-path", copy.path)
    );
    if (copy.starred) row.append(el("span", "dupe-star", "★"));
    if (copy.rating) row.append(el("span", "dupe-star", "●".repeat(copy.rating)));
    card.append(row);
  });

  const actions = el("div", "dupe-actions");
  actions.append(
    action("Keep selected, remove the rest", "primary", async () => {
      const chosen = card.querySelector(`input[name="${CSS.escape(name)}"]:checked`);
      if (!chosen) return;
      const keeper = group.copies.find((c) => c.id === chosen.value);
      const losers = group.copies.filter((c) => c.id !== chosen.value);
      // Asked, because this moves audio files and there is no undo button.
      // Everything else that touches a file confirms; this was the one that
      // did not, and it is the one you press two hundred times.
      const lines = [
        `Remove ${losers.length} cop${losers.length === 1 ? "y" : "ies"}, keeping:`,
        `    ${keeper.artist} — ${keeper.title}`,
        `    ${describeCopy(keeper)}`,
        `    ${keeper.path}`,
        "",
        "Moving to duplicates-removed/ inside the library:",
        ...losers.map((c) => `    ${c.artist} — ${c.title}  (${describeCopy(c)})\n    ${c.path}`),
      ];
      if (mixed) lines.push("", "These copies are NOT titled the same.");
      // Navidrome's play count cannot be moved to the kept copy, so say what
      // goes with the removed ones. The companion's own history keeps them.
      const lost = losers.reduce((sum, c) => sum + (c.plays || 0), 0);
      if (lost) {
        lines.push("", `Navidrome's count of your ${lost} play${lost === 1 ? "" : "s"} `
          + "of the removed cop" + (losers.length === 1 ? "y" : "ies")
          + " goes with it; Home's listening history keeps them.");
      }
      if (!confirm(lines.join("\n"))) return;
      await postDupe("/api/duplicates/resolve",
        { key: group.key, keeper: chosen.value });
    }),
    action("Keep both", "", async () => {
      await postDupe("/api/duplicates/dismiss", { key: group.key });
    })
  );
  card.append(actions);
  return card;
}

// The server reports what it actually managed to do: which annotation it
// moved, which file it could not. Discarding that and simply reloading meant
// a failed move looked exactly like a successful one - the group disappeared
// from the list either way, whether or not anything had happened.
function reportResolution(payload) {
  if (!payload || payload.quarantined === undefined) {
    setBanner(dupeResult, "");
    return;
  }
  const moved = payload.quarantined || [];
  const failed = payload.failed || [];
  const migrated = payload.migrated || [];
  const parts = [];
  if (moved.length) {
    parts.push(`Set aside ${moved.length} file${moved.length === 1 ? "" : "s"} ` +
               `to duplicates-removed/.`);
  }
  if (migrated.length) {
    parts.push(`Moved ${migrated.join(" and ")} onto the copy you kept.`);
  }
  if (failed.length) parts.push(`Could not move: ${failed.join("; ")}`);
  if (!moved.length && !failed.length) parts.push("Nothing was moved.");
  setBanner(dupeResult, parts.join(" "), failed.length ? "warn" : "notice");
}

async function postDupe(path, body) {
  showError("");
  try {
    const response = await apiFetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      showError(payload.detail || `Request failed (${response.status})`);
      return;
    }
    reportResolution(payload);
    await loadDupes();
    // Keep the set-aside list honest if it is open, since a resolve is
    // exactly the thing that adds to it.
    if (quarantineEl.open) await loadQuarantine();
  } catch {
    showError("Could not reach the server.");
  }
}

function renderDupes(payload) {
  dupeGroups = payload.groups;
  dupesEl.replaceChildren(...dupeGroups.map(renderGroup));
  dupesEmpty.textContent = dupeGroups.length ? "" : "No duplicates found.";
  dupesEmpty.hidden = dupeGroups.length > 0;

  setBadge(dupeBadge, dupeGroups.length);

  setBanner(
    dupeNote,
    payload.confident
      ? `${payload.confident} group(s) share a MusicBrainz recording id and `
        + "can be resolved in one go."
      : "",
    "warn");
}

// --- what has already been set aside -------------------------------------
// Read-only. It exists because "nothing is deleted" is a claim you should be
// able to check, and until now the only way to check it was to ssh in.

const quarantineEl = document.getElementById("quarantine");
const quarantineList = document.getElementById("quarantine-list");
const quarantineEmpty = document.getElementById("quarantine-empty");
const quarantineCount = document.getElementById("quarantine-count");

function bytes(n) {
  if (!n) return "";
  return n > 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${(n / 1e6).toFixed(0)} MB`;
}

function quarantineRow(entry) {
  const row = el("div", `quarantined${entry.present ? "" : " gone"}`);
  const named = entry.artist || entry.title;

  row.append(
    el("span", "quarantined-name",
       named ? `${entry.artist} — ${entry.title}` : entry.name),
    el("span", "quarantined-album", entry.album || "—"),
    el("span", "quarantined-path", entry.was || entry.path),
    el("span", "quarantined-size", bytes(entry.size))
  );

  const notes = [];
  if (!entry.present) notes.push("file no longer there");
  // A file with no ledger row was set aside before the record was kept, or
  // moved here by hand. Worth saying so rather than showing a blank line.
  if (!entry.recorded) notes.push("no record of who removed it");
  if (entry.decided_by) notes.push(`removed by ${entry.decided_by}`);
  if (notes.length) row.append(el("span", "quarantined-note", notes.join(" · ")));
  if (entry.kept) row.title = `Kept instead: ${entry.kept}`;
  return row;
}

async function loadQuarantine() {
  try {
    const data = await getJSON("/api/duplicates/quarantined");
    const entries = data.entries || [];
    quarantineList.replaceChildren(...entries.map(quarantineRow));

    quarantineCount.textContent = entries.length
      ? `${data.total}${data.truncated ? "+" : ""}${data.bytes ? ` · ${bytes(data.bytes)}` : ""}`
      : "none";
    quarantineEmpty.hidden = entries.length > 0;

    const caveats = [];
    if (data.missing) caveats.push(`${data.missing} recorded file(s) are no longer there`);
    if (data.unrecorded) caveats.push(`${data.unrecorded} predate the removal log`);
    quarantineEmpty.textContent = entries.length
      ? "" : "Nothing has been set aside.";
    if (caveats.length) {
      quarantineList.append(el("p", "panel-sub", caveats.join(" · ") + "."));
    }
  } catch (err) {
    quarantineEmpty.textContent = `Could not read the quarantine: ${err.message}`;
    quarantineEmpty.hidden = false;
  }
}

// Only when it is opened. It walks a directory, and most visits to this tab
// are about the list above it.
quarantineEl.addEventListener("toggle", () => {
  if (quarantineEl.open) loadQuarantine();
});

export async function loadDupes() {
  try {
    const response = await apiFetch("/api/duplicates");
    if (!response.ok) throw new Error((await response.json()).detail || response.status);
    renderDupes(await response.json());
  } catch (err) {
    dupesEmpty.textContent = `Could not list duplicates: ${err.message}`;
    dupesEmpty.hidden = false;
  }
}

document.getElementById("dupe-auto").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  let preview;
  try {
    // A refusal or an outage is said as one, not as "nothing is confident
    // enough" - which is what any failure used to read as.
    preview = await postJSON("/api/duplicates/auto", {});
  } catch (err) {
    showError(`Could not check which groups are confident: ${err.message}`);
    button.disabled = false;
    return;
  }
  if (!preview.eligible) {
    showError("Nothing is confident enough to resolve unattended.");
    button.disabled = false;
    return;
  }
  if (!confirm(`Resolve ${preview.eligible} group(s) that share a MusicBrainz recording id?\n\nThe lower-quality copy of each moves to duplicates-removed/ inside its own library. This cannot be undone from here.`)) {
    button.disabled = false;
    return;
  }
  // Exactly the groups just previewed: anything that has appeared since
  // is left for the next look rather than resolved unseen. From here the
  // button follows the operation, so it stays disabled while it runs.
  const outcome = await startOperation("dupes-auto", "/api/duplicates/auto/apply",
                                       { groups: preview.groups });
  if (outcome.refused) button.disabled = false;
});

// The result arrives over the socket. Reported rather than discarded: a run
// that resolved nothing and one that resolved everything used to look the
// same from here.
registerOperation("dupes-auto", {
  note: "dupe-result",
  button: "dupe-auto",
  busy: "Resolving…",
  idle: "Resolve MusicBrainz matches",
  onResult(result) {
    const failed = result.failed || [];
    const skipped = result.skipped || [];
    setBanner(
      dupeResult,
      `Resolved ${result.resolved || 0} group(s).`
      + (skipped.length ? ` ${skipped.length} changed since the preview and were left.` : "")
      + (failed.length
         ? ` ${failed.length} problem(s): ${failed.slice(0, 3).join("; ")}` : ""),
      failed.length ? "warn" : "notice");
    loadDupes();
  },
});

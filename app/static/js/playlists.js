"use strict";

/* --- smart playlists --------------------------------------------------------
   Rules, not a fixed list of tracks. The server hands over the vocabulary a
   rule can be built from along with the playlists themselves, so the form
   cannot offer a field the server would then refuse - add one in
   app/playlists.py and it appears here without touching this file. */

import { apiFetch, el, setBanner } from "./core.js";

// The date operators whose value is a number of days rather than a date.
// Named in full: a substring test for "InTheLast" missed "inTheLast", so "in
// the last" showed a date picker and saved a date as the day count.
// tests/test_frontend.py checks these against playlists.OPERATORS.
const DAY_OPERATORS = ["inTheLast", "notInTheLast"];

const playlistsEl = document.getElementById("playlists");
const playlistsEmpty = document.getElementById("playlists-empty");
const playlistError = document.getElementById("playlist-error");
const playlistEditor = document.getElementById("playlist-editor");
const conditionsEl = document.getElementById("pl-conditions");
const plNote = document.getElementById("pl-note");
const plDelete = document.getElementById("pl-delete");
const plLibraries = document.getElementById("pl-libraries");
const plLibraryBoxes = document.getElementById("pl-library-boxes");

let vocabulary = null;
// null while creating, a playlist id while editing an existing one.
let editingId = null;

function showPlaylistError(message) {
  setBanner(playlistError, message, "warn");
}

function fieldSpec(name) {
  return vocabulary ? vocabulary.fields.find((f) => f.name === name) : null;
}

function conditionRow(condition) {
  const row = el("div", "pl-condition");

  const field = el("select", "pl-field");
  vocabulary.fields.forEach((spec) => {
    const option = el("option", "", spec.label);
    option.value = spec.name;
    field.append(option);
  });
  field.value = (condition && condition.field) || vocabulary.fields[0].name;

  const operator = el("select", "pl-operator");
  const value = el("span", "pl-value");

  const drop = el("button", "ghost remove-condition", "✕");
  drop.type = "button";
  drop.title = "Remove this condition";
  drop.addEventListener("click", () => {
    row.remove();
    // A rule with no conditions matches the whole library, so the server
    // refuses one. Better said here than after a round trip.
    if (!conditionsEl.children.length) conditionsEl.append(conditionRow(null));
  });

  function fillOperators(selected) {
    const spec = fieldSpec(field.value);
    operator.replaceChildren();
    spec.operators.forEach((op) => {
      const option = el("option", "", op.label);
      option.value = op.name;
      operator.append(option);
    });
    operator.value = spec.operators.some((o) => o.name === selected)
      ? selected : spec.operators[0].name;
  }

  function fillValue(current) {
    const spec = fieldSpec(field.value);
    let input;
    if (spec.kind === "boolean") {
      input = el("select", "pl-input");
      [["true", "yes"], ["false", "no"]].forEach((pair) => {
        const option = el("option", "", pair[1]);
        option.value = pair[0];
        input.append(option);
      });
      input.value = (current === false || current === "false") ? "false" : "true";
    } else if (spec.kind === "date" && DAY_OPERATORS.includes(operator.value)) {
      // A date asked "in the last" wants a number of days; asked "before" it
      // wants a date. Same field, different box.
      input = el("input", "pl-input");
      input.type = "number";
      input.min = "1";
      input.placeholder = "days";
      input.value = current === undefined || current === null ? "" : current;
    } else if (spec.kind === "date") {
      input = el("input", "pl-input");
      input.type = "date";
      input.value = current === undefined || current === null ? "" : current;
    } else if (spec.kind === "number") {
      input = el("input", "pl-input");
      input.type = "number";
      input.value = current === undefined || current === null ? "" : current;
    } else {
      input = el("input", "pl-input");
      input.type = "text";
      input.value = current === undefined || current === null ? "" : current;
    }
    if (spec.hint) input.title = spec.hint;
    value.replaceChildren(input);
  }

  field.addEventListener("change", () => {
    // Operators and the kind of value box both belong to the field, so
    // changing it rebuilds them rather than leaving "starts with" sitting
    // on a play count.
    fillOperators(null);
    fillValue(null);
  });
  operator.addEventListener("change", () => fillValue(null));

  fillOperators(condition && condition.operator);
  fillValue(condition ? condition.value : null);
  row.append(field, operator, value, drop);
  return row;
}

function readCondition(row) {
  const input = row.querySelector(".pl-input");
  return {
    field: row.querySelector(".pl-field").value,
    operator: row.querySelector(".pl-operator").value,
    value: input ? input.value : "",
  };
}

function openEditor(playlist) {
  editingId = (playlist && playlist.id) || null;
  showPlaylistError("");
  plNote.textContent = "";

  playlistEditor.elements.name.value = (playlist && playlist.name) || "";
  playlistEditor.elements.comment.value = (playlist && playlist.comment) || "";
  document.getElementById("pl-public").checked = !!(playlist && playlist.public);

  const shape = playlist ? playlist.form : null;
  document.getElementById("pl-match-mode").value = (shape && shape.match) || "all";

  const sortSelect = document.getElementById("pl-sort");
  sortSelect.replaceChildren();
  const none = el("option", "", "nothing in particular");
  none.value = "";
  sortSelect.append(none);
  vocabulary.sorts.forEach((sort) => {
    const option = el("option", "", sort.label);
    option.value = sort.name;
    sortSelect.append(option);
  });
  sortSelect.value = (shape && shape.sort) || "";
  document.getElementById("pl-direction").value = (shape && shape.direction) || "desc";
  document.getElementById("pl-limit").value = (shape && shape.limit) || "";

  const rows = ((shape && shape.conditions) || []).map((c) => conditionRow(c));
  conditionsEl.replaceChildren.apply(
    conditionsEl, rows.length ? rows : [conditionRow(null)]);

  // One library needs no choosing; the server scopes to it regardless.
  // An unscoped playlist opens with every box ticked, which is what saving
  // it will write.
  const libraries = vocabulary.libraries || [];
  const chosen = (shape && shape.libraries) || libraries.map((l) => l.id);
  plLibraries.hidden = libraries.length < 2;
  plLibraryBoxes.replaceChildren.apply(plLibraryBoxes, libraries.map((library) => {
    const label = el("label", "pl-library");
    const box = el("input");
    box.type = "checkbox";
    box.value = String(library.id);
    box.checked = chosen.includes(library.id);
    label.append(box, ` ${library.name}`);
    return label;
  }));

  plDelete.hidden = !editingId;
  playlistEditor.hidden = false;
  playlistEditor.scrollIntoView({ block: "nearest" });
}

function closeEditor() {
  playlistEditor.hidden = true;
  editingId = null;
}

function describeRule(shape) {
  const spoken = shape.conditions.map((condition) => {
    const spec = fieldSpec(condition.field);
    if (!spec) return condition.field;
    const op = spec.operators.find((o) => o.name === condition.operator);
    let value = condition.value;
    if (spec.kind === "boolean") value = value ? "yes" : "no";
    // "in the last 7" is a number of days, and the sentence has to say so
    // - the operator label cannot, because the value box beside it is
    // sometimes a date instead.
    if (spec.kind === "date" && DAY_OPERATORS.includes(condition.operator)) {
      value = `${value} days`;
    }
    return `${spec.label} ${op ? op.label : condition.operator} ${value}`;
  });
  const joined = spoken.join(shape.match === "all" ? ", and " : ", or ");
  const sort = vocabulary.sorts.find((s) => s.name === shape.sort);
  const tail = [];
  // Only worth saying when there was a choice to make.
  const mine = vocabulary.libraries || [];
  if (shape.libraries && mine.length > 1) {
    const names = mine.filter((l) => shape.libraries.includes(l.id)).map((l) => l.name);
    if (names.length < mine.length) tail.push(`from ${names.join(" and ")}`);
  }
  if (sort) tail.push(`sorted by ${sort.label.toLowerCase()}`);
  if (shape.limit) tail.push(`first ${shape.limit}`);
  return tail.length ? `${joined} — ${tail.join(", ")}` : joined;
}

function playlistCard(playlist) {
  const card = el("div", "playlist");

  const head = el("div", "playlist-head");
  head.append(el("span", "playlist-name", playlist.name));
  head.append(el("span", "playlist-count",
    `${playlist.song_count} track${playlist.song_count === 1 ? "" : "s"}`));
  if (playlist.public) head.append(el("span", "badge", "shared"));
  card.append(head);

  if (playlist.comment) card.append(el("div", "playlist-comment", playlist.comment));

  if (playlist.form) {
    card.append(el("div", "playlist-rule", describeRule(playlist.form)));
    // Navidrome evaluates rules against every library on the server, so a
    // rule naming none collects other people's music too. Saving it here
    // writes the limit in.
    if (playlist.form.libraries === null) {
      card.append(el("p", "warn",
        "Draws from every library on the server, not just yours. Edit and save to fix."));
    }
  } else {
    // Rules this form cannot represent are shown and left alone. Opening one
    // would drop the part the form has no row for, and a smart playlist
    // quietly matching the wrong thing is worse than one we decline to edit.
    card.append(el("div", "playlist-rule dim",
      `Written by hand — ${playlist.unsupported}. Edit it where it was written.`));
  }

  const actions = el("div", "playlist-actions");
  if (playlist.form) {
    const edit = el("button", "ghost", "Edit");
    edit.type = "button";
    edit.addEventListener("click", () => openEditor(playlist));
    actions.append(edit);
  }
  card.append(actions);
  return card;
}

export async function loadPlaylists() {
  try {
    const response = await apiFetch("/api/playlists");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load playlists.");
    vocabulary = data.vocabulary;
    playlistsEl.replaceChildren.apply(
      playlistsEl, data.playlists.map(playlistCard));
    playlistsEmpty.textContent = "No smart playlists yet.";
    playlistsEmpty.hidden = data.playlists.length > 0;
    showPlaylistError("");
  } catch (exc) {
    playlistsEl.replaceChildren();
    playlistsEmpty.hidden = true;
    showPlaylistError(String(exc.message || exc));
  }
}

document.getElementById("playlist-new").addEventListener("click", () => {
  if (!vocabulary) return;
  openEditor(null);
});

document.getElementById("pl-add-condition").addEventListener("click", () => {
  conditionsEl.append(conditionRow(null));
});

document.getElementById("pl-cancel").addEventListener("click", closeEditor);

playlistEditor.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = playlistEditor.querySelector("button[type=submit]");
  button.disabled = true;
  plNote.textContent = "";
  showPlaylistError("");

  const body = {
    name: playlistEditor.elements.name.value,
    comment: playlistEditor.elements.comment.value,
    public: document.getElementById("pl-public").checked,
    form: {
      match: document.getElementById("pl-match-mode").value,
      conditions: Array.from(conditionsEl.children).map(readCondition),
      sort: document.getElementById("pl-sort").value,
      direction: document.getElementById("pl-direction").value,
      limit: Number(document.getElementById("pl-limit").value) || 0,
      libraries: Array.from(plLibraryBoxes.querySelectorAll("input:checked"))
        .map((box) => Number(box.value)),
    },
  };
  if (!plLibraries.hidden && !body.form.libraries.length) {
    showPlaylistError("Pick at least one library to draw from.");
    button.disabled = false;
    return;
  }

  try {
    const response = await apiFetch(
      editingId ? `/api/playlists/${editingId}` : "/api/playlists",
      {
        method: editingId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not save.");
    // The editor is now editing the thing it just made. Without this it
    // still believed it was creating, so a second Save made a second
    // playlist, and a third made a third.
    if (!editingId && data.id) {
      editingId = data.id;
      plDelete.hidden = false;
    }
    // The count comes back from Navidrome, which evaluated the rules on
    // save. It is the number the playlist will show there, because it is
    // the number that thing computed - not a second guess made here.
    plNote.textContent =
      `Saved — matches ${data.song_count} track${data.song_count === 1 ? "" : "s"}.`;
    await loadPlaylists();
  } catch (exc) {
    showPlaylistError(String(exc.message || exc));
  } finally {
    button.disabled = false;
  }
});

plDelete.addEventListener("click", async () => {
  if (!editingId) return;
  const name = playlistEditor.elements.name.value || "this playlist";
  if (!confirm(`Delete ${name}? No tracks are touched — only the rules.`)) return;
  try {
    const response = await apiFetch(`/api/playlists/${editingId}`, { method: "DELETE" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not delete.");
    closeEditor();
    await loadPlaylists();
  } catch (exc) {
    showPlaylistError(String(exc.message || exc));
  }
});

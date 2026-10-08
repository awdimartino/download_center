"use strict";

/* --- library: combining into one album -----------------------------------
   Several albums and loose tracks into one, in one step. What used to take a
   rename per album, typed exactly right each time, and a move per loose
   track from behind "More". Nothing changes until Combine is pressed. */

import { el, getJSON, postJSON, setNote } from "./core.js";
import { registerOperation, startOperation } from "./operations.js";
import {
  actionButton,
  albumArt,
  albumKey,
  editField,
  forgetCoverSurvey,
  isBarred,
  libraryDialog,
  libraryState,
  looseTracks,
  plural,
  selection,
  showProgress
} from "./library-shared.js";
import {
  refreshLibrary,
  refreshPicks,
  renderBar,
  setSelecting
} from "./library.js";
import { closeDrawer } from "./library-drawer.js";

let combining = null;

function mostCommon(values) {
  const counts = new Map();
  for (const v of values) if (v) counts.set(v, (counts.get(v) || 0) + 1);
  let best = "";
  let n = 0;
  for (const [v, c] of counts) if (c > n) { best = v; n = c; }
  return best;
}

export function combineGroup(group) {
  closeDrawer();
  setSelecting(true);
  for (const album of group.albums) selection.albums.set(albumKey(album), album);
  refreshPicks();
  openCombine();
}

export async function openCombine() {
  const albums = [...selection.albums.values()];
  const loose = looseTracks();
  const libraries = new Set([...albums.map((a) => a.library_id),
                             ...loose.map((t) => t.album.library_id)]);
  if (libraries.size > 1) {
    setNote("library-op",
      "Those are in different libraries. A combine works inside one library.", "warn");
    return;
  }

  libraryDialog.replaceChildren(dialogShell("Reading the tracks…"));
  libraryDialog.hidden = false;
  renderBar();

  let lists;
  try {
    lists = await Promise.all(albums.map((a) => getJSON(
      `/api/library/album?library_id=${a.library_id}&folder=${encodeURIComponent(a.folder)}`)));
  } catch (err) {
    closeCombine();
    setNote("library-op", `Could not read those albums: ${err.message}`, "warn");
    return;
  }

  // The album to keep goes first: the biggest real album selected, since
  // that is nearly always the record the rest belong on.
  const keep = albums.filter((a) => a.kind === "album")
    .sort((a, b) => b.tracks - a.tracks)[0] || null;
  const items = [];
  const ordered = keep ? [keep, ...albums.filter((a) => a !== keep)] : albums;
  for (const album of ordered) {
    const list = lists[albums.indexOf(album)];
    for (const track of list.items) items.push({ track, album });
  }
  items.push(...loose.map(({ track, album }) => ({ track, album })));
  if (items.length < 2) {
    closeCombine();
    setNote("library-op", "Choose at least two tracks to combine.", "warn");
    return;
  }

  const artist = mostCommon([...albums.map((a) => a.artist),
                             ...loose.map((t) => t.album.artist)]);
  combining = {
    library_id: [...libraries][0],
    albums, loose, items,
    target: keep ? albumKey(keep) : "new",
    newArtist: artist,
    newTitle: "",
    various: false,
    albumArtists: new Set([...albums.map((a) => a.artist),
                           ...loose.map((t) => t.album.artist)]).size,
    renumber: true,
    cover: keep ? { folder: keep.folder, art_id: keep.art_id } : null,
    guess: null,
    error: "",
  };
  if (!combining.cover && albums.length) {
    const first = albums.find((a) => !isBarred(a)) || albums[0];
    combining.cover = { folder: first.folder, art_id: first.art_id };
  }
  renderCombine();
  guessName();
}

// Asks Spotify which album most of these songs are on. Only ever fills an
// empty name and offers a cover; it never chooses anything by itself.
async function guessName() {
  const state = combining;
  const titles = [...new Set(state.items.map((i) => i.track.title))];
  try {
    const { guess } = await postJSON("/api/library/combine/guess",
                                     { artist: state.newArtist, titles });
    if (combining !== state || !guess) return;
    state.guess = guess;
    if (!state.newTitle) state.newTitle = guess.album;
    renderCombine();
  } catch {
    // Spotify not set up or not answering: the name is typed by hand.
  }
}

export function closeCombine() {
  combining = null;
  libraryDialog.hidden = true;
  libraryDialog.replaceChildren();
  renderBar();
}

function dialogShell(message) {
  const sheet = el("div", "lib-sheet");
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-label", "Combine into one album");
  sheet.append(el("p", "empty", message));
  return sheet;
}

function targetOption(checked, art, title, detail, onPick, focusKey) {
  const option = el("label", `lib-target${checked ? " on" : ""}`);
  const radio = el("input");
  radio.type = "radio";
  radio.name = "combine-target";
  radio.dataset.focus = focusKey;
  radio.checked = checked;
  radio.addEventListener("change", onPick);
  const words = el("span", "lib-target-text", title);
  words.append(el("small", "", detail));
  option.append(radio, art, words);
  return option;
}

function renderCombine() {
  const c = combining;
  if (!c) return;

  const sheet = el("div", "lib-sheet");
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-label", "Combine into one album");

  const head = el("div", "lib-sheet-head");
  head.append(el("h2", "", "Combine into one album"),
              el("p", "", `${plural(c.items.length, "track")} from `
                + `${plural(new Set(c.items.map((i) => albumKey(i.album))).size, "album")}. `
                + "Nothing changes until you press Combine."));

  // 1. Where they end up.
  const step1 = el("section", "lib-step");
  step1.append(el("h3", "", "1 · Which album do they end up in?"));
  for (const album of c.albums.filter((a) => a.kind === "album")) {
    step1.append(targetOption(c.target === albumKey(album), albumArt(album, 96),
      album.album, `${album.artist} · keeps its name, stars and play counts`, () => {
        c.target = albumKey(album);
        c.cover = { folder: album.folder, art_id: album.art_id };
        renderCombine();
      }, `target:${albumKey(album)}`));
  }
  step1.append(targetOption(c.target === "new", el("span", "lib-plus", "+"),
    "A new album", "Or type the name of one you already have, and they join it", () => {
      c.target = "new";
      renderCombine();
    }, "target:new"));
  if (c.target === "new") {
    const fields = el("div", "lib-fields");
    const artist = editField("Album artist", c.various ? "Various Artists" : c.newArtist, { wide: true });
    artist.input.disabled = c.various;
    artist.input.dataset.focus = "artist";
    artist.input.addEventListener("input", () => { c.newArtist = artist.input.value; updateSummary(); });
    const name = editField("Album", c.newTitle, { wide: true });
    name.input.placeholder = "The album's name";
    name.input.dataset.focus = "name";
    name.input.addEventListener("input", () => { c.newTitle = name.input.value; updateSummary(); });
    fields.append(artist, name);
    step1.append(fields);
    if (c.guess) {
      const hint = el("p", "lib-note",
        `Spotify has ${plural(c.guess.votes, "of these song")} on `
        + `“${c.guess.album}”${c.guess.year ? ` (${c.guess.year})` : ""}.`);
      if (c.newTitle !== c.guess.album) {
        const use = actionButton("Use that name", "ghost", () => {
          c.newTitle = c.guess.album;
          renderCombine();
        });
        use.dataset.focus = "use-guess";
        hint.append(" ", use);
      }
      step1.append(hint);
    }
    if (c.albumArtists > 1) {
      const various = el("label", "lib-option");
      const box = el("input");
      box.type = "checkbox";
      box.dataset.focus = "various";
      box.checked = c.various;
      box.addEventListener("change", () => { c.various = box.checked; renderCombine(); });
      various.append(box, `These come from ${c.albumArtists} artists — file them as `
        + "Various Artists, keeping each track's own artist");
      step1.append(various);
    }
  }

  // 2. Order.
  const step2 = el("section", "lib-step");
  step2.append(el("h3", "", "2 · Track order"));
  const order = el("div", "lib-order");
  const titles = new Map();
  for (const i of c.items) {
    const t = i.track.title.toLowerCase();
    titles.set(t, (titles.get(t) || 0) + 1);
  }
  let dragFrom = null;
  c.items.forEach((item, index) => {
    const row = el("div", "lib-ord");
    row.draggable = true;
    const words = el("span", "lib-ord-title", item.track.title);
    if (titles.get(item.track.title.toLowerCase()) > 1) {
      words.append(el("span", "lib-dupe", " · same title twice"));
    }
    words.append(el("small", "", `from ${item.album.album}`));
    const up = actionButton("↑", "ghost lib-move", () => move(index, index - 1));
    up.setAttribute("aria-label", `Move ${item.track.title} up`);
    // Keyed by the track, not its place: after a move the same button is
    // found in the row's new place and keeps the focus.
    up.dataset.focus = `up:${item.track.path}`;
    up.disabled = index === 0;
    const down = actionButton("↓", "ghost lib-move", () => move(index, index + 1));
    down.setAttribute("aria-label", `Move ${item.track.title} down`);
    down.dataset.focus = `down:${item.track.path}`;
    down.disabled = index === c.items.length - 1;
    row.append(el("span", "lib-grip", "⋮⋮"),
               el("span", "lib-ord-no", String(c.renumber ? index + 1 : item.track.track_no || "")),
               words, up, down);
    row.addEventListener("dragstart", () => { dragFrom = index; row.classList.add("dragging"); });
    row.addEventListener("dragend", () => row.classList.remove("dragging"));
    row.addEventListener("dragover", (event) => { event.preventDefault(); row.classList.add("over"); });
    row.addEventListener("dragleave", () => row.classList.remove("over"));
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      if (dragFrom !== null) move(dragFrom, index);
    });
    order.append(row);
  });
  function move(from, to) {
    if (to < 0 || to >= c.items.length || from === to) return;
    const [item] = c.items.splice(from, 1);
    c.items.splice(to, 0, item);
    renderCombine();
  }
  const renumber = el("label", "lib-option");
  const box = el("input");
  box.type = "checkbox";
  box.dataset.focus = "renumber";
  box.checked = c.renumber;
  box.addEventListener("change", () => { c.renumber = box.checked; renderCombine(); });
  renumber.append(box, `Number them 1–${c.items.length} in this order`);
  step2.append(order, renumber);
  if ([...titles.values()].some((n) => n > 1)) {
    step2.append(el("p", "lib-note tone-warn",
      "Two tracks share a title. Both are kept; the Duplicates panel can "
      + "choose between them afterwards."));
  }

  // 3. Cover.
  const step3 = el("section", "lib-step");
  step3.append(el("h3", "", "3 · Cover"));
  const covers = el("div", "lib-cover-pick");
  const coverChoice = (checked, art, label, choose, focusKey) => {
    const option = el("label", "lib-cover-option");
    option.title = label;
    const radio = el("input");
    radio.type = "radio";
    radio.name = "combine-cover";
    radio.dataset.focus = focusKey;
    radio.checked = checked;
    radio.addEventListener("change", choose);
    option.append(radio, art);
    return option;
  };
  for (const album of c.albums) {
    covers.append(coverChoice(
      !!c.cover && c.cover.folder === album.folder, albumArt(album, 150), album.album,
      () => { c.cover = { folder: album.folder, art_id: album.art_id }; },
      `cover:${album.folder}`));
  }
  if (c.guess && c.guess.cover) {
    const img = el("div", "art");
    const pic = el("img");
    pic.alt = "";
    pic.src = c.guess.cover;
    img.append(pic);
    covers.append(coverChoice(!!c.cover && c.cover.url === c.guess.cover, img,
      `Spotify: ${c.guess.album}`, () => { c.cover = { url: c.guess.cover }; },
      "cover:spotify"));
  }
  step3.append(covers, el("p", "lib-note",
    "Whichever you pick goes on every track. A YouTube cover is squared first, "
    + "so none of them keep their bars."));

  // Footer.
  const foot = el("div", "lib-sheet-foot");
  const summary = el("span", "lib-sheet-summary");
  const go = actionButton("", "", () => submitCombine(go));
  go.dataset.focus = "go";
  function updateSummary() {
    const name = destination();
    summary.classList.toggle("tone-warn", !!c.error);
    summary.textContent = c.error
      || (name.album ? `Into ${name.albumartist} — ${name.album}` : "Name the album");
    go.textContent = `Combine ${plural(c.items.length, "track")}`;
    go.disabled = !name.album || !name.albumartist;
  }
  const cancel = actionButton("Cancel", "ghost", () => closeCombine());
  cancel.dataset.focus = "cancel";
  foot.append(summary, cancel, go);
  updateSummary();

  const body = el("div", "lib-sheet-body");
  body.append(step1, step2, step3);
  sheet.append(head, body, foot);
  sheet.tabIndex = -1;
  const scroll = libraryDialog.querySelector(".lib-sheet-body");
  const top = scroll ? scroll.scrollTop : 0;
  // The whole sheet is rebuilt on every change, which used to throw the
  // focus to the page behind: Spotify's guess arriving mid-word took the
  // rest of the typing with it, and each keyboard move of a track meant
  // tabbing back in from the top. The same control gets it back, caret and
  // all; the first time, the sheet itself takes it.
  const active = document.activeElement;
  const held = active && libraryDialog.contains(active) ? active.dataset.focus : null;
  const caret = held && typeof active.selectionStart === "number"
    ? [active.selectionStart, active.selectionEnd] : null;
  libraryDialog.replaceChildren(sheet);
  body.scrollTop = top;
  restoreFocus(sheet, held, caret);
}

function restoreFocus(sheet, held, caret) {
  const find = (key) => key && sheet.querySelector(`[data-focus="${CSS.escape(key)}"]`);
  let again = find(held);
  if (again && again.disabled && held) {
    // Moved to the end it was heading for: its other button.
    const [way, path] = [held.slice(0, held.indexOf(":")), held.slice(held.indexOf(":") + 1)];
    again = find(`${way === "up" ? "down" : "up"}:${path}`) || again;
  }
  if (again && !again.disabled) {
    again.focus();
    if (caret && again.setSelectionRange) {
      try { again.setSelectionRange(caret[0], caret[1]); } catch { /* not a text box */ }
    }
  } else if (!held) {
    sheet.focus();
  }
}

// Tab stays inside the dialog while it is open; behind it is a page that
// cannot be seen properly and should not be acted on.
libraryDialog.addEventListener("keydown", (event) => {
  if (event.key !== "Tab" || libraryDialog.hidden) return;
  const focusable = [...libraryDialog.querySelectorAll(
    "button:not([disabled]), input:not([disabled]), [tabindex='0']")]
    .filter((node) => node.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && (document.activeElement === first
                         || !libraryDialog.contains(document.activeElement)
                         || document.activeElement === libraryDialog.querySelector(".lib-sheet"))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

function destination() {
  const c = combining;
  const keepAlbum = c.albums.find((a) => albumKey(a) === c.target);
  if (keepAlbum) return { albumartist: keepAlbum.artist, album: keepAlbum.album };
  return {
    albumartist: (c.various ? "Various Artists" : c.newArtist).trim(),
    album: c.newTitle.trim(),
  };
}

async function submitCombine(go) {
  const c = combining;
  const name = destination();
  const keepAlbum = c.albums.find((a) => albumKey(a) === c.target);
  c.error = "";
  go.disabled = true;
  const payload = await startOperation("combine", "/api/library/combine", {
    library_id: c.library_id,
    albumartist: name.albumartist,
    album: name.album,
    albums: c.albums.map((a) => a.folder),
    tracks: c.loose.map((t) => t.track.path),
    keep: keepAlbum ? keepAlbum.folder : null,
    order: c.renumber ? c.items.map((i) => i.track.path) : [],
    cover_folder: c.cover && c.cover.folder ? c.cover.folder : null,
    cover_url: c.cover && c.cover.url ? c.cover.url : null,
  });
  if (!payload || payload.detail) {
    // Said in the dialog: the panel's own status line is behind it.
    c.error = (payload && payload.detail) || "Could not start the combine.";
    renderCombine();
    return;
  }
  if (!payload.started) {
    // Another combine is running, and this one was not queued behind it.
    // Closing the dialog and clearing the selection here read as success,
    // and the combine set up so carefully was simply never run.
    c.error = "Another combine is still running, so this one was not "
      + "started. Try again when it finishes.";
    renderCombine();
    return;
  }
  closeCombine();
  setSelecting(false);
}

registerOperation("combine", {
  note: "library-op",
  onButtonState(running, operation) {
    showProgress(operation, { label: "Combining" });
  },
  onResult(result) {
    const failed = result.failed || [];
    setNote("library-op",
      `Combined ${plural(result.moved || 0, "track")} into `
      + `${result.albumartist} — ${result.album}.`
      + (failed.length ? ` Problems: ${failed.join("; ")}` : "")
      + " Navidrome shows it as one album after its rescan.",
      failed.length ? "warn" : "notice");
    libraryState.artistsCache = null;
    forgetCoverSurvey();
    refreshLibrary();
  },
});

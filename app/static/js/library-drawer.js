"use strict";

/* --- library: one album, in the panel ------------------------------------
   An album opens in a panel beside the list: its details, its tracks edited
   in place, and everything done to it - renaming and merging, a new cover,
   choosing a MusicBrainz match by hand, setting it aside. */

import { el, getJSON, plural, postJSON, setNote } from "./core.js";
import { registerOperation, startOperation } from "./operations.js";
import {
  actionButton,
  albumArt,
  albumKey,
  albumName,
  artStamps,
  editField,
  forgetCoverSurvey,
  isBarred,
  isPicked,
  libraryDrawer,
  libraryState,
  libraryStatus,
  libraryView,
  RESCAN_WAIT_MS,
  selection
} from "./library-shared.js";
import {
  openArtist,
  refreshLibrary,
  refreshPicks,
  renderBar,
  setSelecting
} from "./library.js";
import { mergeInto } from "./library-combine.js";

export let openAlbum = null;
let drawerTracks = [];
let drawerTracksEl = null;
let drawerTracksHead = null;
// Where the status block lives while no album is open. It is carried into
// the panel while one is, since the panel covers it.
const statusHome = document.createComment("library-status");
libraryStatus.before(statusHome);

function pill(text, tone = "") {
  return el("span", `lib-pill ${tone}`.trim(), text);
}

function statusPills(album) {
  const box = el("div", "lib-pills");
  // Opened from a song in the search results, which says nothing about the
  // album's state - better no pills than wrong ones.
  if (album.stub) return box;
  // Two separate questions. Whether MusicBrainz knows it is a fact about
  // MusicBrainz, and some music will never be in it. Whether it still needs
  // review is the queue: everything arrives needing it, and it leaves by
  // getting a match or by somebody marking it reviewed.
  if (album.matched) box.append(pill("MusicBrainz ✓", "tone-ok"));
  else if (album.partial) box.append(pill(`MusicBrainz: ${album.untagged} of ${album.tracks} unmatched`));
  else box.append(pill("No MusicBrainz match"));
  if (!album.matched) {
    box.append(album.reviewed ? pill("Reviewed ✓", "tone-ok")
                              : pill("Needs review", "tone-warn"));
  }
  box.append(album.no_gain ? pill("No ReplayGain", "tone-warn") : pill("ReplayGain ✓", "tone-ok"));
  if (isBarred(album)) box.append(pill("Cover has bars", "tone-warn"));
  // Renaming, matching, covers, combining and ReplayGain treat a folder as
  // one album, so the server refuses them here; this says so first.
  if (album.albums_here > 1) {
    box.append(pill(`${album.albums_here} albums in this folder — edit track by track`,
                    "tone-warn"));
  }
  return box;
}

function albumMeta(album) {
  const minutes = album.duration ? Math.round(album.duration / 60) : 0;
  const added = album.added ? new Date(album.added) : null;
  return [
    album.year || null,
    album.tracks ? plural(album.tracks, "track") : null,
    minutes ? `${minutes} min` : null,
    added && !Number.isNaN(added.getTime())
      ? `added ${added.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}`
      : null,
    album.plays ? plural(album.plays, "play") : null,
    album.library || null,
  ].filter(Boolean).join(" · ");
}

// The way out of the review queue for music MusicBrainz does not have.
// Only offered where there is something to decide - a match already took
// the album out - and undoable, because a tap on the wrong album should not
// lose it from the queue for good. The panel stays open: marking one and
// moving on to the next is the whole point.
function reviewButton(album, statusHolder) {
  if (album.matched || album.stub) return document.createTextNode("");
  const b = actionButton("", "", () => saveEdit("/api/library/reviewed", {
    library_id: album.library_id, folder: album.folder,
    reviewed: !album.reviewed,
  }, b, () => {
    album.reviewed = !album.reviewed;
    album.needs_review = !album.reviewed;
    statusHolder.querySelector(".lib-pills").replaceWith(statusPills(album));
    refreshLibrary(album);
  // After, not inside: saveEdit puts the button's old words back when it
  // finishes, which would undo the new label.
  }).then(label));
  function label() {
    b.textContent = album.reviewed ? "Needs review" : "Mark reviewed";
    b.className = album.reviewed ? "ghost" : "";
    b.title = album.reviewed ? "Put this album back in the review queue"
      : "Tagged the way you want it, MusicBrainz or not";
  }
  label();
  return b;
}

function moreMenu(album) {
  const wrap = el("div", "lib-more");
  const toggle = el("button", "ghost dropdown", "More");
  toggle.type = "button";
  toggle.setAttribute("aria-haspopup", "true");
  toggle.setAttribute("aria-expanded", "false");
  const menu = el("div", "lib-menu");
  menu.hidden = true;

  const items = [];
  if (album.no_gain) {
    items.push(actionButton("Measure ReplayGain", "ghost", () =>
      startOperation("replaygain", "/api/library/replaygain",
                     { library_id: album.library_id, folder: album.folder })));
  }
  items.push(actionButton("Combine with other albums…", "ghost", () => {
    closeDrawer();
    setSelecting(true);
    selection.albums.set(albumKey(album), album);
    refreshPicks();
    setNote("library-op",
      `${albumName(album)} is selected. Pick the albums or tracks to combine `
      + "with it, then Combine.", "notice");
  }));
  items.push(actionButton("Quarantine album", "ghost danger", (b) => quarantineAlbum(album, b)));
  menu.append(...items);

  toggle.addEventListener("click", (event) => {
    event.stopPropagation();
    menu.hidden = !menu.hidden;
    toggle.setAttribute("aria-expanded", String(!menu.hidden));
  });
  menu.addEventListener("click", () => { menu.hidden = true; });
  wrap.append(toggle, menu);
  return wrap;
}

export function openDrawer(album) {
  openAlbum = album;
  closeCandidates();
  const top = el("div", "lib-drawer-top");
  const close = actionButton("✕ Close", "ghost", () => closeDrawer());
  const pick = actionButton(selection.on ? "Done selecting" : "Select tracks", "ghost", () => {
    setSelecting(!selection.on);
    pick.textContent = selection.on ? "Done selecting" : "Select tracks";
  });
  top.append(close, pick);

  const hero = el("div", "lib-hero");
  const words = el("div", "lib-hero-text");
  const artistLink = actionButton(album.artist || "Unknown artist", "lib-artist-link",
                            () => openArtist(album.artist));
  words.append(
    pill(album.kind === "single" ? "Single" : "Album"),
    el("h2", "lib-hero-title", album.album || album.folder),
    artistLink,
    el("p", "lib-hero-meta", albumMeta(album)),
    statusPills(album));
  hero.append(albumArt(album, 600), words);

  const editWrap = el("div", "lib-panel");
  editWrap.hidden = true;
  const picker = el("div", "cover-picker");
  picker.hidden = true;

  const actions = el("div", "lib-actions");
  const edit = actionButton("Edit details", "ghost", () => {
    if (album.tracks === undefined) {
      // A song-search stub before its tracks arrive: the names the editor
      // would start from are not the album's yet.
      setNote("library-op", "Still reading this album; try again in a moment.",
              "warn");
      return;
    }
    if (!editWrap.childElementCount) editWrap.append(albumEditor(album));
    editWrap.hidden = !editWrap.hidden;
    edit.textContent = editWrap.hidden ? "Edit details" : "Close editor";
  });
  const cover = actionButton("Fetch cover", isBarred(album) ? "" : "ghost",
                       (b) => showCovers(album, b, picker));
  cover.title = "Replace only the cover art - every tag stays as it is";
  const match = actionButton("Find matches", "ghost", (b) => askForCandidates(album, b));
  match.title = "Ask MusicBrainz, then retag every track from the one you pick";
  const missingBox = el("div", "lib-panel miss");
  missingBox.hidden = true;
  const missing = actionButton("Missing tracks", "ghost", (b) => showMissing(album, b, missingBox));
  missing.title = "Compare with the album's full tracklist on MusicBrainz or Spotify";
  actions.append(reviewButton(album, words), edit, cover, match, missing, moreMenu(album));

  drawerTracksHead = el("div", "lib-tracks-head");
  drawerTracksEl = el("div", "lib-tracks");
  drawerTracksEl.append(el("p", "empty", "Reading…"));

  const body = el("div", "lib-drawer-body");
  body.append(hero, libraryStatus, actions, editWrap, picker, missingBox,
              drawerTracksHead, drawerTracksEl);
  libraryDrawer.replaceChildren(top, body);
  libraryDrawer.hidden = false;
  libraryView.classList.add("drawer-open");
  libraryDrawer.scrollTop = 0;
  close.focus({ preventScroll: true });
  loadTracks(album);
}

/* --- what the album is missing ----------------------------------------------
   The album's full tracklist - its MusicBrainz release, or Spotify's copy
   when it has none - with what the library holds marked off, and the rest a
   button away. Other editions are a choice in the panel: a standard copy is
   complete, and missing its bonus tracks against the deluxe. */

async function showMissing(album, button, box, edition) {
  if (!edition && !box.hidden) {
    box.hidden = true;
    button.textContent = "Missing tracks";
    return;
  }
  box.hidden = false;
  button.textContent = "Hide missing";
  box.replaceChildren(el("p", "empty",
    edition ? "Reading that edition…" : "Comparing with the album's full tracklist…"));
  const asked = album;
  let data;
  try {
    data = await getJSON(
      `/api/library/album/missing?library_id=${album.library_id}`
      + `&folder=${encodeURIComponent(album.folder)}`
      + (edition ? `&edition=${encodeURIComponent(edition)}` : ""));
  } catch (err) {
    if (openAlbum === asked) box.replaceChildren(el("p", "empty", `Could not compare: ${err.message}`));
    return;
  }
  if (openAlbum !== asked) return;
  renderMissing(album, button, box, data);
}

function renderMissing(album, button, box, data) {
  const where = data.source === "musicbrainz" ? "MusicBrainz" : "Spotify";
  const from = el("p", "miss-from");
  const link = el("a", "", data.label);
  if (data.url) {
    link.href = data.url;
    link.target = "_blank";
    link.rel = "noopener";
  }
  from.append("Compared with ", link, ` on ${where}`);

  const parts = [from];
  if (data.editions.length > 1) {
    const choose = el("label", "miss-edition");
    const select = el("select");
    for (const one of data.editions) {
      const option = el("option", "", one.label);
      option.value = one.id;
      option.selected = one.id === data.edition;
      select.append(option);
    }
    select.addEventListener("change", () => showMissing(album, button, box, select.value));
    choose.append(el("span", "", "Edition"), select);
    parts.push(choose);
  }

  const total = data.tracks.length;
  const summary = el("p", "miss-sum", data.missing
    ? `${data.missing} of ${total} tracks missing`
    : `Nothing missing: all ${total} tracks of this edition are in your library.`);
  if (data.elsewhere) {
    const n = data.elsewhere;
    summary.append(el("small", "", ` · ${n} ${n === 1 ? "is" : "are"} in another album`));
  }
  if (data.not_on_edition) {
    const n = data.not_on_edition;
    summary.append(el("small", "",
      ` · ${n} of your tracks ${n === 1 ? "is" : "are"} not on this edition`));
  }
  parts.push(summary);

  const discs = new Set(data.tracks.map((t) => t.disc)).size > 1;
  const gets = [];
  const rows = data.tracks.map((track) => {
    const row = el("div", `miss-trk${track.held ? " held" : ""}`);
    const number = !track.number ? ""
      : discs ? `${track.disc}-${String(track.number).padStart(2, "0")}` : String(track.number);
    const name = el("span", "miss-name", track.title);
    if (track.artist && track.artist !== data.artist) name.append(el("small", "", track.artist));
    let end;
    if (track.held) {
      end = el("span", "lib-pill tone-ok", "In library");
      if (track.held_as && track.held_as !== track.title) end.title = `As “${track.held_as}”`;
    } else if (track.elsewhere) {
      // Filed as its own single, or on a compilation. Downloading would
      // make a second copy; merging brings the copy here instead.
      const on = track.elsewhere_on || [];
      if (on.length) {
        name.append(el("small", "", `On ${on.map((o) => o.album.album).join(", ")}`));
        end = actionButton("Merge…", "ghost miss-get", () => mergeInto(album, on));
        end.title = "Open the combine dialog with this album and that copy. "
          + "Nothing changes until you press Combine.";
      } else {
        // The only copy is in this album, under a title the comparison
        // did not take for this track.
        end = el("span", "lib-pill", "In library");
        end.title = "This album has a copy under a slightly different title.";
      }
    } else if (!track.downloadable) {
      end = el("span", "lib-pill", "Untitled");
      end.title = "MusicBrainz has no title for this track, so there is nothing to search for.";
    } else {
      end = actionButton("Download", "ghost miss-get", (b) => queueMissing(album, data, [track], [b]));
      gets.push(end);
    }
    row.append(el("span", "miss-no", number), name, end);
    return row;
  });
  const list = el("div", "miss-list");
  list.append(...rows);
  parts.push(list);

  if (gets.length > 1) {
    parts.push(actionButton(`Download all ${gets.length} missing`, "miss-all",
      (b) => queueMissing(album, data,
        data.tracks.filter((t) => !t.held && !t.elsewhere && t.downloadable), [b, ...gets])));
  }
  // Every copy filed elsewhere, merged in one combine. Keyed by folder, so
  // an album holding several of these songs comes once with all of them.
  const elsewhere = new Map();
  for (const track of data.tracks) {
    for (const { album: other, tracks } of track.elsewhere_on || []) {
      const seen = elsewhere.get(other.folder) || { album: other, tracks: [] };
      for (const t of tracks) {
        if (!seen.tracks.some((s) => s.path === t.path)) seen.tracks.push(t);
      }
      elsewhere.set(other.folder, seen);
    }
  }
  const merges = data.tracks.filter((t) => (t.elsewhere_on || []).length).length;
  if (merges > 1) {
    parts.push(actionButton(`Merge all ${merges} into this album…`, "ghost miss-all",
      () => mergeInto(album, [...elsewhere.values()])));
  }
  for (const problem of data.problems || []) parts.push(el("p", "miss-note", problem));
  box.replaceChildren(...parts);
}

async function queueMissing(album, data, tracks, buttons) {
  buttons.forEach((b) => { b.disabled = true; });
  try {
    await postJSON("/api/library/album/missing/download", {
      library_id: album.library_id, folder: album.folder, total: data.tracks.length,
      tracks: tracks.map((t) => ({
        title: t.title, artist: t.artist || data.artist, disc: t.disc, number: t.number,
        length_ms: t.length_ms || null, spotify_id: t.spotify_id || null,
      })),
    });
  } catch (err) {
    buttons.forEach((b) => { b.disabled = false; });
    setNote("library-op", `Could not queue: ${err.message}`, "warn");
    return;
  }
  buttons.forEach((b) => { b.textContent = "Queued"; });
  setNote("library-op", `Queued ${plural(tracks.length, "track")}. They join this album as each `
    + "finishes; the Download tab's Queue shows how they are getting on.", "notice");
}

export function closeDrawer() {
  if (!openAlbum) return;
  openAlbum = null;
  statusHome.after(libraryStatus);
  closeCandidates();
  libraryDrawer.hidden = true;
  libraryDrawer.replaceChildren();
  libraryView.classList.remove("drawer-open");
}

async function loadTracks(album) {
  const asked = album;
  try {
    const data = await getJSON(
      `/api/library/album?library_id=${album.library_id}`
      + `&folder=${encodeURIComponent(album.folder)}`);
    if (openAlbum !== asked) return;
    drawerTracks = data.items;
    // Opened from a song in search results, which knows less than a listing
    // row: fill in what the panel's header can now say - and the album's
    // own names, since the song only knew its track artist.
    if (album.tracks === undefined) album.tracks = data.items.length;
    if (album.stub) {
      album.artist = data.artist || album.artist;
      album.album = data.album || album.album;
    }
    renderTracks();
  } catch (err) {
    if (openAlbum !== asked) return;
    drawerTracksEl.replaceChildren(el("p", "empty", err.message));
  }
}

export function renderTracks() {
  const album = openAlbum;
  if (!album || !drawerTracksEl) return;
  const allPicked = isPicked(album);
  drawerTracksHead.replaceChildren(el("h3", "lib-section-title", "Tracks"));
  if (selection.on && !allPicked) {
    drawerTracksHead.append(actionButton("Select all", "ghost", () => {
      for (const track of drawerTracks) selection.tracks.set(track.path, { track, album });
      renderTracks();
      renderBar();
    }));
  }
  const nodes = [];
  for (const track of drawerTracks) {
    const { row, more } = trackRow(album, track, allPicked);
    nodes.push(row, more);
  }
  drawerTracksEl.replaceChildren(...nodes);
}

// The rarer edits for one track: its disc number, and moving it out of this
// album into another entirely, which is how a misfiled track is rescued and
// how one gets split off by mistake. Kept behind "More" so it cannot be
// triggered by the same accidental tap that would edit the title.
function trackMorePanel(album, track, titleInput, artistInput, gone) {
  const form = el("div", "track-edit");
  const disc = editField("Disc", track.disc_no || "", { number: true });
  const discSave = el("button", "ghost primary", "Save disc");

  discSave.addEventListener("click", async () => {
    const d = parseInt(disc.input.value, 10);
    if (Number.isNaN(d) || d === (track.disc_no || 0)) return;
    await saveEdit("/api/library/track/edit",
      { library_id: album.library_id, path: track.path, disc_no: d },
      discSave, () => {
        track.disc_no = d;
        setNote("library-op", "Saved.", "notice");
      });
  });

  const moveArtist = editField("Album artist", album.artist, { wide: true });
  const moveAlbum = editField("Album", album.album, { wide: true });
  const single = el("button", "ghost", "As its own single");
  const moveSave = el("button", "ghost primary", "Move this track");

  // A single is an album of one, which is how Spotify presents it and how
  // the filer files it. Reads the title and artist fields on the row itself,
  // in case they have not been saved yet.
  single.addEventListener("click", () => {
    moveArtist.input.value = artistInput.value.trim()
      || track.artist || album.artist;
    moveAlbum.input.value = titleInput.value.trim() || track.title;
  });

  moveSave.addEventListener("click", async () => {
    const wantArtist = moveArtist.input.value.trim();
    const wantAlbum = moveAlbum.input.value.trim();
    if (!wantArtist || !wantAlbum) {
      setNote("library-op", "An artist and an album cannot be blank.", "warn");
      return;
    }
    if (!confirm(
      `Move "${track.title}" to "${wantArtist} — ${wantAlbum}"?\n\n`
      + "Only this track moves. It leaves this album and joins that one, "
      + "taking its own stars and play count with it.\n"
      + "Everything else in this album stays where it is.")) return;

    await saveEdit("/api/library/track/edit", {
      library_id: album.library_id, path: track.path,
      album_artist: wantArtist, album: wantAlbum,
    }, moveSave, () => {
      setNote("library-op", `"${track.title}" moved.`, "notice");
      gone();
    });
  });

  const moveWrap = el("div", "track-move");
  moveWrap.append(moveArtist, moveAlbum, single, moveSave);

  const quarantine = el("button", "ghost danger", "Quarantine this track");
  quarantine.addEventListener("click", () => quarantineTrack(album, track, quarantine, gone));

  form.append(disc, discSave, moveWrap, quarantine);
  return form;
}

// One track, edited in its row: number, title and artist save as soon as
// they lose focus with a changed value. Disc number and moving to another
// album are rarer, so they stay one tap away behind "More".
function trackRow(album, track, allPicked) {
  const row = el("div", `album-track${track.tagged ? "" : " unmatched"}`);

  const tick = el("input", "lib-track-tick");
  tick.type = "checkbox";
  tick.setAttribute("aria-label", `Select ${track.title}`);
  tick.checked = allPicked || selection.tracks.has(track.path);
  tick.disabled = allPicked;
  tick.addEventListener("change", () => {
    if (tick.checked) selection.tracks.set(track.path, { track, album });
    else selection.tracks.delete(track.path);
    row.classList.toggle("picked", tick.checked);
    renderBar();
  });
  row.classList.toggle("picked", tick.checked);

  const no = el("input", "track-no-input");
  no.type = "number";
  no.min = "0";
  no.value = track.track_no || "";
  no.setAttribute("aria-label", "Track number");

  const title = el("input", "track-title-input");
  title.type = "text";
  title.value = track.title || "";
  title.setAttribute("aria-label", "Title");

  const artist = el("input", "track-artist-input");
  artist.type = "text";
  artist.value = track.artist || "";
  artist.setAttribute("aria-label", "Artist");

  // The list and Needs attention are re-read behind the panel; the panel
  // keeps showing what was typed, since Navidrome has not rescanned yet and
  // reading it back would show the value from before the save.
  // One save at a time, in the order they were made. Each saves on leaving
  // the field, and the server holds the album's folder while it writes, so
  // tabbing from one field to the next while the first was still saving had
  // the second refused (409) - and that refusal left the unsaved text in
  // the field, looking saved.
  function saveField(input, key, value, previous) {
    trackEdits = trackEdits.then(() => saveFieldNow(input, key, value, previous));
    return trackEdits;
  }

  async function saveFieldNow(input, key, value, previous) {
    if (value === previous) return;
    // Setting the artist of a track with no album artist changes which
    // album the file is on, so the server may move it. Say so first. Asked
    // of the track: the album's artist falls back to the track artist, so
    // it was almost never empty and the file moved unannounced.
    if (key === "artist" && track.has_albumartist === false) {
      if (!confirm(
        `Set this track's artist to "${value}"?\n\n`
        + "It has no album artist, so this also decides which folder it "
        + "lives in and the file will move.")) {
        input.value = previous;
        return;
      }
    }
    const saved = await saveEdit("/api/library/track/edit",
      { library_id: album.library_id, path: track.path, [key]: value },
      input, (data) => {
        track[key] = value;
        const was = track.path;
        if (data.path) track.path = data.path;
        // A ticked track is keyed by its path, which a title edit can
        // rename: it showed unticked while still counted, and a move left
        // it selected under the album it had left.
        const picked = selection.tracks.get(was);
        if (picked) {
          selection.tracks.delete(was);
          if (!data.moved) selection.tracks.set(track.path, picked);
          renderBar();
        }
        if (data.moved) {
          // It is in another folder now; listing it here would offer edits
          // against an album it has left.
          drawerTracks = drawerTracks.filter((t) => t !== track);
          album.tracks = drawerTracks.length;
          renderTracks();
          setNote("library-op", `Saved, and moved to ${data.path}.`, "notice");
        } else {
          setNote("library-op", "Saved.", "notice");
        }
        refreshLibrary(album);
      });
    if (!saved) {
      // Back to what is really on the file; the warning says why.
      input.value = previous || "";
    }
  }

  no.addEventListener("change", () => {
    const n = parseInt(no.value, 10);
    if (Number.isNaN(n)) { no.value = track.track_no || ""; return; }
    saveField(no, "track_no", n, track.track_no || 0);
  });
  title.addEventListener("change", () => {
    saveField(title, "title", title.value.trim(), track.title || "");
  });
  artist.addEventListener("change", () => {
    saveField(artist, "artist", artist.value.trim(), track.artist || "");
  });

  // Moved or set aside: it is not on this album any more.
  const gone = () => {
    drawerTracks = drawerTracks.filter((t) => t !== track);
    selection.tracks.delete(track.path);
    renderTracks();
    renderBar();
    refreshLibrary(album);
  };

  const more = el("button", "ghost", "More…");
  const moreWrap = el("div", "track-editor");
  moreWrap.hidden = true;
  more.addEventListener("click", () => {
    if (!moreWrap.childElementCount) {
      moreWrap.append(trackMorePanel(album, track, title, artist, gone));
    }
    moreWrap.hidden = !moreWrap.hidden;
    more.textContent = moreWrap.hidden ? "More…" : "Less";
  });

  row.append(tick, no, title, artist, more);
  return { row, more: moreWrap };
}

/* --- editing ------------------------------------------------------------- */

// Take an album that has moved or gone out of the selection, with its
// tracks; `renamed`, when given, says what it is now and keeps it selected.
function forgetPicks(album, renamed = null) {
  const key = albumKey(album);
  const wasPicked = selection.albums.delete(key);
  for (const [path, picked] of [...selection.tracks]) {
    if (albumKey(picked.album) === key) selection.tracks.delete(path);
  }
  if (wasPicked && renamed) {
    const now = renamed();
    selection.albums.set(albumKey(now), now);
  }
  renderBar();
}

// The chain of inline track saves; see saveField.
let trackEdits = Promise.resolve();

async function saveEdit(path, body, button, done) {
  button.disabled = true;
  const was = button.textContent;
  if (button.tagName === "BUTTON") button.textContent = "Saving…";
  setNote("library-op", "");
  try {
    done(await postJSON(path, body));
    return true;
  } catch (err) {
    setNote("library-op", err.message, "warn");
    return false;
  } finally {
    button.disabled = false;
    if (button.tagName === "BUTTON") button.textContent = was;
  }
}

// Renaming an album is one action over every file in it, because one album
// is one UUID - the artist and the title are properties of the folder, and
// editing them on a single track is how a record becomes two.
function albumEditor(album) {
  const form = el("div", "album-edit");
  const artist = editField("Album artist", album.artist, { wide: true });
  const name = editField("Album", album.album, { wide: true });
  const save = el("button", "ghost primary", "Save album");

  save.addEventListener("click", async () => {
    const wantArtist = artist.input.value.trim();
    const wantAlbum = name.input.value.trim();
    if (!wantArtist || !wantAlbum) {
      setNote("library-op", "An artist and an album cannot be blank.", "warn");
      return;
    }
    if (wantArtist === album.artist && wantAlbum === album.album) return;
    if (!confirm(
      `Rename this album to "${wantArtist} — ${wantAlbum}"?\n\n`
      + `All ${plural(album.tracks, "track")} are retagged and the files move `
      + "to match.\nThe album keeps its identity, so stars and play counts "
      + "survive — unless an album of that name already exists, in which "
      + "case these join it.")) return;

    await saveEdit("/api/library/album/edit", {
      library_id: album.library_id, folder: album.folder,
      album_artist: wantArtist, album: wantAlbum,
    }, save, (data) => {
      const failed = data.failed || [];
      setNote("library-op",
              `Renamed, and ${plural(data.moved, "file")} moved to match.`
              + (failed.length ? ` Problems: ${failed.join("; ")}` : ""),
              failed.length ? "warn" : "notice");
      // Still selected under its old folder, a later Combine was refused
      // for an album that was not there. Kept selected, under where it is.
      forgetPicks(album, () => {
        album.folder = data.folder || album.folder;
        album.artist = wantArtist;
        album.album = wantAlbum;
        return album;
      });
      closeDrawer();
      refreshLibrary(album);
    });
  });

  // Retagging to an album name that already exists is what merges into it,
  // but that meant retyping an existing artist and title exactly right - one
  // typo made a new album instead of joining the one you meant. This searches
  // the album's own library - a merge happens inside one; picking another
  // library's album only made a new album here with its name - and fills
  // the two fields from a real match.
  const mergeLabel = el("span", "edit-label", "Merge into an existing album");
  const mergeInput = el("input", "album-merge-input");
  mergeInput.type = "search";
  mergeInput.autocomplete = "off";
  mergeInput.placeholder = "Search artist or album to merge into";
  const mergeResults = el("div", "album-merge-results");
  mergeResults.hidden = true;

  // Only the newest search's answer is drawn; see loadAlbums.
  let mergeRequest = 0;
  async function searchMergeTargets(query) {
    const mine = ++mergeRequest;
    let data;
    try {
      data = await getJSON(`/api/library?q=${encodeURIComponent(query)}`
        + `&library_id=${album.library_id}&limit=8`);
    } catch (err) {
      if (mine !== mergeRequest) return;
      mergeResults.replaceChildren(
        el("p", "album-merge-empty", `Could not search: ${err.message}`));
      mergeResults.hidden = false;
      return;
    }
    if (mine !== mergeRequest) return;
    const matches = (data.albums || []).filter((a) => !(
      a.library_id === album.library_id && a.folder === album.folder));
    if (!matches.length) {
      mergeResults.replaceChildren(
        el("p", "album-merge-empty", "No other album matches."));
      mergeResults.hidden = false;
      return;
    }
    mergeResults.replaceChildren(...matches.map((a) => {
      const row = el("button", "album-merge-result");
      row.type = "button";
      row.append(
        el("span", "album-merge-name", albumName(a)),
        el("span", "album-merge-count", plural(a.tracks, "track")));
      row.addEventListener("click", () => {
        artist.input.value = a.artist;
        name.input.value = a.album;
        mergeInput.value = "";
        mergeResults.hidden = true;
        mergeResults.replaceChildren();
      });
      return row;
    }));
    mergeResults.hidden = false;
  }

  // Debounced: one request per pause, not one per keystroke.
  let mergeTimer = null;
  mergeInput.addEventListener("input", () => {
    clearTimeout(mergeTimer);
    const query = mergeInput.value.trim();
    if (query.length < 2) {
      mergeRequest += 1;  // so a search still in flight does not reopen it
      mergeResults.hidden = true;
      mergeResults.replaceChildren();
      return;
    }
    mergeTimer = setTimeout(() => searchMergeTargets(query), 250);
  });

  const merge = el("div", "album-merge");
  merge.append(mergeLabel, mergeInput, mergeResults);

  form.append(artist, name, save, merge);
  return form;
}

/* --- the cover, and nothing else -------------------------------------------
   "Find matches" brings art with it, but it also rewrites every tag and can
   move the files. Most of the time only the picture is wrong - a YouTube
   download arrives with the video frame, bars and all - so this offers
   covers and changes only that. */

async function applyCover(album, candidate, button, picker) {
  button.disabled = true;
  try {
    const data = await postJSON("/api/library/cover/apply", {
      library_id: album.library_id, folder: album.folder, url: candidate.url,
    });
    const failed = data.failed || [];
    setNote("library-op",
      data.already_square ? "That cover is already square; nothing changed."
        : `New cover on ${plural(data.written, "track")}.`
          + (failed.length ? ` Could not change: ${failed.join("; ")}` : ""),
      failed.length ? "warn" : "notice");
    picker.hidden = true;
    picker.replaceChildren();
    libraryState.barredKeys.delete(albumKey(album));
    album.barred = false;
    if (data.already_square) return;

    // Shown straight away from the choice itself, since Navidrome serves the
    // old picture until it has rescanned - then read back for real.
    const key = albumKey(album);
    libraryView.querySelectorAll(`[data-key="${CSS.escape(key)}"] .art img, .lib-hero .art img`)
      .forEach((img) => {
        if (img.closest(".lib-hero") && openAlbum && albumKey(openAlbum) !== key) return;
        img.src = candidate.preview;
      });
    forgetCoverSurvey();
    setTimeout(() => {
      artStamps.set(key, Date.now());
      refreshLibrary(album);
    }, RESCAN_WAIT_MS);
  } catch (err) {
    setNote("library-op", err.message, "warn");
    button.disabled = false;
  }
}

async function showCovers(album, button, picker) {
  if (!picker.hidden) {
    picker.hidden = true;
    picker.replaceChildren();
    return;
  }
  button.disabled = true;
  picker.replaceChildren(el("p", "cover-empty", "Looking for covers…"));
  picker.hidden = false;
  let data;
  try {
    data = await postJSON("/api/library/cover/candidates",
                          { library_id: album.library_id, folder: album.folder });
  } catch (err) {
    picker.replaceChildren(el("p", "cover-empty", `Could not look: ${err.message}`));
    return;
  } finally {
    button.disabled = false;
  }

  if (!data.candidates.length) {
    picker.replaceChildren(el("p", "cover-empty",
      "No covers found. The current one is already square, and nothing "
      + "else matched this album's name."));
    return;
  }
  picker.replaceChildren(...data.candidates.map((candidate) => {
    const choice = el("button", "cover-choice");
    choice.type = "button";
    choice.title = `Use this cover for ${albumName(album)}`;
    const img = el("img");
    img.alt = "";
    img.loading = "lazy";
    img.src = candidate.preview;
    // A Cover Art Archive link is offered without asking whether the
    // release has a front; one that does not just drops out of the list.
    img.addEventListener("error", () => choice.remove());
    choice.append(img, el("span", "cover-label", candidate.label),
                  el("span", "cover-detail", candidate.detail || ""));
    choice.addEventListener("click", () => applyCover(album, candidate, choice, picker));
    return choice;
  }));
}

/* --- choosing a match by hand ---------------------------------------------
   Beets refuses whenever it cannot tell two releases apart, which for a
   popular record means five near-identical pressings and no winner. It knows
   perfectly well what the candidates are; `quiet_fallback: skip` throws the
   list away. This asks for the list back and lets a person point at one. */

const candidatesEl = document.getElementById("candidates");
// What the pending lookup was for. The answer arrives over the websocket,
// by which time nothing in the message says which album asked.
let candidatesFor = null;

function closeCandidates() {
  candidatesFor = null;
  candidatesEl.replaceChildren();
  candidatesEl.hidden = true;
}

function candidateRow(album, candidate) {
  const row = el("div", "candidate");
  const title = candidate.title || "(untitled)";
  const detail = [
    candidate.artist,
    candidate.year || null,
    candidate.tracks ? `${candidate.tracks} tracks` : null,
    candidate.album || null,
  ].filter(Boolean).join(" · ");

  const use = el("button", "ghost primary", "Use this");
  use.addEventListener("click", () => useCandidate(album, candidate, use));

  row.append(
    el("div", "candidate-title", title),
    el("div", "candidate-detail", detail),
    // Beets' own number. Lower is closer; it is shown because the gap
    // between the first and second is usually the whole story.
    el("div", "candidate-distance", candidate.distance.toFixed(2)),
    use
  );
  if (candidate.penalties && candidate.penalties.length) {
    row.append(el("div", "candidate-why",
                  `held against it: ${candidate.penalties.join(", ")}`));
  }
  return row;
}

// Whether an answer (or the operation in flight) is about this album. Your
// lookups run one at a time, so the answer arriving can be an earlier
// album's, from this tab or another.
function sameAlbum(target, album) {
  return Boolean(target && album)
    && target.library_id === album.library_id && target.folder === album.folder;
}

function showCandidates(result) {
  const album = candidatesFor;
  if (!album || !sameAlbum(result, album)) return;
  const head = el("div", "candidates-head");
  head.append(el("span", "candidates-title", `Matches for ${albumName(album)}`));
  const close = el("button", "ghost", "Close");
  close.addEventListener("click", closeCandidates);
  head.append(close);

  const nodes = [head];
  if (result.error) {
    nodes.push(el("p", "candidates-empty", result.error));
  } else if (!result.candidates.length) {
    nodes.push(el("p", "candidates-empty",
      "MusicBrainz has nothing close enough to offer. The album stays as it "
      + "is, tagged the way it arrived."));
  } else {
    nodes.push(...result.candidates.map((c) => candidateRow(album, c)));
  }
  candidatesEl.replaceChildren(...nodes);
  candidatesEl.hidden = false;
}

async function askForCandidates(album, button) {
  candidatesFor = album;
  button.disabled = true;
  candidatesEl.replaceChildren(
    el("p", "candidates-empty",
       `Asking MusicBrainz about ${albumName(album)}…`));
  candidatesEl.hidden = false;
  const outcome = await startOperation(
    "candidates", "/api/library/match",
    { library_id: album.library_id, folder: album.folder });
  button.disabled = false;
  if (outcome.refused) {
    closeCandidates();
  } else if (!outcome.started && !sameAlbum(outcome.operation.target, album)) {
    closeCandidates();
    setNote("library-op",
      "MusicBrainz is already being asked about another album; "
      + "try again when that finishes.", "warn");
  }
}

async function useCandidate(album, candidate, button) {
  if (!confirm(
    `Tag "${albumName(album)}" as "${candidate.title}"`
    + `${candidate.artist ? ` by ${candidate.artist}` : ""}?\n\n`
    + "Every track in the folder is retagged. If the artist or album "
    + "changes they move to match, and the album keeps its identity — "
    + "stars and play counts survive.")) {
    return;
  }
  button.disabled = true;
  const outcome = await startOperation(
    "import", "/api/library/match/apply",
    { library_id: album.library_id, folder: album.folder,
      release_id: candidate.id });
  button.disabled = false;
  if (!outcome.started) {
    // Said here, where the person is looking: the panel's status line is
    // behind the drawer. The drawer used to close first, so a retag that
    // was refused, or never started because another was running, looked
    // exactly like one that had begun.
    const why = outcome.refused
      || "Another retag is still running, so this one was not started. "
        + "Try again when it finishes.";
    candidatesEl.prepend(el("p", "candidates-empty warn", why));
    return;
  }
  closeCandidates();
  closeDrawer();
}

/* --- quarantine ---------------------------------------------------------- */

// Reports a quarantine outcome the same way whether it moved one track or a
// whole album, so the two callers below cannot drift in wording.
function reportQuarantine(data) {
  const moved = data.quarantined || [];
  const failed = data.failed || [];
  const parts = [];
  if (moved.length) {
    parts.push(`Moved ${plural(moved.length, "track")} to the Quarantine page.`);
  }
  if (failed.length) parts.push(`Could not move: ${failed.join("; ")}`);
  setNote("library-op", parts.join(" ") || "Nothing was moved.",
         failed.length ? "warn" : "notice");
}

// Set aside every file in an album folder by hand - the wrong record
// entirely, not a worse copy of a right one.
async function quarantineAlbum(album, button) {
  if (album.tracks === undefined) {
    // The same song-search stub Edit details waits for: with no track count
    // yet, the question below threw before it was asked, and nothing at all
    // happened.
    setNote("library-op", "Still reading this album; try again in a moment.",
            "warn");
    return;
  }
  if (!confirm(
    `Quarantine ${plural(album.tracks, "track")}?\n\n${albumName(album)}\n\n`
    + "They leave the library and wait on the Quarantine page, where they "
    + "can be restored, stars and plays included, or deleted for good.")) return;

  button.disabled = true;
  try {
    const data = await postJSON("/api/library/quarantine", {
      library_id: album.library_id, folder: album.folder,
      album: album.album, artist: album.artist,
    });
    reportQuarantine(data);
    // Set aside, so no longer anything to combine.
    forgetPicks(album);
    closeDrawer();
    refreshLibrary();
  } catch (err) {
    setNote("library-op", err.message, "warn");
    button.disabled = false;
  }
}

// Set aside one track by hand, leaving the rest of the album alone.
async function quarantineTrack(album, track, button, gone) {
  if (!confirm(
    `Quarantine "${track.title}"?\n\n${track.path}\n\n`
    + "It leaves the library and waits on the Quarantine page, where it "
    + "can be restored, stars and plays included, or deleted for good.")) return;

  button.disabled = true;
  try {
    const data = await postJSON("/api/library/track/quarantine", {
      library_id: album.library_id, folder: album.folder,
      track_id: track.id, album: album.album, artist: album.artist,
    });
    reportQuarantine(data);
    gone();
  } catch (err) {
    setNote("library-op", err.message, "warn");
    button.disabled = false;
  }
}

function importSummary(result) {
  const failed = result.failed || [];
  if (failed.length) {
    return [(result.imported ? "Retagged, with problems: " : "Retagging problems: ")
            + failed.join("; "), "warn"];
  }
  if (result.imported) {
    return ["Retagged. Its tracks keep their stars and plays; if that "
            + "release was already in the library, the album joined it.",
            "notice"];
  }
  // Nothing changed and nothing failed: beets ran and refused. Said plainly,
  // because the album looking untouched is exactly how this used to hide.
  if (result.skipped) {
    return ["That release did not tag it; the album is unchanged.", "warn"];
  }
  return ["", "notice"];
}

// The album's own buttons take the same folder lock, so they cannot be
// live while a retag is in flight.
registerOperation("import", {
  note: "library-op",
  onButtonState(running) {
    libraryDrawer.querySelectorAll(".lib-actions button").forEach((b) => { b.disabled = running; });
  },
  onResult(result) {
    if (!result.ran) {
      setNote("library-op", `Nothing to retag: ${result.reason}`, "warn");
    } else {
      const [message, tone] = importSummary(result);
      setNote("library-op", message, tone);
    }
    refreshLibrary();
  },
});

// No button of its own: it is started from an album, and its result is a
// list rather than a message.
registerOperation("candidates", {
  note: "library-op",
  onFailed() {
    closeCandidates();
  },
  onResult(result) {
    showCandidates(result);
  },
});

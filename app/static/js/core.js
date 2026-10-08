"use strict";

const errorEl = document.getElementById("error");
export const warnEl = document.getElementById("warn");

export function duration(ms) {
  if (!ms) return "";
  const total = Math.round(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

// Every banner in the application, raised the same way and closable. They
// used to be plain text set on a paragraph, which meant one could only be
// got rid of by doing something else that happened to overwrite it - so a
// message about a thing you had already dealt with sat there indefinitely.
//
// `tone` is the class: "error", "warn" or "notice". Passing an empty message
// clears the banner, which is how most callers reset one.
export function setBanner(node, message, tone) {
  if (!node) return;
  if (!message) {
    node.replaceChildren();
    node.hidden = true;
    return;
  }
  node.className = tone || node.dataset.tone || "warn";
  const close = el("button", "banner-close", "×");
  close.type = "button";
  close.title = "Dismiss";
  close.setAttribute("aria-label", "Dismiss");
  close.addEventListener("click", () => setBanner(node, ""));
  node.replaceChildren(el("span", "banner-text", message), close);
  node.hidden = false;
}

export function showError(message) {
  setBanner(errorEl, message, "error");
}

// Called when the server says the session has gone. Set by main.js, which
// owns the sign-in form; nothing else needs to know how signing out works.
let signedOut = null;

export function whenSignedOut(callback) {
  signedOut = callback;
}

// Every API call goes through here, so a 401 from any of them shows the
// sign-in form. Only the socket's close code used to: a session that ended
// while the socket stayed open - the 30-day cap, a removed account, a
// sign-out in another tab - left every panel saying "Please sign in."
// with no way to, until a reload.
export async function apiFetch(path, options) {
  const response = await fetch(path, options);
  if (response.status === 401 && signedOut) signedOut();
  return response;
}

// Whether Escape is the page's to act on. Not when a text box has it - in a
// search field it clears the text, and the page also took it as "back out",
// so Escape in the search box wiped a selection built across several
// searches, and in the editor's "Merge into" box closed the unsaved editor.
// Not when something above has already used it either: the menu, when open.
const TYPING = "input:not([type=checkbox]):not([type=radio]):not([type=button])"
  + ":not([type=submit]):not([type=range]), textarea, select";

export function pageEscape(event) {
  if (event.key !== "Escape" || event.defaultPrevented) return false;
  const target = event.target;
  return !(target instanceof HTMLElement
           && (target.isContentEditable || target.matches(TYPING)));
}

// A request whose answer is used: a 4xx or 5xx is thrown, with the
// server's own reason, rather than read as an empty answer.
export async function getJSON(path, options) {
  const response = await apiFetch(path, options);
  const data = await response.json().catch(() => ({}));
  // fetch does not throw on 4xx or 5xx, and an error body is a {detail}
  // with nothing else - which used to fall through to "your library is
  // empty", the most alarming possible way to be wrong.
  if (!response.ok || data.available === false) {
    throw new Error(data.reason || data.detail
                    || `the server answered ${response.status}`);
  }
  return data;
}

export function postJSON(path, body) {
  return getJSON(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// A cover from a URL, for art that is not in the library yet - Spotify's,
// on a search result or a download still in flight. Lazy, and removed if it
// fails rather than leaving a broken-image glyph on an empty square.
export function remoteArt(url, className = "") {
  const box = el("div", `art ${className}`.trim());
  if (!url) return box;
  const img = el("img");
  img.loading = "lazy";
  img.decoding = "async";
  img.alt = "";
  img.src = url;
  img.addEventListener("error", () => img.remove());
  box.append(img);
  return box;
}

// One song as a row: its cover, its title, and a muted line under it. The
// Library's search and Browse both list songs, and two builders for the same
// row would drift apart the way the panels' colours once did. `sub` is text
// or nodes - Browse puts a link to the album in it. Anything a page needs
// beside that (a status, a button) goes in `end`, at the row's far side.
export function songRow(tag, art, title, sub, end = []) {
  const row = el(tag, "lib-song");
  const subEl = el("span", "lib-card-sub");
  subEl.append(...[].concat(sub));
  row.append(art, el("span", "lib-song-title", title), subEl);
  if (end.length) {
    const tail = el("span", "lib-song-end");
    tail.append(...end);
    row.append(tail);
  }
  return row;
}

async function call(path) {
  showError("");
  try {
    const response = await apiFetch(path, { method: "POST" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showError(body.detail || `Request failed (${response.status})`);
    }
  } catch {
    showError("Could not reach the server.");
  }
}

export function action(label, className, handler) {
  const button = el("button", `ghost ${className}`, label);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    handler();
  });
  return button;
}

// An operation's outcome belongs to the panel that started it. The banner at
// the top of the page is outside every section, so anything left there
// followed you onto Queue, Browse, Playlists and Settings and stayed until
// something else happened to clear it - which for an import that reported
// "already running" was easily never.
export function setNote(id, message, tone) {
  setBanner(document.getElementById(id), message, tone);
}

/* --- which library new music goes into ---------------------------------------
   Only a question for an account with more than one library; everybody else
   never sees it, and the server uses their one library. Browse and Drop share
   the answer, and this browser remembers it. Without it, a second library
   could never receive a download or a drop. */

let libraries = [];
let chosenLibrary = null;
const libraryPickers = [];

function paintPicker(select) {
  select.closest(".target-library").hidden = libraries.length < 2;
  select.replaceChildren(...libraries.map((library) => {
    const option = el("option", "", library.name);
    option.value = String(library.id);
    return option;
  }));
  if (chosenLibrary !== null) select.value = String(chosenLibrary);
}

export function setLibraries(list) {
  libraries = list || [];
  let saved = null;
  try { saved = Number(localStorage.getItem("target.library")); } catch { /* fine */ }
  chosenLibrary = libraries.some((l) => l.id === saved) ? saved
    : libraries.length ? libraries[0].id : null;
  libraryPickers.forEach(paintPicker);
}

export function libraryPicker(select) {
  libraryPickers.push(select);
  select.addEventListener("change", () => {
    chosenLibrary = Number(select.value);
    try { localStorage.setItem("target.library", select.value); } catch { /* fine */ }
    libraryPickers.forEach(paintPicker);
  });
  paintPicker(select);
}

// The library to send with a download or an upload, or null for "my only one".
export function targetLibrary() {
  return libraries.length > 1 ? chosenLibrary : null;
}

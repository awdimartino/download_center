"use strict";

// The entry point - the only module index.html references. Everything else
// is reached through imports, which is also what wires the view registry
// (nav.js) and settles module evaluation order before anything runs.

import { setBanner, setLibraries, showError, warnEl } from "./core.js";
import { connect, disconnect } from "./ws.js";
import { viewHandlers, closeMenu } from "./nav.js";
import { catchUpOperations } from "./operations.js";
import { loadHome } from "./home.js";
import { loadListening } from "./listening.js";
import { loadLibrary } from "./library.js";
import { loadHealth } from "./health.js";
import { loadDupes } from "./duplicates.js";
import { loadPlaylists } from "./playlists.js";
import { loadSettings, checkSpotify } from "./settings.js";
import { focusSearchIfPointer } from "./browse.js";
// No bindings needed from this one - it wires its own DOM listeners as a
// side effect of being imported, the same as every other panel module.
import "./drop.js";

// --- sign in ----------------------------------------------------------------
// Navidrome owns the accounts, so this only forwards credentials to it and
// keeps the session cookie it hands back. Which library a download lands in,
// whose stars a duplicate carries and who owns a new playlist all follow from
// who signed in, rather than from configuration.

const signinEl = document.getElementById("signin");
const signinForm = document.getElementById("signin-form");
const signinError = document.getElementById("signin-error");
const whoamiEl = document.getElementById("whoami");

let session = null;

function showSignin(show) {
  // A restart signs everyone out. Leaving the menu up over the sign-in form
  // would hide the only thing there is to do.
  if (show) closeMenu();
  signinEl.hidden = !show;
  document.querySelector("header").hidden = show;
  document.querySelector("main").hidden = show;
  if (show) signinForm.elements.username.focus();
}

function applySession(me) {
  session = me;
  setLibraries(me.libraries);
  const libraries = (me.libraries || []).map((l) => l.name).join(", ");
  whoamiEl.textContent = libraries
    ? `${me.username} · ${libraries}`
    : `${me.username} · no library assigned`;
  showSignin(false);
}

async function checkSession() {
  try {
    const me = await fetch("/api/auth/me").then((r) => r.json());
    if (me.signed_in) {
      applySession(me);
      return true;
    }
  } catch {
    /* server unreachable; the sign-in form is still the right thing to show */
  }
  showSignin(true);
  return false;
}

signinForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setBanner(signinError, "");
  const button = signinForm.querySelector("button");
  button.disabled = true;
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: signinForm.elements.username.value,
        password: signinForm.elements.password.value,
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      setBanner(signinError,
                body.detail || `Sign in failed (${response.status})`, "error");
      return;
    }
    signinForm.reset();
    applySession({ signed_in: true, ...body });
    start();
  } finally {
    button.disabled = false;
  }
});

document.getElementById("signout").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
  disconnect();
  location.reload();
});

// --- the view registry --------------------------------------------------
// showView (nav.js) calls viewHandlers[view]() and nothing else - it does
// not import any panel module itself, so this is the one place that wires
// "showing this view" to "load this panel's data".

Object.assign(viewHandlers, {
  home: () => { loadHome(); loadListening(); },
  browse: focusSearchIfPointer,
  library: loadLibrary,
  health: loadHealth,
  dupes: loadDupes,
  playlists: loadPlaylists,
  settings: loadSettings,
});

// --- bootstrap ------------------------------------------------------------
// Nothing runs until we know who is asking - the socket, the health poll and
// every panel are all views of somebody's library.

let started = false;
let healthTimer = null;

// The four side effects the websocket's 4401 (session gone) close code used
// to perform inline. Owned here rather than in ws.js, which only knows "the
// server says the session is gone" and calls this back.
function handleSessionExpired() {
  started = false;
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = null;
  setBanner(warnEl, "");
  showError("");
  showSignin(true);
}

function start() {
  if (started) return;
  started = true;
  connect(handleSessionExpired, resumeOperations);
  // The panel the page opens on, so it is not blank until somebody
  // navigates away and back. Everything else waits for it: the Pi works
  // through requests one at a time near enough, and the health checks and
  // the library listing sent alongside Home used to be answered first while
  // the page somebody was actually looking at sat empty.
  Promise.allSettled([loadHome(), loadListening()]).then(() => {
    loadHealth();
    loadLibrary();
    checkSpotify();
  });
  healthTimer = setInterval(loadHealth, 5 * 60 * 1000);
}

// On every socket open. A ReplayGain run can outlast the page that started
// it, and an operation can finish while the socket is down; either way the
// page would otherwise show the wrong buttons until something else changed.
function resumeOperations() {
  if (session) catchUpOperations(session.username);
}

checkSession().then((signedIn) => {
  if (signedIn) start();
});

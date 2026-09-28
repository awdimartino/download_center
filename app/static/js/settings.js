"use strict";

import { setBanner, warnEl } from "./core.js";

const settingsForm = document.getElementById("settings");
const settingsNote = document.getElementById("settings-note");

// A view like any other, loaded when it is shown. It used to be a form that
// toggled on top of whichever panel you were looking at, which meant Settings
// appeared above a list of duplicates and left you with no clear way back.
export async function loadSettings() {
  settingsNote.textContent = "";
  const values = await fetch("/api/settings").then((r) => r.json());
  const secrets = ["spotify_client_secret", "navidrome_password", "acoustid_key"];
  // A non-admin is sent nothing but `editable: false` - these settings hold
  // the service credentials and decide where every library lives, so there
  // is nothing here for them to see and something to leak.
  Object.entries(values).forEach(([key, value]) => {
    const field = settingsForm.elements[key];
    if (field && !secrets.includes(key)) field.value = value ?? "";
  });
  // Secrets are never sent back, only whether one is set.
  secrets.forEach((key) => {
    const field = settingsForm.elements[key];
    if (field) field.placeholder = values[`${key}_set`] ? "unchanged" : "not set";
  });
  // These belong to the installation, not to a person. Showing an editable
  // form to someone who will be refused on save is worse than not offering
  // it at all.
  const mayEdit = values.editable !== false;
  Array.from(settingsForm.elements).forEach((field) => {
    field.disabled = !mayEdit;
  });
  settingsNote.textContent = mayEdit
    ? "" : "Only a Navidrome administrator can change these.";
}

settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {};
  new FormData(settingsForm).forEach((value, key) => {
    if (value === "") return;
    payload[key] = ["concurrency", "max_attempts"].includes(key)
      ? parseInt(value, 10)
      : key === "rate_limit_sleep"
      ? parseFloat(value)
      : value;
  });
  const response = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (response.ok) {
    settingsNote.textContent = "Saved.";
    // Every secret field, not just the Spotify one. A value left sitting in
    // the form is submitted again on the next save, and a field the server
    // deliberately never sends back should not keep holding one either.
    ["spotify_client_secret", "navidrome_password", "acoustid_key"].forEach((key) => {
      const field = settingsForm.elements[key];
      if (field) field.value = "";
    });
    loadSettings();
    setBanner(warnEl, "");
  } else {
    const body = await response.json().catch(() => ({}));
    settingsNote.textContent = body.detail || "Could not save.";
  }
});

// Only once there is a session, and only on an answer we actually got. Run
// at load it fired while signed out, read `spotify_configured` off a 401
// body, found it undefined and announced that credentials were missing -
// which reads as the sign-in having failed rather than as a note about
// Spotify.
export async function checkSpotify() {
  try {
    const response = await fetch("/api/status");
    if (!response.ok) return;
    const status = await response.json();
    setBanner(
      warnEl,
      status.spotify_configured
        ? ""
        : "Spotify credentials are not configured. Add them in Settings.",
      "warn");
  } catch {
    /* the queue will report its own errors; a missing banner is not one */
  }
}

"use strict";

import { apiFetch, setBanner, warnEl } from "./core.js";

const settingsForm = document.getElementById("settings");
const settingsNote = document.getElementById("settings-note");

// Text a person may want gone: an empty one is sent as empty and clears
// the setting. A blank secret still means "unchanged", since the form never
// holds one, and a blank number or bitrate is not a value at all - so those
// are left out rather than sent.
const CLEARABLE = ["spotify_client_id", "navidrome_url", "navidrome_user"];
const NUMBERS = { concurrency: parseInt, max_attempts: parseInt,
                  rate_limit_sleep: parseFloat };

// A view like any other, loaded when it is shown. It used to be a form that
// toggled on top of whichever panel you were looking at, which meant Settings
// appeared above a list of duplicates and left you with no clear way back.
export async function loadSettings() {
  settingsNote.textContent = "";
  const values = await apiFetch("/api/settings").then((r) => r.json());
  const secrets = ["spotify_client_secret", "navidrome_password", "acoustid_key"];
  // A non-admin is sent nothing but `editable: false` - these settings hold
  // the service credentials and decide where every library lives, so there
  // is nothing here for them to see and something to leak.
  Object.entries(values).forEach(([key, value]) => {
    const field = settingsForm.elements[key];
    if (!field || secrets.includes(key)) return;
    if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = value ?? "";
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
  // Set by an environment variable: it wins on every restart, so an edit
  // here would silently revert. Shown, locked, with where to change it.
  const locked = values.locked || {};
  Array.from(settingsForm.elements).forEach((field) => {
    const env = locked[field.name];
    field.disabled = !mayEdit || Boolean(env);
    field.title = env ? `Set by ${env} in the container's environment` : "";
  });
  settingsNote.textContent = mayEdit
    ? "" : "Only a Navidrome administrator can change these.";
}

settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {};
  // Disabled fields - set by the environment - are not in FormData, so
  // nothing locked is ever sent.
  new FormData(settingsForm).forEach((value, key) => {
    const field = settingsForm.elements[key];
    if (field.type === "checkbox") return;
    if (value === "" && !CLEARABLE.includes(key)) return;
    payload[key] = NUMBERS[key] ? NUMBERS[key](value, 10) : value;
  });
  // An unticked checkbox is absent from FormData, which would make it
  // impossible to turn off.
  settingsForm.querySelectorAll('input[type="checkbox"]').forEach((box) => {
    if (!box.disabled) payload[box.name] = box.checked;
  });
  const response = await apiFetch("/api/settings", {
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
    const response = await apiFetch("/api/status");
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

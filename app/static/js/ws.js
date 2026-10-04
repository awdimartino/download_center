"use strict";

import { showOperation } from "./operations.js";

const connEl = document.getElementById("conn");

// Job id -> job. The server is authoritative; this is only a cache of what
// it last said. Drawn by downloads.js, and read by browse.js so a search
// result can say it is already downloading without anyone opening the queue.
export const jobs = new Map();

// Everything that wants to know the jobs changed. A list rather than one
// renderer, because two panels draw from the same jobs now: the Downloads
// rail, and the state painted onto each search result.
const listeners = new Set();

export function onJobs(listener) {
  listeners.add(listener);
}

function changed() {
  listeners.forEach((listener) => listener());
}

// A delta from the server: the job's status plus only the items that moved.
function applyProgress(message) {
  const job = jobs.get(message.id);
  if (!job) return;
  job.status = message.status;
  job.error = message.error;
  const byId = new Map(job.items.map((i) => [i.id, i]));
  message.items.forEach((patch) => {
    const item = byId.get(patch.id);
    if (item) Object.assign(item, patch);
  });
  changed();
}

function handleMessage(message) {
  if (message.type === "snapshot") {
    jobs.clear();
    message.jobs.forEach((job) => jobs.set(job.id, job));
  } else if (message.type === "job") {
    jobs.set(message.job.id, message.job);
  } else if (message.type === "job_progress") {
    applyProgress(message);
    return;
  } else if (message.type === "job_deleted") {
    jobs.delete(message.id);
  } else if (message.type === "operation") {
    // Import and audit finish long after their request returned.
    showOperation(message.operation);
    return;
  } else {
    return;
  }
  changed();
}

let socket = null;
let retryDelay = 1000;

// `onSessionExpired` is called instead of this module reaching into
// sign-in/bootstrap state directly - main.js owns started/healthTimer/the
// sign-in form, this module only knows "the server says the session is
// gone".
export function connect(onSessionExpired) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws`);

  socket.addEventListener("open", () => {
    retryDelay = 1000;
    connEl.textContent = "live";
    connEl.className = "conn online";
  });

  socket.addEventListener("message", (event) => {
    handleMessage(JSON.parse(event.data));
  });

  socket.addEventListener("close", (event) => {
    connEl.textContent = "offline";
    connEl.className = "conn offline";
    // 4401 is this server saying the session has gone - a restart signs
    // everyone out. Reconnecting cannot fix that, and doing so forever
    // leaves the page looking merely offline when it needs a sign-in.
    if (event.code === 4401) {
      onSessionExpired();
      return;
    }
    // Back off to at most 15s so a restarting server is picked up quickly
    // without hammering it while it is down.
    setTimeout(() => connect(onSessionExpired), retryDelay);
    retryDelay = Math.min(retryDelay * 2, 15000);
  });
}

// Called on sign-out, so the server sees a clean close rather than the
// connection just dropping when the page reloads.
export function disconnect() {
  if (socket) socket.close();
}

"use strict";

// Long operations - a retag, ReplayGain, a combine, the disk audit - are
// started, not awaited: each can take far longer than a browser will hold a
// request open. The server pushes the outcome over the socket.
//
// Panels register what an operation means to them rather than this module
// knowing about panels: `library.js` and `health.js` each call
// registerOperation() at their own module's top level. That keeps this
// module a leaf - it does not import library.js or health.js - while the
// original single-scope app.js had one hardcoded OPERATION_LABELS table and
// an if/elseif chain that reached directly into both.
import { apiFetch, getJSON, setNote } from "./core.js";

const registry = {};

// Operations this page has shown as running, by name. An outcome is
// announced once, over the socket; one that finished while the socket was
// down was never heard, and the page kept its progress line and its
// disabled buttons until a reload.
const watching = new Set();

export function registerOperation(name, spec) {
  registry[name] = spec;
}

// Always one shape, so callers ask one question each:
//   { started: true,  refused: null, operation }  - it is running now;
//   { started: false, refused: null, operation }  - one was already running,
//                                                    and that one is watched;
//   { started: false, refused: "why", operation: null } - nothing started.
// It used to return the payload, a payload with only `detail`, or null, and
// each caller decoded the three by hand - two of them got it wrong (2M23).
export async function startOperation(name, path, body) {
  const spec = registry[name];
  setNote(spec.note, "");
  let payload;
  try {
    const request = { method: "POST" };
    if (body) {
      request.headers = { "Content-Type": "application/json" };
      request.body = JSON.stringify(body);
    }
    payload = await apiFetch(path, request).then((r) => r.json());
  } catch (err) {
    const refused = `Could not start: ${err.message}`;
    setNote(spec.note, refused, "warn");
    return { started: false, refused, operation: null };
  }
  if (payload.detail || !payload.operation) {
    const refused = payload.detail || "Could not start.";
    setNote(spec.note, refused, "warn");
    return { started: false, refused, operation: null };
  }
  if (!payload.started) {
    setNote(spec.note,
            "That is already running; watching the one in flight.", "warn");
  }
  showOperation(payload.operation);
  return { started: Boolean(payload.started), refused: null,
           operation: payload.operation };
}

// Asked on every socket open: the first shows what is already running (a
// ReplayGain run outlasts the page that started it), and each one after a
// drop delivers the outcomes missed while it was down.
export async function catchUpOperations(owner) {
  let data;
  try {
    data = await getJSON("/api/operations");
  } catch {
    return;  // The socket reports the next change anyway.
  }
  (data.operations || [])
    .filter((op) => op.owner === owner
                    && (op.status === "running" || watching.has(op.name)))
    .forEach(showOperation);
}

export function showOperation(operation) {
  const spec = registry[operation.name];
  if (!spec) return;
  const running = operation.status === "running";
  const button = spec.button ? document.getElementById(spec.button) : null;
  if (button) {
    button.disabled = running;
    button.textContent = running ? spec.busy : spec.idle;
  }
  // Passed the whole operation, not just `running` - replaygain's progress
  // display needs `operation.progress` too, the same way the original
  // showOperation's replaygain branch did.
  spec.onButtonState?.(running, operation);
  if (running) {
    watching.add(operation.name);
    return;
  }
  watching.delete(operation.name);

  if (operation.status === "failed") {
    spec.onFailed?.(operation.error);
    setNote(spec.note, `${operation.name} failed: ${operation.error}`, "warn");
    return;
  }
  spec.onResult?.(operation.result || {});
}

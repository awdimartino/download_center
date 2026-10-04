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
import { setNote } from "./core.js";

const registry = {};

export function registerOperation(name, spec) {
  registry[name] = spec;
}

export async function startOperation(name, path, body) {
  const spec = registry[name];
  setNote(spec.note, "");
  try {
    const request = { method: "POST" };
    if (body) {
      request.headers = { "Content-Type": "application/json" };
      request.body = JSON.stringify(body);
    }
    const payload = await fetch(path, request).then((r) => r.json());
    if (payload.detail) {
      setNote(spec.note, payload.detail, "warn");
      return payload;
    }
    if (!payload.started) {
      setNote(spec.note,
              "That is already running; watching the one in flight.", "warn");
    }
    showOperation(payload.operation);
    return payload;
  } catch (err) {
    setNote(spec.note, `Could not start: ${err.message}`, "warn");
    return null;
  }
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
  if (running) return;

  if (operation.status === "failed") {
    spec.onFailed?.(operation.error);
    setNote(spec.note, `${operation.name} failed: ${operation.error}`, "warn");
    return;
  }
  spec.onResult?.(operation.result || {});
}

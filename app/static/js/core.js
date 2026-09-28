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

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function call(path) {
  showError("");
  try {
    const response = await fetch(path, { method: "POST" });
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

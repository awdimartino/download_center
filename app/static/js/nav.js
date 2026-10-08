"use strict";

/* --- navigation ----------------------------------------------------------
   One menu at every width, replacing the desktop tabs and the fixed bottom
   bar. The bar needed a 6rem overhang to cover the iOS home indicator, short
   labels behind a font-size:0 trick and badge positioning of its own; none
   of that survives, and there is one layout to keep working instead of two. */

import { showError } from "./core.js";

const menu = document.getElementById("menu");
const menuToggle = document.getElementById("menu-toggle");
const menuBackdrop = document.getElementById("menu-backdrop");
const menuDot = document.getElementById("menu-dot");
const viewTitle = document.getElementById("current-view");
export const navItems = () => document.querySelectorAll(".nav-item[data-view]");

// Populated by main.js once every panel module is imported: view name ->
// callback to run when that view is shown. Kept here as a registry rather
// than this module importing every panel directly, so nav.js stays a leaf
// - showView used to call loadHome/loadLibrary/loadHealth/... by name,
// which would otherwise make this module depend on all of them.
export const viewHandlers = {};

// The panel hangs below the header rather than covering it, so it needs to
// know where the header ends. Measured rather than assumed: the height moves
// with the safe-area inset, the font size and the 720px breakpoint.
function syncHeaderHeight() {
  const header = document.querySelector("header");
  if (!header) return;
  document.documentElement.style.setProperty(
    "--header-h", `${Math.round(header.getBoundingClientRect().height)}px`);
}

syncHeaderHeight();
addEventListener("resize", syncHeaderHeight);
addEventListener("orientationchange", syncHeaderHeight);

function openMenu() {
  syncHeaderHeight();
  menu.hidden = false;
  menuToggle.setAttribute("aria-expanded", "true");
  menuToggle.setAttribute("aria-label", "Close menu");
  // The active section, so the menu opens on where you already are rather
  // than at the top of the list.
  const current = document.querySelector(".nav-item.active");
  if (current) current.focus();
}

export function closeMenu() {
  if (menu.hidden) return;
  menu.hidden = true;
  menuToggle.setAttribute("aria-expanded", "false");
  menuToggle.setAttribute("aria-label", "Open menu");
  // Back to the control that opened it, or the focus ring is left on an
  // element that is now display:none and the next Tab starts from the top.
  menuToggle.focus();
}

menuToggle.addEventListener("click", () => {
  if (menu.hidden) openMenu();
  else closeMenu();
});
menuBackdrop.addEventListener("click", closeMenu);

// Capturing, so it runs before the panels' own Escape handlers, and marks
// the key used: closing the menu over the Library also closed the album
// panel or ended select mode behind it.
document.addEventListener("keydown", (event) => {
  if (menu.hidden) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeMenu();
    return;
  }
  if (event.key !== "Tab") return;
  // Keep Tab inside the panel while it is open. Without this the focus ring
  // walks off into the page behind an opaque overlay, where it cannot be
  // seen and Enter presses something invisible.
  const focusable = [menuToggle, ...menu.querySelectorAll("button:not([disabled])")];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}, true);

export function showView(view) {
  // Driven off the buttons themselves rather than a hand-kept list: a view
  // removed from the markup used to leave a name here that resolved to
  // null, and the resulting throw hid every panel at once.
  navItems().forEach((item) => {
    const section = document.getElementById(`view-${item.dataset.view}`);
    if (section) section.hidden = item.dataset.view !== view;
    item.classList.toggle("active", item.dataset.view === view);
  });

  // The banner belongs to whatever you were just doing. It lives outside
  // every section, so leaving it up carried one panel's problem onto all the
  // others, where there was nothing to act on and nothing to clear it.
  showError("");

  const active = document.querySelector(`.nav-item[data-view="${view}"]`);
  const label = active && active.querySelector(".nav-label");
  // With the tabs gone this is the only thing saying where you are.
  if (label) viewTitle.textContent = label.textContent;

  viewHandlers[view]?.();
}

navItems().forEach((item) => {
  item.addEventListener("click", () => {
    showView(item.dataset.view);
    closeMenu();
  });
});

// One place that sets a nav count, so the attention dot cannot fall out of
// step with the badges it summarises.
export function setBadge(element, count) {
  element.textContent = count || "";
  element.hidden = !count;
  updateAttentionDot();
}

function updateAttentionDot() {
  const anything = [...document.querySelectorAll(".nav-item .badge")]
    .some((badge) => !badge.hidden);
  menuDot.hidden = !anything;
}

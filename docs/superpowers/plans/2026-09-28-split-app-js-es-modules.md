# Split app.js into ES modules — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single 3,353-line `app/static/app.js` (one global scope, per PLAN.md Session 10) with native ES modules under `app/static/js/`, with no bundler, no behaviour change.

**Architecture:** Sixteen modules, one per existing panel plus a handful of genuinely shared pieces (`core.js`, `charts.js`, `operations.js`, `nav.js`), wired together with native `import`/`export`. Three places in the original file let one panel reach into another (`showView`'s per-panel `load*()` calls, the operation-outcome dispatcher that both Library and Health depend on, and the websocket's sign-out path) — each becomes a small callback/registry so the module graph stays a strict DAG with no circular imports, rather than relying on ESM's technically-legal-but-fragile support for cycles.

**Tech Stack:** Vanilla JS, native ES modules (`<script type="module">`, relative imports with explicit `.js` extensions, no import maps, no bundler, no build step). FastAPI/Starlette backend serves the new files as-is; `RevalidatedStatic` already covers subdirectories under `/static`.

**Spec:** PLAN.md, `## Last`, Session 10: "split `app.js` into ES modules (no bundler)." ARCHITECTURE.md's "Front end" section describes the current single-scope organisation this replaces.

## Global Constraints

- No bundler, no build step. Every `.js` file under `app/static/js/` is served directly by Starlette; the browser resolves the module graph itself.
- Relative import specifiers must include the `.js` extension (native ESM has no extension resolution).
- Only `app/static/js/main.js` is referenced from `index.html`, as `<script type="module" src="/static/js/main.js">`.
- No behaviour change. Every DOM id, every fetch path, every confirm() string, every event listener stays exactly as it is today — this is a mechanical relocation plus the minimum wiring needed to break three cross-panel call sites into callbacks.
- `app/static/app.js` is deleted only in the final task, once every module exists and the cutover is verified — the app must never be half-migrated with both old and new script tags able to load at once.
- There is deliberately no JS test runner in this repo (ARCHITECTURE.md: "Node is not available here or in CI"). Do not add one as a side effect of this task. Verification is: (a) the existing `tests/test_frontend.py` structural checks, generalised to scan every file under `app/static/js/` instead of one `app.js`; (b) a manual, non-committed V8 syntax check per new file (the `mini-racer` trick from `docs/superpowers/plans/` session memory — see Task 1); (c) a live-server curl smoke test; (d) a manual click-through by the user, since this session has no browser automation tool.

## Review Focus

- **A function used from another module but not exported.** Silent at write time; fails at runtime with `ReferenceError`/`undefined is not a function` the first time that code path runs, which nothing here can execute to catch automatically. Every module's task below lists its exact export set — cross-check call sites against it before moving on.
- **A relative import missing its `.js` extension, or pointing at a renamed file.** The browser refuses to resolve it and the whole module graph fails to load (`net::ERR_ABORTED` in the console) — the page would render its static HTML shell and then do nothing, including never showing the sign-in form. Grep every `from "./` in the finished files against the actual filenames.
- **Losing the operations registry population order.** `library.js` and `health.js` call `registerOperation(...)` at module top level; `operations.js` must not call back into them during that registration (it doesn't — registration just stores a plain object), and `main.js` must import both `library.js` and `health.js` (even if only for side effects) so their registrations run before any operation can start. Missing this means `startOperation("audit", ...)` throws on `registry[name]` being undefined.
- **The websocket's sign-out path losing its side effects.** The original `connect()` directly cleared `warnEl`, `showError`, `healthTimer` and called `showSignin(true)` on a 4401 close. This plan moves all of that into a `handleSessionExpired` callback owned by `main.js`, passed into `connect()`. Miss one of the four and a forced sign-out (server restart) leaves stale UI state instead of showing the sign-in form.
- **The asset-version hash not covering every module file.** If `asset_version()` only hashes `js/main.js` (the one file linked from `index.html`) instead of every file under `js/`, a change to e.g. `library.js` alone would not bump the version — and since every `/static/*` response already carries `Cache-Control: no-cache` (`RevalidatedStatic`), this is not the stale-cache bug from ARCHITECTURE.md's gotcha list, but it does mean `asset_version()`'s docstring claim ("changes when the assets do") goes quietly false for every file but one. Task 16 hashes the whole `js/` tree.

---

## File structure

```
app/static/
  index.html            (modified: one script tag)
  style.css              (unchanged)
  app.js                 (deleted in the final task)
  js/
    core.js
    charts.js
    nav.js
    operations.js
    ws.js
    drop.js
    browse.js
    library.js
    home.js
    listening.js
    health.js
    duplicates.js
    lookup.js
    playlists.js
    settings.js
    main.js
app/main.py               (modified: ASSETS, asset_version)
tests/test_frontend.py    (modified: scans app/static/js/*.js)
```

### Module map

Every entry below names the symbols to move (with their current line numbers in `app/static/app.js`, for locating them — line numbers will drift as earlier tasks remove code, so re-locate by function name, not by number, for any task after the first that touches a given region), what to `export`, and what to `import` from sibling modules. Unless stated otherwise, a symbol keeps its exact body — only `export` keywords and import headers are added.

**`core.js`** — truly cross-panel primitives.
- Move: `errorEl` (L5, kept private — not exported), `warnEl` (L6, **exported**), `duration()` (L16-20), `setBanner()` (L22-44), `showError()` (L46-48), `el()` (L50-55), `call()` (L114-125 — dead code, never called anywhere in the current file; move it verbatim, do not export it, do not delete it — this task is a relocation, not a cleanup), `action()` (L127-134), `setNote()` (L1866-1868).
- Exports: `el`, `action`, `duration`, `setBanner`, `showError`, `setNote`, `warnEl`.
- Imports: none (leaf module).

**`charts.js`** — the "name against a plays count" bar/chart toolkit, shared by Home, Listening and Library's genre tally.
- Move: `svg()` (L2007-2013), `MONTH_NAMES`/`monthLabel()`/`PLOT` (L2023-2034, private helpers for `monthlyChart`), `monthlyChart()` (L2036-2149), `barRows()` (L2153-2166), `artistBars()` (L2168-2171), `albumBars()` (L2173-2177), `genreBars()` (L2179-2182), `hourlyBars()` (L2184-2203), `formatDuration()` (L2205-2211), `sessionStats()` (L2213-2224), `stat()` (L2328-2337).
- Exports: `svg`, `monthlyChart`, `barRows`, `artistBars`, `albumBars`, `genreBars`, `hourlyBars`, `formatDuration`, `sessionStats`, `stat`.
- Imports: `{ el }` from `./core.js`.
- Note: `stat()` is defined after `sessionStats()` uses it in the original file — fine as-is, function declarations hoist within a module.

**`nav.js`** — the burger menu and view switcher, deliberately panel-agnostic.
- Move: `menu`/`menuToggle`/`menuBackdrop`/`menuDot`/`viewTitle` DOM refs (L683-687), `navItems()` (L688), `syncHeaderHeight()` + its load/resize/orientationchange listeners (L693-702), `openMenu()` (L704-713), `closeMenu()` (L715-723), the `menuToggle`/`menuBackdrop` click listeners (L725-729), the document `keydown` focus-trap listener (L731-752), `showView()` (L754-789, **modified**: replace the six `if (view === "x") loadX();` lines with a single `viewHandlers[view]?.();`), the `navItems().forEach(...)` click listener (L791-796), `setBadge()` (L800-804), `updateAttentionDot()` (L806-810).
- New: `export const viewHandlers = {};` — a plain object, `view name -> callback`, populated by `main.js` after every panel module is imported.
- Exports: `showView`, `viewHandlers`, `setBadge`, `navItems`.
- Imports: `{ showError }` from `./core.js`.
- Nothing here imports any panel module — that is the point of the registry.

**`operations.js`** — the long-running-operation status dispatcher (`app/operations.py`'s browser-side counterpart), rewritten from a hardcoded `OPERATION_LABELS` table + if/elseif chain into a registry, for the same reason as `nav.js`.
- Move (rewritten): `OPERATION_LABELS` (L1802-1815) becomes a private `const registry = {}` filled by a new exported `registerOperation(name, spec)`. `spec` shape: `{ button?, idle?, busy?, note, onButtonState?(running), onFailed?(error), onResult?(result) }`.
- `showOperation()` (L1885-1936, **rewritten** as a generic dispatcher):
  ```js
  export function showOperation(operation) {
    const spec = registry[operation.name];
    if (!spec) return;
    const running = operation.status === "running";
    const button = spec.button ? document.getElementById(spec.button) : null;
    if (button) {
      button.disabled = running;
      button.textContent = running ? spec.busy : spec.idle;
    }
    spec.onButtonState?.(running);
    if (running) return;
    if (operation.status === "failed") {
      spec.onFailed?.(operation.error);
      setNote(spec.note, `${operation.name} failed: ${operation.error}`, "warn");
      return;
    }
    spec.onResult?.(operation.result || {});
  }
  ```
- `startOperation()` (L1938-1962) keeps its body, reading `registry[name].note` instead of `OPERATION_LABELS[name].note`.
- Exports: `registerOperation`, `startOperation`, `showOperation`.
- Imports: `{ setNote }` from `./core.js`.
- The three call sites this replaces (all in `library.js`/`health.js`, see below) each call `registerOperation(...)` once at their own module's top level — a side-effecting import, not a function call back into `operations.js`'s internals, so there is no ordering hazard.

**`ws.js`** — the job queue + websocket. The "queue" panel from ARCHITECTURE.md's front-end list.
- Move: `jobsEl`/`emptyEl` DOM refs (L3-4), `connEl` DOM ref (L7, now private here — it was only ever used inside `connect()`), `jobs` Map (L12), `collapsed` Set (L14), `ACTIVE` (L57), `renderTrack()` (L59-83), `updateTrack()` (L85-100), `summarise()` (L102-112), `jobNodes` Map (L141), `buildJob()` (L143-175), `updateJob()` (L177-209), `render()` (L211-232), `applyProgress()` (L235-246), `handleMessage()` (L248-268), `socket`/`retryDelay` (L270-271), `connect()` (L273-307, **modified**: takes one parameter, `onSessionExpired`; the 4401 branch becomes `onSessionExpired(); return;` — delete the inline `started = false`, `healthTimer` clear, `setBanner(warnEl, "")`, `showError("")`, `showSignin(true)` lines, they move to `main.js`).
  ```js
  export function connect(onSessionExpired) {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws`);
    socket.addEventListener("open", () => { ... unchanged ... });
    socket.addEventListener("message", (event) => handleMessage(JSON.parse(event.data)));
    socket.addEventListener("close", (event) => {
      connEl.textContent = "offline";
      connEl.className = "conn offline";
      if (event.code === 4401) {
        onSessionExpired();
        return;
      }
      setTimeout(() => connect(onSessionExpired), retryDelay);
      retryDelay = Math.min(retryDelay * 2, 15000);
    });
  }
  ```
  (The retry's `setTimeout` must re-pass `onSessionExpired` explicitly now that `connect` takes a parameter — the original relied on `connect` being callable with no arguments for the retry.)
- Exports: `connect`.
- Imports: `{ el, duration }` from `./core.js`; `{ showOperation }` from `./operations.js` (used inside `handleMessage`'s `"operation"` branch).

**`drop.js`** — self-contained, no exports needed.
- Move verbatim: everything from the `/* --- drop --- */` comment through the last drop event listener (L440-665): DOM refs, `DROP_AUDIO_EXT`/`DROP_COVER_EXT`, `dropExt()`, `dropUploadable()`, `dropReadEntries()`, `dropFilesFromEntry()`, `dropFilesFromTransfer()`, `dropBuildBatch()`, `dropBuildRow()`, `dropUpload()`, `dropFinish()`, `dropChain`, `dropQueueBatch()`, `dropRunBatch()`, and the `dropZone`/`dropInput` event listeners.
- Exports: none.
- Imports: `{ el, showError }` from `./core.js`.

**`browse.js`** — search + queue-by-URL. Also owns `#search-form`/`#query`, since Session 3 merged Queue and Browse onto one input.
- Move: `resultsEl`/`browseEmpty`/`crumbEl` DOM refs (L670-672), `kind`/`searchToken` (L674-675), the `form`/`urlInput` DOM refs (originally L8-9, relocate here), `looksLikeUrl()` (originally L311-313, relocate here), the `#search-form` submit listener (originally L315-344, relocate here — it is the one that decides download-vs-search), `.kind` button listeners (L812-819), `setBrowse()` (L821-825), `cover()` (L827-837), `queue()` (L839-854), `queueButton()` (L856-863), `albumCard()` (L865-885), `heldMarker()` (L897-902), `trackCard()` (L904-919), `artistCard()` (L921-939), `RENDERERS` (L941), `runSearch()` (L943-966), `crumb()` (L968-974), `openAlbum()` (L976-1016), `openArtist()` (L1018-1025).
- New: `export function focusSearchIfPointer() { if (matchMedia("(hover: hover) and (pointer: fine)").matches) urlInput.focus(); }` — extracts the inline check that used to live in `showView` (original L780-782).
- Exports: `focusSearchIfPointer`.
- Imports: `{ el, duration, showError }` from `./core.js`.

**`settings.js`**
- Move: `settingsForm`/`settingsNote` DOM refs (L346-347), `loadSettings()` (L352-377), the settings form submit listener (L379-410), `checkSpotify()` (L417-431).
- Exports: `loadSettings`, `checkSpotify`.
- Imports: `{ setBanner, warnEl }` from `./core.js`.

**`listening.js`**
- Move: `listeningEl`...`listeningSession` DOM refs (L2315-2322), `listeningDays` (L2323), `coverage()` (L2339-2361), `listeningRow()` (L2363-2383), `loadListening()` (L2385-2413), `selectRange()` (L2417-2423), the `.listen-range .range` button listeners (L2425-2427).
- Exports: `loadListening`, `selectRange`.
- Imports: `{ el }` from `./core.js`; `{ stat, hourlyBars, albumBars, genreBars, sessionStats }` from `./charts.js`.

**`home.js`**
- Move: `homeGreeting`...`homeEmpty` DOM refs (L1995-2003), `greeting()` (L2015-2021), `heroFact()` (L2226-2235), `homeTiles()` (L2237-2271), `loadHome()` (L2273-2303), the `homeHero` click listener (L2305-2306).
- Exports: `loadHome`.
- Imports: `{ el, setBanner }` from `./core.js`; `{ monthlyChart, artistBars, stat }` from `./charts.js`; `{ showView }` from `./nav.js`; `{ selectRange }` from `./listening.js`.

**`health.js`**
- Move: `healthEl`...`healthBadge` DOM refs (L2434-2437), `renderCheck()` (L2439-2450), `renderHealth()` (L2452-2474), `loadHealth()` (L2476-2485), the `health-audit` click listener (L2495-2497, **modified**: calls `startOperation("audit", "/api/health/audit")` — unchanged call, but `startOperation` now comes from `operations.js`).
- New: `registerOperation("audit", { button: "health-audit", idle: "Re-read files", busy: "Reading…", note: "health-op", onResult() { setNote("health-op", ""); loadHealth(); } });` at module top level. (`setNote` re-exported from `core.js` via `operations.js`'s own import is not visible here — import `{ setNote }` from `./core.js` directly in `health.js` too.)
- Exports: `loadHealth`.
- Imports: `{ el, setBanner, setNote }` from `./core.js`; `{ setBadge }` from `./nav.js`; `{ registerOperation, startOperation }` from `./operations.js`.

**`duplicates.js`**
- Move: `dupesEl`...`dupeResult` DOM refs (L2504-2508), `dupeGroups` (L2510), `describeCopy()` (L2512-2518), `titlesDiffer()` (L2524-2526), `renderGroup()` (L2528-2600), `reportResolution()` (L2606-2625), `postDupe()` (L2627-2648), `renderDupes()` (L2650-2665), `quarantineEl`/`quarantineList`/`quarantineEmpty`/`quarantineCount` DOM refs (L2671-2674), `bytes()` (L2676-2679, private), `quarantineRow()` (L2681-2702), `loadQuarantine()` (L2704-2727), the `quarantineEl` toggle listener (L2731-2733), `loadDupes()` (L2735-2744), the `dupe-auto` click listener (L2746-2772).
- Exports: `loadDupes`.
- Imports: `{ el, action, setBanner, showError }` from `./core.js`; `{ setBadge }` from `./nav.js`.

**`lookup.js`**
- Move verbatim: `lookupForm`...`lookupResult` DOM refs, `quarantineLookup()`, `renderLookupTrack()`, `renderLookupAlbum()`, `runLookup()`, the form submit listener (L2780-2883).
- Exports: none.
- Imports: `{ el, action, showError }` from `./core.js`.

**`playlists.js`**
- Move verbatim: everything from `playlistsEl` (L2973) through the `pl-delete` click listener (L3315): DOM refs, `vocabulary`/`editingId`, `showPlaylistError()`, `fieldSpec()`, `conditionRow()`, `readCondition()`, `openEditor()`, `closeEditor()`, `describeRule()`, `playlistCard()`, `loadPlaylists()`, and every button/form listener in this section.
- Exports: `loadPlaylists`.
- Imports: `{ el, setBanner }` from `./core.js`.

**`library.js`** — the largest module; build it last, after the registry pattern has already been proven by `health.js`.
- Move: `libraryEl`...`libraryGenresSection` DOM refs (L1038-1048), `LIBRARY_PAGE`/`libraryShownCount` (L1056-1057), `libraryOpen` Set (L1061), `albumKey()` (L1063-1065), `libraryArt()` (L1073-1086), `candidatesEl`/`candidatesFor` (L1094-1097), `closeCandidates()` (L1099-1103), `albumName()` (L1105-1108), `candidateRow()` (L1110-1136), `showCandidates()` (L1138-1159), `askForCandidates()` (L1161-1175), `useCandidate()` (L1177-1191), `saveEdit()` (L1198-1224), `field()` (L1226-1237), `albumEditor()` (L1242-1347), `trackMorePanel()` (L1353-1412), `trackRow()` (L1418-1493), `loadTracks()` (L1495-1519), `albumRow()` (L1521-1618), `LIBRARY_MAX_PAGE` (L1622), `plural()` (L1624-1626, private), `fetchLibraryPage()` (L1628-1646), `loadLibrary()` (L1656-1738), `refreshLibrary()` (L1742-1744), the `libraryMore`/`libraryShow`/`libraryGainAll`/`libraryGenresToggle` listeners (L1746-1789), the `librarySearch` input listener (L1793-1797), `showGainProgress()` (L1817-1844), `gainSummary()` (L1846-1859), `importSummary()` (L1870-1883), the `library-rescan` click listener (L1967-1982).
- New, replacing the `OPERATION_LABELS` entries for `import`/`candidates`/`replaygain` and the corresponding branches of the old `showOperation`:
  ```js
  registerOperation("import", {
    note: "library-op",
    onButtonState(running) {
      libraryEl.querySelectorAll("button").forEach((b) => { b.disabled = running; });
    },
    onResult(result) {
      if (result.busy) {
        setNote("library-op", "An import is already running; this one was not started.", "warn");
      } else if (!result.ran) {
        setNote("library-op", `Nothing to import: ${result.reason}`, "warn");
      } else {
        const [message, tone] = importSummary(result);
        setNote("library-op", message, tone);
      }
      refreshLibrary();
    },
  });
  registerOperation("candidates", {
    note: "library-op",
    onFailed() { closeCandidates(); },
    onResult(result) {
      showCandidates(result);
      refreshLibrary(candidatesFor);
    },
  });
  registerOperation("replaygain", {
    note: "library-op",
    onButtonState(running) {
      showGainProgress({ status: running ? "running" : "done" }); // see note below
      libraryGainAll.disabled = running;
    },
    onResult(result) {
      const [message, tone] = gainSummary(result);
      setNote("library-op", message, tone);
      refreshLibrary();
    },
  });
  ```
  **Careful with `showGainProgress`**: the original signature is `showGainProgress(operation)` and reads `operation.status`/`operation.progress` directly (it is called from the *old* `showOperation` with the full `operation` object, before the running/not-running split). Keep `onButtonState(running)` calling it with the *actual* `operation` object instead of a synthesized one — the cleanest fix is for `operations.js`'s generic dispatcher to pass the whole `operation` to `onButtonState(running, operation)`, not just `running`. Update the `operations.js` spec shape and the `showOperation` dispatcher accordingly: `spec.onButtonState?.(running, operation);`. `health.js`'s and `library.js`'s `import`/`audit` hooks ignore the second argument; only `replaygain`'s uses it, as `onButtonState(running, operation) { showGainProgress(operation); libraryGainAll.disabled = running; }`.
- Exports: `loadLibrary`, `refreshLibrary`.
- Imports: `{ el, setNote, showError }` from `./core.js`; `{ barRows }` from `./charts.js`; `{ setBadge }` from `./nav.js`; `{ registerOperation, startOperation }` from `./operations.js`.

**`main.js`** — the entry point, and the only module `index.html` references.
- Move: the sign-in section verbatim (L2891-2963: `signinEl`/`signinForm`/`signinError`/`whoamiEl` DOM refs, `session`, `showSignin()`, `applySession()`, `checkSession()`, the sign-in form submit listener, the sign-out button listener), `started` (L3319), `start()` (L3321-3334, **modified**: `healthTimer` now lives here, not in `health.js`), `resumeOperations()` (L3338-3348), the bottom `checkSession().then(...)` call (L3350-3352).
- New: `let healthTimer = null;` (moved here from the old health section — nothing else needs it once the 4401 handler is a callback owned by this file).
- New: `function handleSessionExpired() { started = false; if (healthTimer) clearInterval(healthTimer); healthTimer = null; setBanner(warnEl, ""); showError(""); showSignin(true); }` — the four side effects `connect()`'s 4401 branch used to perform inline.
- New: wire the view registry once, after every panel module is imported:
  ```js
  Object.assign(viewHandlers, {
    home: () => { loadHome(); loadListening(); },
    browse: focusSearchIfPointer,
    library: loadLibrary,
    health: loadHealth,
    dupes: loadDupes,
    playlists: loadPlaylists,
    settings: loadSettings,
  });
  ```
  (`drop` and `lookup` have no view-shown side effect in the original — they are absent from the old `showView`'s if-chain too, so they get no entry here either.)
- `start()`'s body changes `connect();` to `connect(handleSessionExpired);` and keeps everything else as-is (`loadHome(); loadListening(); loadHealth(); loadLibrary(); checkSpotify(); resumeOperations(); healthTimer = setInterval(loadHealth, 5 * 60 * 1000);`).
- Imports: `{ setBanner, showError, warnEl }` from `./core.js`; `{ connect }` from `./ws.js`; `{ viewHandlers, showView }` from `./nav.js`; `{ showOperation }` from `./operations.js`; `{ loadHome }` from `./home.js`; `{ loadListening }` from `./listening.js`; `{ loadLibrary }` from `./library.js`; `{ loadHealth }` from `./health.js`; `{ loadDupes }` from `./duplicates.js`; `{ loadPlaylists }` from `./playlists.js`; `{ loadSettings, checkSpotify }` from `./settings.js`; `{ focusSearchIfPointer }` from `./browse.js`; plain imports (no bindings used, but needed for their side-effecting `registerOperation` calls and event-listener wiring) of `./drop.js` and `./lookup.js`.

### Dependency graph (must stay acyclic)

```
core.js  (leaf)
  ^
  |-- charts.js
  |-- nav.js
  |-- operations.js
  |-- drop.js
  |-- browse.js
  |-- settings.js
  |-- listening.js  <-- charts.js
  |-- home.js        <-- charts.js, nav.js, listening.js
  |-- health.js       <-- nav.js, operations.js
  |-- duplicates.js    <-- nav.js
  |-- lookup.js
  |-- playlists.js
  '-- library.js        <-- charts.js, nav.js, operations.js

ws.js         --> core.js, operations.js
main.js       --> everything
```

No two modules import each other. `operations.js` does not import `library.js` or `health.js` (the registry inverts that dependency); `nav.js` does not import any panel module (same trick via `viewHandlers`).

---

## Tasks

Each task after Task 1 depends on the previous ones (shared `core.js`/`charts.js`/`nav.js`/`operations.js` must exist first). `app/static/app.js` and `index.html`'s script tag are untouched until Task 16, so the live app keeps working off the old file for the entire migration — nothing is at risk until the cutover.

### Task 1: `core.js` and the syntax-check tool

**Files:**
- Create: `app/static/js/core.js`
- Reference only (not modified yet): `app/static/app.js`

**Interfaces:**
- Produces: `el(tag, className?, text?)`, `action(label, className, handler)`, `duration(ms)`, `setBanner(node, message, tone?)`, `showError(message)`, `setNote(id, message, tone?)`, `warnEl` (the `#warn` element). Every later task imports a subset of these from `./core.js`.

- [ ] **Step 1: Write `app/static/js/core.js`**, moving the eight symbols listed in the File Structure section's `core.js` entry, verbatim except for adding `export` to `el`, `action`, `duration`, `setBanner`, `showError`, `setNote`, and `warnEl`. `errorEl` and `call` stay unexported.

- [ ] **Step 2: Syntax-check it.** From the project root:
  ```bash
  .venv/Scripts/python.exe -c "
  from py_mini_racer import MiniRacer
  import re, json
  src = open('app/static/js/core.js', encoding='utf-8').read()
  stripped = re.sub(r'^export (const|let|function|async function)', r'\1', src, flags=re.M)
  MiniRacer().eval('new Function(' + json.dumps(stripped) + ')')
  print('OK')
  "
  ```
  If `py_mini_racer` is not installed, `pip install --target <scratch>/pylib py_mini_racer` and put that on `PYTHONPATH` first (per the local-tooling-quirks note — `quickjs` does not build on this Python). Expected: prints `OK`, no exception.

- [ ] **Step 3: Commit.**
  ```bash
  git add app/static/js/core.js
  git commit -m "Add core.js: the shared DOM/banner helpers for the app.js module split"
  ```

### Task 2: `charts.js`

**Files:** Create `app/static/js/charts.js`.
**Interfaces:** Consumes `{ el }` from `./core.js`. Produces `svg`, `monthlyChart`, `barRows`, `artistBars`, `albumBars`, `genreBars`, `hourlyBars`, `formatDuration`, `sessionStats`, `stat`.

- [ ] Write the file per the `charts.js` entry above.
- [ ] Syntax-check it (same recipe as Task 1 Step 2, new file path; the regex needs to also strip `import { el } from "./core.js";` — replace that line with nothing before the `new Function` call, since `new Function` bodies cannot contain top-level `import`).
- [ ] Commit.

### Task 3: `operations.js`

**Files:** Create `app/static/js/operations.js`.
**Interfaces:** Consumes `{ setNote }` from `./core.js`. Produces `registerOperation(name, spec)`, `startOperation(name, path, body)`, `showOperation(operation)`.

- [ ] Write the file per the `operations.js` entry above — this is the one module whose body is genuinely rewritten (registry instead of a hardcoded table), not just relocated. Double-check the `onButtonState(running, operation)` two-argument signature (needed by `library.js`'s `replaygain` entry in Task 15).
- [ ] Syntax-check it.
- [ ] Commit.

### Task 4: `nav.js`

**Files:** Create `app/static/js/nav.js`.
**Interfaces:** Consumes `{ showError }` from `./core.js`. Produces `showView(view)`, `viewHandlers` (a plain object), `setBadge(element, count)`, `navItems()`.

- [ ] Write the file per the `nav.js` entry above, including the `showView` rewrite that replaces the six-line if-chain with `viewHandlers[view]?.();`.
- [ ] Syntax-check it.
- [ ] Commit.

### Task 5: `ws.js`

**Files:** Create `app/static/js/ws.js`.
**Interfaces:** Consumes `{ el, duration }` from `./core.js`, `{ showOperation }` from `./operations.js`. Produces `connect(onSessionExpired)`.

- [ ] Write the file per the `ws.js` entry above, including the `connect(onSessionExpired)` signature change and the retry `setTimeout` re-passing the callback.
- [ ] Syntax-check it.
- [ ] Commit.

### Task 6: `drop.js`

**Files:** Create `app/static/js/drop.js`.
**Interfaces:** Consumes `{ el, showError }` from `./core.js`. Produces nothing (side-effecting only).

- [ ] Write the file verbatim per the `drop.js` entry.
- [ ] Syntax-check it.
- [ ] Commit.

### Task 7: `browse.js`

**Files:** Create `app/static/js/browse.js`.
**Interfaces:** Consumes `{ el, duration, showError }` from `./core.js`. Produces `focusSearchIfPointer()`.

- [ ] Write the file per the `browse.js` entry above, including relocating `form`/`urlInput`/`looksLikeUrl`/the search-form submit listener from their original position near the top of `app.js`, and extracting `focusSearchIfPointer`.
- [ ] Syntax-check it.
- [ ] Commit.

### Task 8: `settings.js`

**Files:** Create `app/static/js/settings.js`.
**Interfaces:** Consumes `{ setBanner, warnEl }` from `./core.js`. Produces `loadSettings()`, `checkSpotify()`.

- [ ] Write the file per the `settings.js` entry above.
- [ ] Syntax-check it.
- [ ] Commit.

### Task 9: `listening.js`

**Files:** Create `app/static/js/listening.js`.
**Interfaces:** Consumes `{ el }` from `./core.js`, `{ stat, hourlyBars, albumBars, genreBars, sessionStats }` from `./charts.js`. Produces `loadListening()`, `selectRange(days)`.

- [ ] Write the file per the `listening.js` entry above.
- [ ] Syntax-check it.
- [ ] Commit.

### Task 10: `home.js`

**Files:** Create `app/static/js/home.js`.
**Interfaces:** Consumes `{ el, setBanner }` from `./core.js`, `{ monthlyChart, artistBars, stat }` from `./charts.js`, `{ showView }` from `./nav.js`, `{ selectRange }` from `./listening.js`. Produces `loadHome()`.

- [ ] Write the file per the `home.js` entry above.
- [ ] Syntax-check it.
- [ ] Commit.

### Task 11: `health.js`

**Files:** Create `app/static/js/health.js`.
**Interfaces:** Consumes `{ el, setBanner, setNote }` from `./core.js`, `{ setBadge }` from `./nav.js`, `{ registerOperation, startOperation }` from `./operations.js`. Produces `loadHealth()`.

- [ ] Write the file per the `health.js` entry above, including the `registerOperation("audit", {...})` call.
- [ ] Syntax-check it.
- [ ] Commit.

### Task 12: `duplicates.js`

**Files:** Create `app/static/js/duplicates.js`.
**Interfaces:** Consumes `{ el, action, setBanner, showError }` from `./core.js`, `{ setBadge }` from `./nav.js`. Produces `loadDupes()`.

- [ ] Write the file per the `duplicates.js` entry above.
- [ ] Syntax-check it.
- [ ] Commit.

### Task 13: `lookup.js`

**Files:** Create `app/static/js/lookup.js`.
**Interfaces:** Consumes `{ el, action, showError }` from `./core.js`. Produces nothing (side-effecting only).

- [ ] Write the file verbatim per the `lookup.js` entry.
- [ ] Syntax-check it.
- [ ] Commit.

### Task 14: `playlists.js`

**Files:** Create `app/static/js/playlists.js`.
**Interfaces:** Consumes `{ el, setBanner }` from `./core.js`. Produces `loadPlaylists()`.

- [ ] Write the file verbatim per the `playlists.js` entry.
- [ ] Syntax-check it.
- [ ] Commit.

### Task 15: `library.js`

**Files:** Create `app/static/js/library.js`.
**Interfaces:** Consumes `{ el, setNote, showError }` from `./core.js`, `{ barRows }` from `./charts.js`, `{ setBadge }` from `./nav.js`, `{ registerOperation, startOperation }` from `./operations.js`. Produces `loadLibrary()`, `refreshLibrary(album?)`.

- [ ] Write the file per the `library.js` entry above — the largest single move, and the one with the `registerOperation` calls for `import`/`candidates`/`replaygain`. Pay particular attention to the `showGainProgress(operation)` call inside `replaygain`'s `onButtonState(running, operation)` hook (see the note in the File Structure section).
- [ ] Syntax-check it.
- [ ] Commit.

### Task 16: Cutover — `main.js`, `index.html`, `main.py`, `tests/test_frontend.py`, delete `app.js`

This is the only task that changes anything the running app actually loads, and the only one with real (Python-level) tests to drive it.

**Files:**
- Create: `app/static/js/main.js`
- Modify: `app/static/index.html:417` (script tag), `app/main.py:1888` (`ASSETS`) and `app/main.py:1891-1906` (`asset_version`), `tests/test_frontend.py`
- Delete: `app/static/app.js`

**Interfaces:** `main.js` consumes every export listed in the File Structure section's `main.js` entry.

- [ ] **Step 1: Write `app/static/js/main.js`** per the File Structure entry, including `handleSessionExpired`, the `viewHandlers` wiring, and `connect(handleSessionExpired)`.

- [ ] **Step 2: Syntax-check `main.js`** (same recipe, stripping all its `import` lines first).

- [ ] **Step 3: Update `app/static/index.html`.** Change:
  ```html
  <script src="/static/app.js"></script>
  ```
  to:
  ```html
  <script type="module" src="/static/js/main.js"></script>
  ```

- [ ] **Step 4: Update `app/main.py`.** Replace:
  ```python
  ASSETS = ("app.js", "style.css")
  ```
  with:
  ```python
  ASSETS = ("js/main.js", "style.css")
  ```
  (this tuple is still what `index()` walks to stamp `?v=` onto the two URLs actually referenced from `index.html` — unchanged logic, just the one path). Then replace the body of `asset_version()`:
  ```python
  @functools.lru_cache(maxsize=1)
  def asset_version() -> str:
      """..." (keep the existing docstring, it still describes the intent)"""
      digest = hashlib.sha256()
      for path in sorted((STATIC_DIR / "js").glob("*.js")):
          digest.update(path.read_bytes())
      digest.update((STATIC_DIR / "style.css").read_bytes())
      return digest.hexdigest()[:12]
  ```
  This hashes every module file, not just `main.js`, so the version token changes when any of them does — `ASSETS` alone no longer describes what feeds the hash, which is why this reads the `js/` directory directly instead.

- [ ] **Step 5: Update `tests/test_frontend.py`.** This is the real regression risk in this whole task — read it against the new layout line by line, not just skimmed:
  - Replace the single-file read:
    ```python
    JS = (STATIC / "app.js").read_text(encoding="utf-8")
    ```
    with a concatenation over every module, keeping each file's own text available too:
    ```python
    JS_DIR = STATIC / "js"
    JS_FILES = {p.name: p.read_text(encoding="utf-8") for p in sorted(JS_DIR.glob("*.js"))}
    JS = "\n".join(JS_FILES.values())
    ```
    `JS_IDS`, `OPERATION_IDS`, and the four `test_every_*`/`test_the_menu_and_the_panels_agree` tests keep working unmodified against this concatenated `JS`, since they only do regex scans that don't care about file boundaries.
  - `test_app_js_is_not_truncated` — generalise from one blob to per-file, since a truncated *any* file is the failure mode, and the whole-blob count could still balance by accident across files:
    ```python
    @pytest.mark.parametrize("pair", ["{}", "()", "[]"])
    def test_no_js_module_is_truncated(pair):
        for name, src in JS_FILES.items():
            assert src.count(pair[0]) == src.count(pair[1]), name
    ```
  - `test_no_top_level_function_is_declared_twice` — change the regex to also match exported functions, and check **per file**, not across the whole concatenation (the same function name in two different modules is no longer a bug — ES module scoping means it can't shadow anything):
    ```python
    def test_no_top_level_function_is_declared_twice():
        for name, src in JS_FILES.items():
            names = re.findall(r'^(?:export )?(?:async )?function ([A-Za-z0-9_$]+)\(', src, re.M)
            duplicates = sorted({n for n in names if names.count(n) > 1})
            assert duplicates == [], name
    ```
  - `test_the_error_banner_is_cleared_when_the_view_changes` — `showView` now lives in `js/nav.js`; search that file specifically instead of the whole-blob `JS`:
    ```python
    def test_the_error_banner_is_cleared_when_the_view_changes():
        body = JS_FILES["nav.js"]
        body = body[body.index("function showView("):]
        body = body[:body.index("\n}\n")]
        assert 'showError("")' in body
    ```
  - `test_the_version_follows_the_asset_contents` — rewrite for the new hashing scheme. It currently asserts `set(main.ASSETS) == {"app.js", "style.css"}` and writes only those two files into `tmp_path`; the new version reads every `js/*.js` file plus `style.css`, so the fixture needs a `js/` subdirectory:
    ```python
    def test_the_version_follows_the_asset_contents(tmp_path, monkeypatch):
        from app import main

        assert main.ASSETS == ("js/main.js", "style.css")

        def version_of(js: str) -> str:
            js_dir = tmp_path / "js"
            js_dir.mkdir(exist_ok=True)
            (js_dir / "main.js").write_text(js, encoding="utf-8")
            (tmp_path / "style.css").write_text("body{}", encoding="utf-8")
            monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
            main.asset_version.cache_clear()
            return main.asset_version()

        first = version_of("console.log('one');")
        second = version_of("console.log('two');")

        assert len(first) == 12
        assert first != second
        main.asset_version.cache_clear()
    ```
  - `test_the_shell_stamps_a_version_onto_its_assets` needs no change beyond the fact that `ASSETS` now yields `/static/js/main.js?v=...` instead of `/static/app.js?v=...` — the assertion is already parametrised over `ASSETS`, so it will just work once `main.py` is updated. Read it once after the `main.py` change to confirm.

- [ ] **Step 6: Run the whole test suite and confirm every relevant test passes.**
  ```bash
  .venv/Scripts/python.exe -m pytest tests/test_frontend.py -v
  ```
  Expected: all pass. If `test_every_id_app_js_asks_for_exists` or `test_every_id_in_the_markup_is_used` fail, it means a `getElementById`/`querySelector` call was dropped or an id was left unreferenced somewhere in the module split — go back to the specific task that moved that code.

- [ ] **Step 7: Delete the old file.**
  ```bash
  git rm app/static/app.js
  ```

- [ ] **Step 8: Live-server smoke test**, per this session's standing note that there is no browser automation tool available — verify via curl against a fixture-backed live server instead of clicking through a real browser:
  ```bash
  .venv/Scripts/python.exe -m uvicorn app.main:app --port 8899 &
  sleep 1
  curl -s http://127.0.0.1:8899/ | grep -o '/static/js/main.js?v=[a-f0-9]*'
  curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://127.0.0.1:8899/static/js/main.js
  curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://127.0.0.1:8899/static/js/library.js
  # repeat the content_type check for every file under app/static/js/ - each must be a JS
  # mimetype (text/javascript or application/javascript), never text/plain or octet-stream,
  # or the browser refuses to execute it as a module.
  kill %1
  ```
  Expected: the `index.html` fetch shows the versioned `main.js` reference; every `js/*.js` file answers `200` with a JavaScript content type.

- [ ] **Step 9: Ask the user to click through the app in an actual browser** before calling this done — sign in, open every panel from the menu (Home, Drop, Browse, Library, Health, Duplicates, Lookup, Playlists, Settings), and specifically exercise the three rewritten cross-panel paths this plan is riskiest about: (a) Library → "Find matches" → "Use this" (the `candidates`/`import` operation flow), (b) Library → "ReplayGain" on an album with missing gain (the `replaygain` operation flow, including the Stop button), (c) forcing a session expiry (restart the server while signed in) to confirm the websocket's 4401 path still shows the sign-in form cleanly. Report back anything that doesn't behave exactly as it did before — this task must not change behaviour, only file layout.

- [ ] **Step 10: Commit.**
  ```bash
  git add app/static/js/main.js app/static/index.html app/main.py tests/test_frontend.py
  git commit -m "Cut app.js over to app/static/js/ ES modules; delete the old single file"
  ```

---

## Self-review

**Spec coverage:** PLAN.md's Session 10 line is one sentence ("split app.js into ES modules (no bundler)"); every symbol in the current `app/static/app.js` is accounted for in the module map above, and the "no bundler" constraint is enforced by using only relative `import`/`export` with explicit extensions and a single `type="module"` script tag. Nothing in ARCHITECTURE.md's front-end section is contradicted — `style.css`'s token-based structure and `test_frontend.py`'s CSS checks are untouched, and the `no-cache`/`?v=` caching scheme this task depends on is preserved, just extended to hash the whole `js/` tree.

**Placeholder scan:** every task step names the exact file, the exact symbols (with original line numbers for reference), and the exact new code for every place the plan changes behaviour instead of just relocating it (`operations.js`'s rewrite, `nav.js`'s `viewHandlers`, `ws.js`'s callback, `main.py`'s `asset_version`, `tests/test_frontend.py`'s five changed tests). No task says "similar to" without the actual code.

**Type consistency:** `registerOperation(name, spec)` / `spec.onButtonState(running, operation)` / `spec.onFailed(error)` / `spec.onResult(result)` are used with the same shapes in `operations.js` (Task 3), `health.js` (Task 11), and `library.js` (Task 15) — cross-checked above, including the two-argument `onButtonState` needed only by `replaygain`. `viewHandlers[view]` is a zero-argument callback everywhere it's populated (Task 16) and everywhere it's read (Task 4).

**Review Focus coverage:** all five items above (missing `export`, missing `.js` extension, registry population order, the 4401 callback's four side effects, and the asset-version hash scope) each have a concrete guard in the plan — a Review Focus item without one would mean a gap; there isn't one left uncovered.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-28-split-app-js-es-modules.md`. Please review the plan. Which execution approach would you prefer?

- **Subagent-driven** - A fresh subagent implements each task and a fresh reviewer checks it before the next one starts, then a whole-branch review at the end. Most thorough; costs a fresh context per task and per review.
- **Native** - I implement every task myself in this session, then one fresh reviewer on the most capable model checks the whole branch. Cheapest and fastest; no independent review until the end.

**For this plan I recommend Native**, because every task after Task 1 is a mechanical relocation governed entirely by the module map above (not an independent design decision a per-task reviewer would meaningfully second-guess), the tasks are strictly sequential (Task *N* literally cannot be checked as "working" in isolation — there is no way to load a partial module graph in a browser I can drive), and the one task with real design judgment (the `operations.js`/`nav.js` registry rewrite, Task 3–4) is small enough to get right the first time with the dependency analysis already done above. A single careful whole-branch review at the end, plus the user's own click-through in Task 16 Step 9, will catch more than sixteen isolated per-task reviews would. Does the plan capture what you want, and which approach should we use?

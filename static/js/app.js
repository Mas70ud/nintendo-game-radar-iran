const API = { timeline: "/api/timeline", run: "/api/run", stop: "/api/stop", runLog: "/api/run-log", sources: "/api/sources" };
const REFRESH_MS = 30_000;
const state = { search: "", filters: new Set(), sources: [], enabledSources: new Set(), sourcesReady: false, updateRunning: false };
const el = {
  timeline: document.getElementById("timeline"),
  status: document.getElementById("status"),
  btnRun: document.getElementById("btn-run"),
  runProgress: document.getElementById("run-progress"),
  runProgressRing: document.getElementById("run-progress-ring"),
  runProgressPercent: document.getElementById("run-progress-percent"),
  runProgressLabel: document.getElementById("run-progress-label"),
  runLogDialog: document.getElementById("run-log-dialog"),
  runLogContent: document.getElementById("run-log-content"),
  runLogClose: document.getElementById("run-log-close"),
  btnTheme: document.getElementById("btn-theme"),
  inpSearch: document.getElementById("inp-search"),
  filterButtons: [...document.querySelectorAll(".filter-chip")],
  sourcePicker: document.getElementById("source-picker"),
  sourceOptions: document.getElementById("source-options"),
  sourcePickerError: document.getElementById("source-picker-error"),
  sourcePickerClose: document.getElementById("source-picker-close"),
  sourceSelectAll: document.getElementById("source-select-all"),
  sourceRunCancel: document.getElementById("source-run-cancel"),
  sourceRunStart: document.getElementById("source-run-start"),
};

// Remove the service worker and cached shell from the previous install-enabled UI.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations().then((registrations) =>
    registrations
      .filter((registration) => registration.active?.scriptURL.endsWith("/service-worker.js"))
      .forEach((registration) => registration.unregister())
  ).catch(() => {});
}
if ("caches" in window) {
  caches.keys().then((keys) => Promise.all(
    keys.filter((key) => key.startsWith("nintendo-notifier-")).map((key) => caches.delete(key))
  )).catch(() => {});
}

function setStatus(message, kind = "") {
  el.status.textContent = message;
  el.status.className = `status ${kind}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function applyTheme(dark) {
  document.body.classList.toggle("dark", dark);
  const label = dark ? "Switch to light mode" : "Switch to dark mode";
  el.btnTheme.textContent = dark ? "☀️" : "🌙";
  el.btnTheme.title = label;
  el.btnTheme.setAttribute("aria-label", label);
  el.btnTheme.setAttribute("aria-pressed", String(dark));
  document.querySelector('meta[name="theme-color"]').content = dark ? "#161c1a" : "#fcf9f2";
}

function matchesFilters(item) {
  const platforms = ["Switch1", "Switch2"].filter((value) => state.filters.has(value));
  const conditions = ["new", "used"].filter((value) => state.filters.has(value));
  return item.vendors?.some((vendor) =>
    (!conditions.length || conditions.includes(vendor.condition))
    && (!platforms.length || platforms.includes(vendor.platform))
  );
}

try { applyTheme(localStorage.getItem("notifier-theme") === "dark"); }
catch { applyTheme(false); }
el.btnTheme.addEventListener("click", () => {
  const dark = !document.body.classList.contains("dark");
  try { localStorage.setItem("notifier-theme", dark ? "dark" : "light"); } catch {}
  applyTheme(dark);
});

function renderItems(items) {
  const filtered = items.filter(matchesFilters);
  if (!filtered.length) {
    el.timeline.innerHTML = '<div class="empty"><div class="big">🎮</div><div>No games found</div></div>';
    return;
  }
  el.timeline.innerHTML = filtered.map((item) => {
    const title = escapeHtml(item.title || "No title");
    const image = item.image_url
      ? `<img class="game-image" src="${escapeHtml(item.image_url)}" alt="${title}" loading="lazy" referrerpolicy="no-referrer" onerror="this.hidden=true">`
      : "";
    const offers = item.vendors.map((vendor) => {
      const condition = vendor.condition === "used" ? "Second-hand" : "New";
      const platform = vendor.platform === "Switch2" ? "NS2" : "NS1";
      const shopName = vendor.link
        ? `<a class="offer-store" href="${escapeHtml(vendor.link)}" target="_blank" rel="noopener">${escapeHtml(vendor.name)}</a>`
        : `<span class="offer-store">${escapeHtml(vendor.name)}</span>`;
      return `<div class="offer-row"><div class="offer-shop">
        <div class="offer-shop-header">${shopName}<span class="platform-badge">${platform}</span></div>
        <span class="offer-condition">${condition}</span>
        </div>
        <strong class="offer-price">${escapeHtml(vendor.price)}</strong></div>`;
    }).join("");
    return `<div class="item"><div class="card">
      ${image}
      <div class="card-content">
      <div class="title">${title}</div>
      <div class="offers">${offers}</div>
      </div>
    </div></div>`;
  }).join("");
}

async function fetchTimeline() {
  try {
    if (!state.sourcesReady) return;
    const params = new URLSearchParams();
    if (state.search) params.set("q", state.search);
    const response = await fetch(`${API.timeline}?${params}`);
    const result = await response.json();
    if (!result.ok) throw new Error(result.error || "Could not load games");
    renderItems(result.items);
    const visibleCount = result.items.filter(matchesFilters).length;
    const latestUpdate = result.items
      .map((item) => new Date(item.timestamp))
      .filter((date) => !Number.isNaN(date.getTime()))
      .sort((a, b) => b - a)[0];
    const updateDate = latestUpdate || new Date();
    const datePart = [updateDate.getFullYear(),
      String(updateDate.getMonth() + 1).padStart(2, "0"),
      String(updateDate.getDate()).padStart(2, "0")].join("/");
    const timePart = updateDate.toLocaleTimeString("en-GB", {
      hour: "2-digit", minute: "2-digit", hour12: false,
    });
    const updatedAt = `${datePart}, ${timePart}`;
    setStatus(`Updated: ${updatedAt} | ${visibleCount} Games`);
  } catch (error) { setStatus(`Error: ${error.message}`, "error"); }
}

async function runNow() {
  state.updateRunning = true;
  el.btnRun.disabled = false;
  el.btnRun.textContent = "■ Stop Now";
  el.btnRun.title = "Stop update";
  clearTimeout(runDoneTimer);
  el.runProgress.hidden = false;
  updateRunProgress({ state: "running", script: "Starting…", percent: 0 });
  setStatus("Updating game listings...");
  try {
    const response = await fetch(API.run, { method: "POST" });
    const result = await response.json();
    if (!result.ok) throw new Error(result.error || "Update failed");
  } catch (error) {
    state.updateRunning = false;
    setStatus(`Error: ${error.message}`, "error");
    el.btnRun.disabled = false;
    el.btnRun.textContent = "▶ Run now";
    el.btnRun.title = "Run now";
    el.runProgressLabel.textContent = "Error";
    runDoneTimer = setTimeout(() => { el.runProgress.hidden = true; }, 30_000);
  }
}

async function stopNow() {
  el.btnRun.disabled = true;
  el.btnRun.textContent = "Stopping…";
  el.btnRun.title = "Stopping update";
  try {
    const response = await fetch(API.stop, { method: "POST" });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "Could not stop update");
    setStatus("Stopping update...");
  } catch (error) {
    el.btnRun.disabled = false;
    el.btnRun.textContent = "■ Stop Now";
    el.btnRun.title = "Stop update";
    setStatus(`Error: ${error.message}`, "error");
  }
}

async function loadSources() {
  const response = await fetch(API.sources);
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.error || "Could not load websites");
  state.sources = result.sources || [];
  state.enabledSources = new Set(result.selected || state.sources.map((source) => source.id));
  el.sourceOptions.innerHTML = state.sources.map((source) => `
    <label class="source-option">
      <input type="checkbox" value="${escapeHtml(source.id)}" ${state.enabledSources.has(source.id) ? "checked" : ""}>
      <span>${escapeHtml(source.label)}</span>
    </label>`).join("");
}

function openSourcePicker() {
  if (!state.sourcesReady) return;
  el.sourcePickerError.textContent = "";
  for (const checkbox of el.sourceOptions.querySelectorAll("input[type=checkbox]")) {
    checkbox.checked = state.enabledSources.has(checkbox.value);
  }
  el.sourcePicker.showModal();
}

async function startSelectedSources() {
  const selected = [...el.sourceOptions.querySelectorAll("input:checked")].map((input) => input.value);
  if (!selected.length) {
    el.sourcePickerError.textContent = "Choose at least one website.";
    return;
  }
  el.sourceRunStart.disabled = true;
  el.sourcePickerError.textContent = "";
  try {
    const response = await fetch(API.sources, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sources: selected }),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "Could not save website selection");
    state.enabledSources = new Set(result.selected);
    el.sourcePicker.close();
    await fetchTimeline();
    await runNow();
  } catch (error) {
    el.sourcePickerError.textContent = error.message;
  } finally {
    el.sourceRunStart.disabled = false;
  }
}

let runDoneTimer;
function updateRunProgress(data) {
  el.runProgress.hidden = false;
  const percent = Math.max(0, Math.min(100, Number(data.percent) || 0));
  el.runProgressRing.style.setProperty("--progress", `${percent}%`);
  el.runProgressRing.setAttribute("aria-valuenow", String(percent));
  el.runProgressPercent.textContent = `${percent}%`;
  el.runProgressRing.classList.toggle("is-done", data.state === "done");
  el.runProgressRing.classList.toggle("is-error", data.state === "error");
  if (data.state === "done") {
    state.updateRunning = false;
    el.runProgressPercent.textContent = "100%";
    el.runProgressLabel.textContent = "Done";
    el.btnRun.disabled = false;
    el.btnRun.textContent = "▶ Run now";
    el.btnRun.title = "Run now";
    clearTimeout(runDoneTimer);
    runDoneTimer = setTimeout(() => { el.runProgress.hidden = true; }, 30_000);
  } else if (data.state === "stopped") {
    state.updateRunning = false;
    el.runProgressLabel.textContent = "Stopped";
    el.btnRun.disabled = false;
    el.btnRun.textContent = "▶ Run now";
    el.btnRun.title = "Run now";
    setStatus("Update stopped.");
    clearTimeout(runDoneTimer);
    runDoneTimer = setTimeout(() => { el.runProgress.hidden = true; }, 30_000);
  } else if (data.state === "error") {
    state.updateRunning = false;
    el.runProgressLabel.textContent = "Error";
    el.btnRun.disabled = false;
    el.btnRun.textContent = "▶ Run now";
    el.btnRun.title = "Run now";
    setStatus(`Error: ${data.error || "Update failed"}`, "error");
    clearTimeout(runDoneTimer);
    runDoneTimer = setTimeout(() => { el.runProgress.hidden = true; }, 30_000);
  } else {
    state.updateRunning = true;
    el.runProgressLabel.textContent = data.script || "Updating…";
    el.btnRun.disabled = false;
    el.btnRun.textContent = "■ Stop Now";
    el.btnRun.title = "Stop update";
  }
  if (el.runLogDialog.open) fetchRunLog();
}

function formatRunDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function fetchRunLog() {
  try {
    const response = await fetch(API.runLog);
    const result = await response.json();
    if (!result.ok || !result.log) {
      el.runLogContent.textContent = "No update log is available yet.";
      return;
    }
    const log = result.log;
    const mode = log.trigger === "manual" ? "Manual" : "Automatic";
    const overall = log.status === "running" ? "In progress" : log.status === "cancelled" ? "Stopped by user" : log.status === "success" ? "Completed successfully" : "Completed with errors";
    const sites = (log.sites || []).map((site) => {
      const status = site.status === "success" ? "Success" : site.status === "failure" ? "Failed" : site.status === "cancelled" ? "Stopped" : "Running";
      const cssStatus = site.status === "success" ? "success" : site.status === "failure" ? "failure" : "";
      const output = [site.stdout, site.stderr && `STDERR:\n${site.stderr}`, site.issues?.length && `Issues:\n${site.issues.join("\n")}`]
        .filter(Boolean).join("\n\n") || (site.status === "running" ? "Waiting for this script to finish…" : "No output captured.");
      return `<details class="run-log-site" ${site.status === "failure" || site.status === "running" ? "open" : ""}>
        <summary><span>${escapeHtml(site.name)}.py</span><span class="run-log-result ${cssStatus}">${status}</span></summary>
        <pre>${escapeHtml(output)}</pre>
      </details>`;
    }).join("");
    el.runLogContent.innerHTML = `<div class="run-log-summary">${mode} · ${overall}<br>Started: ${escapeHtml(formatRunDate(log.started_at))}<br>Finished: ${escapeHtml(formatRunDate(log.finished_at))}</div>${sites || "No scripts were run."}`;
  } catch (error) {
    el.runLogContent.textContent = `Could not load update log: ${error.message}`;
  }
}

function openRunLog() {
  if (!el.runLogDialog.open) el.runLogDialog.showModal();
  fetchRunLog();
}

el.runProgress.addEventListener("click", openRunLog);
el.runProgress.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    openRunLog();
  }
});
el.runLogClose.addEventListener("click", () => el.runLogDialog.close());
el.runLogDialog.addEventListener("click", (event) => {
  if (event.target === el.runLogDialog) el.runLogDialog.close();
});

el.btnRun.disabled = true;
el.btnRun.addEventListener("click", () => {
  if (state.updateRunning) stopNow();
  else openSourcePicker();
});
el.sourcePickerClose.addEventListener("click", () => el.sourcePicker.close());
el.sourceRunCancel.addEventListener("click", () => el.sourcePicker.close());
el.sourceRunStart.addEventListener("click", startSelectedSources);
el.sourceSelectAll.addEventListener("click", () => {
  const checkboxes = [...el.sourceOptions.querySelectorAll("input[type=checkbox]")];
  const checkAll = checkboxes.some((checkbox) => !checkbox.checked);
  checkboxes.forEach((checkbox) => { checkbox.checked = checkAll; });
  el.sourceSelectAll.textContent = checkAll ? "Deselect all" : "Select all";
});
el.sourcePicker.addEventListener("change", (event) => {
  if (event.target.matches("input[type=checkbox]")) {
    const checkboxes = [...el.sourceOptions.querySelectorAll("input[type=checkbox]")];
    el.sourceSelectAll.textContent = checkboxes.every((checkbox) => checkbox.checked)
      ? "Deselect all" : "Select all";
  }
});
el.sourcePicker.addEventListener("click", (event) => {
  if (event.target === el.sourcePicker) el.sourcePicker.close();
});
el.filterButtons.forEach((button) => button.addEventListener("click", () => {
  const filter = button.dataset.filter;
  if (state.filters.has(filter)) state.filters.delete(filter);
  else state.filters.add(filter);
  button.classList.toggle("active", state.filters.has(filter));
  button.setAttribute("aria-pressed", String(state.filters.has(filter)));
  fetchTimeline();
}));
let searchTimer;
el.inpSearch.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = el.inpSearch.value.trim();
    fetchTimeline();
  }, 300);
});

const events = new EventSource("/api/events");
events.onmessage = (event) => {
  try {
    const message = JSON.parse(event.data);
    if (message.type === "catalog_refreshed") fetchTimeline();
    if (message.type === "run_progress") updateRunProgress(message.data || {});
  } catch {}
};
loadSources().then(() => {
  state.sourcesReady = true;
  el.btnRun.disabled = false;
  fetchTimeline();
}).catch((error) => setStatus(`Error: ${error.message}`, "error"));
setInterval(fetchTimeline, REFRESH_MS);

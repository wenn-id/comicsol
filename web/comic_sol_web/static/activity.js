// Activity drawer — mounts beside #studio-main.
// EventSource with last-cursor reconnect; fetch polling fallback with same cursor.
// Events rendered via textContent only. Width/collapsed persisted in localStorage.

import { workflowEventsUrl } from "./api.js";

const STORAGE_KEY = "comic-sol-activity-drawer";
const MIN_WIDTH = 320;
const MAX_WIDTH = 720;

function loadPrefs() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { collapsed: false, width: 420 };
    const parsed = JSON.parse(raw);
    const collapsed = parsed.collapsed === true;
    const width = Number.isFinite(parsed.width)
      ? Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, parsed.width))
      : 420;
    return { collapsed, width };
  } catch {
    return { collapsed: false, width: 420 };
  }
}

function savePrefs(collapsed, width) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ collapsed, width }));
  } catch { /* storage full or unavailable */ }
}

export function mountActivityDrawer(container) {
  const prefs = loadPrefs();
  let collapsed = prefs.collapsed;
  let width = prefs.width;
  let lastId = 0;
  let sse = null;
  let pollTimer = null;
  let projectId = null;

  // Root element: positioned beside #studio-main via CSS
  const root = document.createElement("aside");
  root.id = "activity-drawer";
  root.setAttribute("role", "complementary");
  root.setAttribute("aria-label", "Activity timeline");
  root.dataset.collapsed = String(collapsed);

  const header = document.createElement("div");
  header.className = "activity-header";

  const toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "activity-toggle";
  toggleBtn.textContent = collapsed ? "▸ Activity" : "▾ Activity";
  toggleBtn.addEventListener("click", () => {
    collapsed = !collapsed;
    root.dataset.collapsed = String(collapsed);
    toggleBtn.textContent = collapsed ? "▸ Activity" : "▾ Activity";
    list.hidden = collapsed;
    range.hidden = collapsed;
    savePrefs(collapsed, width);
  });

  const range = document.createElement("input");
  range.type = "range";
  range.className = "activity-resize";
  range.min = String(MIN_WIDTH);
  range.max = String(MAX_WIDTH);
  range.step = "1";
  range.value = String(width);
  range.hidden = collapsed;
  range.setAttribute("aria-label", "Drawer width");
  range.addEventListener("input", () => {
    width = Number(range.value) || 420;
    root.style.width = width + "px";
    savePrefs(collapsed, width);
  });

  header.append(toggleBtn, range);

  const list = document.createElement("div");
  list.className = "activity-list";
  list.hidden = collapsed;

  root.append(header, list);
  root.style.width = width + "px";
  container.append(root);

  function appendEvent(ev) {
    const eventId = Number(ev.event_id ?? ev.id);
    if (Number.isInteger(eventId)) lastId = Math.max(lastId, eventId);
    const card = document.createElement("div");
    card.className = "activity-card";

    const fields = [
      ["type", ev.type],
      ["phase", ev.phase],
      ["status", ev.status],
      ["provider", ev.provider || ev.image_provider],
      ["model", ev.model || ev.image_model],
      ["attempt", ev.attempt != null ? String(ev.attempt) : null],
      ["summary", ev.summary],
      ["time", ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : null],
    ];

    for (const [label, value] of fields) {
      if (value == null || value === "") continue;
      const row = document.createElement("div");
      row.className = "activity-field";
      const lbl = document.createElement("span");
      lbl.className = "activity-label";
      lbl.textContent = label;
      const val = document.createElement("span");
      val.className = "activity-value";
      val.textContent = value;
      row.append(lbl, val);
      card.append(row);
    }

    list.prepend(card);
  }

  function eventsUrl(after) {
    return workflowEventsUrl(projectId, after);
  }

  function connect() {
    if (!projectId) return;
    close();
    try {
      sse = new EventSource(eventsUrl(lastId));
      sse.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data && typeof data === "object") appendEvent(data);
        } catch { /* malformed frame */ }
      };
      sse.onerror = () => {
        // Reconnect from the last seen cursor; fall back to polling the same cursor.
        close();
        startPolling();
      };
    } catch {
      startPolling();
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(() => {
      if (!projectId) return;
      fetch(eventsUrl(lastId), {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      })
        .then((response) => (response.ok ? response.json() : null))
        .then((data) => {
          const events = Array.isArray(data) ? data : (Array.isArray(data?.events) ? data.events : []);
          for (const item of events) {
            if (item && typeof item === "object") appendEvent(item);
          }
        })
        .catch(() => { /* transient network failure */ });
    }, 3000);
  }

  function close() {
    if (sse) { sse.close(); sse = null; }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function destroy() {
    close();
    root.remove();
  }

  function setProject(id) {
    if (id === projectId) return;
    projectId = id;
    lastId = 0;
    list.replaceChildren();
    connect();
  }

  return { setProject, destroy, root };
}

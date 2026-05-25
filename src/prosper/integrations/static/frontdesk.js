"use strict";

const listEl = document.getElementById("list");
const detailEl = document.getElementById("detail");
const mailView = document.getElementById("mail-view");
const calView = document.getElementById("cal-view");
const calGrid = document.getElementById("cal-grid");
const calRange = document.getElementById("cal-range");
const tabMail = document.getElementById("tab-mail");
const tabCal = document.getElementById("tab-cal");

let mail = [];
// Identity of the currently-open message, so a 2s refresh keeps the selection.
// Mail has no stable id; (session_id, ts, subject) is unique enough in practice.
let selectedKey = null;

const KINDS = ["booking_confirmation", "handoff", "bot_failed"];
const kindClass = (k) => (KINDS.includes(k) ? k : "default");

// ---- time helpers -----------------------------------------------------------

// Relative-ish, receptionist-friendly label for an epoch-seconds timestamp.
function relTime(ts) {
  const then = ts * 1000;
  const diff = Date.now() - then;
  if (diff < 0) return new Date(then).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  const min = Math.floor(diff / 60000);
  if (min < 1) return "just now";
  if (min < 60) return min + "m ago";
  const hr = Math.floor(min / 60);
  if (hr < 24) return hr + "h ago";
  return new Date(then).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

const absTime = (ts) =>
  new Date(ts * 1000).toLocaleString("en-US", {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });

// Slot datetimes arrive as NAIVE-UTC ISO strings (no tz suffix); their
// wall-clock numbers ARE the clinic's business-hours grid (see seed.py).
// `new Date(iso)` would re-interpret those numbers in the viewer's local
// zone and shift them — so parse the components literally into a local Date.
function parseNaive(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(String(iso || ""));
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
}

const hhmm = (d) =>
  d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });

const dayKey = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;

// ---- XSS-safe DOM builder ---------------------------------------------------
// Text is always set via textContent, never innerHTML, so record fields
// (PII) cannot inject markup.
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// ---- mail -------------------------------------------------------------------

const mailKey = (m) => `${m.session_id}|${m.ts}|${m.subject}`;

function renderList() {
  listEl.replaceChildren();
  if (!mail.length) {
    listEl.appendChild(el("div", "list-empty", "No mail yet."));
    return;
  }
  mail.forEach((m) => {
    const row = el("div", "row");
    if (mailKey(m) === selectedKey) row.classList.add("sel");
    row.appendChild(el("span", "dot dot-" + kindClass(m.kind)));

    const mid = el("div");
    mid.appendChild(el("div", "subj", m.subject));
    mid.appendChild(el("div", "to", m.to_label));
    row.appendChild(mid);

    row.appendChild(el("div", "time", relTime(m.ts)));
    row.addEventListener("click", () => {
      selectedKey = mailKey(m);
      renderList();
      renderDetail(m);
    });
    listEl.appendChild(row);
  });
}

function renderDetail(m) {
  detailEl.replaceChildren();
  const box = el("div", "email");

  box.appendChild(el("span", "tag tag-" + kindClass(m.kind), m.kind.replace(/_/g, " ")));
  box.appendChild(el("h2", null, m.subject));
  box.appendChild(el("div", "to-line", "To: " + m.to_label));
  box.appendChild(el("pre", null, m.body));

  const parts = ["Patient: " + m.patient_name];
  if (m.patient_phone) parts.push(m.patient_phone);
  parts.push(absTime(m.ts));
  box.appendChild(el("div", "footer", parts.join("  ·  ")));

  detailEl.appendChild(box);
}

async function pollMail() {
  try {
    const r = await fetch("/frontdesk/mail");
    mail = (await r.json()).mail || [];
    renderList();
    // Re-open the same message if it's still present after a refresh.
    if (selectedKey) {
      const still = mail.find((m) => mailKey(m) === selectedKey);
      if (still) renderDetail(still);
    }
  } catch (_e) {
    // Transient network error — next tick retries automatically.
  }
}

// ---- calendar ---------------------------------------------------------------

const isoDate = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

// Subtle per-specialty colouring; falls back to a neutral grey.
const SPECIALTY_COLORS = {
  Therapist: { bg: "#eef2ff", accent: "#4f46e5" },
  Psychiatrist: { bg: "#f0fdfa", accent: "#0d9488" },
  "General Practice": { bg: "#f0f9ff", accent: "#0284c7" },
  Dermatologist: { bg: "#fef2f8", accent: "#db2777" },
  Physiotherapist: { bg: "#fefce8", accent: "#ca8a00" },
};
const specColor = (s) => SPECIALTY_COLORS[s] || { bg: "#eef1f4", accent: "#64748b" };

function weekDays() {
  const out = [];
  const base = new Date();
  base.setHours(0, 0, 0, 0);
  for (let i = 0; i < 7; i++) {
    const d = new Date(base);
    d.setDate(base.getDate() + i);
    out.push(d);
  }
  return out;
}

function renderCalendar(entries) {
  // The 5s poll rebuilds the grid; remember scroll so it doesn't jump under
  // a receptionist mid-scroll.
  const scrollTop = calGrid.scrollTop;
  const scrollLeft = calGrid.scrollLeft;

  const days = weekDays();
  calRange.textContent =
    days[0].toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
    " – " +
    days[6].toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });

  // Bucket appointments by calendar day, ordered by time within each day.
  const byDay = new Map();
  let placed = 0;
  entries.forEach((e) => {
    const start = parseNaive(e.start_at);
    if (!start) return;
    const k = dayKey(start);
    if (!byDay.has(k)) byDay.set(k, []);
    byDay.get(k).push({ start, e });
    placed++;
  });
  byDay.forEach((arr) => arr.sort((a, b) => a.start - b.start));

  calGrid.replaceChildren();
  if (placed === 0) {
    calGrid.appendChild(el("div", "cal-empty", "No appointments this week."));
    return;
  }

  const todayKey = dayKey(new Date());
  days.forEach((d) => {
    const col = el("div", "cal-col");
    if (dayKey(d) === todayKey) col.classList.add("today");

    const head = el("div", "day-head");
    head.appendChild(el("div", "dow", d.toLocaleDateString("en-US", { weekday: "short" })));
    head.appendChild(el("div", "dnum", String(d.getDate())));
    col.appendChild(head);

    const body = el("div", "day-body");
    const items = byDay.get(dayKey(d)) || [];
    if (!items.length) {
      body.appendChild(el("div", "none", "—"));
    } else {
      items.forEach(({ start, e }) => {
        const c = specColor(e.specialty);
        const ev = el("div", "event");
        ev.style.setProperty("--ev-bg", c.bg);
        ev.style.setProperty("--ev-accent", c.accent);
        ev.appendChild(el("div", "etime", hhmm(start)));
        ev.appendChild(el("div", "epat", e.patient_name));
        ev.appendChild(el("div", "eprov", e.provider_name));
        ev.appendChild(el("div", "espec", e.specialty));
        if (e.notes) ev.appendChild(el("div", "enote", e.notes));
        body.appendChild(ev);
      });
    }
    col.appendChild(body);
    calGrid.appendChild(col);
  });

  calGrid.scrollTop = scrollTop;
  calGrid.scrollLeft = scrollLeft;
}

async function loadCalendar() {
  const days = weekDays();
  try {
    const r = await fetch(
      "/frontdesk/appointments?from=" + isoDate(days[0]) + "&to=" + isoDate(days[6])
    );
    const entries = (await r.json()).entries || [];
    renderCalendar(entries);
  } catch (_e) {
    calGrid.replaceChildren();
    calGrid.appendChild(el("div", "cal-empty", "Could not load calendar."));
  }
}

// ---- tabs -------------------------------------------------------------------

// Which pane is visible. The calendar poll runs only while "cal" is active, so
// switching to mail stops the wasted /frontdesk/appointments calls.
let activeTab = "mail";

function showMail() {
  activeTab = "mail";
  tabMail.classList.add("active");
  tabCal.classList.remove("active");
  mailView.classList.remove("hidden");
  calView.classList.add("hidden");
}

function showCalendar() {
  activeTab = "cal";
  tabCal.classList.add("active");
  tabMail.classList.remove("active");
  mailView.classList.add("hidden");
  calView.classList.remove("hidden");
  loadCalendar(); // immediate paint; the interval keeps it live thereafter
}

tabMail.addEventListener("click", showMail);
tabCal.addEventListener("click", showCalendar);

// ---- boot -------------------------------------------------------------------

pollMail();
setInterval(pollMail, 2000);

// Refresh the calendar only while its tab is showing, so a booking made
// mid-call appears within ~5s without a manual re-click.
setInterval(() => {
  if (activeTab === "cal") loadCalendar();
}, 5000);

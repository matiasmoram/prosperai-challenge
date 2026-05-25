"use strict";

const listEl = document.getElementById("list");
const detailEl = document.getElementById("detail");
const mailView = document.getElementById("mail-view");
const calView = document.getElementById("cal-view");
const calGrid = document.getElementById("cal-grid");
const calRange = document.getElementById("cal-range");
const calPrev = document.getElementById("cal-prev");
const calToday = document.getElementById("cal-today");
const calNext = document.getElementById("cal-next");
const tabMail = document.getElementById("tab-mail");
const tabCal = document.getElementById("tab-cal");

let mail = [];
// Identity of the currently-open message, so a 2s refresh keeps the selection.
// Mail has no stable id; (session_id, ts, subject) is unique enough in practice.
let selectedKey = null;

const KINDS = ["booking_confirmation", "cancellation", "reschedule", "handoff", "bot_failed"];
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

// Clinic hours. Slots run 09:00–16:30 start; show the 9am→5pm band as 30-min rows.
const DAY_START_H = 9;
const DAY_END_H = 17; // exclusive upper bound for the last row label
// Min pixels a single 30-min appointment block occupies; a longer visit scales
// up (e.g. 60 min ≈ 2×) so it visibly spans more of the day.
const EVENT_BASE_PX = 40;

// "9 AM", "12 PM", "1 PM", "5 PM"
function fmtHour(h) {
  const ampm = h < 12 || h === 24 ? "AM" : "PM";
  const hr = h % 12 === 0 ? 12 : h % 12;
  return `${hr} ${ampm}`;
}

// Which week the grid is showing, in weeks from today (0 = current week,
// -1 = last week, +1 = next week). Nav buttons mutate this; the poll reads it.
let weekOffset = 0;

// The 7 days for `today + offset*7`, today-anchored (day 0 is today's weekday
// in the current week) to match the original "this week = today..today+6".
function weekDays(offset) {
  const out = [];
  const base = new Date();
  base.setHours(0, 0, 0, 0);
  base.setDate(base.getDate() + offset * 7);
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

  const days = weekDays(weekOffset);
  const span =
    days[0].toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
    " – " +
    days[6].toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  calRange.textContent = weekOffset === 0 ? "This week · " + span : span;

  // 30-min slot rows from DAY_START_H to DAY_END_H.
  const slots = [];
  for (let hh = DAY_START_H; hh < DAY_END_H; hh++) {
    slots.push({ h: hh, m: 0 });
    slots.push({ h: hh, m: 30 });
  }
  const slotIndex = (start) => {
    const i = (start.getHours() - DAY_START_H) * 2 + (start.getMinutes() >= 30 ? 1 : 0);
    return i >= 0 && i < slots.length ? i : -1;
  };

  // Bucket appointments by (dayKey, slotIndex).
  const byCell = new Map();
  entries.forEach((e) => {
    const start = parseNaive(e.start_at);
    if (!start) return;
    const si = slotIndex(start);
    if (si < 0) return;
    const k = `${dayKey(start)}|${si}`;
    if (!byCell.has(k)) byCell.set(k, []);
    byCell.get(k).push({ start, e });
  });

  const todayKey = dayKey(new Date());
  const matrix = el("div", "cal-matrix");

  // Header row: corner over the gutter + 7 day headers.
  matrix.appendChild(el("div", "cal-corner"));
  days.forEach((d) => {
    const h = el("div", "cal-dayhead");
    if (dayKey(d) === todayKey) h.classList.add("today");
    h.appendChild(el("div", "dow", d.toLocaleDateString("en-US", { weekday: "short" })));
    h.appendChild(el("div", "dnum", String(d.getDate())));
    matrix.appendChild(h);
  });

  // One grid row per 30-min slot: hour label + 7 day cells (events stacked).
  slots.forEach((slot, si) => {
    const onHour = slot.m === 0;
    const gutter = el("div", "cal-hourcell" + (onHour ? "" : " half"), onHour ? fmtHour(slot.h) : "");
    matrix.appendChild(gutter);

    days.forEach((d) => {
      const cell = el("div", "cal-slot" + (onHour ? " hour" : ""));
      if (dayKey(d) === todayKey) cell.classList.add("today");
      const evs = (byCell.get(`${dayKey(d)}|${si}`) || []).sort((a, b) => a.start - b.start);
      evs.forEach(({ start, e }) => {
        const dur = Number(e.duration_minutes) || 30;
        const end = new Date(start.getTime() + dur * 60000);
        const c = specColor(e.specialty);
        const ev = el("div", "cal-event");
        // Scale height with duration so a 60-min visit visibly spans ~2 slots.
        ev.style.minHeight = Math.max(EVENT_BASE_PX, (dur / 30) * EVENT_BASE_PX) + "px";
        ev.style.setProperty("--ev-bg", c.bg);
        ev.style.setProperty("--ev-accent", c.accent);
        const range = `${hhmm(start)}–${hhmm(end)}`;
        ev.title = `${range} · ${e.patient_name} · ${e.provider_name} (${e.specialty})`;
        ev.appendChild(el("div", "etime", range));
        ev.appendChild(el("div", "epat", e.patient_name));
        ev.appendChild(el("div", "eprov", `${e.provider_name} · ${e.specialty}`));
        cell.appendChild(ev);
      });
      matrix.appendChild(cell);
    });
  });

  calGrid.replaceChildren(matrix);
  calGrid.scrollTop = scrollTop;
  calGrid.scrollLeft = scrollLeft;
}

async function loadCalendar() {
  const days = weekDays(weekOffset);
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

// Move the grid by `delta` weeks (or jump to 0 for "Today") and re-fetch.
function gotoWeek(offset) {
  weekOffset = offset;
  loadCalendar();
}

calPrev.addEventListener("click", () => gotoWeek(weekOffset - 1));
calNext.addEventListener("click", () => gotoWeek(weekOffset + 1));
calToday.addEventListener("click", () => gotoWeek(0));

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

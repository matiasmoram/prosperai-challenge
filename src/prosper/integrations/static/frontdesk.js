"use strict";

const listEl = document.getElementById("list");
const detailEl = document.getElementById("detail");
const calEl = document.getElementById("cal-view");
const mailView = document.getElementById("mail-view");
const tabMail = document.getElementById("tab-mail");
const tabCal = document.getElementById("tab-cal");

let mail = [];

const fmt = (ts) => new Date(ts * 1000).toLocaleString("en-US");

// Build a DOM element safely — text is always set via textContent,
// never via innerHTML, so record fields cannot inject markup (XSS-safe).
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function renderList() {
  listEl.replaceChildren();
  mail.forEach((m) => {
    const row = el("div", "row");
    row.appendChild(el("div", "k", m.kind.replace(/_/g, " ")));
    row.appendChild(el("div", "meta", m.subject + " · " + fmt(m.ts)));
    row.addEventListener("click", () => renderDetail(m));
    listEl.appendChild(row);
  });
}

function renderDetail(m) {
  detailEl.replaceChildren();
  const box = el("div", "email");
  // Build header line with textContent — no template interpolation into innerHTML
  const hd = el("div", "hd");
  hd.textContent = "To: " + m.to_label + "   ·   " + m.subject;
  box.appendChild(hd);
  box.appendChild(el("pre", null, m.body));
  const meta = el("div", "meta");
  meta.textContent =
    "Patient: " + m.patient_name + " · " + m.patient_phone + " · " + fmt(m.ts);
  box.appendChild(meta);
  detailEl.appendChild(box);
}

async function pollMail() {
  try {
    const r = await fetch("/frontdesk/mail");
    mail = (await r.json()).mail || [];
    renderList();
  } catch (_e) {
    // Transient network error — next tick retries automatically
  }
}

const isoDate = (d) => d.toISOString().slice(0, 10);

async function loadCalendar() {
  calEl.replaceChildren();
  const from = new Date();
  const to = new Date(Date.now() + 7 * 86400000);
  try {
    const r = await fetch(
      "/frontdesk/appointments?from=" + isoDate(from) + "&to=" + isoDate(to)
    );
    const entries = (await r.json()).entries || [];
    entries.forEach((e) => {
      const card = el("div", "card");
      card.appendChild(el("div", "when", new Date(e.start_at).toLocaleString("en-US")));
      card.appendChild(el("div", null, e.patient_name));
      card.appendChild(el("div", null, e.provider_name + " · " + e.specialty));
      if (e.notes) card.appendChild(el("div", "reason", "“" + e.notes + "”"));
      calEl.appendChild(card);
    });
    if (!entries.length) calEl.appendChild(el("p", null, "No upcoming appointments."));
  } catch (_e) {
    calEl.appendChild(el("p", null, "Could not load calendar."));
  }
}

tabMail.addEventListener("click", () => {
  tabMail.classList.add("active");
  tabCal.classList.remove("active");
  mailView.classList.remove("hidden");
  calEl.classList.add("hidden");
});

tabCal.addEventListener("click", () => {
  tabCal.classList.add("active");
  tabMail.classList.remove("active");
  mailView.classList.add("hidden");
  calEl.classList.remove("hidden");
  loadCalendar();
});

pollMail();
setInterval(pollMail, 2000);

// Operator Console front-end — vanilla JS, no build step.
//
// Consumes the Server-Sent Events stream exposed by `src/prosper/console/sse.py`
// and renders the split-pane UI defined in `index.html`.
//
// Design notes (mirrors §5 of the design spec):
// - One renderer per event type. Adding a 9th type means adding one
//   `case "<type>": ...` block.
// - The clinical (top) and dev (bottom) panes share the SAME event stream;
//   there is no second source of truth.
// - PII never enters this file in raw form — the bus enforces masking on
//   `_masked` payload fields, so anything `console.js` receives is already
//   safe to render verbatim.
// - All DOM mutations go through `document.getElementById` lookups against
//   ids defined in `index.html`. If the HTML is restructured, only the id
//   constants below need updating.

const STATE_PALETTE = {
  GREETING: "bg-sky-100 text-sky-700",
  IDENTIFY_PATIENT: "bg-indigo-100 text-indigo-700",
  REGISTER_PATIENT: "bg-violet-100 text-violet-700",
  CHOOSE_INTENT: "bg-amber-100 text-amber-700",
  BOOK_FLOW: "bg-emerald-100 text-emerald-700",
  CANCEL_FLOW: "bg-rose-100 text-rose-700",
  RESCHEDULE_FLOW: "bg-orange-100 text-orange-700",
  CONFIRM_BOOK: "bg-emerald-200 text-emerald-800",
  CONFIRM_CANCEL: "bg-rose-200 text-rose-800",
  CONFIRM_RESCHEDULE: "bg-orange-200 text-orange-800",
  END: "bg-slate-200 text-slate-700",
};

const HUMAN_ACTIVITY = {
  GREETING: "Greeting the caller…",
  IDENTIFY_PATIENT: "Identifying the caller…",
  REGISTER_PATIENT: "Registering a new patient…",
  CHOOSE_INTENT: "Asking what they need…",
  BOOK_FLOW: "Finding a time…",
  CANCEL_FLOW: "Looking up upcoming visits…",
  RESCHEDULE_FLOW: "Moving an existing visit…",
  CONFIRM_BOOK: "Confirming the booking…",
  CONFIRM_CANCEL: "Confirming the cancellation…",
  CONFIRM_RESCHEDULE: "Confirming the reschedule…",
  END: "Wrapping up.",
};

// Rolling latency window — we keep the last N durations per phase so
// the p50 displayed in the dev strip is meaningful and tracks the
// current call, not the lifetime average.
const LATENCY_WINDOW = 10;
const latencyHistory = new Map(); // phase → number[]

// Connection-state helpers — keep the live-dot colour and label honest.
// Dot colours: emerald = live, amber = replay/reconnecting, red = error.
function setConnState(state) {
  const dot = document.getElementById("live-dot");
  const label = document.getElementById("conn-label");
  const dotClasses = {
    live:         "live-dot inline-block w-2 h-2 rounded-full bg-emerald-500",
    replay:       "inline-block w-2 h-2 rounded-full bg-amber-400",
    reconnecting: "inline-block w-2 h-2 rounded-full bg-amber-400",
    error:        "inline-block w-2 h-2 rounded-full bg-red-500",
    done:         "inline-block w-2 h-2 rounded-full bg-slate-400",
    idle:         "inline-block w-2 h-2 rounded-full bg-slate-300",
  };
  const labels = {
    live:         "live",
    replay:       "replaying",
    reconnecting: "reconnecting…",
    error:        "connection error",
    done:         "replay complete",
    idle:         "no sessions yet — start a call",
  };
  if (dot) dot.className = dotClasses[state] || dotClasses.idle;
  if (label) label.textContent = labels[state] || state;
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function appendTo(id, child, opts = {}) {
  const el = document.getElementById(id);
  if (!el) return;
  // Drop the placeholder italic on first real append.
  const placeholder = el.querySelector(":scope > .italic");
  if (placeholder) placeholder.remove();
  el.appendChild(child);
  if (opts.cap) {
    while (el.children.length > opts.cap) el.removeChild(el.firstChild);
  }
  if (opts.scrollBottom) {
    el.scrollTop = el.scrollHeight;
  }
}

function renderStateChange(ev) {
  const { to_state, from_state, trigger } = ev.payload;
  const badge = document.getElementById("state-badge");
  if (badge) {
    // replaceAll handles multi-underscore state names added in future (e.g.
    // CONFIRM_RESCHEDULE_FLOW); the string-literal form only replaces the first.
    badge.textContent = (to_state || "?").toLowerCase().replaceAll("_", " ");
    badge.className = "state-badge text-xs font-semibold uppercase tracking-wide px-2.5 py-1 rounded-full " + (STATE_PALETTE[to_state] || "bg-slate-100 text-slate-600");
  }
  setText("activity-line", HUMAN_ACTIVITY[to_state] || `In ${to_state}…`);
  setText("fsm-state", to_state);
  setText("fsm-from", `← ${from_state} via ${trigger}`);
}

function renderPatient(ev) {
  const { name_masked, dob_year, phone_masked } = ev.payload;
  const el = document.getElementById("patient-card");
  if (!el) return;
  el.innerHTML = `
    <div class="font-semibold text-slate-800">${escapeHtml(name_masked)}</div>
    <div class="text-xs text-slate-500 mt-0.5">DOB ${dob_year || "—"}</div>
    <div class="text-xs text-slate-500 mt-0.5 mono">${escapeHtml(phone_masked)}</div>
    <div class="mt-3 inline-flex items-center text-xs text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded-full">
      <span class="w-1.5 h-1.5 rounded-full bg-emerald-500 mr-1.5"></span>identified
    </div>
  `;
}

function renderSlots(ev) {
  const { count, providers, first_date, last_date, recommended_specialty, recommended_duration_minutes } = ev.payload;
  const ul = document.getElementById("slots-list");
  if (!ul) return;
  ul.innerHTML = "";
  // Triage handoff context: show the routed specialty + visit length so a
  // receptionist/doctor sees why this caller is being booked where.
  if (recommended_specialty) {
    const rec = document.createElement("li");
    rec.className = "inline-flex items-center gap-1.5 text-xs font-semibold text-violet-700 bg-violet-50 px-2 py-0.5 rounded-full mb-1";
    const dur = recommended_duration_minutes ? ` · ${recommended_duration_minutes} min` : "";
    rec.textContent = `Triage → ${recommended_specialty}${dur}`;
    ul.appendChild(rec);
  }
  const summary = document.createElement("li");
  summary.className = "text-xs text-slate-500";
  summary.textContent = `${count} slot${count === 1 ? "" : "s"} · ${formatDateShort(first_date)} – ${formatDateShort(last_date)}`;
  ul.appendChild(summary);
  for (const p of providers.slice(0, 4)) {
    const li = document.createElement("li");
    li.className = "flex items-center gap-2";
    li.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-sky-500"></span><span class="mono text-xs text-slate-700">${escapeHtml(p)}</span>`;
    ul.appendChild(li);
  }
}

function renderToolStart(ev) {
  const { tool, args_redacted, call_id } = ev.payload;
  const rowHtml = `
    <td class="px-2 py-1 text-sky-300">${escapeHtml(tool)}</td>
    <td class="px-2 py-1 text-zinc-400 max-w-[200px] truncate" title="${escapeHtml(JSON.stringify(args_redacted))}">${escapeHtml(JSON.stringify(args_redacted))}</td>
    <td class="px-2 py-1 text-right text-zinc-500">…</td>
    <td class="px-2 py-1 text-zinc-500">running</td>
  `;
  // Idempotent: a reconnect/replay can re-deliver the same call_id. Reuse
  // the existing row instead of appending a duplicate (otherwise the
  // matching tool_call_end updates the FIRST row and the dupes stick on
  // "running"). Safe for live too — call_ids are unique within a session.
  const existing = document.getElementById(`tool-${call_id}`);
  if (existing) {
    existing.innerHTML = rowHtml;
    return;
  }
  const tr = document.createElement("tr");
  tr.id = `tool-${call_id}`;
  tr.className = "border-t border-zinc-800";
  tr.innerHTML = rowHtml;
  appendTo("tool-tbody", tr, { cap: 20 });
}

function renderToolEnd(ev) {
  const { tool, call_id, outcome, code, duration_ms } = ev.payload;
  const tr = document.getElementById(`tool-${call_id}`);
  if (!tr) return;
  const ms = duration_ms < 1 ? "<1" : Math.round(duration_ms).toString();
  const cells = tr.children;
  cells[2].textContent = ms;
  cells[2].className = "px-2 py-1 text-right mono " + (outcome === "ok" ? "text-emerald-400" : "text-amber-400");
  cells[3].innerHTML = outcome === "ok"
    ? '<span class="text-emerald-400">ok</span>'
    : `<span class="text-amber-400" title="${escapeHtml(code || "")}">err · ${escapeHtml(code || "?")}</span>`;
}

function renderTranscript(ev) {
  const { role, text, turn_id } = ev.payload;
  const isUser = role === "user";
  // Idempotent on (role, turn_id) so a replay/reconnect doesn't double the
  // transcript. Distinct ids within a live session keep every turn.
  const key = `turn-${role}-${turn_id}`;
  if (document.getElementById(key)) return;
  const li = document.createElement("li");
  li.id = key;
  li.className = "flex gap-2";
  li.innerHTML = `
    <span class="text-xs ${isUser ? "text-sky-700" : "text-emerald-700"} font-medium shrink-0 mono">${isUser ? "Caller" : "Bot   "} #${turn_id}</span>
    <span class="text-slate-700">${escapeHtml(text)}</span>
  `;
  appendTo("transcript-list", li, { cap: 24, scrollBottom: true });
}

function renderOutcome(ev) {
  const { outcome, details } = ev.payload;
  const banner = document.getElementById("outcome-banner");
  if (!banner) return;
  const palette = {
    booked: "bg-emerald-50 text-emerald-800 border-emerald-200",
    rescheduled: "bg-orange-50 text-orange-800 border-orange-200",
    cancelled: "bg-amber-50 text-amber-800 border-amber-200",
    refused: "bg-slate-100 text-slate-700 border-slate-200",
    abandoned: "bg-slate-100 text-slate-500 border-slate-200",
  };
  banner.className = "px-5 py-3 text-sm font-semibold border-t " + (palette[outcome] || palette.abandoned);
  banner.textContent = `Call ended · ${outcome.toUpperCase()} · ${details.turns} turns`;
  banner.classList.remove("hidden");
}

function renderInterrupted(ev) {
  // The caller talked over the bot. Surface it in the transcript stream so
  // the operator sees the bot was cut off mid-sentence (and what it had
  // managed to say). spoken_text is the bot audio that reached the caller.
  const { spoken_text, state, turn_id } = ev.payload;
  const key = `interrupt-${turn_id}`;
  if (document.getElementById(key)) return;
  const li = document.createElement("li");
  li.id = key;
  li.className = "flex gap-2 items-start";
  const heard = spoken_text ? `“${escapeHtml(spoken_text)}…”` : "(nothing heard)";
  li.innerHTML = `
    <span class="text-xs text-amber-700 font-medium shrink-0 mono">⚠ interrupted</span>
    <span class="text-amber-700 text-sm">caller cut the bot off in ${escapeHtml((state || "").toLowerCase().replaceAll("_", " "))} — bot had said ${heard}</span>
  `;
  appendTo("transcript-list", li, { cap: 24, scrollBottom: true });
}

function renderLatency(ev) {
  const { phase, duration_ms } = ev.payload;
  const arr = latencyHistory.get(phase) || [];
  arr.push(duration_ms);
  while (arr.length > LATENCY_WINDOW) arr.shift();
  latencyHistory.set(phase, arr);
  // Update only the strip — full-resolution numbers live in raw log.
  const llm = (latencyHistory.get("llm") || []).slice().sort((a, b) => a - b);
  const tool = [...latencyHistory.entries()]
    .filter(([k]) => k.startsWith("tool:"))
    .flatMap(([, v]) => v)
    .sort((a, b) => a - b);
  const p50 = (xs) => xs.length ? Math.round(xs[Math.floor((xs.length - 1) / 2)]) : null;
  const fmt = (v) => v === null ? "—" : `${v}ms`;
  setText("latency-strip", `llm ${fmt(p50(llm))} · tool ${fmt(p50(tool))} · ${arr.length} ${phase} samples`);
}

function appendRaw(ev) {
  const log = document.getElementById("raw-log");
  if (!log) return;
  const line = document.createElement("div");
  line.textContent = JSON.stringify(ev);
  log.appendChild(line);
  while (log.children.length > 200) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatDateShort(iso) {
  if (!iso) return "—";
  // Accept "2026-05-21T09:00:00" and "2026-05-21" — render "May 21".
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return iso;
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return `${months[parseInt(m[2], 10) - 1]} ${parseInt(m[3], 10)}`;
}

// ---- Entry point: pick a session and open the SSE stream. ----------------

function pickSessionFromPath() {
  // URL shape: `/console` (root) or `/console/<id>`. Extract <id> if present.
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts.length >= 2 && parts[0] === "console" && parts[1] !== "sessions") {
    return parts[1];
  }
  return null;
}

// Populate the session picker <select> from the /console/sessions API.
// Returns the id of the newest session, or null if none exist.
// On fetch failure, shows a retry banner and returns null.
async function loadSessions() {
  const picker = document.getElementById("session-picker");
  const errorBanner = document.getElementById("conn-error-banner");
  try {
    const r = await fetch("/console/sessions");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    // API returns [{id, mtime_ts}, …] sorted newest-first.
    const sessions = j.sessions || [];
    if (picker) {
      picker.innerHTML = "";
      if (sessions.length === 0) {
        const opt = document.createElement("option");
        opt.textContent = "no sessions yet";
        opt.disabled = true;
        picker.appendChild(opt);
      } else {
        for (const s of sessions) {
          const opt = document.createElement("option");
          opt.value = s.id;
          // Show id + human date derived from mtime.
          const d = new Date(s.mtime_ts * 1000);
          opt.textContent = `${s.id}  (${d.toLocaleDateString()} ${d.toLocaleTimeString()})`;
          picker.appendChild(opt);
        }
        picker.value = sessions[0].id;
      }
    }
    return sessions.length ? sessions[0].id : null;
  } catch (err) {
    // Distinguish genuine "no sessions" from a network/server failure.
    if (errorBanner) {
      const errText = document.getElementById("conn-error-text");
      if (errText) errText.textContent = `Could not load sessions: ${err.message}. Check the console server is running.`;
      errorBanner.classList.remove("hidden");
    }
    setConnState("error");
    return null;
  }
}

// Reveal/hide the amber "REPLAY" banner so a recorded session is never mistaken
// for a live call. When showing, label it with the selected session's id + time
// (read from the picker option text).
function toggleReplayBanner(on) {
  const banner = document.getElementById("replay-banner");
  if (!banner) return;
  if (!on) {
    banner.classList.add("hidden");
    return;
  }
  const meta = document.getElementById("replay-meta");
  const picker = document.getElementById("session-picker");
  if (meta && picker && picker.selectedIndex >= 0) {
    const opt = picker.options[picker.selectedIndex];
    meta.textContent = opt && opt.textContent ? ` · ${opt.textContent}` : "";
  }
  banner.classList.remove("hidden");
}

// Idle landing shown when /console is opened with no live call in progress.
// Replaces the "awaiting first turn…" placeholders that otherwise read like a
// call is starting. `hasSessions` toggles the replay hint.
function showIdleLanding(hasSessions) {
  setText("activity-line", "No live call in progress.");
  const transcript = document.getElementById("transcript-list");
  if (transcript) {
    const hint = hasSessions
      ? "No live call. Pick a recorded session above to replay it."
      : "No live call, and no recorded sessions yet.";
    transcript.replaceChildren();
    const li = document.createElement("li");
    li.className = "text-slate-400 italic";
    li.textContent = hint;
    transcript.appendChild(li);
  }
}

function connect(sessionId) {
  setText("session-label", `session ${sessionId}`);
  // Decide live vs replay: try live first.  Heuristic: if no events arrive
  // within 1.5 s, switch to the replay endpoint which reads from JSONL.
  let url = `/console/stream/${encodeURIComponent(sessionId)}`;
  let source = new EventSource(url);
  let receivedAny = false;

  const fallbackTimer = setTimeout(() => {
    if (!receivedAny) {
      source.close();
      url = `/console/replay/${encodeURIComponent(sessionId)}`;
      source = new EventSource(url);
      attach(source);
    }
  }, 1500);

  function attach(src) {
    src.onopen = () => {
      const isReplay = url.includes("replay");
      setConnState(isReplay ? "replay" : "live");
      toggleReplayBanner(isReplay);
    };
    src.onerror = () => setConnState("reconnecting");
    src.onmessage = (msg) => {
      receivedAny = true;
      clearTimeout(fallbackTimer);
      try {
        const ev = JSON.parse(msg.data);
        appendRaw(ev);
        dispatch(ev);
      } catch (err) {
        // Bad frame is silently dropped — operator UI must not error.
      }
    };
    // Replay sends a terminal `replay_complete` named event once the JSONL
    // is exhausted. Close the source on it — otherwise the browser treats
    // the closed stream as a dropped connection and auto-reconnects,
    // replaying the whole session again and duplicating every row.
    src.addEventListener("replay_complete", () => {
      src.close();
      setConnState("done");
    });
  }
  attach(source);
}

function dispatch(ev) {
  switch (ev.type) {
    case "state_change":       return renderStateChange(ev);
    case "patient_identified": return renderPatient(ev);
    case "slots_offered":      return renderSlots(ev);
    case "tool_call_start":    return renderToolStart(ev);
    case "tool_call_end":      return renderToolEnd(ev);
    case "transcript_turn":    return renderTranscript(ev);
    case "outcome":            return renderOutcome(ev);
    case "latency_tick":       return renderLatency(ev);
    case "turn_interrupted":   return renderInterrupted(ev);
  }
}

// Raw-log toggle.
document.addEventListener("DOMContentLoaded", async () => {
  const btn = document.getElementById("raw-toggle");
  const raw = document.getElementById("raw-log");
  const ph = document.getElementById("raw-placeholder");
  if (btn && raw && ph) {
    btn.addEventListener("click", () => {
      const wasHidden = raw.classList.contains("hidden");
      raw.classList.toggle("hidden");
      ph.classList.toggle("hidden");
      btn.textContent = wasHidden ? "hide" : "show";
    });
  }

  // Session picker — navigate when operator selects a different session.
  const picker = document.getElementById("session-picker");
  if (picker) {
    picker.addEventListener("change", () => {
      const sid = picker.value;
      if (sid) window.location.href = `/console/${encodeURIComponent(sid)}`;
    });
  }

  // Dismiss button for the connection-error banner.
  const dismissBtn = document.getElementById("conn-error-dismiss");
  const errorBanner = document.getElementById("conn-error-banner");
  if (dismissBtn && errorBanner) {
    dismissBtn.addEventListener("click", () => errorBanner.classList.add("hidden"));
  }

  // Always populate the picker; only AUTO-CONNECT when the session was named
  // explicitly in the URL (`/console/<id>`). Opening `/console` with no id must
  // NOT auto-replay the newest recording — that looked like a live call when
  // nothing was happening. Default = idle landing; replay is opt-in via picker.
  const newest = await loadSessions();
  if (errorBanner && !errorBanner.classList.contains("hidden")) return; // fetch failed
  const fromPath = pickSessionFromPath();
  if (fromPath) {
    if (picker) picker.value = fromPath;
    connect(fromPath);
  } else {
    setConnState("idle");
    showIdleLanding(Boolean(newest));
  }
});

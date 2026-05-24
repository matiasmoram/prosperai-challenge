// Custom call UI for the Prosper Health voice agent.
//
// Talks WebRTC directly to the Pipecat runner on :7860 (POST /api/offer),
// bypassing the prebuilt UI that ships with pipecat-ai-small-webrtc-prebuilt.
// CORS on the runner is `allow_origins=["*"]`, so a page served from
// :7861/call can dial the bot at :7860 without proxying.
//
// What we render:
//   - "Tap to call" landing
//   - Connecting → Live → Ended state machine on the pill + rings
//   - Live timer
//   - Animated mouth on the avatar, driven by a WebAudio AnalyserNode on
//     the bot's remote audio track. No visemes — just loudness → height.
//     Cheap, surprisingly believable for demo.
//
// Why not RTVI events for mouth motion? The remote audio stream already
// IS the bot speech; running an analyser on it gives us tighter sync
// than waiting for a control-channel "tts-started" event, with zero
// extra plumbing.

const BOT_ORIGIN = window.PROSPER_BOT_ORIGIN || "http://127.0.0.1:7860";

const els = {
  phone: document.querySelector(".phone"),
  pill: document.getElementById("state-pill"),
  pillText: document.getElementById("state-text"),
  timer: document.getElementById("timer"),
  hangup: document.getElementById("btn-hangup"),
  dial: document.getElementById("btn-dial"),
  dialLabel: document.getElementById("dial-label"),
  mute: document.getElementById("btn-mute"),
  audio: document.getElementById("remote-audio"),
  mouth: document.getElementById("mouth"),
  avatarWrap: document.getElementById("avatar-wrap"),
  hint: document.getElementById("hint"),
  botOrigin: document.getElementById("bot-origin"),
};

if (els.botOrigin) els.botOrigin.textContent = BOT_ORIGIN;

let pc = null;            // RTCPeerConnection
let micStream = null;     // local mic MediaStream
let audioCtx = null;      // shared AudioContext
let analyser = null;      // AnalyserNode tapping the remote stream
let analyserBuf = null;   // Uint8Array reused per frame
let rafId = null;         // animation frame for mouth driver
let timerInt = null;      // setInterval for the live timer
let timerStart = 0;
let muted = false;

// Dial button labels per state — green = fresh call, orange = retry.
// Must be declared before setPhone() is first called (const is not hoisted).
const DIAL_LABELS = {
  idle: "Tap to call Sarah",
  error: "Try again",
  ended: "Call again",
};

setPhone("idle");
setState("idle", "Ready");

els.dial.addEventListener("click", startCall);
els.hangup.addEventListener("click", () => hangup("user"));
els.mute.addEventListener("click", toggleMute);

window.addEventListener("beforeunload", () => hangup("nav"));

// ----------------------------------------------------------------- state

function setPhone(stage) {
  els.phone.classList.remove("idle", "connecting", "live", "ended", "error");
  els.phone.classList.add(stage);
  // Update dial button label to match context — avoids the mismatch of a
  // green "Tap to call Sarah" button appearing after an error.
  if (els.dialLabel && DIAL_LABELS[stage]) {
    els.dialLabel.textContent = DIAL_LABELS[stage];
  }
}

function setState(stage, label) {
  els.pill.classList.remove("connecting", "live", "ended", "error");
  if (stage !== "idle") els.pill.classList.add(stage);
  els.pillText.textContent = label;
  // Avatar rings animate in both connecting (slow sweep) and live (pulse).
  if (stage === "live") {
    els.avatarWrap.classList.remove("connecting");
    els.avatarWrap.classList.add("live");
  } else if (stage === "connecting") {
    els.avatarWrap.classList.remove("live");
    els.avatarWrap.classList.add("connecting");
  } else {
    els.avatarWrap.classList.remove("live", "connecting");
  }
}

function startTimer() {
  timerStart = Date.now();
  els.timer.textContent = "00:00";
  timerInt = setInterval(() => {
    const s = Math.floor((Date.now() - timerStart) / 1000);
    const mm = String(Math.floor(s / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    els.timer.textContent = `${mm}:${ss}`;
  }, 500);
}

function stopTimer() {
  if (timerInt) { clearInterval(timerInt); timerInt = null; }
}

// ----------------------------------------------------------------- WebRTC

async function startCall() {
  setPhone("connecting");
  setState("connecting", "Connecting…");
  els.hint.textContent = "Requesting microphone…";
  // Create the AudioContext HERE, inside the dial-button click handler. An
  // AudioContext constructed outside a user gesture (e.g. later in ontrack)
  // starts `suspended` under Chrome's autoplay policy, so the mouth analyser
  // would read flat silence and never animate. Resume() in a gesture is the
  // documented escape hatch.
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") await audioCtx.resume();
  } catch (_) { audioCtx = null; }
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    }, video: false });
  } catch (e) {
    failed(`Microphone access denied. Allow mic access in your browser settings, then try again.`, true);
    return;
  }
  els.hint.textContent = "Calling Sarah…";

  pc = new RTCPeerConnection({
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
  });

  // Listen to the bot's reply track.
  pc.ontrack = (ev) => {
    const [stream] = ev.streams;
    if (!stream) return;
    els.audio.srcObject = stream;
    startMouthAnalyser(stream);
  };

  pc.onconnectionstatechange = () => {
    const s = pc.connectionState;
    if (s === "connected") {
      setPhone("live");
      setState("live", "Live");
      els.hint.textContent = "Connected. Speak naturally.";
      els.mute.disabled = false;
      startTimer();
    } else if (s === "failed") {
      failed("Connection failed");
    } else if (s === "disconnected" || s === "closed") {
      // Browser-initiated drop — treat as ended.
      if (els.phone.classList.contains("live")) hangup("remote");
    }
  };

  // Outbound mic.
  for (const track of micStream.getAudioTracks()) pc.addTrack(track, micStream);
  // Make sure we get a recv transceiver even if pipecat creates the sendrecv pair lazily.
  pc.addTransceiver("audio", { direction: "recvonly" });

  // The pipecat-react reference client also opens a data channel for RTVI
  // messages; do the same so the bot's RTVIProcessor handshake completes.
  try { pc.createDataChannel("rtvi-ai"); } catch (_) { /* harmless */ }

  let offer;
  try {
    offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
  } catch (e) {
    failed(`Could not create offer: ${e.message}`);
    return;
  }

  // Wait briefly for ICE gathering — simpler than the candidate-trickle path,
  // adequate for localhost demo where gathering completes in <300ms.
  await waitForIce(pc, 1500);

  let answer;
  try {
    const r = await fetch(`${BOT_ORIGIN}/api/offer`, {
      method: "POST",
      mode: "cors",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sdp: pc.localDescription.sdp,
        type: pc.localDescription.type,
        request_data: {},
      }),
    });
    if (!r.ok) {
      failed(`Bot rejected offer (${r.status}). Is \`make bot\` running?`);
      return;
    }
    answer = await r.json();
  } catch (e) {
    failed(`Cannot reach bot on ${BOT_ORIGIN}: ${e.message}`);
    return;
  }

  try {
    await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
  } catch (e) {
    failed(`Bad answer from bot: ${e.message}`);
  }
}

async function waitForIce(pc, timeoutMs) {
  if (pc.iceGatheringState === "complete") return;
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener("icegatheringstatechange", check);
      clearTimeout(t);
      resolve();
    };
    const check = () => { if (pc.iceGatheringState === "complete") done(); };
    pc.addEventListener("icegatheringstatechange", check);
    const t = setTimeout(done, timeoutMs);
  });
}

// isMicError: mic-permission denials need the user to act (open settings,
// grant permission) — auto-dismissing the message before they can read it
// gives no path forward. Network/bot errors are transient so they still
// auto-reset to let the user retry.
function failed(reason, isMicError = false) {
  els.hint.textContent = reason;
  setState("error", "Failed");
  setPhone("error");
  cleanup();
  if (!isMicError) {
    // Allow another attempt after a readable pause.
    setTimeout(() => {
      setPhone("idle");
      setState("idle", "Ready");
      els.hint.textContent = `Bot must be running on ${BOT_ORIGIN}.`;
    }, 8000);
  }
  // Mic-permission errors stay until the user acts — clicking "Tap to call"
  // again will re-request permission (or succeed if they've granted it).
}

function hangup(_origin) {
  if (!pc) return;
  setState("ended", "Call ended");
  setPhone("ended");
  cleanup();
  // 3 s lets the operator read the final timer and "Call ended" pill before
  // the UI resets to idle. 1.5 s was too brief to register.
  setTimeout(() => {
    setPhone("idle");
    setState("idle", "Ready");
    els.timer.textContent = "00:00";
    els.hint.textContent = `Bot must be running on ${BOT_ORIGIN}.`;
  }, 3000);
}

function cleanup() {
  stopTimer();
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
  if (analyser) { try { analyser.disconnect(); } catch (_) {} analyser = null; }
  if (audioCtx) { try { audioCtx.close(); } catch (_) {} audioCtx = null; }
  if (pc) { try { pc.close(); } catch (_) {} pc = null; }
  if (micStream) { for (const t of micStream.getTracks()) t.stop(); micStream = null; }
  els.mute.disabled = true;
  els.mute.setAttribute("aria-pressed", "false");
  if (els.mouth) els.mouth.style.setProperty("--mouth-open", "0px");
  muted = false;
  els.mute.classList.remove("active");
}

function toggleMute() {
  if (!micStream) return;
  muted = !muted;
  for (const t of micStream.getAudioTracks()) t.enabled = !muted;
  els.mute.classList.toggle("active", muted);
  els.mute.setAttribute("aria-pressed", String(muted));
}

// ------------------------------------------------------------ mouth driver

function startMouthAnalyser(stream) {
  try {
    // Reuse the gesture-created context from startCall(); only build one as
    // a fallback (it may be suspended, but better than crashing).
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (audioCtx.state === "suspended") audioCtx.resume();
    const source = audioCtx.createMediaStreamSource(stream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.6;
    source.connect(analyser);
    analyserBuf = new Uint8Array(analyser.frequencyBinCount);
    const MAX_OPEN_PX = 26;     // mouth full-open height (in px)
    const NOISE_FLOOR = 0.04;   // ignore tiny ambient buzz
    const drive = () => {
      // Teardown (hangup / remote drop) nulls `analyser` and cancels the
      // frame, but a frame can already be in flight — guard so the last
      // tick after cleanup doesn't throw on a null analyser.
      if (!analyser) return;
      analyser.getByteTimeDomainData(analyserBuf);
      // RMS over the 0-255 PCM-ish buffer → 0..1
      let sumSq = 0;
      for (let i = 0; i < analyserBuf.length; i++) {
        const v = (analyserBuf[i] - 128) / 128;
        sumSq += v * v;
      }
      const rms = Math.sqrt(sumSq / analyserBuf.length);
      const scaled = Math.max(0, rms - NOISE_FLOOR) / (1 - NOISE_FLOOR);
      // Easing — mouths look better with a soft curve than linear.
      const eased = Math.pow(scaled, 0.6);
      const px = Math.min(MAX_OPEN_PX, Math.round(eased * MAX_OPEN_PX * 1.6));
      els.mouth.style.setProperty("--mouth-open", `${px}px`);
      rafId = requestAnimationFrame(drive);
    };
    rafId = requestAnimationFrame(drive);
  } catch (e) {
    // Mouth animation is a nice-to-have; never break the call for it.
    console.warn("Mouth analyser failed:", e);
  }
}

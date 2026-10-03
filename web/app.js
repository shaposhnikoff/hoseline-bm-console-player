const elements = Object.fromEntries(
  ["feed-state", "theme-button", "active-count", "player-title", "player-status", "talkgroup", "add-group", "selected-groups", "play-button", "volume", "record", "speaker-callsign", "speaker-links", "speaker-qrz", "speaker-radioid", "speaker-details", "speaker-phonetic", "audio-bars", "updated-at", "country-filter", "group-filter", "regex", "favorites-only", "filter-error", "call-grid", "empty-state"]
    .map((id) => [id, document.getElementById(id)]),
);

const REGIONS = {
  202: ["Greece", "🇬🇷"], 206: ["Belgium", "🇧🇪"], 208: ["France", "🇫🇷"],
  214: ["Spain", "🇪🇸"], 216: ["Hungary", "🇭🇺"], 218: ["Bosnia and Herzegovina", "🇧🇦"],
  219: ["Croatia", "🇭🇷"], 222: ["Italy", "🇮🇹"], 228: ["Switzerland", "🇨🇭"],
  230: ["Czechia", "🇨🇿"], 232: ["Austria", "🇦🇹"], 234: ["United Kingdom", "🇬🇧"],
  235: ["United Kingdom", "🇬🇧"], 238: ["Denmark", "🇩🇰"], 240: ["Sweden", "🇸🇪"],
  242: ["Norway", "🇳🇴"], 244: ["Finland", "🇫🇮"], 250: ["Russia", "🇷🇺"],
  255: ["Ukraine", "🇺🇦"], 260: ["Poland", "🇵🇱"], 262: ["Germany", "🇩🇪"],
  263: ["Germany", "🇩🇪"], 264: ["Germany", "🇩🇪"], 268: ["Portugal", "🇵🇹"],
  272: ["Ireland", "🇮🇪"], 274: ["Iceland", "🇮🇸"], 276: ["Estonia", "🇪🇪"],
  284: ["Bulgaria", "🇧🇬"], 286: ["Türkiye", "🇹🇷"], 293: ["Slovenia", "🇸🇮"],
  294: ["North Macedonia", "🇲🇰"], 302: ["Canada", "🇨🇦"], 330: ["Puerto Rico", "🇵🇷"],
  334: ["Mexico", "🇲🇽"], 401: ["Kazakhstan", "🇰🇿"], 460: ["China", "🇨🇳"],
  466: ["Taiwan", "🇹🇼"], 502: ["Malaysia", "🇲🇾"], 505: ["Australia", "🇦🇺"],
  510: ["Indonesia", "🇮🇩"], 515: ["Philippines", "🇵🇭"], 520: ["Thailand", "🇹🇭"],
  530: ["New Zealand", "🇳🇿"], 655: ["South Africa", "🇿🇦"], 716: ["Peru", "🇵🇪"],
  724: ["Brazil", "🇧🇷"], 730: ["Chile", "🇨🇱"], 732: ["Colombia", "🇨🇴"],
  734: ["Venezuela", "🇻🇪"],
};

function regionFor(call) {
  if (call.group < 100) return [call.group === 93 ? "North America" : "Worldwide", "🌍"];
  const prefix = Number(String(call.group).slice(0, 3));
  if (prefix >= 310 && prefix <= 319) return ["United States", "🇺🇸"];
  return REGIONS[prefix] || [call.operator.country || "Other", "🌍"];
}

const state = {
  sessions: [],
  labels: {},
  favorites: new Set(JSON.parse(localStorage.getItem("hoseline-favorites") || "[]")),
  groups: new Set([91]),
  sockets: new Map(),
  reconnectTimers: new Map(),
  speakers: new Map(),
  audio: null,
  worklet: null,
  gain: null,
  listening: false,
  audioPromise: null,
  generation: 0,
};

function setStatus(message, type = "") {
  elements["player-status"].textContent = message;
  elements["player-status"].className = `status-pill ${type}`;
}

function setSpeaker(operator, fallback = "Waiting for a call", group = null) {
  const callsign = operator?.callsign || fallback;
  elements["speaker-callsign"].textContent = group ? `TG ${group} · ${callsign}` : callsign;
  const profileCallsign = typeof operator?.callsign === "string" ? operator.callsign.trim().toUpperCase() : "";
  const hasProfile = /^[A-Z0-9/]{2,24}$/.test(profileCallsign);
  elements["speaker-links"].hidden = !hasProfile;
  if (hasProfile) {
    const encoded = encodeURIComponent(profileCallsign);
    elements["speaker-qrz"].href = `https://www.qrz.com/db/${encoded}`;
    elements["speaker-qrz"].setAttribute("aria-label", `Open ${profileCallsign} on QRZ.com`);
    elements["speaker-radioid"].href = `https://radioid.net/database/view?callsign=${encoded}`;
    elements["speaker-radioid"].setAttribute("aria-label", `Open ${profileCallsign} on RadioID`);
  } else {
    elements["speaker-qrz"].removeAttribute("href");
    elements["speaker-radioid"].removeAttribute("href");
  }
  const location = [operator?.city, operator?.country].filter(Boolean).join(", ");
  elements["speaker-details"].textContent = [operator?.name, location].filter(Boolean).join(" · ") || "No operator details yet";
  elements["speaker-phonetic"].textContent = operator?.phonetic || "";
}

function make(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function formatTime(seconds) {
  const minutes = Math.floor(seconds / 60).toString().padStart(2, "0");
  return `${minutes}:${(seconds % 60).toString().padStart(2, "0")}`;
}

function matchingSessions() {
  const country = elements["country-filter"].value;
  const query = elements["group-filter"].value.trim();
  const regexMode = elements.regex.checked;
  let matcher = null;
  elements["filter-error"].hidden = true;
  if (regexMode && query) {
    try {
      matcher = new RegExp(query, "i");
    } catch (_error) {
      elements["filter-error"].textContent = "Invalid regular expression";
      elements["filter-error"].hidden = false;
      return [];
    }
  }
  return state.sessions.filter((call) => {
    if (country !== "all" && regionFor(call)[0] !== country) return false;
    if (elements["favorites-only"].checked && !state.favorites.has(call.group)) return false;
    if (!query) return true;
    const searchable = `${call.group} ${state.labels[call.group] || ""} ${call.operator.callsign} ${call.operator.name} ${call.alias}`;
    return matcher ? matcher.test(searchable) : searchable.toLowerCase().includes(query.toLowerCase());
  });
}

function renderCards() {
  const calls = matchingSessions();
  const grid = elements["call-grid"];
  grid.replaceChildren();
  elements["empty-state"].hidden = calls.length !== 0;
  for (const call of calls) {
    const card = make("article", `call-card${call.active ? " active" : ""}${state.groups.has(call.group) ? " selected" : ""}`);
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.setAttribute("aria-label", `Listen to talkgroup ${call.group}`);
    const top = make("div", "card-top");
    top.append(make("strong", "", `${regionFor(call)[1]} ${call.group}`), make("span", "card-time", call.active ? `${formatTime(call.elapsed)}  ●` : `${formatTime(call.age)} ago`));
    const name = make("div", "card-label", state.labels[call.group] || `Talkgroup ${call.group}`);
    const speaker = make("div", "card-speaker", call.operator.callsign || `DMR ${call.source}`);
    if (call.operator.name) speaker.textContent += ` (${call.operator.name})`;
    const bottom = make("div", "card-bottom");
    bottom.append(make("span", "", call.alias || call.operator.country || "No talker alias"));
    const favorite = make("button", `card-favorite${state.favorites.has(call.group) ? " saved" : ""}`, state.favorites.has(call.group) ? "★" : "☆");
    favorite.type = "button";
    favorite.setAttribute("aria-label", `Toggle favorite TG ${call.group}`);
    favorite.addEventListener("click", (event) => {
      event.stopPropagation();
      if (state.favorites.has(call.group)) state.favorites.delete(call.group);
      else state.favorites.add(call.group);
      localStorage.setItem("hoseline-favorites", JSON.stringify([...state.favorites]));
      renderCards();
    });
    bottom.append(favorite);
    card.append(top, name, speaker, bottom);
    card.addEventListener("click", () => {
      elements.talkgroup.value = call.group;
      if (addGroup(call.group) && !state.listening) startListening();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    card.addEventListener("keydown", (event) => {
      if (event.target === card && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        card.click();
      }
    });
    grid.append(card);
  }
}

function renderCountries() {
  const selected = elements["country-filter"].value;
  const countries = [...new Set(state.sessions.map((call) => regionFor(call)[0]))].sort();
  elements["country-filter"].replaceChildren(new Option("Everything", "all"), ...countries.map((country) => new Option(country, country)));
  elements["country-filter"].value = countries.includes(selected) ? selected : "all";
}

function updateSpeakerFromSessions() {
  const active = [...state.speakers.entries()].at(-1);
  if (!state.listening || !active) return;
  const [group, speaker] = active;
  const call = state.sessions.find((item) => item.group === group && item.source === speaker.source);
  setSpeaker(call?.operator || speaker.operator, `DMR ${speaker.source}`, group);
}

async function refreshState() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error("Feed request failed");
    const data = await response.json();
    state.sessions = data.sessions;
    state.labels = data.talkgroups;
    elements["feed-state"].textContent = data.feedConnected ? "Last Heard connected" : "Last Heard reconnecting";
    elements["feed-state"].classList.toggle("connected", data.feedConnected);
    elements["active-count"].textContent = state.sessions.filter((call) => call.active).length;
    elements["updated-at"].textContent = `Updated ${new Date().toLocaleTimeString()}`;
    renderCountries();
    renderCards();
    updateSpeakerFromSessions();
  } catch (_error) {
    elements["feed-state"].textContent = "Dashboard connection lost";
    elements["feed-state"].classList.remove("connected");
  }
}

async function prepareAudio() {
  if (state.audio) {
    await state.audio.resume();
    return;
  }
  if (!state.audioPromise) {
    state.audioPromise = (async () => {
      const context = new AudioContext();
      try {
        await context.audioWorklet.addModule("/static/audio-worklet.js");
        const worklet = new AudioWorkletNode(context, "hoseline-audio", { outputChannelCount: [2] });
        const gain = context.createGain();
        gain.gain.value = Number(elements.volume.value) / 100;
        worklet.connect(gain).connect(context.destination);
        await context.resume();
        state.audio = context;
        state.worklet = worklet;
        state.gain = gain;
      } catch (error) {
        await context.close();
        throw error;
      }
    })();
  }
  try { await state.audioPromise; }
  finally { state.audioPromise = null; }
}

function renderGroups() {
  const selected = elements["selected-groups"];
  selected.replaceChildren();
  for (const group of state.groups) {
    const chip = make("span", "group-chip", `TG ${group}`);
    const remove = make("button", "", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove TG ${group}`);
    remove.addEventListener("click", () => removeGroup(group));
    chip.append(remove);
    selected.append(chip);
  }
  elements["player-title"].textContent = `Talkgroups ${[...state.groups].join(", ") || "—"}`;
  renderCards();
}

function addGroup(group = Number(elements.talkgroup.value)) {
  if (!Number.isInteger(group) || group <= 90 || group > 99999999) {
    setStatus("Invalid TG", "error");
    return false;
  }
  if (!state.groups.has(group) && state.groups.size >= 4) {
    setStatus("Maximum 4 TGs", "error");
    return false;
  }
  const added = !state.groups.has(group);
  state.groups.add(group);
  renderGroups();
  if (added && state.listening) connectGroup(group);
  return true;
}

function removeGroup(group) {
  state.groups.delete(group);
  clearTimeout(state.reconnectTimers.get(group));
  state.reconnectTimers.delete(group);
  const socket = state.sockets.get(group);
  if (socket) {
    socket.onclose = null;
    socket.close();
    state.sockets.delete(group);
  }
  state.speakers.delete(group);
  state.worklet?.port.postMessage({ type: "reset", key: group });
  renderGroups();
  updateSpeakingState();
  if (state.listening && state.groups.size === 0) stopListening();
  else if (state.listening) setStatus(`Listening to ${state.groups.size} TG${state.groups.size === 1 ? "" : "s"}`, "playing");
}

function stopListening() {
  state.generation++;
  state.listening = false;
  for (const timer of state.reconnectTimers.values()) clearTimeout(timer);
  state.reconnectTimers.clear();
  for (const socket of state.sockets.values()) {
    socket.onclose = null;
    socket.close();
  }
  state.sockets.clear();
  state.speakers.clear();
  state.worklet?.port.postMessage({ type: "reset" });
  elements["play-button"].innerHTML = "▶ <span>Listen</span>";
  elements["audio-bars"].classList.remove("active");
  setStatus("Stopped");
  setSpeaker(null, "Waiting for a call");
  renderCards();
}

function updateSpeakingState() {
  const active = [...state.speakers.entries()].at(-1);
  elements["audio-bars"].classList.toggle("active", Boolean(active));
  if (active) updateSpeakerFromSessions();
  else setSpeaker(null, "Waiting for the next call");
}

function connectGroup(group) {
  if (!state.listening || state.sockets.has(group)) return;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${location.host}/ws/audio?tg=${group}&record=${elements.record.checked ? "1" : "0"}`;
  const socket = new WebSocket(url);
  socket.binaryType = "arraybuffer";
  state.sockets.set(group, socket);
  socket.onmessage = (event) => {
    if (state.sockets.get(group) !== socket || !state.listening) return;
    if (typeof event.data !== "string") {
      state.worklet.port.postMessage({ type: "audio", key: group, buffer: event.data }, [event.data]);
      return;
    }
    const message = JSON.parse(event.data);
    if (message.type === "status") setStatus(`Listening to ${state.groups.size} TG${state.groups.size === 1 ? "" : "s"}`, "playing");
    if (message.type === "call-start") {
      state.worklet.port.postMessage({ type: "reset", key: group });
      state.speakers.delete(group);
      state.speakers.set(group, { source: message.source, operator: message.operator });
      setSpeaker(message.operator, message.source ? `DMR ${message.source}` : "Unknown speaker", group);
      elements["audio-bars"].classList.add("active");
    }
    if (message.type === "call-end") {
      state.worklet.port.postMessage({ type: "flush", key: group });
      state.speakers.delete(group);
      updateSpeakingState();
    }
    if (message.type === "error") setStatus(message.message, "error");
  };
  socket.onclose = () => {
    if (state.sockets.get(group) !== socket || !state.listening) return;
    state.sockets.delete(group);
    state.speakers.delete(group);
    state.worklet?.port.postMessage({ type: "reset", key: group });
    updateSpeakingState();
    setStatus(`TG ${group} reconnecting…`, "error");
    const timer = setTimeout(() => {
      state.reconnectTimers.delete(group);
      if (state.listening && state.groups.has(group)) connectGroup(group);
    }, 3000);
    state.reconnectTimers.set(group, timer);
  };
}

async function startListening(addInput = true) {
  if (state.listening) stopListening();
  if (addInput && !addGroup()) return;
  if (state.groups.size === 0) {
    setStatus("Add a TG first", "error");
    return;
  }
  const generation = ++state.generation;
  try {
    await prepareAudio();
  } catch (_error) {
    if (generation === state.generation) setStatus("Audio unavailable", "error");
    return;
  }
  if (generation !== state.generation || state.groups.size === 0) return;
  state.listening = true;
  elements["play-button"].innerHTML = "■ <span>Stop</span>";
  setSpeaker(null, "Waiting for a call");
  setStatus("Connecting…");
  for (const group of state.groups) connectGroup(group);
  renderCards();
}

elements["play-button"].addEventListener("click", () => state.listening ? stopListening() : startListening());
elements["add-group"].addEventListener("click", () => addGroup());
elements.talkgroup.addEventListener("keydown", (event) => { if (event.key === "Enter") addGroup(); });
elements.record.addEventListener("change", () => { if (state.listening) startListening(false); });
elements.volume.addEventListener("input", () => { if (state.gain) state.gain.gain.value = Number(elements.volume.value) / 100; });
for (const id of ["country-filter", "group-filter", "regex", "favorites-only"]) elements[id].addEventListener("input", renderCards);
elements["theme-button"].addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("hoseline-theme", next);
});
document.documentElement.dataset.theme = localStorage.getItem("hoseline-theme") || "light";
renderGroups();
refreshState();
setInterval(refreshState, 3000);

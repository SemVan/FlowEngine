// Bootstraps the store, opens WS, wires panels that exist on the current page.

import { createStore } from "./store.js";
import { connectWs } from "./ws.js";
import { api } from "./api.js";
import { mountJog } from "./panels/jog.js";
import { mountStatusBadge } from "./panels/status.js";
import { mountLog } from "./panels/log.js";

const store = createStore({
  controllerState: "unknown",
  controllerDetail: "",
  positions: {},
  endstops: {},
  log: [],
  wsConnected: false,
  queueDepth: 0,
  motionEnabled: false,
  port: null,
  baud: null,
});

connectWs(store);
mountStatusBadge(store);
mountLog(store);

const jogTbody = document.getElementById("jog-tbody");
if (jogTbody) mountJog(store, jogTbody, document.querySelector(".jog-panel"));

const abortBtn = document.getElementById("abort-btn");
abortBtn?.addEventListener("click", async () => {
  abortBtn.disabled = true;
  try { await api.stop(); }
  catch (e) { alert("Abort failed: " + e.message); }
  finally { abortBtn.disabled = false; }
});

// Surface an initial state snapshot so the UI paints even before the first WS message.
api.state().then((s) => {
  store.set({
    controllerState: s.state,
    controllerDetail: s.detail,
    positions: s.positions,
    queueDepth: s.queue_depth,
    motionEnabled: s.motion_enabled,
    port: s.port,
    baud: s.baud,
  });
}).catch(() => {});

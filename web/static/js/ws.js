// WebSocket client with auto-reconnect and exponential backoff.
// Dispatches typed messages into the store.

export function connectWs(store) {
  let backoff = 500;
  const max = 8000;

  function open() {
    const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
    const ws = new WebSocket(url);
    ws.addEventListener("open", () => {
      backoff = 500;
      store.set({ wsConnected: true });
    });
    ws.addEventListener("close", () => {
      store.set({ wsConnected: false });
      setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, max);
    });
    ws.addEventListener("error", () => ws.close());
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      dispatch(store, msg);
    });
  }
  open();
}

function dispatch(store, msg) {
  switch (msg.type) {
    case "state":
      store.set({ controllerState: msg.state, controllerDetail: msg.detail || "" });
      break;
    case "position":
      store.update((s) => ({ ...s, positions: { ...s.positions, ...(msg.positions || {}) }, positionSettled: !!msg.settled }));
      break;
    case "endstops":
      store.set({ endstops: msg.triggered });
      break;
    case "pressure":
      store.update((s) => ({ ...s, pressure: { value: msg.value, units: msg.units, t: msg.t_ms } }));
      break;
    case "log":
      store.update((s) => {
        const log = (s.log || []).slice(-199);
        log.push({ level: msg.level, message: msg.message, t: msg.t_ms });
        return { ...s, log };
      });
      break;
    case "queue":
      store.set({ queueDepth: msg.depth });
      break;
    default:
      console.warn("unknown ws message:", msg);
  }
}

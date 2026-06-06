// Top-bar status badge: connection dot + state label.

export function mountStatusBadge(store) {
  const dot = document.getElementById("connection-dot");
  const label = document.getElementById("state-label");
  if (!dot || !label) return;

  store.subscribe((state) => {
    let klass = "dot dot-unknown";
    if (!state.wsConnected) klass = "dot dot-disconnected";
    else if (state.controllerState === "connected_idle") klass = "dot dot-connected";
    else if (state.controllerState === "disconnected") klass = "dot dot-disconnected";
    else if (["homing", "moving", "aborting"].includes(state.controllerState)) klass = "dot dot-busy";
    else if (state.controllerState === "errored") klass = "dot dot-disconnected";
    dot.className = klass;
    label.textContent = state.controllerState + (state.controllerDetail ? ` — ${state.controllerDetail}` : "");
  });
}

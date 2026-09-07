// Jog panel: per-axis ± buttons, step input, feedrate input, home button.
// Subscribes to controllerState + positions; disables motion buttons when not idle.

import { api } from "../api.js";

export function mountJog(store, tbody, panelRoot) {
  tbody.addEventListener("click", async (ev) => {
    const t = ev.target;
    if (t.tagName !== "BUTTON") return;

    if (t.dataset.jog) {
      const axis = t.dataset.jog;
      const dir = Number(t.dataset.dir);
      const row = t.closest("tr");
      const step = Number(row.querySelector(`[data-step="${axis}"]`).value);
      const feedrate = Number(row.querySelector(`[data-feedrate="${axis}"]`).value);
      try {
        await api.jog(axis, dir * step, feedrate);
      } catch (e) {
        alert(`Jog failed (${axis}): ${e.message}`);
      }
    } else if (t.dataset.home) {
      try { await api.home([t.dataset.home]); }
      catch (e) { alert(`Home failed (${t.dataset.home}): ${e.message}`); }
    }
  });

  document.getElementById("home-all")?.addEventListener("click", async () => {
    try { await api.home(null); }
    catch (e) { alert("Home all failed: " + e.message); }
  });
  document.getElementById("read-endstops")?.addEventListener("click", async () => {
    try {
      const e = await api.endstops();
      alert("Endstops:\n" + JSON.stringify(e.triggered, null, 2));
    } catch (e) { alert("Endstops read failed: " + e.message); }
  });
  document.getElementById("read-firmware")?.addEventListener("click", async () => {
    try {
      const f = await api.firmware();
      alert(`Firmware: ${f.flavor}\nRaw: ${f.raw}\nFeatures: ${f.features.join(", ")}`);
    } catch (e) { alert("Firmware read failed: " + e.message); }
  });

  const output = document.getElementById("diagnostic-output");
  async function showDiagnostic(label, call) {
    if (output) output.textContent = `${label}: waiting…`;
    try {
      const result = await call();
      if (output) output.textContent = JSON.stringify(result, null, 2);
    } catch (e) {
      if (output) output.textContent = `${label} failed: ${e.message}`;
    }
  }
  document.getElementById("read-position")?.addEventListener("click", () =>
    showDiagnostic("M114", () => api.position()));
  document.getElementById("read-settings")?.addEventListener("click", () =>
    showDiagnostic("M503", () => api.settings()));
  document.getElementById("read-drivers")?.addEventListener("click", () =>
    showDiagnostic("M122", () => api.drivers()));

  store.subscribe((state) => {
    // Update positions.
    for (const [axis, val] of Object.entries(state.positions || {})) {
      const cell = panelRoot.querySelector(`[data-axis-pos="${axis}"]`);
      if (cell) cell.textContent = (typeof val === "number") ? val.toFixed(3) : val;
    }
    // Lock motion buttons unless idle and connected.
    const enabled = state.controllerState === "connected_idle" && state.wsConnected && state.motionEnabled;
    const buttons = panelRoot.querySelectorAll("button[data-jog], button[data-home]");
    buttons.forEach((b) => { b.disabled = !enabled; });
    const homeAll = document.getElementById("home-all");
    if (homeAll) homeAll.disabled = !enabled;
    const interlock = document.getElementById("motion-interlock");
    if (interlock) {
      const link = state.port ? `${state.port} @ ${state.baud}` : "no serial port";
      interlock.textContent = state.motionEnabled
        ? `Motion enabled · ${link}`
        : `Diagnostics only · motion locked · ${link}`;
    }
  });
}

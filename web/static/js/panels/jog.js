
import { api } from "../api.js";

function applyHomeResult(store, result) {
  store.set({
    homed: result.homed || {},
    positions: result.positions || store.get().positions,
    referenceCounts: result.reference_counts || {},
    referenceSides: result.reference_sides || {},
    referenceStatus: Object.fromEntries(
      Object.entries(result.homed || {}).map(([axis, referenced]) => [
        axis,
        referenced ? "referenced" : (store.get().calibrated?.[axis] ? "calibrated_unreferenced" : "uncalibrated"),
      ])
    ),
  });
}

export function mountJog(store, tbody, panelRoot) {
  let homeBusy = false;

  function homeMessage(text) {
    const msg = document.getElementById("home-primary-message");
    if (msg) msg.textContent = text;
  }

  async function refreshHomeProgress() {
    try {
      const state = await api.state();
      store.set({
        controllerState: state.state,
        positions: state.positions || store.get().positions,
        homed: state.homed || store.get().homed,
        calibrated: state.calibrated || store.get().calibrated,
        referenceCounts: state.reference_counts || store.get().referenceCounts,
        referenceSides: state.reference_sides || store.get().referenceSides,
      });
      if (state.state === "homing") homeMessage(state.detail || "Home выполняется…");
    } catch (_) {  }
  }

  async function doHome(axes, label = "Home") {
    if (homeBusy) return;
    homeBusy = true;
    store.set({ homeBusy: true });
    homeMessage(`${label}: запускаем…`);
    let jobId = null;
    try {
      const started = await api.homeStart(axes);
      jobId = started.job_id;
      homeMessage(started.detail || `${label}: выполняется…`);

// ждём Home через опрос, чтобы не держать один долгий запрос
      while (homeBusy && jobId) {
        await new Promise((resolve) => setTimeout(resolve, 150));
        const status = await api.homeStatus(jobId);
        if (status.detail) homeMessage(status.detail);
        await refreshHomeProgress();
        if (status.state === "completed") {
          if (status.result) applyHomeResult(store, status.result);
          homeMessage(`${label}: ✓ reference установлен`);
          break;
        }
        if (status.state === "failed" || status.state === "cancelled") {
          throw new Error(status.error || status.detail || "Home остановлен");
        }
      }
    } catch (e) {
      homeMessage(`${label}: ${e.message}`);
      if (!String(e.message).toLowerCase().includes("останов")) {
        alert(`${label} failed: ${e.message}`);
      }
    } finally {
      homeBusy = false;
      store.set({ homeBusy: false });
      await refreshHomeProgress();
    }
  }

  tbody.addEventListener("click", async (ev) => {
    const t = ev.target;
    if (t.tagName !== "BUTTON") return;

    if (t.dataset.jog) {
      const axis = t.dataset.jog;
      const dir = Number(t.dataset.dir);
      const row = t.closest("tr");
      const step = Number(row.querySelector(`[data-step="${axis}"]`).value);
      const feedrate = Number(row.querySelector(`[data-feedrate="${axis}"]`).value);
      if (!Number.isFinite(step) || step <= 0) {
        alert(`Jog refused (${axis}): step must be a positive number written with a decimal point.`);
        return;
      }
      if (!Number.isFinite(feedrate) || feedrate <= 0) {
        alert(`Jog refused (${axis}): feedrate must be a positive number written with a decimal point.`);
        return;
      }
      try {
        const result = await api.jog(axis, dir * step, feedrate);
        if (result.positions) store.set({ positions: result.positions });
      } catch (e) {
        alert(`Jog failed (${axis}): ${e.message}`);
      }
    } else if (t.dataset.home) {
      const axis = t.dataset.home;
      await doHome([axis], `Home ${axis}`);
    }
  });

  document.getElementById("home-all")?.addEventListener("click", async () => {
    const axes = [...tbody.querySelectorAll('tr[data-calibrated="true"]')]
      .map((row) => row.dataset.axis)
      .filter((axis) => axis === "X" || axis === "Y");
    if (!axes.length) {
      alert("Home X/Y недоступен: сначала выполните физическую калибровку.");
      return;
    }
    await doHome(axes, "Home X/Y");
  });

  document.getElementById("home-x-primary")?.addEventListener("click", () => doHome(["X"], "Home X"));
  document.getElementById("home-y-primary")?.addEventListener("click", () => doHome(["Y"], "Home Y"));
  document.getElementById("home-xy-primary")?.addEventListener("click", () => {
    const axes = ["X", "Y"].filter((axis) => {
      const row = tbody.querySelector(`tr[data-axis="${axis}"]`);
      return row?.dataset.calibrated === "true" || !!store.get().calibrated?.[axis];
    });
    if (!axes.length) { alert("Home X/Y недоступен: сначала выполните физическую калибровку."); return; }
    doHome(axes, "Home X/Y");
  });

  document.getElementById("home-cancel-primary")?.addEventListener("click", async () => {
    if (!homeBusy) return;
    try {
      homeMessage("Home: останавливаем движение…");
      await api.homeCancel();
    } catch (e) {
      homeMessage(`Home stop: ${e.message}`);
    }
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
    for (const [axis, val] of Object.entries(state.positions || {})) {
      const cell = panelRoot.querySelector(`[data-axis-pos="${axis}"]`);
      if (cell) cell.textContent = (typeof val === "number") ? val.toFixed(3) : val;
    }

    for (const row of panelRoot.querySelectorAll("tr[data-axis]")) {
      const axis = row.dataset.axis;
      const calibrated = row.dataset.calibrated === "true" || !!state.calibrated?.[axis];
      const referenced = !!state.homed?.[axis];
      const refCell = row.querySelector(`[data-axis-reference="${axis}"]`);
      if (refCell) {
        if (referenced) {
          const side = state.referenceSides?.[axis];
          refCell.textContent = side ? `✓ ${side}` : "✓ referenced";
        } else if (calibrated) refCell.textContent = "калибрована · нужен Home";
        else refCell.textContent = "не калибрована";
      }

      const idle = state.controllerState === "connected_idle" && state.wsConnected;
      for (const button of row.querySelectorAll("button[data-jog]")) {
        button.disabled = !(idle && state.motionEnabled && referenced);
      }
      const home = row.querySelector("button[data-home]");
      if (home) {
// Home калиброванных X/Y разрешён и при выключенном обычном движении
        home.disabled = !!state.homeBusy || !(idle && (calibrated || state.motionEnabled));
      }
    }

    for (const axis of ["X", "Y"]) {
      const calibrated = !!state.calibrated?.[axis] || tbody.querySelector(`tr[data-axis="${axis}"]`)?.dataset.calibrated === "true";
      const referenced = !!state.homed?.[axis];
      const cal = document.getElementById(`home-${axis.toLowerCase()}-cal`);
      const ref = document.getElementById(`home-${axis.toLowerCase()}-ref`);
      if (cal) cal.textContent = calibrated ? "✓ откалибрована" : "не откалибрована";
      if (ref) ref.textContent = referenced ? "✓ REFERENCED" : (calibrated ? "нужен Home" : "—");
      const primary = document.getElementById(`home-${axis.toLowerCase()}-primary`);
      if (primary) primary.disabled = !!state.homeBusy || !(state.controllerState === "connected_idle" && state.wsConnected && calibrated);
    }
    const primaryAll = document.getElementById("home-xy-primary");
    if (primaryAll) {
      const anyCalibrated = ["X", "Y"].some((axis) => !!state.calibrated?.[axis] || tbody.querySelector(`tr[data-axis="${axis}"]`)?.dataset.calibrated === "true");
      primaryAll.disabled = !!state.homeBusy || !(state.controllerState === "connected_idle" && state.wsConnected && anyCalibrated);
    }

    const homeAll = document.getElementById("home-all");
    if (homeAll) {
      const hasCalibratedXY = [...tbody.querySelectorAll('tr[data-calibrated="true"]')]
        .some((row) => row.dataset.axis === "X" || row.dataset.axis === "Y");
      homeAll.disabled = !!state.homeBusy || !(state.controllerState === "connected_idle" && state.wsConnected && hasCalibratedXY);
    }

    const cancelHome = document.getElementById("home-cancel-primary");
    if (cancelHome) cancelHome.disabled = !state.homeBusy;

    const interlock = document.getElementById("motion-interlock");
    if (interlock) {
      const link = state.transport === "mock" ? "simulator (no hardware)" : state.port ? `${state.port} @ ${state.baud}` : "no serial port";
      const xy = ["X", "Y"].map((axis) => `${axis}:${state.homed?.[axis] ? "REF" : state.calibrated?.[axis] ? "CAL" : "—"}`).join(" · ");
      interlock.textContent = state.motionEnabled
        ? `Motion enabled · ${xy} · ${link}`
        : `Normal motion locked · physical Home available · ${xy} · ${link}`;
    }
  });
}

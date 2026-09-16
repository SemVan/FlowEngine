import { api } from "../api.js";

const panel = document.getElementById("developer-workbench");
if (panel) {
  const output = document.getElementById("wb-result");
  const entries = [];
  const axis = () => document.getElementById("wb-axis").value;
  function show(entry) {
    entries.push({ timestamp: new Date().toISOString(), ...entry });
    if (entries.length > 300) entries.shift();
    output.textContent = entries.map(e => JSON.stringify(e, null, 2)).join("\n");
  }
  async function refresh() {
    const s = await api.state();
    document.getElementById("connection-view").textContent =
      `Transport: ${s.transport} | Profile: ${s.profile}\nActual port: ${s.port ?? "— (simulator)"} | Baud: ${s.baud ?? "—"}\nState: ${s.state} | ${s.detail}\nReferenced axes: ${JSON.stringify(s.homed)}`;
  }
  async function execute(action, body) {
    try { show({ action, request: body, response: await api.workbench(action, body) }); }
    catch (e) { show({ action, request: body, error: e.message }); }
    await refresh().catch(() => {});
  }
  document.getElementById("wb-jog").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const accel = document.getElementById("wb-accel").value;
    if (!window.confirm(`Move ${axis()} on the connected controller?`)) return;
    button.disabled = true;
    try { await execute("jog", { axis: axis(), by: Number(document.getElementById("wb-by").value),
      feedrate: Number(document.getElementById("wb-speed").value),
      units: document.getElementById("wb-units").value,
      speed_units: document.getElementById("wb-speed-units").value,
      acceleration: accel === "" ? null : Number(accel) }); }
    finally { button.disabled = false; }
  });
  for (const [id, enabled, all] of [["wb-release", false, false], ["wb-enable", true, false], ["wb-release-all", false, true]]) {
    document.getElementById(id).addEventListener("click", async (event) => {
      if (!window.confirm(`${enabled ? "Enable" : "Release"} ${all ? "ALL motors" : axis()}?\nReleasing can let gravity or pressure move the mechanism.`)) return;
      event.currentTarget.disabled = true;
      try { await execute("motors", { enabled, axes: all ? null : [axis()] }); }
      finally { document.getElementById(id).disabled = false; }
    });
  }
  document.getElementById("console-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const command = document.getElementById("console-command").value.trim();
    const readonly = /^(M105|M114|M115|M119|M122|M503)$/i.test(command);
    if (!readonly && !window.confirm(`Send raw command ${command}?\nIt can move hardware, energize outputs or change settings. Positions will be invalidated.`)) return;
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    try {
      await execute("console", { command, confirmed: !readonly });
      const option = document.createElement("option"); option.value = command;
      document.getElementById("console-history").append(option);
    } finally { button.disabled = false; }
  });
  document.getElementById("wb-set-current").addEventListener("click", async () => {
    if (!window.confirm("Change running motor current? Confirm motor/driver limits first.")) return;
    await execute("current", { axis: axis(), current_ma: Number(document.getElementById("wb-current").value) });
  });
  document.getElementById("wb-sensors").addEventListener("click", async () => {
    try { document.getElementById("sensor-view").textContent = JSON.stringify(await api.workbench("sensors"), null, 2); }
    catch (e) { document.getElementById("sensor-view").textContent = e.message; }
  });
  document.getElementById("wb-export").addEventListener("click", async () => {
    try {
      const report = { ...await api.workbench("report"), capabilities: await api.workbench("capabilities"), console_session: entries,
        note: "Fill in hardware, exact reproduction steps, expected/actual result. Procedures are exported separately." };
      const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url; link.download = "flowengine-feedback.json"; link.click();
      URL.revokeObjectURL(url);
    } catch (e) { show({ error: e.message }); }
  });
  api.workbench("capabilities").then(c => { document.getElementById("capabilities-view").textContent = JSON.stringify(c, null, 2); }).catch(e => show({ error: e.message }));
  refresh().catch(e => show({ error: e.message }));
  // Read-only connection snapshot. No polling commands are sent to the hardware.
  setInterval(() => refresh().catch(() => {}), 2000);
}

import { api } from "../api.js";

const list = document.getElementById("proc-list");
const editor = document.getElementById("proc-detail");
const parameters = document.getElementById("proc-parameters");
const parameterFields = document.getElementById("proc-parameter-fields");
const result = document.getElementById("proc-result");
const nameInput = document.getElementById("proc-name");
const descriptionInput = document.getElementById("proc-description");
const draftInput = document.getElementById("proc-draft");
const stepsList = document.getElementById("proc-steps");
const stepOp = document.getElementById("step-op");
const stepFields = document.getElementById("step-fields");

let procedure = emptyProcedure();
let hardware = { device_map: { axes: [], valves: [] } };
let knownProcedures = [];

function emptyProcedure() {
  return { name: "new_procedure", description: "", version: 1, draft: true, parameters: {}, steps: [] };
}

function show(value) {
  result.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function optionList(values, selected = "") {
  const escape = v => String(v).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  return values.map((value) => `<option value="${escape(value)}" ${value === selected ? "selected" : ""}>${escape(value)}</option>`).join("");
}

function syncModelFromHeader() {
  procedure.name = nameInput.value.trim();
  procedure.description = descriptionInput.value.trim();
  procedure.draft = draftInput.checked;
}

function syncEditor() {
  syncModelFromHeader();
  editor.value = JSON.stringify(procedure, null, 2);
}

function describeStep(step) {
  if (step.op === "move") return `${step.axis}: ${step.to !== undefined ? `to ${step.to}` : `by ${step.by}`}${step.feedrate !== undefined ? ` at F${step.feedrate}` : ""}`;
  if (step.op === "set_valve") return `${step.name} → ${step.position}`;
  if (step.op === "home") return step.axes?.length ? step.axes.join(", ") : "all axes";
  if (step.op === "dwell") return `${step.seconds} s`;
  if (step.op === "log") return step.message;
  if (step.op === "checkpoint") return step.name;
  if (step.op === "call") return `${step.procedure} with ${JSON.stringify(step.parameters || {})}`;
  if (step.op === "move_multi") return `${step.relative ? "relative" : "absolute"} ${JSON.stringify(step.axes)}`;
  return JSON.stringify(step);
}

function renderSteps() {
  stepsList.replaceChildren();
  procedure.steps.forEach((step, index) => {
    const item = document.createElement("li");
    item.className = "procedure-step";
    const summary = document.createElement("code");
    summary.textContent = `${step.op} — ${describeStep(step)}`;
    const actions = document.createElement("div");
    actions.className = "step-actions";
    for (const [action, label, disabled] of [
      ["up", "↑", index === 0],
      ["down", "↓", index === procedure.steps.length - 1],
      ["remove", "Remove", false],
      ["run", "Run only this step", false],
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.action = action;
      button.dataset.index = String(index);
      button.textContent = label;
      button.disabled = disabled;
      actions.append(button);
    }
    item.append(summary, actions);
    stepsList.append(item);
  });
  syncEditor();
}

function setProcedure(value) {
  procedure = structuredClone(value);
  procedure.parameters ||= {};
  procedure.steps ||= [];
  nameInput.value = procedure.name;
  descriptionInput.value = procedure.description || "";
  draftInput.checked = Boolean(procedure.draft);
  const defaults = {};
  for (const [key, definition] of Object.entries(procedure.parameters)) {
    if (definition.default !== null && definition.default !== undefined) defaults[key] = definition.default;
  }
  parameters.value = JSON.stringify(defaults, null, 2);
  renderParameterFields(defaults);
  renderSteps();
}

function renderParameterFields(values) {
  parameterFields.replaceChildren();
  const entries = Object.entries(procedure.parameters);
  if (!entries.length) {
    parameterFields.textContent = "This procedure has no run parameters.";
    return;
  }
  for (const [name, definition] of entries) {
    const label = document.createElement("label");
    label.className = "parameter-field";
    label.append(document.createTextNode(name));
    let input;
    if (definition.type === "boolean") {
      input = document.createElement("select");
      for (const [value, text] of [["", "required"], ["true", "true"], ["false", "false"]]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = text;
        input.append(option);
      }
      if (name in values) input.value = String(values[name]);
    } else {
      input = document.createElement("input");
      input.type = ["number", "integer"].includes(definition.type) ? "number" : "text";
      if (definition.type === "integer") input.step = "1";
      else if (definition.type === "number") input.step = "any";
      if (definition.minimum !== null && definition.minimum !== undefined) input.min = definition.minimum;
      if (definition.maximum !== null && definition.maximum !== undefined) input.max = definition.maximum;
      if (name in values) input.value = values[name];
      input.placeholder = definition.default === null || definition.default === undefined ? "required" : "";
    }
    input.dataset.parameter = name;
    input.dataset.type = definition.type;
    label.append(input);
    if (definition.description) {
      const help = document.createElement("small");
      help.textContent = definition.description;
      label.append(help);
    }
    parameterFields.append(label);
  }
}

parameterFields?.addEventListener("input", () => {
  const values = {};
  for (const input of parameterFields.querySelectorAll("[data-parameter]")) {
    if (input.value === "") continue;
    if (input.dataset.type === "number") values[input.dataset.parameter] = Number(input.value);
    else if (input.dataset.type === "integer") values[input.dataset.parameter] = Number.parseInt(input.value, 10);
    else if (input.dataset.type === "boolean") values[input.dataset.parameter] = input.value === "true";
    else values[input.dataset.parameter] = input.value;
  }
  parameters.value = JSON.stringify(values, null, 2);
});

async function openProcedure(name) {
  setProcedure(await api.procedure(name));
  show(procedure.draft
    ? "Draft loaded. Complete execution is blocked; individual steps can be commissioned deliberately."
    : "Executable procedure loaded.");
}

async function reload() {
  list.textContent = "loading…";
  try {
    const procedures = await api.procedures();
    knownProcedures = procedures.filter((item) => item.ok).map((item) => item.name);
    list.replaceChildren();
    for (const itemData of procedures) {
      const item = document.createElement("li");
      if (!itemData.ok) {
        item.textContent = `${itemData.file} — ${itemData.error}`;
      } else {
        const open = document.createElement("button");
        open.type = "button";
        open.dataset.open = itemData.name;
        open.textContent = itemData.name;
        item.append(open, ` — ${itemData.description || ""} (${itemData.steps} steps)`);
        if (itemData.draft) {
          const badge = document.createElement("strong");
          badge.textContent = " DRAFT";
          item.append(badge);
        }
      }
      list.append(item);
    }
  } catch (error) { list.textContent = `Failed to load: ${error.message}`; }
}

function inputField(label, id, value = "", type = "text") {
  return `<label>${label}<input id="${id}" type="${type}" value="${value}"></label>`;
}

function renderStepFields() {
  const axes = hardware.device_map.axes.map((axis) => axis.marlin_axis);
  const valves = hardware.device_map.valves.map((valve) => valve.name);
  if (stepOp.value === "move") {
    stepFields.innerHTML = `<label>Axis<select id="step-axis">${optionList(axes)}</select></label>
      <label>Movement<select id="step-move-kind"><option value="by">Relative (by)</option><option value="to">Absolute (to)</option></select></label>
      ${inputField("Value or \${parameter}", "step-value")}${inputField("Feedrate (optional)", "step-feedrate")}
      <label>Displacement units<select id="step-units"><option value="axis">mm/deg</option><option value="steps">STEP pulses</option></select></label>
      <label>Speed units<select id="step-speed-units"><option value="axis/min">axis units/min</option><option value="axis/s">axis units/s</option><option value="steps/s">STEP pulses/s</option></select></label>
      ${inputField("Acceleration (optional, axis units/s²)", "step-accel")}`;
  } else if (stepOp.value === "set_valve") {
    stepFields.innerHTML = `<label>Valve<select id="step-valve">${optionList(valves)}</select></label>
      <label>Position<select id="step-position"><option value="A">A / position 1</option><option value="B">B / position 2</option></select></label>`;
  } else if (stepOp.value === "home") {
    stepFields.innerHTML = inputField("Axes, comma separated (empty = all)", "step-axes");
  } else if (stepOp.value === "dwell") {
    stepFields.innerHTML = inputField("Seconds or \${parameter}", "step-seconds", "1");
  } else if (stepOp.value === "log") {
    stepFields.innerHTML = inputField("Message", "step-message");
  } else if (stepOp.value === "checkpoint") {
    stepFields.innerHTML = inputField("Checkpoint name", "step-checkpoint");
  } else if (stepOp.value === "call") {
    stepFields.innerHTML = `<label>Procedure<select id="step-procedure">${optionList(knownProcedures.filter((name) => name !== procedure.name))}</select></label>
      ${inputField("Child parameters (JSON)", "step-call-parameters", "{}")}
      <small id="step-call-help"></small>`;
    document.getElementById("step-procedure")?.addEventListener("change", fillCallParameterTemplate);
    fillCallParameterTemplate();
  } else if (stepOp.value === "move_multi") {
    stepFields.innerHTML = `${inputField("Axes JSON", "step-axes-json", "{&quot;X&quot;: 1, &quot;Y&quot;: 2}")}
      ${inputField("Feedrate (optional)", "step-feedrate")}
      ${inputField("Nominal duration s (instead of feedrate)", "step-duration")}
      ${inputField("Acceleration (optional)", "step-accel")}
      <label class="inline-check"><input id="step-relative" type="checkbox"> Relative</label>`;
  } else if (stepOp.value === "pump") {
    stepFields.innerHTML = `<label>Pump<select id="step-pump">${optionList((hardware.device_map.pumps || []).map(p => p.name))}</select></label>
      ${inputField("Volume µL", "step-volume")}${inputField("Flow µL/min", "step-flow")}
      <label>Direction<select id="step-direction"><option value="dispense">Dispense</option><option value="aspirate">Aspirate</option></select></label>
      ${inputField("Acceleration (optional)", "step-accel")}`;
  } else if (stepOp.value === "motors") {
    stepFields.innerHTML = `${inputField("Axes, comma separated (empty = all)", "step-axes")}
      <label>Action<select id="step-enabled"><option value="false">Release (reference lost)</option><option value="true">Enable</option></select></label>`;
  } else if (stepOp.value === "pump_multi") {
    stepFields.replaceChildren();
    for (const pump of hardware.device_map.pumps || []) {
      const row = document.createElement("fieldset"); row.dataset.pumpDose = pump.name;
      const legend = document.createElement("legend"); legend.textContent = pump.name; row.append(legend);
      for (const [key, title, type] of [["selected", "Include", "checkbox"], ["volume_ul", "Volume µL", "text"], ["flow_ul_min", "Flow µL/min", "text"]]) {
        const label = document.createElement("label"); label.textContent = title;
        const input = document.createElement("input"); input.type = type; input.dataset.doseField = key; label.append(input); row.append(label);
      }
      const select = document.createElement("select"); select.dataset.doseField = "direction";
      for (const name of ["dispense", "aspirate"]) { const option = document.createElement("option"); option.value = name; option.textContent = name; select.append(option); }
      row.append(select); stepFields.append(row);
    }
    const help = document.createElement("small"); help.textContent = "Select ≥2 calibrated pumps. volume/flow must give the same nominal duration; acceleration changes actual duration."; stepFields.append(help);
  } else if (stepOp.value === "seek") {
    stepFields.innerHTML = `<label>Axis<select id="step-axis">${optionList(axes)}</select></label>
      ${inputField("Verified probe channel", "step-channel")}${inputField("Maximum relative search (axis units)", "step-value")}
      ${inputField("Feedrate (axis units/min)", "step-feedrate")}
      <label><input type="checkbox" id="step-zero"> Establish zero at contact</label><small>Requires bench-verified G38.2 support; input must be released.</small>`;
  } else if (stepOp.value === "read_sensors") {
    stepFields.textContent = "Read configured sources once and add report to event log.";
  } else if (["calibrate_valve", "test_reference"].includes(stepOp.value)) {
    stepFields.innerHTML = `<label>Axis<select id="step-axis">${optionList(axes)}</select></label>
      ${inputField("Verified shared probe channel", "step-channel")}
      ${inputField(stepOp.value === "calibrate_valve" ? "Maximum search distance" : "Round trip displacement (signed away from home)", "step-value")}
      ${inputField("Feedrate (axis units/min)", "step-feedrate")}
      ${inputField("Backoff (positive axis units)", "step-backoff")}
      ${inputField("Repeats (1–20)", "step-repeats", "3")}
      ${stepOp.value === "test_reference" ? inputField("Slow reference feedrate", "step-reference-feedrate") + inputField("Tolerance STEP pulses", "step-tolerance") + inputField("Trial acceleration (optional)", "step-accel") : ""}
      <small>Firmware-stopped contact search required. Initial input must be released. Reports do not automatically change configuration.</small>`;
  }
}

async function fillCallParameterTemplate() {
  const select = document.getElementById("step-procedure");
  const input = document.getElementById("step-call-parameters");
  const help = document.getElementById("step-call-help");
  if (!select?.value || !input) return;
  try {
    const child = await api.procedure(select.value);
    const defaults = {};
    const required = [];
    for (const [name, definition] of Object.entries(child.parameters || {})) {
      if (definition.default === null || definition.default === undefined) required.push(name);
      else defaults[name] = definition.default;
    }
    input.value = JSON.stringify(defaults);
    if (help) help.textContent = required.length
      ? `Required child parameters to add: ${required.join(", ")}`
      : "All child parameters have defaults.";
  } catch (error) {
    if (help) help.textContent = `Cannot load child parameters: ${error.message}`;
  }
}

function numberOrReference(value, required = true) {
  const text = value.trim();
  if (!text && !required) return undefined;
  if (/^\$\{[A-Za-z_][A-Za-z0-9_]*\}$/.test(text)) return text;
  const number = Number(text);
  if (!Number.isFinite(number)) throw new Error(`Expected a number or parameter reference, got ${text || "empty value"}`);
  return number;
}

function buildStep() {
  if (stepOp.value === "move") {
    const kind = document.getElementById("step-move-kind").value;
    const step = { op: "move", axis: document.getElementById("step-axis").value };
    step[kind] = numberOrReference(document.getElementById("step-value").value);
    const feedrate = numberOrReference(document.getElementById("step-feedrate").value, false);
    if (feedrate !== undefined) step.feedrate = feedrate;
    step.units = document.getElementById("step-units").value;
    step.speed_units = document.getElementById("step-speed-units").value;
    const acceleration = numberOrReference(document.getElementById("step-accel").value, false);
    if (acceleration !== undefined) step.acceleration = acceleration;
    return step;
  }
  if (stepOp.value === "set_valve") return { op: "set_valve", name: document.getElementById("step-valve").value, position: document.getElementById("step-position").value };
  if (stepOp.value === "home") {
    const axes = document.getElementById("step-axes").value.split(",").map((value) => value.trim()).filter(Boolean);
    return { op: "home", axes: axes.length ? axes : null };
  }
  if (stepOp.value === "dwell") return { op: "dwell", seconds: numberOrReference(document.getElementById("step-seconds").value) };
  if (stepOp.value === "log") return { op: "log", message: document.getElementById("step-message").value };
  if (stepOp.value === "checkpoint") return { op: "checkpoint", name: document.getElementById("step-checkpoint").value };
  if (stepOp.value === "call") return {
    op: "call",
    procedure: document.getElementById("step-procedure").value,
    parameters: JSON.parse(document.getElementById("step-call-parameters").value || "{}"),
  };
  if (stepOp.value === "move_multi") {
    const step = { op: "move_multi", axes: JSON.parse(document.getElementById("step-axes-json").value), relative: document.getElementById("step-relative").checked };
    const feedrate = numberOrReference(document.getElementById("step-feedrate").value, false);
    if (feedrate !== undefined) step.feedrate = feedrate;
    const duration = numberOrReference(document.getElementById("step-duration").value, false);
    if (duration !== undefined) step.duration_s = duration;
    const acceleration = numberOrReference(document.getElementById("step-accel").value, false);
    if (acceleration !== undefined) step.acceleration = acceleration;
    return step;
  }
  if (stepOp.value === "pump") {
    const step = { op: "pump", name: document.getElementById("step-pump").value,
      volume_ul: numberOrReference(document.getElementById("step-volume").value),
      flow_ul_min: numberOrReference(document.getElementById("step-flow").value),
      direction: document.getElementById("step-direction").value };
    const acceleration = numberOrReference(document.getElementById("step-accel").value, false);
    if (acceleration !== undefined) step.acceleration = acceleration;
    return step;
  }
  if (stepOp.value === "motors") {
    const axes = document.getElementById("step-axes").value.split(",").map(s => s.trim()).filter(Boolean);
    return { op: "motors", enabled: document.getElementById("step-enabled").value === "true", axes: axes.length ? axes : null };
  }
  if (stepOp.value === "pump_multi") {
    const pumps = {};
    for (const row of stepFields.querySelectorAll("[data-pump-dose]")) {
      if (!row.querySelector('[data-dose-field="selected"]').checked) continue;
      pumps[row.dataset.pumpDose] = { volume_ul: numberOrReference(row.querySelector('[data-dose-field="volume_ul"]').value),
        flow_ul_min: numberOrReference(row.querySelector('[data-dose-field="flow_ul_min"]').value),
        direction: row.querySelector('[data-dose-field="direction"]').value };
    }
    return { op: "pump_multi", pumps };
  }
  if (stepOp.value === "read_sensors") return { op: "read_sensors" };
  if (stepOp.value === "seek") return { op: "seek", axis: document.getElementById("step-axis").value,
    channel: document.getElementById("step-channel").value, by: numberOrReference(document.getElementById("step-value").value),
    feedrate: numberOrReference(document.getElementById("step-feedrate").value), zero: document.getElementById("step-zero").checked };
  if (["calibrate_valve", "test_reference"].includes(stepOp.value)) {
    const step = { op: stepOp.value, axis: document.getElementById("step-axis").value,
      channel: document.getElementById("step-channel").value, feedrate: numberOrReference(document.getElementById("step-feedrate").value),
      backoff: numberOrReference(document.getElementById("step-backoff").value), repeats: Number(document.getElementById("step-repeats").value) };
    if (step.op === "calibrate_valve") step.search_distance = numberOrReference(document.getElementById("step-value").value);
    else { step.by = numberOrReference(document.getElementById("step-value").value);
      step.reference_feedrate = numberOrReference(document.getElementById("step-reference-feedrate").value);
      step.tolerance_pulses = numberOrReference(document.getElementById("step-tolerance").value); }
    if (step.op === "test_reference") {
      const acceleration = numberOrReference(document.getElementById("step-accel").value, false);
      if (acceleration !== undefined) step.acceleration = acceleration;
    }
    return step;
  }
  throw new Error(`Unsupported operation ${stepOp.value}`);
}

function parseParameters() {
  const value = JSON.parse(parameters.value || "{}");
  if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("Run parameters must be a JSON object");
  return value;
}

async function saveCurrent() {
  syncModelFromHeader();
  const saved = await api.saveProcedure(procedure.name, procedure);
  syncEditor();
  await reload();
  return saved;
}

list?.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-open]");
  if (!button) return;
  try { await openProcedure(button.dataset.open); }
  catch (error) { show(`Load failed: ${error.message}`); }
});

stepsList?.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const index = Number(button.dataset.index);
  if (button.dataset.action === "remove") procedure.steps.splice(index, 1);
  if (button.dataset.action === "up" && index > 0) [procedure.steps[index - 1], procedure.steps[index]] = [procedure.steps[index], procedure.steps[index - 1]];
  if (button.dataset.action === "down" && index < procedure.steps.length - 1) [procedure.steps[index + 1], procedure.steps[index]] = [procedure.steps[index], procedure.steps[index + 1]];
  if (button.dataset.action === "run") {
    const confirmed = window.confirm(`Run step ${index + 1} only?\n\n${describeStep(procedure.steps[index])}\n\nThe command will be sent to the connected controller.`);
    if (!confirmed) return;
    button.disabled = true;
    try {
      await saveCurrent();
      show(await api.runProcedureStep(procedure.name, index + 1, parseParameters()));
    } catch (error) { show(`Step refused or failed: ${error.message}`); }
    finally { button.disabled = false; }
    return;
  }
  renderSteps();
});

document.getElementById("new-proc")?.addEventListener("click", () => {
  setProcedure(emptyProcedure());
  show("New draft. Add steps, save it, then commission individual steps if needed.");
});

document.getElementById("export-proc")?.addEventListener("click", () => {
  syncModelFromHeader();
  const url = URL.createObjectURL(new Blob([JSON.stringify(procedure, null, 2)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = `${procedure.name.replace(/[^A-Za-z0-9_-]/g, "_")}.json`; link.click(); URL.revokeObjectURL(url);
});

document.getElementById("add-step")?.addEventListener("click", () => {
  try { procedure.steps.push(buildStep()); renderSteps(); }
  catch (error) { show(`Cannot add step: ${error.message}`); }
});

document.getElementById("apply-json")?.addEventListener("click", () => {
  try { setProcedure(JSON.parse(editor.value)); show("JSON changes applied locally. Save to validate them."); }
  catch (error) { show(`Invalid JSON: ${error.message}`); }
});

document.getElementById("save-proc")?.addEventListener("click", async () => {
  try { show(await saveCurrent()); }
  catch (error) { show(`Save failed: ${error.message}`); }
});

document.getElementById("preview-proc")?.addEventListener("click", async () => {
  try { await saveCurrent(); show(await api.previewProcedure(procedure.name, parseParameters())); }
  catch (error) { show(`Preview failed: ${error.message}`); }
});

document.getElementById("run-proc")?.addEventListener("click", async () => {
  if (draftInput.checked) { show("Complete run refused locally: clear Draft only after every step has been commissioned."); return; }
  const confirmed = window.confirm(`Run the complete procedure “${nameInput.value.trim()}”?`);
  if (!confirmed) return;
  try { await saveCurrent(); show(await api.runProcedure(procedure.name, parseParameters())); }
  catch (error) { show(`Run refused: ${error.message}`); }
});

document.getElementById("abort-proc")?.addEventListener("click", async () => {
  try { show(await api.abortProcedure()); }
  catch (error) { show(`Abort failed: ${error.message}`); }
});

document.getElementById("reload-procs")?.addEventListener("click", reload);
stepOp?.addEventListener("change", renderStepFields);

Promise.all([api.config(), reload()]).then(([config]) => {
  hardware = config;
  renderStepFields();
  setProcedure(procedure);
}).catch((error) => show(`Initialization failed: ${error.message}`));

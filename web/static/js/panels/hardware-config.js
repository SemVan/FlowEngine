import { api } from "../api.js";

const editor = document.getElementById("config-editor");
const container = document.getElementById("hardware-forms");
let profile;
let calibrated;
const specs = {
  axes: [["marlin_axis", "Ось", "text"], ["name", "Название", "text"],
    ["kind", "Тип механизма", "choice:syringe_pump,peristaltic_pump,valve,autosampler_axis"],
    ["units", "Единицы", "choice:mm,deg"], ["home_direction", "Направление home", "choice:min,max"],
    ["steps_per_unit", "Шагов на единицу", "number"], ["travel", "Ход (для X/Y измеряется по концевикам)", "optional"],
    ["feedrate_default", "Скорость, ед/мин", "number"], ["feedrate_max", "Макс. скорость", "number"],
    ["accel_max", "Макс. ускорение", "optional"], ["driver_current_max_ma", "Макс. ток RMS, мА", "optional"]],
  pumps: [["name", "Насос", "text"], ["axis", "Ось", "text"],
    ["kind", "Тип насоса", "choice:syringe,peristaltic"],
    ["volume_per_unit_ul", "мкл на единицу", "number"], ["dispense_direction", "Направление подачи ±1", "number"],
    ["calibrated", "Калибровка подтверждена", "checkbox"], ["calibration_notes", "Заметки", "text"]],
  valves: [["name", "Клапан", "text"], ["axis", "Ось", "text"], ["position_a", "Позиция A", "number"],
    ["position_b", "Позиция B", "number"], ["home_position", "Исходная позиция", "choice:A,B"]],
  endstop_inputs: [["channel", "Marlin channel", "text"], ["axes", "Shared axes (comma separated)", "list"], ["notes", "Wiring / switches", "text"]],
  analog_inputs: [["name", "Sensor", "text"], ["source", "Firmware source (T0/B/ADC0)", "text"],
    ["quantity", "temperature / analog", "text"], ["units", "Units", "text"], ["scale", "Scale", "number"],
    ["offset", "Offset", "number"], ["poll_hz", "Desired Hz (manual read in this version)", "number"], ["notes", "Notes", "text"]],
};

function field(value, type) {
  if (type.startsWith("choice:")) {
    const select = document.createElement("select");
    for (const v of type.slice(7).split(",")) { const option = document.createElement("option"); option.textContent = v; option.value = v; select.append(option); }
    select.value = value ?? type.slice(7).split(",")[0]; select.dataset.valueType = "text"; return select;
  }
  const input = document.createElement("input");
  input.type = type === "checkbox" ? "checkbox" : ["number", "optional"].includes(type) ? "number" : "text";
  input.step = "any";
  if (type === "checkbox") input.checked = Boolean(value);
  else input.value = type === "list" ? (value || []).join(", ") : value ?? "";
  input.dataset.valueType = type;
  return input;
}

export function renderHardwareForms() {
  if (!container) return;
  profile = JSON.parse(editor.value);
  container.replaceChildren();
  const runtimeSection = document.createElement('details');
  const runtimeTitle = document.createElement('summary'); runtimeTitle.textContent = 'Подключение и параметры движения';
  runtimeSection.append(runtimeTitle);
  const runtimeGrid = document.createElement('div'); runtimeGrid.className = 'device-grid device-card';
  for (const [group, name, title, type] of [
    ['transport','port','Порт (auto или COM…)','text'],
    ['transport','baud','Скорость порта','number'],
    ['motion','feedrate_cap','Общий предел скорости','number'],
    ['motion','enabled','Разрешить движения на приборе','checkbox'],
    ['motion','configure_firmware','Применять параметры к прошивке','checkbox'],
  ]) {
    const label = document.createElement('label'); label.textContent = title;
    const input = field(profile.runtime[group][name], type);
    input.dataset.runtimeGroup = group; input.dataset.runtimeField = name;
    label.append(input); runtimeGrid.append(label);
  }
  runtimeSection.append(runtimeGrid); container.append(runtimeSection);
  for (const [kind, columns] of Object.entries(specs)) {
    const titles = {axes:'Моторы и оси', pumps:'Насосы', valves:'Клапаны', endstop_inputs:'Концевики', analog_inputs:'Датчики'};
    const section = document.createElement('details');
    section.open = kind === 'axes';
    const legend = document.createElement('summary'); legend.textContent = titles[kind];
    section.append(legend);
    const table = document.createElement('div');
    const items = profile.device_map[kind] ||= [];
    items.forEach((item, index) => {
      const row = document.createElement('article'); row.className = 'device-card';
      const title = document.createElement('h3'); title.textContent = `${item.marlin_axis || item.axis || item.channel || ''}  ${item.name || ''}`;
      row.append(title);
      const grid = document.createElement('div'); grid.className = 'device-grid'; row.append(grid);
      for (const [name, labelText, type] of columns) {
        const cell = document.createElement('label'); cell.textContent = labelText;
        const input = field(item[name], type);
        input.dataset.kind = kind; input.dataset.index = index; input.dataset.field = name;
        if (kind === "axes" && name === "travel" && ["X", "Y"].includes(item.marlin_axis) && item.endstop_channels_shared !== false) {
          input.disabled = true;
          input.value = "";
          input.title = "Для X/Y с параллельными концевиками ход измеряется физической калибровкой";
        }
        cell.append(input); grid.append(cell);
      }
      const remove = document.createElement('button'); remove.className = 'device-remove';
      remove.type = 'button'; remove.textContent = 'Удалить устройство';
      remove.addEventListener('click', () => { applyHardwareForms(); profile.device_map[kind].splice(index, 1); editor.value = JSON.stringify(profile, null, 2); renderHardwareForms(); container.dispatchEvent(new Event('input', {bubbles:true})); });
      row.append(remove); table.append(row);
    });
    section.append(table);
    if (kind in specs) {
      const add = document.createElement("button"); add.type = "button"; add.textContent = `+ Добавить`;
      add.addEventListener("click", () => {
        applyHardwareForms();
        const defaults = {
          axes: {marlin_axis: "Z", name: "new_axis", kind: "autosampler_axis", steps_per_unit: 80, units: "mm", travel: 10,
            home_direction: "min", feedrate_default: 60, feedrate_max: 60, accel_max: null, driver_current_max_ma: null},
          pumps: {name: "new_pump", axis: "Z", kind: "syringe", volume_per_unit_ul: 1, dispense_direction: 1, calibrated: false, calibration_notes: ""},
          valves: {name: "new_valve", axis: "Z", position_a: 0, position_b: 1, home_position: "A"},
          endstop_inputs: {channel: "probe", axes: [], active_high: true, notes: ""},
          analog_inputs: {name: "sensor", source: "T0", quantity: "temperature", units: "°C", scale: 1, offset: 0, poll_hz: 1, notes: ""},
        };
        profile.device_map[kind].push(defaults[kind]); container.dispatchEvent(new Event('input', {bubbles:true}));
        editor.value = JSON.stringify(profile, null, 2); renderHardwareForms();
      });
      section.append(add);
    }
    container.append(section);
  }
  const pumps = document.getElementById("calibration-pump"); pumps.replaceChildren();
  for (const p of profile.device_map.pumps || []) { const option = document.createElement("option"); option.textContent = p.name; option.value = p.name; pumps.append(option); }
  editor.value = JSON.stringify(profile, null, 2);
}

export function applyHardwareForms() {
  if (!profile) return;
  // Refuse stale forms after a manual JSON edit; do not silently overwrite that edit.
  const current = JSON.parse(editor.value);
  if (JSON.stringify(current) !== JSON.stringify(profile)) throw new Error("JSON changed: load forms from JSON before applying forms");
  for (const input of container.querySelectorAll('[data-runtime-field]')) {
    const value = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value;
    if (typeof value === 'number' && (!input.value.trim() || !Number.isFinite(value))) throw new Error('Введите корректное число');
    profile.runtime[input.dataset.runtimeGroup][input.dataset.runtimeField] = value;
  }
  for (const input of container.querySelectorAll("[data-field]")) {
    if (input.disabled) continue;
    const type = input.dataset.valueType;
    const value = type === "checkbox" ? input.checked : type === "list" ? input.value.split(",").map(s => s.trim()).filter(Boolean) :
      ["number", "optional"].includes(type) ? (input.value === "" && type === "optional" ? null : Number(input.value)) : input.value;
    if (typeof value === "number" && !Number.isFinite(value)) throw new Error("Numeric values must be finite");
    profile.device_map[input.dataset.kind][Number(input.dataset.index)][input.dataset.field] = value;
  }
  editor.value = JSON.stringify(profile, null, 2);
}

if (container) {
  const report = document.getElementById("calibration-result");
  const show = value => { report.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2); };
  for (const [id, fn] of [["config-render-forms", renderHardwareForms], ["config-apply-forms", applyHardwareForms]]) {
    document.getElementById(id).addEventListener("click", () => { try { fn(); } catch(e) { show(e.message); } });
  }
  function addMeasurement() {
    const row = document.createElement("div"); row.className = "calibration-measurement";
    for (const [name, title, value] of [["step_pulses", "STEP pulses", ""], ["volume_ul", "Measured µL (or mass)", ""],
      ["mass_g", "Mass g (or volume)", ""], ["density_g_ml", "Density g/mL", "1"]]) {
      const label = document.createElement("label"); label.textContent = title;
      const input = field(value, "number"); input.dataset.measurement = name; label.append(input); row.append(label);
    }
    const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "Remove";
    remove.addEventListener("click", () => row.remove()); row.append(remove);
    document.getElementById("calibration-measurements").append(row);
  }
  document.getElementById("calibration-add").addEventListener("click", addMeasurement);
  addMeasurement();
  document.getElementById("calibration-calculate").addEventListener("click", async () => {
    document.getElementById("calibration-apply").disabled = true;
    calibrated = null;
    try {
      const measurements = [...document.querySelectorAll(".calibration-measurement")].map(row =>
        Object.fromEntries([...row.querySelectorAll("input")].filter(i => i.value !== "").map(i => [i.dataset.measurement, Number(i.value)])));
      applyHardwareForms();
      const pump = profile.device_map.pumps.find(p => p.name === document.getElementById("calibration-pump").value);
      const axis = profile.device_map.axes.find(a => a.marlin_axis === pump.axis);
      calibrated = await api.workbench("calibrate-pump", { name: pump.name, steps_per_unit: axis.steps_per_unit, measurements });
      show(calibrated); document.getElementById("calibration-apply").disabled = false;
    } catch(e) { show(e.message); }
  });
  document.getElementById("calibration-apply").addEventListener("click", () => {
    if (!calibrated || !window.confirm("Confirm this measured coefficient? It changes only the edited profile, not live hardware.")) return;
    try {
      applyHardwareForms();
      const p = profile.device_map.pumps.find(p => p.name === calibrated.name);
      if (!p) throw new Error("Pump no longer exists in edited profile");
      if (profile.device_map.axes.find(a => a.marlin_axis === p.axis).steps_per_unit !== calibrated.steps_per_unit) throw new Error("Steps/unit changed since calculation: recalculate");
      p.volume_per_unit_ul = calibrated.volume_per_unit_ul; p.calibrated = true;
      p.calibration_notes = `Measured ${new Date().toISOString()}, repeats=${calibrated.repeats}, relative_stddev=${calibrated.relative_stddev}; ADD speed/fluid/pressure/direction`;
      editor.value = JSON.stringify(profile, null, 2); renderHardwareForms(); show("Coefficient added. Fill conditions, save profile and restart to activate.");
    } catch(e) { show(e.message); }
  });
}

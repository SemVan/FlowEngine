import { api } from "../api.js";

const $ = id => document.getElementById(id);
const axes = () => hardware.device_map.axes;
const valves = () => hardware.device_map.valves;
const pumps = () => hardware.device_map.pumps || [];
const valveNames = { wash_valve: "Кран промывки", sheath_valve: "Кран обжимающей жидкости", sample_valve: "Кран сэмплера" };
const pumpNames = { sample_pump: "Насос суспензии", sheath_pump: "Насос обжимающей жидкости", wash_pump: "Насос промывки" };
const opNames = Object.fromEntries([...$("step-op").options].map(o => [o.value, o.textContent]));
let hardware = { device_map: { axes: [], valves: [], pumps: [] } };
let knownProcedures = [];
let editing = null;
let busy = true;
let running = false;
let activeStatus = {};
let procedure = emptyProcedure();
let savedText = "";

function emptyProcedure() {
  return { name: "new_procedure", title: "", description: "", version: 1, draft: true, parameters: {}, steps: [] };
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function show(value, message) {
  $("proc-result").textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  $("proc-feedback").replaceChildren(el("p", message || (typeof value === "string" ? value : "Готово.")));
}

function syncHeader() {
  procedure.name = $("proc-name").value.trim();
  procedure.title = $("proc-title").value.trim();
  procedure.description = $("proc-description").value.trim();
  procedure.draft = $("proc-draft").checked;
}

function syncEditor() {
  syncHeader();
  $("proc-detail").value = JSON.stringify(procedure, null, 2);
  updateButtons();
}

function updateButtons() {
  for (const id of ["save-proc", "copy-proc", "new-proc", "preview-proc", "apply-json", "add-step", "save-run-defaults", "add-parameter"]) $(id).disabled = busy || running;
  const reason = busy ? "Подождите, выполняется загрузка или сохранение."
    : running ? "Сейчас выполняется процедура."
    : !procedure.steps.length ? "Добавьте хотя бы один шаг."
    : procedure.steps.some(s => s.op === "require_setup") ? "В процедуре остались шаги «Требуется настройка»."
    : "";
  $("run-proc").disabled = Boolean(reason);
  $("run-proc").textContent = $("proc-draft").checked ? "Проверить и запустить" : "Запустить процедуру";
  $("run-proc").title = reason || "Выполнить выбранную процедуру на аппарате";
  $("proc-launch-hint").textContent = reason || `Выбрана: ${procedure.title || procedure.name}. Перед запуском проверим параметры и нули.`;
  $("abort-proc").disabled = !running;
  for (const button of $("proc-steps").querySelectorAll("button")) {
    const index = Number(button.dataset.index);
    button.disabled = busy || running || (button.dataset.action === "up" && index === 0)
      || (button.dataset.action === "down" && index === procedure.steps.length - 1)
      || (button.dataset.action === "run" && procedure.steps[index]?.op === "require_setup");
  }
  for (const button of $("proc-list").querySelectorAll("button")) button.disabled = busy || running;
}

function describeStep(s) {
  if (s.op === "home_valve") return `${valveNames[s.name] || s.name}: HOME${s.only_if_needed ? " только при потере нуля" : " повторно"}, затем отход`;
  if (s.op === "set_valve") return `${valveNames[s.name] || s.name} → сохранённое положение ${s.position}${s.feedrate != null ? "; скорость " + s.feedrate + " ед/мин" : ""}${s.acceleration != null ? "; ускорение " + s.acceleration : ""}`;
  if (s.op === "home") return `${s.axes?.join(", ") || "все оси"}: HOME ${s.only_if_needed ? "только при потере нуля" : "повторно"}`;
  if (s.op === "move") return `${s.axis}: ${s.to != null ? "в координату " + s.to : "на " + s.by} ${s.units === "steps" ? "шагов" : axisUnit(s.axis)}${s.duration_s != null ? "; номинальное время " + s.duration_s + " с" : s.feedrate != null ? "; скорость " + s.feedrate + " " + speedUnit(s.axis, s.speed_units) : ""}`;
  if (s.op === "move_for") return `${s.axis}: ${s.direction === -1 ? "−" : "+"}, ${s.feedrate} ${speedUnit(s.axis, s.speed_units)} в течение ${s.seconds} с (без учёта разгона/торможения)`;
  if (s.op === "move_multi_for") return `Вместе ${s.seconds} с: ` + Object.entries(s.axes).map(([axis, move]) => `${axis}: ${move.direction === -1 ? "−" : "+"}${move.feedrate} ${speedUnit(axis, move.speed_units)}`).join("; ");
  if (s.op === "move_multi") return `Совместно: ${Object.entries(s.axes).map(([a, n]) => `${a} → ${n}${s.axis_feedrates ? ", " + s.axis_feedrates[a] + " " + speedUnit(a) : ""}`).join("; ")}${s.relative ? " (смещения)" : ""}${s.duration_s != null ? "; номинальное время " + s.duration_s + " с" : s.feedrate != null ? "; общая скорость " + s.feedrate : ""}`;
  if (s.op === "pump") return `${pumpNames[s.name] || s.name}: ${s.volume_ul} мкл; ${s.flow_ul_min} мкл/мин; ${s.direction === "aspirate" ? "забор" : "подача"}`;
  if (s.op === "pump_multi") return Object.entries(s.pumps).map(([name, d]) => `${pumpNames[name] || name}: ${d.volume_ul} мкл, ${d.flow_ul_min} мкл/мин${typeof d.volume_ul === "number" && typeof d.flow_ul_min === "number" ? ", номинально " + (60 * d.volume_ul / d.flow_ul_min).toFixed(2) + " с" : ""}`).join("; ");
  if (s.op === "dwell") return `${s.seconds} с`;
  if (s.op === "log" || s.op === "require_setup") return s.message;
  if (s.op === "checkpoint") return s.name;
  if (s.op === "require_reference") return `Проверить нули: ${s.axes.join(", ")}`;
  if (s.op === "repeat") return `${s.procedure} × ${s.times}; параметры: ${JSON.stringify(s.parameters || {})}`;
  if (s.op === "call") return `${s.procedure}; параметры: ${JSON.stringify(s.parameters || {})}`;
  if (s.op === "motors") return `${s.enabled ? "Удерживать" : "Отключить удержание, сбросить привязку"}: ${s.axes?.join(", ") || "все оси"}`;
  if (s.op === "read_sensors") return `${s.names?.join(", ") || "Все настроенные датчики"}: ${s.samples || 1} измерений${s.samples > 1 ? ", пауза " + s.interval_s + " с" : ""}${s.require_available ? "; отсутствие сигнала — ошибка" : ""}`;
  return JSON.stringify(s);
}

function renderSteps() {
  $("proc-steps").replaceChildren();
  if (!procedure.steps.length) $("proc-steps").append(el("li", "Добавьте первое действие ниже.", "empty-steps"));
  procedure.steps.forEach((step, index) => {
    const item = el("li", undefined, "procedure-step");
    item.dataset.stepIndex = index;
    item.classList.toggle("needs-setup", step.op === "require_setup");
    item.classList.toggle("editing", index === editing);
    item.append(el("strong", step.title || opNames[step.op] || step.op), el("p", describeStep(step), "step-description"));
    const actions = el("div", undefined, "step-actions");
    for (const [action, label] of [["up", "↑"], ["down", "↓"], ["edit", "Изменить"], ["copy", "Копия"], ["remove", "Удалить"], ["run", "Проверить этот шаг"]]) {
      const button = el("button", label);
      button.type = "button";
      button.dataset.action = action;
      button.dataset.index = index;
      actions.append(button);
    }
    item.append(actions);
    $("proc-steps").append(item);
  });
  syncEditor();
}

function resetStepEditor() {
  editing = null;
  $("step-title").value = "";
  $("step-builder-title").textContent = "Добавить шаг";
  $("add-step").textContent = "Добавить шаг";
  $("cancel-step-edit").hidden = true;
  renderStepFields();
}

function rememberProcedure(name) {
  try { localStorage.setItem("flowengine.procedure", name); } catch (_) { }
  const url = new URL(window.location.href);
  url.searchParams.set("procedure", name);
  window.history.replaceState(null, "", url);
}

function setProcedure(value, saved = true) {
  if (!value || typeof value !== "object" || !Array.isArray(value.steps)) throw new Error("У процедуры должен быть список steps");
  procedure = structuredClone(value);
  procedure.parameters ||= {};
  $("proc-name").value = procedure.name;
  $("proc-title").value = procedure.title || "";
  $("proc-description").value = procedure.description || "";
  $("proc-draft").checked = Boolean(procedure.draft);
  resetStepEditor();
  renderParameterFields();
  renderSteps();
  savedText = saved ? JSON.stringify(procedure) : "";
  if (saved) rememberProcedure(procedure.name);
}

function mayReplaceProcedure() {
  syncHeader();
  if (!procedure.steps.length && !procedure.title && !procedure.description && !Object.keys(procedure.parameters).length) return true;
  return (editing === null && JSON.stringify(procedure) === savedText)
    || window.confirm("В редакторе есть несохранённые изменения. Открыть другую процедуру и потерять их?");
}

function renderParameterFields(preserve = false) {
  let previous = {};
  if (preserve) { try { previous = parseParameters(); } catch (_) { } }
  const values = {};
  $("proc-parameter-fields").replaceChildren();
  for (const [name, definition] of Object.entries(procedure.parameters)) {
    const label = el("label", definition.description || name, "parameter-field");
    const input = definition.type === "boolean" ? el("select") : el("input");
    if (definition.type === "boolean") {
      for (const [v, text] of [["", "Не задано"], ["true", "Да"], ["false", "Нет"]]) {
        const option = el("option", text); option.value = v; input.append(option);
      }
    } else {
      input.type = ["number", "integer"].includes(definition.type) ? "number" : "text";
      input.step = definition.type === "integer" ? "1" : "any";
      if (definition.minimum != null) input.min = definition.minimum;
      if (definition.maximum != null) input.max = definition.maximum;
    }
    const current = previous[name] ?? definition.default;
    if (current != null) { values[name] = current; input.value = current; }
    input.dataset.parameter = name;
    input.dataset.type = definition.type;
    const remove = el("button", "Убрать параметр"); remove.type = "button"; remove.dataset.removeParameter = name;
    label.append(input, el("small", `Ссылка в шаге: ${"${" + name + "}"}`), remove);
    $("proc-parameter-fields").append(label);
  }
  if (!Object.keys(procedure.parameters).length) $("proc-parameter-fields").textContent = "У этой процедуры нет параметров запуска.";
  $("proc-parameters").value = JSON.stringify(values, null, 2);
  $("numeric-parameters").replaceChildren(...Object.entries(procedure.parameters)
    .filter(([, definition]) => ["number", "integer"].includes(definition.type))
    .map(([name, definition]) => { const option = el("option"); option.value = "${" + name + "}"; option.label = definition.description || name; return option; }));
}

$("add-parameter").addEventListener("click", () => {
  if (busy || running) return;
  try {
    const name = $("parameter-name").value.trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) throw new Error("Имя параметра: латинские буквы, цифры и _, первая — буква или _.");
    const type = $("parameter-type").value;
    const raw = $("parameter-default").value.trim();
    if (!raw) throw new Error("Укажите значение по умолчанию.");
    let value = raw;
    if (["number", "integer"].includes(type)) {
      value = Number(raw);
      if (!Number.isFinite(value) || (type === "integer" && !Number.isInteger(value))) throw new Error("Укажите число нужного типа.");
    } else if (type === "boolean") {
      if (!["true", "false", "да", "нет"].includes(raw.toLowerCase())) throw new Error("Для Да / нет введите true/false или да/нет.");
      value = ["true", "да"].includes(raw.toLowerCase());
    }
    const definition = {type, description: $("parameter-description").value.trim(), default: value};
    if (["number", "integer"].includes(type)) {
      for (const [key, id] of [["minimum", "parameter-min"], ["maximum", "parameter-max"]]) {
        if ($(id).value.trim()) definition[key] = Number($(id).value);
      }
      if (definition.minimum != null && value < definition.minimum || definition.maximum != null && value > definition.maximum) throw new Error("Значение по умолчанию должно быть внутри границ.");
    }
    procedure.parameters[name] = definition;
    const values = parseParameters(); values[name] = value;
    $("proc-parameters").value = JSON.stringify(values);
    renderParameterFields(true); syncEditor();
    show(`Параметр ${name} добавлен. В числовом поле шага выберите ${"${" + name + "}"}.`);
  } catch (error) { show(error.message); }
});

$("proc-parameter-fields").addEventListener("click", event => {
  const name = event.target.dataset.removeParameter;
  if (!name || busy || running) return;
  if (JSON.stringify(procedure.steps).includes("${" + name + "}")) { show("Параметр используется в шагах. Сначала замените ссылки на него."); return; }
  delete procedure.parameters[name]; renderParameterFields(true); syncEditor();
});

$("proc-parameter-fields").addEventListener("input", () => {
  const values = {};
  for (const input of $("proc-parameter-fields").querySelectorAll("[data-parameter]")) {
    if (input.value === "") continue;
    const type = input.dataset.type;
    values[input.dataset.parameter] = type === "boolean" ? input.value === "true" : ["number", "integer"].includes(type) ? Number(input.value) : input.value;
  }
  $("proc-parameters").value = JSON.stringify(values, null, 2);
});

function field(key, title, value = "", choices = null) {
  const label = el("label", title);
  const input = choices ? el("select") : el("input");
  input.id = `step-${key}`;
  if (choices) {
    for (const choice of choices) {
      const [v, text] = Array.isArray(choice) ? choice : [choice, choice];
      const option = el("option", text); option.value = v; input.append(option);
    }
    if (value !== "" && ![...input.options].some(o => o.value === String(value))) {
      const option = el("option", `${value} (нет в текущем профиле)`); option.value = value; input.append(option);
    }
  } else {
    input.type = "text";
    if (["feedrate", "duration_s", "seconds", "value", "acceleration", "volume_ul", "flow_ul_min", "times"].includes(key)) input.setAttribute("list", "numeric-parameters");
  }
  if (value != null && value !== "") input.value = String(value);
  label.append(input);
  $("step-fields").append(label);
  return input;
}

function help(text) { $("step-fields").append(el("p", text, "hint step-help")); }

function childParameterFields(selected, parameters) {
  const panel = el("div");
  panel.id = "child-parameter-fields";
  panel.className = "step-fields";
  const json = field("parameters", "Параметры дочерней процедуры (JSON, дополнительно)", JSON.stringify(parameters));
  const details = el("details");
  details.append(el("summary", "Параметры в JSON"), json.closest("label"));
  $("step-fields").append(panel, details);
  function render(values) {
    panel.replaceChildren();
    const child = knownProcedures.find(p => p.name === selected.value);
    const definitions = child?.parameter_definitions || {};
    const note = el("p", child ? `${child.title || child.name}: ${child.steps} шагов${child.draft ? ", черновик — для полного запуска сначала подготовьте дочернюю процедуру" : ""}. Пустое поле использует значение по умолчанию дочерней процедуры.` : "Сначала сохраните атомарную процедуру, затем выберите её здесь.", "hint");
    panel.append(note);
    for (const [name, definition] of Object.entries(definitions)) {
      const label = el("label", `${name} · ${definition.description || definition.type}`);
      const input = el("input");
      input.dataset.childParameter = name;
      input.dataset.type = definition.type;
      input.value = values[name] == null ? "" : String(values[name]);
      input.placeholder = definition.default == null ? "Обязательный параметр" : `По умолчанию: ${definition.default}`;
      if (["number", "integer"].includes(definition.type)) input.setAttribute("list", "numeric-parameters");
      input.addEventListener("input", () => {
        input.setCustomValidity("");
        try {
          const mapped = JSON.parse(json.value || "{}");
          const text = input.value.trim();
          if (!text) delete mapped[name];
          else if (/^\$\{[A-Za-z_][A-Za-z0-9_]*\}$/.test(text)) mapped[name] = text;
          else if (["number", "integer"].includes(definition.type)) {
            mapped[name] = number(text);
            if (definition.type === "integer" && !Number.isInteger(mapped[name])) throw new Error("Нужно целое число");
            if (definition.minimum != null && mapped[name] < definition.minimum) throw new Error(`Минимум: ${definition.minimum}`);
            if (definition.maximum != null && mapped[name] > definition.maximum) throw new Error(`Максимум: ${definition.maximum}`);
          } else if (definition.type === "boolean") {
            if (!["true", "false"].includes(text)) throw new Error("Введите true или false");
            mapped[name] = text === "true";
          } else mapped[name] = input.value;
          json.value = JSON.stringify(mapped);
        } catch (error) { input.setCustomValidity(error.message); }
      });
      label.append(input); panel.append(label);
    }
  }
  selected.addEventListener("change", () => { json.value = "{}"; render({}); });
  json.addEventListener("change", () => {
    try { render(JSON.parse(json.value || "{}")); } catch (error) { show(error.message); }
  });
  render(parameters);
}
function axisChoices() { return axes().map(a => [a.marlin_axis, `${a.marlin_axis} · ${a.name} (${a.units})`]); }
function motionAxisChoices() { return axisChoices().filter(([axis]) => !valves().some(v => v.axis === axis)); }
function axisUnit(axis) { const unit = axes().find(a => a.marlin_axis === axis)?.units || "ед. оси"; return ({mm:"мм", deg:"°"})[unit] || unit; }
function speedUnit(axis, unit = "axis/min") { return unit === "steps/s" ? "STEP/с" : axisUnit(axis) + (unit === "axis/s" ? "/с" : "/мин"); }
function valveChoices() { return valves().map(v => [v.name, `${valveNames[v.name] || v.name} · ${v.axis}`]); }
function pumpChoices() { return pumps().map(p => [p.name, `${pumpNames[p.name] || p.name} · ${p.axis}`]); }
function speedFields(s) { field("feedrate", "Скорость, ед. оси/мин (пусто — из профиля)", s.feedrate); field("acceleration", "Ускорение, ед. оси/с² (необязательно)", s.acceleration); }
function motionTimingFields(s, independent = false) {
  const choices = [["speed", independent ? "Расстояние и общая скорость" : "Расстояние и скорость"], ["duration", independent ? "Расстояние и общее время" : "Расстояние и время"]];
  if (independent) choices.push(["independent", "Своя скорость для каждого насоса"]);
  const mode = field("timing", "Как задать ход", s.axis_feedrates ? "independent" : s.duration_s != null ? "duration" : "speed", choices);
  const speed = field("feedrate", "Скорость (пусто — из профиля)", s.feedrate);
  const duration = field("duration_s", "Номинальное время хода, с", s.duration_s);
  const toggle = () => {
    speed.closest("label").hidden = mode.value !== "speed";
    duration.closest("label").hidden = mode.value !== "duration";
    if (independent) for (const label of $("step-fields").querySelectorAll("[data-individual-speed]")) label.hidden = mode.value !== "independent";
  };
  mode.addEventListener("change", toggle); toggle();
  field("acceleration", "Ускорение, ед. оси/с² (необязательно)", s.acceleration);
}

function renderStepFields(s = {}) {
  $("step-fields").replaceChildren();
  const op = $("step-op").value;
  if (["home", "home_valve"].includes(op)) field("only_if_needed", "Когда искать ноль", String(s.only_if_needed ?? (s.op ? false : true)), [["true", "Только если привязка потеряна"], ["false", "Повторно, даже если ноль уже найден"]]);
  if (op === "set_valve" || op === "home_valve") {
    const selectedValve = field("name", "Кран", s.name, valveChoices());
    if (op === "set_valve") {
      const position = field("position", "Сохранённая позиция", s.position || "A", []);
      const refreshPositions = () => {
        const valve = valves().find(v => v.name === selectedValve.value);
        const choices = [["A", "A · положение 1"], ["B", "B · положение 2"], ...Object.entries(valve?.position_names || {}).map(([name, slot]) => [name, `${name} (${slot})`])];
        const previous = position.value || s.position || "A";
        position.replaceChildren(...choices.map(([name, title]) => { const option = el("option", title); option.value = name; return option; }));
        if (choices.some(([name]) => name === previous)) position.value = previous;
      };
      selectedValve.addEventListener("change", refreshPositions);
      refreshPositions();
      speedFields(s);
    }
    help("HOME — ноль по одному микрику. Рабочие A/B — сохранённые смещения от нуля. Позиции и параметры поиска HOME настраиваются на странице кранов.");
  } else if (["home", "motors", "require_reference"].includes(op)) {
    field("axes", "Оси через запятую", s.axes?.join(",") ?? (op === "motors" ? "" : "X,Y"));
    if (op === "motors") field("enabled", "Действие", String(s.enabled ?? false), [["false", "Отключить удержание"], ["true", "Включить удержание"]]);
    help(op === "require_reference" ? "Проверяет привязку без движения. Ноль автоматически не устанавливает." : op === "home" ? "Готовые оси можно пропустить. Полный промер хода не повторяется. Пустое поле — все узлы с HOME, без U." : "Отключение удержания завершает сеанс и сбрасывает нули. Для серии процедур оставьте моторы включёнными; в конце всей работы отпустите их кнопкой над конструктором.");
  } else if (op === "move") {
    field("axis", "Ось", s.axis, motionAxisChoices());
    field("kind", "Перемещение", s.to != null ? "to" : "by", [["to", "В координату"], ["by", "На смещение"]]);
    field("value", "Величина или ${parameter}", s.to ?? s.by);
    motionTimingFields(s);
    field("units", "Единицы перемещения", s.units || "axis", [["axis", "Единицы оси из профиля"], ["steps", "Импульсы STEP"]]);
    field("speed_units", "Единицы скорости", s.speed_units || "axis/min", ["axis/min", "axis/s", "steps/s"]);
    help("Выберите расстояние со скоростью либо расстояние со временем. Заданное время не учитывает разгон и торможение. Краны задаются действием «Положение крана».");
  } else if (op === "move_for") {
    field("axis", "Ось", s.axis, motionAxisChoices());
    field("direction", "Направление", String(s.direction ?? 1), [["1", "+"], ["-1", "−"]]);
    field("feedrate", "Скорость", s.feedrate ?? 120);
    field("speed_units", "Единицы скорости", s.speed_units || "axis/min", [["axis/min", "Единицы оси/мин (X/Y: мм/мин)"], ["axis/s", "Единицы оси/с (X/Y: мм/с)"], ["steps/s", "STEP/с"]]);
    field("seconds", "Время движения, с", s.seconds ?? 2);
    field("acceleration", "Ускорение, ед. оси/с² (необязательно)", s.acceleration);
    help("Планируется ход: скорость × время. Например, 120 мм/мин × 2 с = 4 мм. Разгон и торможение увеличивают фактическое время. Нужен HOME; настройки объёма здесь не используются.");
  } else if (op === "move_multi_for") {
    for (const axis of axes().filter(a => ["syringe_pump", "peristaltic_pump"].includes(a.kind))) {
      const move = s.axes?.[axis.marlin_axis];
      const row = el("fieldset"); row.dataset.timedAxis = axis.marlin_axis;
      const pump = pumps().find(p => p.axis === axis.marlin_axis);
      row.append(el("legend", `${axis.marlin_axis} · ${pumpNames[pump?.name] || axis.name}`));
      const enabledLabel = el("label", "Участвует"); const enabled = el("input"); enabled.type = "checkbox"; enabled.dataset.timedEnabled = ""; enabled.checked = Boolean(move); enabledLabel.prepend(enabled); row.append(enabledLabel);
      const directionLabel = el("label", "Направление"); const direction = el("select"); direction.dataset.timedDirection = "";
      for (const [value, title] of [["1", "+"], ["-1", "−"]]) { const option = el("option", title); option.value = value; direction.append(option); }
      direction.value = String(move?.direction ?? 1); directionLabel.append(direction); row.append(directionLabel);
      const speedLabel = el("label", `Скорость привода ${axis.marlin_axis}`); const speed = el("input"); speed.dataset.timedFeedrate = ""; speed.setAttribute("list", "numeric-parameters"); speed.value = move?.feedrate ?? 120; speedLabel.append(speed); row.append(speedLabel);
      const unitsLabel = el("label", "Единицы скорости"); const units = el("select"); units.dataset.timedUnits = "";
      for (const value of ["axis/min", "axis/s", "steps/s"]) { const option = el("option", speedUnit(axis.marlin_axis, value)); option.value = value; units.append(option); }
      units.value = move?.speed_units || "axis/min"; unitsLabel.append(units); row.append(unitsLabel);
      const toggle = () => { direction.disabled = speed.disabled = units.disabled = !enabled.checked; };
      enabled.addEventListener("change", toggle); toggle(); $("step-fields").append(row);
    }
    field("seconds", "Общее номинальное время подачи, с", s.seconds ?? 10);
    field("acceleration", "Ускорение (необязательно)", s.acceleration);
    help("Оба насоса работают весь этап одним совместным ходом. У каждого своя скорость и направление; расстояния рассчитываются автоматически. Разгон и торможение добавляют время. Для объёмов и расходов в мкл используйте «Насосы вместе: объём и расход каждого» после измерения коэффициентов.");
  } else if (op === "move_multi") {
    const grid = el("div", undefined, "multi-axis-fields");
    for (const axis of axes().filter(a => !valves().some(v => v.axis === a.marlin_axis))) {
      const row = el("fieldset"); row.append(el("legend", `${axis.marlin_axis} · ${axis.name} (${axisUnit(axis.marlin_axis)})`));
      const distanceLabel = el("label", "Координата / смещение");
      const input = el("input"); input.dataset.multiAxis = axis.marlin_axis; input.placeholder = "Пусто — не участвует"; input.value = s.axes?.[axis.marlin_axis] ?? "";
      distanceLabel.append(input); row.append(distanceLabel);
      const speedLabel = el("label", `Скорость ${axis.marlin_axis}, ${speedUnit(axis.marlin_axis)}`); speedLabel.dataset.individualSpeed = "";
      const speed = el("input"); speed.dataset.axisFeedrate = axis.marlin_axis; speed.setAttribute("list", "numeric-parameters"); speed.value = s.axis_feedrates?.[axis.marlin_axis] ?? axis.feedrate_default;
      speedLabel.append(speed); row.append(speedLabel); grid.append(row);
    }
    $("step-fields").append(grid);
    field("relative", "Координаты", String(s.relative ?? false), [["false", "Абсолютные"], ["true", "Смещения"]]);
    motionTimingFields(s, true);
    help("Для двух насосов выберите «Своя скорость для каждого насоса»: задайте ход и скорость X и Y. Насосы начнут вместе, каждый завершит свой ход отдельно. Значения в мм и мм/мин для X/Y; калибровка объёма не нужна. Пустая координата исключает ось. Разгон и торможение влияют на время.");
  } else if (op === "pump") {
    field("name", "Насос", s.name, pumpChoices());
    field("volume_ul", "Объём, мкл", s.volume_ul);
    field("flow_ul_min", "Расход, мкл/мин", s.flow_ul_min);
    field("direction", "Направление", s.direction || "dispense", [["dispense", "Подача"], ["aspirate", "Забор"]]);
    field("acceleration", "Ускорение, ед. оси/с² (необязательно)", s.acceleration);
    help("Для подачи объёма нужен откалиброванный коэффициент мкл/ед. оси.");
  } else if (op === "pump_multi") {
    for (const pump of pumps()) {
      const dose = s.pumps?.[pump.name] || {};
      const row = el("fieldset"); row.dataset.pump = pump.name;
      row.append(el("legend", `${pumpNames[pump.name] || pump.name} · ${pump.axis}`));
      for (const [key, title] of [["volume_ul", "Объём, мкл"], ["flow_ul_min", "Расход, мкл/мин"]]) {
        const label = el("label", title); const input = el("input"); input.dataset.doseField = key; input.value = dose[key] ?? ""; label.append(input); row.append(label);
      }
      const select = el("select"); select.dataset.doseField = "direction";
      for (const [v, text] of [["dispense", "Подача"], ["aspirate", "Забор"]]) { const option = el("option", text); option.value = v; select.append(option); }
      select.value = dose.direction || "dispense"; row.append(select); $("step-fields").append(row);
      const time = el("output", "Время рассчитаем из объёма и расхода"); row.append(time);
      const updateTime = () => {
        const volume = Number(row.querySelector('[data-dose-field="volume_ul"]').value);
        const flow = Number(row.querySelector('[data-dose-field="flow_ul_min"]').value);
        time.textContent = volume > 0 && flow > 0 ? `Номинальное время: ${(60 * volume / flow).toFixed(2)} с` : "Время рассчитаем при проверке параметров";
      };
      row.addEventListener("input", updateTime); updateTime();
    }
    field("acceleration", "Ускорение (необязательно)", s.acceleration);
    help("Каждому насосу задайте свой объём и расход. Оба начинают вместе; время каждого считается как 60 × объём / расход. Закончивший свою дозу насос останавливается. Для одновременного окончания задайте одинаковое отношение объём/расход. Нужны измеренные коэффициенты насосов; пустые насосы не участвуют.");
  } else if (op === "dwell") field("seconds", "Пауза, с", s.seconds ?? 1);
  else if (op === "log" || op === "require_setup") {
    field("message", op === "log" ? "Текст сообщения" : "Что требуется настроить", s.message);
    if (op === "require_setup") help("Этот пункт сохраняет незавершённую часть плана. Запуск будет запрещён, пока вы не замените или не удалите его.");
  } else if (op === "checkpoint") field("name", "Отметка этапа", s.name);
  else if (op === "call" || op === "repeat") {
    const selected = field("procedure", "Сохранённая процедура", s.procedure, knownProcedures.filter(p => p.ok && p.name !== procedure.name).map(p => [p.name, `${p.title || p.name} · ${p.composite ? "составная" : "атомарная"}${p.draft ? " · черновик" : ""}`]));
    if (op === "repeat") field("times", "Число повторений", s.times ?? 1);
    childParameterFields(selected, s.parameters || {});
    help("Можно вкладывать составные процедуры друг в друга. Число повторений: 1–1000 или ${cycles}. Рекурсивные вызовы запрещены; предел — 8 уровней и 10 000 развёрнутых шагов.");
  } else if (op === "read_sensors") {
    field("sensor_names", "Имена датчиков через запятую (пусто — все)", s.names?.join(", ") || "");
    field("samples", "Число измерений, 1–1000", s.samples ?? 1);
    field("interval_s", "Пауза между ответом и следующим чтением, с (от 0,1)", s.interval_s ?? 1);
    field("require_available", "Если прошивка не вернула сигнал", String(s.require_available ?? false), [["false", "Записать «недоступен»"], ["true", "Остановить процедуру с ошибкой"]]);
    help("Только чтение M105, без включения нагревателей. Источники задаются в профиле → Датчики. Серия измерений выполняется последовательно, не параллельно движению.");
  }
  else if (["seek", "test_reference"].includes(op)) {
    field("axis", "Ось", s.axis, axisChoices());
    field("channel", "Проверенное имя входа", s.channel);
    field("by", "Предельное смещение со знаком", s.by);
    field("feedrate", "Скорость, ед. оси/мин", s.feedrate);
    if (op === "seek") field("zero", "Установить ноль при касании", String(s.zero ?? false), [["false", "Нет"], ["true", "Да"]]);
    else {
      field("reference_feedrate", "Скорость точного подхода", s.reference_feedrate);
      field("backoff", "Отход", s.backoff);
      field("tolerance_pulses", "Допуск, импульсы STEP", s.tolerance_pulses);
      field("repeats", "Повторения", s.repeats ?? 3);
      field("acceleration", "Ускорение (необязательно)", s.acceleration);
    }
    help("Требуется подтверждённый firmware-stopped probe. Для кранов используйте Home крана.");
  }
}

function number(value, required = true) {
  const text = String(value ?? "").trim();
  if (!text) { if (!required) return undefined; throw new Error("Числовое поле не заполнено"); }
  if (/^\$\{[A-Za-z_][A-Za-z0-9_]*\}$/.test(text)) return text;
  const n = Number(text);
  if (!Number.isFinite(n)) throw new Error(`Ожидается число или ссылка на параметр: ${text}`);
  return n;
}

function buildStep() {
  const op = $("step-op").value;
  const s = { op };
  const value = key => $(`step-${key}`)?.value;
  const numeric = (keys, required = true) => { for (const key of keys) { const n = number(value(key), required); if (n !== undefined) s[key] = n; } };
  if ($("step-title").value.trim()) s.title = $("step-title").value.trim();
  if (["home_valve", "set_valve", "checkpoint", "pump"].includes(op)) { s.name = value("name"); if (!s.name) throw new Error("Выберите узел / укажите имя"); }
  if (op === "set_valve") { s.position = value("position"); numeric(["feedrate", "acceleration"], false); }
  if (["home", "motors", "require_reference"].includes(op)) { const a = value("axes").split(",").map(v => v.trim()).filter(Boolean); s.axes = a.length ? a : null; if (op === "require_reference" && !a.length) throw new Error("Укажите оси для проверки нулей"); }
  if (op === "motors") s.enabled = value("enabled") === "true";
  if (["home", "home_valve"].includes(op)) s.only_if_needed = value("only_if_needed") === "true";
  if (op === "move") { s.axis = value("axis"); s[value("kind")] = number(value("value")); s.units = value("units"); s.speed_units = value("speed_units"); numeric(value("timing") === "duration" ? ["duration_s"] : ["feedrate"], value("timing") === "duration"); numeric(["acceleration"], false); }
  if (op === "move_for") { s.axis = value("axis"); s.direction = Number(value("direction")); s.speed_units = value("speed_units"); numeric(["feedrate", "seconds"]); numeric(["acceleration"], false); }
  if (op === "move_multi_for") {
    s.axes = {};
    for (const row of $("step-fields").querySelectorAll("[data-timed-axis]")) {
      if (!row.querySelector("[data-timed-enabled]").checked) continue;
      s.axes[row.dataset.timedAxis] = {direction: Number(row.querySelector("[data-timed-direction]").value), feedrate: number(row.querySelector("[data-timed-feedrate]").value), speed_units: row.querySelector("[data-timed-units]").value};
    }
    if (Object.keys(s.axes).length < 2) throw new Error("Выберите минимум два насоса");
    numeric(["seconds"]); numeric(["acceleration"], false);
  }
  if (op === "move_multi") {
    s.axes = {};
    for (const input of $("step-fields").querySelectorAll("[data-multi-axis]")) { if (input.value.trim()) s.axes[input.dataset.multiAxis] = number(input.value); }
    if (Object.keys(s.axes).length < 2) throw new Error("Для совместного движения укажите минимум две оси");
    s.relative = value("relative") === "true";
    if (value("timing") === "independent") {
      s.axis_feedrates = {};
      for (const input of $("step-fields").querySelectorAll("[data-axis-feedrate]")) {
        if (input.dataset.axisFeedrate in s.axes) s.axis_feedrates[input.dataset.axisFeedrate] = number(input.value);
      }
    } else numeric(value("timing") === "duration" ? ["duration_s"] : ["feedrate"], value("timing") === "duration");
    numeric(["acceleration"], false);
  }
  if (op === "pump") { numeric(["volume_ul", "flow_ul_min"]); s.direction = value("direction"); numeric(["acceleration"], false); }
  if (op === "pump_multi") {
    s.pumps = {};
    for (const row of $("step-fields").querySelectorAll("[data-pump]")) {
      const dose = key => row.querySelector(`[data-dose-field="${key}"]`).value;
      if (!dose("volume_ul").trim() && !dose("flow_ul_min").trim()) continue;
      s.pumps[row.dataset.pump] = { volume_ul: number(dose("volume_ul")), flow_ul_min: number(dose("flow_ul_min")), direction: dose("direction") };
    }
    if (Object.keys(s.pumps).length < 2) throw new Error("Укажите минимум два насоса");
    numeric(["acceleration"], false);
  }
  if (op === "dwell") numeric(["seconds"]);
  if (["log", "require_setup"].includes(op)) { s.message = value("message"); if (!s.message.trim()) throw new Error("Заполните текст шага"); }
  if (op === "call" || op === "repeat") {
    for (const input of $("child-parameter-fields").querySelectorAll("input")) if (!input.reportValidity()) throw new Error(input.validationMessage);
    s.procedure = value("procedure"); s.parameters = JSON.parse(value("parameters") || "{}");
    if (!s.procedure) throw new Error("Выберите сохранённую процедуру");
    if (!s.parameters || typeof s.parameters !== "object" || Array.isArray(s.parameters)) throw new Error("Параметры должны быть объектом");
  }
  if (op === "repeat") {
    numeric(["times"]);
    if (typeof s.times === "number" && (!Number.isInteger(s.times) || s.times < 1 || s.times > 1000)) throw new Error("Число повторений — целое от 1 до 1000");
  }
  if (op === "read_sensors") {
    numeric(["samples", "interval_s"]);
    const names = value("sensor_names").split(",").map(n => n.trim()).filter(Boolean);
    if (names.length) s.names = names;
    s.require_available = value("require_available") === "true";
  }
  if (["seek", "test_reference"].includes(op)) {
    s.axis = value("axis"); s.channel = value("channel"); numeric(["by", "feedrate"]);
    if (op === "seek") s.zero = value("zero") === "true";
    else { numeric(["reference_feedrate", "backoff", "tolerance_pulses", "repeats"]); numeric(["acceleration"], false); }
  }
  return s;
}

function parseParameters() {
  const data = JSON.parse($("proc-parameters").value || "{}");
  if (!data || Array.isArray(data) || typeof data !== "object") throw new Error("Параметры запуска должны быть объектом JSON");
  return data;
}

async function reload() {
  knownProcedures = await api.procedures();
  $("proc-list").replaceChildren();
  for (const p of knownProcedures) {
    const item = el("li");
    if (!p.ok) item.textContent = `${p.file}: ${p.error}`;
    else {
      const button = el("button", p.title || p.name); button.type = "button"; button.dataset.open = p.name;
      item.append(button, el("small", `${p.steps} шагов · ${p.composite ? "составная" : "атомарная"} · ${p.draft ? "черновик" : "сценарий"}`));
      if (p.description) button.title = p.description;
    }
    $("proc-list").append(item);
  }
  updateButtons();
}

async function saveCurrent() {
  if (editing !== null) throw new Error("Сначала примените или отмените изменение шага");
  syncHeader();
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(procedure.name)) throw new Error("Имя файла: до 64 латинских букв, цифр, _ и -; первый символ — буква или цифра");
  const saved = await api.saveProcedure(procedure.name, procedure);
  syncEditor();
  savedText = JSON.stringify(procedure);
  rememberProcedure(procedure.name);
  await reload();
  return saved;
}

async function action(fn) {
  if (busy || running) return;
  busy = true; updateButtons();
  try { await fn(); }
  catch (error) { show(error.message); }
  finally { busy = false; updateButtons(); }
}

function previewReport(report) {
  show(report, `Проверено ${report.steps.length} шагов. Команды на аппарат не отправлялись.`);
  const issues = report.execution_issues || [];
  if (report.draft) $("proc-feedback").append(el("p", "Черновик: при запуске проверим готовность и снимем эту отметку после подтверждения запуска."));
  for (const warning of report.session_warnings || []) $("proc-feedback").append(el("p", warning, "hint"));
  issues.push(...(report.readiness_issues || []));
  if (issues.length) {
    const list = el("ul", undefined, "preview-issues");
    for (const issue of issues) list.append(el("li", issue
      .replace(/^step (\d+): /, "Шаг $1: ")
      .replace("shared_valve_endstop channel is not configured", "Не выбран общий вход концевиков на странице кранов")
      .replace("valve A/B commissioning required", "Рабочие положения крана A/B ещё не подтверждены")
      .replace("pump is unknown or uncalibrated", "Насос не определён или не откалиброван по объёму")));
    $("proc-feedback").append(el("strong", "Что осталось настроить:"), list);
  } else $("proc-feedback").append(el("p", "Статических ошибок не найдено. Физические позиции проверяются на установке."));
  const sequence = el("ol");
  report.steps.forEach((step, index) => {
    const item = el("li", `${step.title || opNames[step.op] || step.op}: ${describeStep(step)}`);
    item.append(el("small", report.step_paths?.[index] || "", "hint"));
    sequence.append(item);
  });
  $("proc-feedback").append(sequence);
}

$("proc-list").addEventListener("click", event => {
  const button = event.target.closest("button[data-open]");
  if (!button) return;
  if (!mayReplaceProcedure()) return;
  action(async () => { setProcedure(await api.procedure(button.dataset.open)); show("Процедура открыта. Изменения применяются после сохранения."); });
});

$("proc-steps").addEventListener("click", event => {
  const button = event.target.closest("button[data-action]");
  if (!button || busy || running) return;
  const index = Number(button.dataset.index);
  const what = button.dataset.action;
  if (what === "run") {
    if (!window.confirm(`Выполнить только шаг ${index + 1}?\n\n${describeStep(procedure.steps[index])}\n\nКоманда будет отправлена на подключённый аппарат.`)) return;
    action(async () => { await saveCurrent(); const report = await api.runProcedureStep(procedure.name, index + 1, parseParameters()); show(report, "Шаг завершён. Проверьте фактическое положение на установке."); await refreshStatus(); });
    return;
  }
  if (what === "edit") {
    const step = procedure.steps[index];
    if (!(step.op in opNames)) { show("Для этого действия используйте JSON в разделе разработчика."); return; }
    editing = index;
    $("step-title").value = step.title || "";
    $("step-op").value = step.op;
    renderStepFields(step);
    $("step-builder-title").textContent = `Изменить шаг ${index + 1}`;
    $("add-step").textContent = "Применить изменение";
    $("cancel-step-edit").hidden = false;
    $("step-builder-title").scrollIntoView({ behavior: "smooth", block: "center" });
    renderSteps(); return;
  }
  if (editing !== null) { show("Сначала примените или отмените изменение шага."); return; }
  if (what === "copy") procedure.steps.splice(index + 1, 0, structuredClone(procedure.steps[index]));
  if (what === "remove") procedure.steps.splice(index, 1);
  if (what === "up" && index > 0) [procedure.steps[index - 1], procedure.steps[index]] = [procedure.steps[index], procedure.steps[index - 1]];
  if (what === "down" && index < procedure.steps.length - 1) [procedure.steps[index + 1], procedure.steps[index]] = [procedure.steps[index], procedure.steps[index + 1]];
  renderSteps();
});

$("add-step").addEventListener("click", () => {
  try {
    const step = buildStep();
    if (editing === null) procedure.steps.push(step); else procedure.steps[editing] = step;
    resetStepEditor(); renderSteps(); show("Шаг применён. Сохраните процедуру.");
  } catch (error) { show(error.message); }
});
$("cancel-step-edit").addEventListener("click", () => { resetStepEditor(); renderSteps(); });
$("step-op").addEventListener("change", () => renderStepFields());
$("proc-draft").addEventListener("change", syncEditor);
$("new-proc").addEventListener("click", () => { if (!mayReplaceProcedure()) return; const p = emptyProcedure(); let i = 1; while (knownProcedures.some(v => v.name === p.name)) p.name = `new_procedure_${i++}`; setProcedure(p, false); show("Новый черновик. Задайте имя и добавьте действия."); });
$("copy-proc").addEventListener("click", () => {
  if (editing !== null) { show("Сначала примените или отмените изменение шага."); return; }
  syncHeader(); const p = structuredClone(procedure); const base = p.name.slice(0, 52); let i = 1;
  do { p.name = `${base}_copy_${i++}`; } while (knownProcedures.some(v => v.name === p.name));
  p.title = `${p.title || procedure.name} — копия`; p.draft = true; setProcedure(p, false); show("Копия создана в редакторе. Сохраните её с новым именем.");
});
$("save-proc").addEventListener("click", () => action(async () => show(await saveCurrent(), "Процедура сохранена.")));
$("save-run-defaults").addEventListener("click", () => action(async () => {
  const values = parseParameters();
  for (const [name, value] of Object.entries(values)) {
    if (!(name in procedure.parameters)) throw new Error(`Неизвестный параметр ${name}`);
    procedure.parameters[name].default = value;
  }
  await saveCurrent();
  show("Параметры сохранены как значения по умолчанию.");
}));
$("preview-proc").addEventListener("click", () => action(async () => { await saveCurrent(); previewReport(await api.previewProcedure(procedure.name, parseParameters())); }));
$("reload-procs").addEventListener("click", () => action(reload));
$("apply-json").addEventListener("click", () => { try { setProcedure(JSON.parse($("proc-detail").value), false); show("JSON применён в редакторе. Сохраните для проверки формата."); } catch (error) { show(error.message); } });
$("export-proc").addEventListener("click", () => {
  syncHeader();
  const url = URL.createObjectURL(new Blob([JSON.stringify(procedure, null, 2)], { type: "application/json" }));
  const link = el("a"); link.href = url; link.download = `${procedure.name.replace(/[^A-Za-z0-9_-]/g, "_")}.json`; link.click(); URL.revokeObjectURL(url);
});
$("run-proc").addEventListener("click", () => {
  action(async () => {
    await saveCurrent();
    const report = await api.previewProcedure(procedure.name, parseParameters());
    if (report.execution_issues.length || report.readiness_issues?.length) { previewReport(report); return; }
    const state = await api.state();
    if (state.state !== "connected_idle") { show(`Аппарат занят или не подключён: ${state.detail || state.state}`); return; }
    if (!window.confirm(`Запустить «${procedure.title || procedure.name}» на ${state.transport === "mock" ? "симуляторе" : "подключённом аппарате"}?${procedure.draft ? "\nПроверка пройдена, отметка черновика будет снята." : ""}${report.session_warnings?.length ? "\n\n" + report.session_warnings.join("\n") : ""}`)) return;
    if (procedure.draft) { $("proc-draft").checked = false; await saveCurrent(); }
    show(await api.runProcedure(procedure.name, parseParameters()), "Процедура запущена."); await refreshStatus();
  });
});
$("abort-proc").addEventListener("click", async () => {
  try { show(await api.abortProcedure(), "Процедура остановлена. Привязка координат потеряна."); await refreshStatus(); }
  catch (error) { show(error.message); }
});

async function refreshStatus() {
  try {
    activeStatus = await api.procedureStatus(); running = activeStatus.running;
    $("proc-status").textContent = activeStatus.error ? `Ошибка: ${activeStatus.error}` : running ? `Выполняется ${activeStatus.name}: ${activeStatus.step_path || activeStatus.step}` : activeStatus.name ? `Завершено: ${activeStatus.name}` : "Нет запущенной процедуры";
    updateButtons();
  } catch { $("proc-status").textContent = "Нет связи с сервером"; }
}

async function init() {
  updateButtons();
  try {
    hardware = await api.config();
    await reload();
    const requested = new URL(window.location.href).searchParams.get("procedure");
    let remembered = null;
    try { remembered = localStorage.getItem("flowengine.procedure"); } catch (_) { }
    const selected = knownProcedures.find(p => p.name === requested && p.ok)
      || (!requested && knownProcedures.find(p => p.name === remembered && p.ok));
    if (requested && !selected) {
      setProcedure(emptyProcedure(), false);
      show(`Процедура «${requested}» не найдена. Выберите её из списка слева.`);
    } else {
      const initial = selected;
      setProcedure(initial ? await api.procedure(initial.name) : emptyProcedure(), Boolean(initial));
    }
    await refreshStatus();
  } catch (error) { show(`Не удалось загрузить конструктор: ${error.message}`); }
  finally { busy = false; updateButtons(); }
}
init();
let polling = false;
setInterval(async () => {
  if (document.hidden || polling) return;
  polling = true;
  try { await refreshStatus(); } finally { polling = false; }
}, 1000);

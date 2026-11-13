import { api } from "../api.js";

const el = id => document.getElementById(`commission-${id}`);
const value = id => el(id).value.trim();
const number = id => {
  if (!value(id)) throw new Error(`Заполните ${id}`);
  const n = Number(value(id));
  if (!Number.isFinite(n)) throw new Error(`Неверное число: ${id}`);
  return n;
};
const name = () => value("name");
const root = () => `/api/commissioning/profiles/${encodeURIComponent(name())}`;
const show = data => { el("result").textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2); };
const serviceLabels = { A: "кран промывки", B: "кран обжимающей жидкости", C: "кран сэмплера",
  X: "шприц пробы", Y: "шприц обжимающей жидкости", U: "насос промывки" };
async function request(path, data) {
  const response = await fetch(path, { method: data === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" }, ...(data === undefined ? {} : { body: JSON.stringify(data) }) });
  const text = await response.text();
  let result;
  try { result = JSON.parse(text); } catch { throw new Error(text || response.statusText); }
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : JSON.stringify(result.detail));
  return result;
}
function render(profile) {
  el("profile").value = JSON.stringify(profile, null, 2);
  el("name").value = profile.name;
  for (const [id, items, key] of [["axis", profile.device_map.axes, "marlin_axis"],
    ["service-axis", profile.device_map.axes, "marlin_axis"],
    ["pump", profile.device_map.pumps, "name"], ["valve", profile.device_map.valves, "name"]]) {
    const selected = el(id).value;
    el(id).replaceChildren(...items.map(item => new Option(id === "service-axis"
      ? `${item[key]} — ${serviceLabels[item[key]] || item.name}`
      : `${item[key]}${item.axis ? ` (${item.axis})` : ""}`, item[key])));
    if (items.some(i => i[key] === selected)) el(id).value = selected;
  }
}
function action(id, fn) {
  el(id).addEventListener("click", async () => {
    const buttons = [...document.querySelectorAll("#commissioning button")];
    buttons.forEach(b => { b.disabled = true; });
    try {
      const result = await fn();
      if (result?.profile && typeof result.profile === "object") render(result.profile);
      if (result !== undefined) show(result);
    } catch (error) { show(`ОШИБКА: ${error.message}`); }
    finally { buttons.forEach(b => { b.disabled = false; }); }
  });
}
async function refresh() { render(await api.profile(name())); }
async function saveObservation(kind, device, passed, notes, data = {}) {
  return request(`${root()}/observations`, { kind, device, passed, notes, data });
}

el("service-axis").addEventListener("change", () => {
  const axis = value("service-axis");
  el("service-channel").value = axis === "X" ? "x_min" : axis === "Y" ? "y_min" : "z_min";
});
action("service-read", async () => {
  const result = await api.endstops();
  el("service-result").textContent = JSON.stringify(result, null, 2);
  return result;
});
action("service-run", async () => {
  el("service-result").textContent = "Проверка настроек…";
  try {
    const data = { axis: value("service-axis"), channel: value("service-channel"),
      delta: number("service-delta"), feedrate: number("service-speed"), release_motor: true, confirmed: true };
    if (!data.delta || Math.abs(data.delta) > 20 || data.feedrate <= 0 || data.feedrate > 120) {
      throw new Error("Нужен ненулевой шаг до 20 единиц и скорость от 0 до 120 (не включая 0).");
    }
    await api.profile(name());
    const state = await api.state();
    if (state.profile !== name()) throw new Error("Откройте активный профиль перед пробой.");
    if (state.state !== "connected_idle") throw new Error("Контроллер должен быть подключён и свободен.");
    const inputs = await api.endstops();
    if (inputs.triggered[data.channel] !== false) throw new Error("Контрольный вход должен быть OPEN и присутствовать в M119.");
    el("service-result").textContent = `Выполняется ${data.axis}: ${data.delta}, скорость ${data.feedrate}…`;
    const result = await api.workbench("service-jog", data);
    el("service-result").textContent = `Команда выполнена. Отпускание мотора ${data.axis}: ${result.motor_released ? "подтверждено платой" : "не подтверждено"}.\nПоложение после сервисной пробы не считается найденным.\n${JSON.stringify(result, null, 2)}`;
    try {
      await saveObservation("motion_result", data.axis, true, "Сервисная команда выполнена; физический результат проверяет оператор", { ...data, ...result });
    } catch (error) {
      el("service-result").textContent += `\nЖурнал не сохранён: ${error.message}. Движение повторять не нужно.`;
    }
    return result;
  } catch (error) {
    el("service-result").textContent = `ОШИБКА: ${error.message}`;
    throw error;
  }
});
action("active", async () => {
  const data = await request("/api/commissioning/active"); render(data.profile);
  return "Активные настройки скопированы в редактор. Сохраните профиль перед записью измерений.";
});
action("load", async () => { await refresh(); return "Сохранённый профиль открыт. На железо не применён."; });
action("save", async () => {
  const profile = JSON.parse(el("profile").value); profile.name = name();
  const result = await api.saveProfile(name(), profile); await refresh(); return result;
});
action("apply", async () => {
  if (!window.confirm("Применить СОХРАНЁННЫЙ профиль? Несохранённый JSON не применяется. Все оси потеряют нули; моторы не перемещаются.")) return;
  return request(`${root()}/apply`, {});
});
action("reference", async () => {
  if (!window.confirm("Механизм физически установлен в указанную координату? Это ручное утверждение, датчик не проверяется.")) return;
  const data = { axis: value("axis"), position: number("zero"), notes: value("reference-notes"), confirmed: true };
  // Require a saved profile before issuing a hardware command, so the result has a home.
  await api.profile(name());
  const result = await request("/api/commissioning/reference", data);
  await saveObservation("reference_result", data.axis, true, data.notes, result);
  await refresh(); return result;
});
action("jog", async () => {
  if (!window.confirm(`Переместить ${value("axis")} на ${value("delta")} с удержанием?`)) return;
  await api.profile(name());
  const data = { axis: value("axis"), by: number("delta"), feedrate: number("feedrate") };
  const result = await request("/api/commissioning/jog", data);
  await saveObservation("motion_result", data.axis, true, "Наладочное перемещение найденной оси", { ...data, ...result });
  await refresh(); return result;
});
action("position", () => api.state());
for (const slot of ["a", "b"]) action(`capture-${slot}`, async () => {
  const state = await api.state();
  const profile = JSON.parse(el("profile").value);
  const valve = profile.device_map.valves.find(v => v.name === value("valve"));
  if (!state.homed[valve.axis]) throw new Error(`Ось ${valve.axis} не найдена / была отпущена`);
  el(`valve-${slot}`).value = state.positions[valve.axis];
  return `Координата ${state.positions[valve.axis]} подставлена в ${slot.toUpperCase()}. Проверьте физический маршрут и сохраните оба положения.`;
});
action("valve-save", async () => request(`${root()}/valve`, {
  name: value("valve"), position_a: number("valve-a"), position_b: number("valve-b"),
  notes: value("valve-notes"), confirmed: el("valve-confirm").checked,
}));
action("pump-save", async () => request(`${root()}/pump`, {
  name: value("pump"), measurements: JSON.parse(el("measurements").value),
  dispense_direction: number("dispense"), notes: value("pump-notes"), confirmed: el("pump-confirm").checked,
}));
action("period", async () => {
  if (!window.confirm("Вход датчика U проверен, ход свободен, направление и скорость выбраны? Насос U будет двигаться до двух срабатываний или предела хода.")) return;
  await api.profile(name());
  let result;
  try {
    result = await request("/api/commissioning/period", {
      channel: value("period-channel"), direction: number("period-direction"),
      step: number("period-step"), max_distance: number("period-distance"), feedrate: number("feedrate"), confirmed: true,
    });
  } catch (error) {
    await saveObservation("period_result", "U", false, value("period-notes") || "Измерение периода не завершилось", { error: error.message });
    await refresh(); throw error;
  }
  await saveObservation("period_result", "U", true, value("period-notes") || "Измерен период датчика U", result);
  await refresh(); return result;
});
action("record", () => saveObservation(value("check"), value("device"), value("passed") === "true", value("notes")));
action("readiness", async () => {
  const result = await request(`${root()}/readiness`);
  el("checklist").replaceChildren(...result.checks.map(c => {
    const p = document.createElement("p");
    p.textContent = `${c.passed ? "Пройдена" : c.stale ? "Повторить после изменения профиля" : "Не пройдена"}: ${c.label}. ${c.notes}`;
    return p;
  }));
  return result;
});
action("export", async () => {
  const bundle = await request(`${root()}/bundle`);
  const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = `${name()}-handoff.json`; link.click(); URL.revokeObjectURL(url);
  return "Скачан сохранённый профиль с журналом и все локальные процедуры. Несохранённый JSON в комплект не входит.";
});
action("import", async () => {
  const file = el("import-file").files[0];
  if (!file) throw new Error("Выберите файл комплекта JSON");
  if (!window.confirm("Заменить одноимённые локальные профили и процедуры из комплекта? Резервный комплект уже скачан?")) return;
  const bundle = JSON.parse(await file.text());
  const result = await request("/api/commissioning/import", bundle);
  render(bundle.profile); return result;
});
request("/api/commissioning/active").then(data => {
  render(data.profile);
  el("storage").textContent = `Каталог конфигов на этом компьютере: ${data.storage_directory}`;
  el("check").replaceChildren(...Object.entries(data.checks).map(([k, label]) => new Option(label, k)));
  show("Откройте существующий профиль по имени или сохраните копию активных настроек. Перед калибровками проверьте карту оборудования.");
}).catch(e => show(e.message));

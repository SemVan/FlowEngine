import { api } from "./api.js";

const $ = id => document.getElementById(id);
let enabled = false;
let pending = false;
let timer = null;
let history = [];
let rowsSeen = 0;

function paint(values) {
  $("sensor-values").replaceChildren();
  for (const value of values) {
    const row = document.createElement("tr");
    for (const text of [value.name, value.source, value.raw_value ?? "—", value.value ?? "—", value.units,
      value.available === false ? `Недоступен: ${value.reason || "источник отсутствует в M105"}` : value.available ? "Доступен" : "Ещё не читали"]) {
      const cell = document.createElement("td"); cell.textContent = String(text); row.append(cell);
    }
    $("sensor-values").append(row);
  }
  if (!values.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td"); cell.colSpan = 6;
    cell.textContent = "В активном профиле нет analog_inputs. Можно прочитать M105 и увидеть доступные источники, затем настроить профиль.";
    row.append(cell); $("sensor-values").append(row);
  }
}

function buttons() {
  $("sensor-read").disabled = pending || enabled;
  $("sensor-start").disabled = pending || enabled;
  $("sensor-stop").disabled = !enabled;
  $("sensor-hz").disabled = enabled;
}

function stop() {
  enabled = false; clearTimeout(timer); buttons();
  $("sensor-status").textContent = "Опрос остановлен. Уже отправленный запрос может ещё завершаться.";
}

async function read() {
  if (pending) return;
  pending = true; buttons();
  try {
    const report = await api.workbench("sensors");
    paint(report.values);
    $("sensor-sources").textContent = JSON.stringify(report.reported_sources, null, 2);
    $("sensor-raw").textContent = report.raw.join("\n");
    const rows = report.values.length ? report.values : Object.entries(report.reported_sources).map(([source, raw_value]) => ({source, raw_value, value: raw_value, available: true, units: "исходные"}));
    for (const value of rows) history.push({timestamp: report.timestamp, profile: report.profile, transport: report.transport, ...value});
    rowsSeen += rows.length;
    history = history.slice(-10000);
    $("sensor-count").textContent = `В памяти ${history.length} из ${rowsSeen} строк. Только в этой вкладке; перед закрытием скачайте CSV.`;
    $("sensor-history").textContent = history.slice(-20).map(r => `${r.timestamp} · ${r.profile} · ${r.name || r.source}: ${r.value ?? "недоступен"} ${r.units || ""}`).join("\n") || "Нет источников в ответе";
    $("sensor-status").textContent = `${report.transport === "mock" ? "СИМУЛЯТОР · " : ""}${report.profile} · ${report.timestamp}${enabled ? " · опрос включён" : " · опрос выключен"}`;
  } catch (error) {
    $("sensor-status").textContent = error.status === 409 ? "Аппарат занят: чтение пропущено. Опрос продолжится после освобождения." : `Ошибка чтения: ${error.message}`;
    if (error.status !== 409) { enabled = false; clearTimeout(timer); }
  } finally { pending = false; buttons(); }
}

async function tick() {
  if (!enabled) return;
  const started = performance.now();
  if (!document.hidden) await read();
  if (enabled) timer = setTimeout(tick, Math.max(0, 1000 / Number($("sensor-hz").value) - (performance.now() - started)));
}

$("sensor-read").addEventListener("click", read);
$("sensor-start").addEventListener("click", () => {
  if (!$("sensor-hz").reportValidity()) return;
  const hz = Number($("sensor-hz").value);
  if (!Number.isFinite(hz) || hz < 0.1 || hz > 10) return;
  enabled = true; buttons();
  $("sensor-status").textContent = document.hidden ? "Опрос включён; скрытая вкладка — пауза." : "Опрос включён…";
  tick();
});
$("sensor-stop").addEventListener("click", stop);
$("sensor-clear").addEventListener("click", () => {
  history = []; rowsSeen = 0;
  $("sensor-history").textContent = "История очищена";
  $("sensor-count").textContent = "0 строк. Экспортируйте результаты до закрытия вкладки.";
});
$("sensor-export").addEventListener("click", () => {
  const columns = ["timestamp", "profile", "transport", "name", "source", "raw_value", "value", "units", "available"];
  const cell = value => {
    let text = String(value ?? "");
    if (/^[=+@\-\t\r]/.test(text) && !/^[-+]?\d+(\.\d+)?$/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  const csv = [columns.join(","), ...history.map(row => columns.map(key => cell(row[key])).join(","))].join("\r\n");
  const url = URL.createObjectURL(new Blob(["\ufeff" + csv], {type: "text/csv;charset=utf-8"}));
  const link = document.createElement("a"); link.href = url; link.download = "flowengine-sensors.csv"; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
window.addEventListener("pagehide", stop);

api.config().then(config => {
  const inputs = config.device_map.analog_inputs || [];
  paint(inputs);
  if (inputs.length) $("sensor-hz").value = Math.max(...inputs.map(s => s.poll_hz));
  $("sensor-status").textContent = "Опрос выключен. Нажмите «Прочитать один раз» или «Начать опрос».";
}).catch(error => { $("sensor-status").textContent = `Не удалось загрузить настройки: ${error.message}`; });

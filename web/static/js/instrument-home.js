import { api } from "./api.js";

const panel = document.getElementById("instrument-home-panel");
if (panel) {
  const start = document.getElementById("instrument-home-start");
  const cancel = document.getElementById("instrument-home-cancel");
  const release = document.getElementById("instrument-release-all");
  const sessionStart = document.getElementById("instrument-session-start");
  const finish = document.getElementById("instrument-park-finish");
  const capture = document.getElementById("instrument-parking-capture");
  const message = document.getElementById("instrument-home-status");
  const references = document.getElementById("instrument-home-references");
  const readCurrents = document.getElementById("instrument-read-currents");
  let axes = [];
  let busy = false;
  let refreshing = false;
  let notice = "";

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const [state, procedure, parking] = await Promise.all([api.state(), api.procedureStatus(), request("/api/session/parking")]);
      const idle = state.state === "connected_idle" && !procedure.running;
      const missing = axes.filter(axis => !state.homed?.[axis]);
      start.disabled = busy || !idle || !missing.length || !state.motion_enabled;
      start.textContent = missing.length ? "Подготовить ненайденные нули · HOME" : "Нули найдены · можно работать";
      release.disabled = busy || !idle;
      sessionStart.disabled = busy || !idle || !parking.saved_parking || !missing.length;
      finish.disabled = busy || !idle || !parking.configured || missing.length > 0 || !state.motion_enabled;
      capture.disabled = busy || !idle || missing.length > 0;
      sessionStart.textContent = !missing.length ? parking.reference_source === "saved_parking" ? "Готов из парковки" : "Сессия готова" : "Начать из парковки";
      document.getElementById("instrument-parking-positions").textContent = parking.configured
        ? "Парковка: " + Object.entries(parking.positions).map(([axis, value]) => `${axis}: ${value}`).join(" · ")
        : "Парковка ещё не задана. Откройте настройку ниже: выставьте узлы и запомните положения один раз.";
      readCurrents.disabled = busy || !idle;
      cancel.disabled = state.state !== "homing";
      references.textContent = axes.map(axis => `${axis}: ${state.homed?.[axis] ? "привязка есть" : parking.saved_parking ? "парковка сохранена" : "нет привязки"}`).join(" · ");
      if (state.state === "homing") message.textContent = state.detail || "Ищем нули…";
      else if (!busy) message.textContent = notice || (procedure.running ? "Выполняется процедура." : idle ? missing.length ? parking.saved_parking ? "Припаркован, моторы отпущены. Следующая сессия начнётся из парковки без HOME." : "Для первой сессии восстановите нули, затем настройте парковку." : "Готов к процедурам. В конце нажмите «Припарковать и завершить сессию»." : state.detail || "Нет связи с платой.");
    } catch (error) {
      start.disabled = cancel.disabled = release.disabled = true;
      sessionStart.disabled = finish.disabled = capture.disabled = true;
      readCurrents.disabled = true;
      message.textContent = `Нет связи: ${error.message}`;
    } finally {
      refreshing = false;
    }
  }

  async function request(path, options) {
    const response = await fetch(path, options); const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
    return data;
  }
  async function sessionAction(path, text) {
    if (busy) return; busy = true; notice = ""; message.textContent = text;
    sessionStart.disabled = finish.disabled = capture.disabled = true;
    try {
      const result = await request(path, {method:"POST"});
      notice = result.parked ? "Узлы припаркованы, моторы отпущены. Следующий запуск — из сохранённой парковки."
        : result.restored ? "Координаты восстановлены из парковки. Можно запускать процедуры без HOME."
        : result.saved ? "Текущие координаты сохранены в конфиге как парковка." : result.reason || "Готово";
    } catch (error) { notice = error.message; }
    finally { busy = false; await refresh(); }
  }
  sessionStart.addEventListener("click", () => sessionAction("/api/session/start", "Начинаем из парковки…"));
  finish.addEventListener("click", () => sessionAction("/api/session/park", "Возвращаем узлы в парковку…"));
  capture.addEventListener("click", () => sessionAction("/api/session/parking/capture", "Сохраняем парковочные координаты…"));

  start.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    start.disabled = release.disabled = true;
    notice = "";
    message.textContent = "Проверяем найденные нули…";
    try {
      const state = await api.state();
      const missing = axes.filter(axis => !state.homed?.[axis]);
      if (!missing.length) { notice = "Нули уже найдены. Можно запускать следующие процедуры."; return; }
      const job = await api.homeStart(missing);
      cancel.disabled = false;
      while (true) {
        const status = await api.homeStatus(job.job_id);
        message.textContent = status.detail || "Ищем нули…";
        if (status.state === "completed") {
          notice = "Нули найдены. Запускайте процедуры подряд без повторного HOME. Для перерыва завершите сеанс и отпустите моторы.";
          break;
        }
        if (["failed", "cancelled"].includes(status.state)) throw new Error(status.error || status.detail || "Поиск остановлен");
        await new Promise(resolve => setTimeout(resolve, 250));
      }
    } catch (error) {
      notice = `HOME не завершён: ${error.message}`;
    } finally {
      busy = false;
      cancel.disabled = true;
      await refresh();
    }
  });

  cancel.addEventListener("click", async () => {
    cancel.disabled = true;
    try {
      await api.homeCancel();
      message.textContent = "Остановка поиска запрошена…";
    } catch (error) {
      notice = `Не удалось остановить поиск: ${error.message}`;
    }
  });

  release.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    start.disabled = release.disabled = true;
    try {
      await api.workbench("motors", {enabled: false, axes: null});
      notice = "Команда снятия удержания подтверждена для всех моторов. Сохранённые настройки остались; перед движением нужен HOME.";
    } catch (error) {
      notice = `Снятие удержания не подтверждено: ${error.message}`;
    } finally {
      busy = false;
      await refresh();
    }
  });

  readCurrents.addEventListener("click", async () => {
    readCurrents.disabled = true;
    try {
      const response = await fetch("/api/diagnostics/currents");
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Не удалось прочитать токи");
      document.getElementById("instrument-currents").textContent = result.raw || "Прошивка не прислала значения токов.";
    } catch (error) {
      document.getElementById("instrument-currents").textContent = error.message;
    } finally { await refresh(); }
  });

  try {
    const config = await api.config();
    axes = config.device_map.axes.filter(axis => axis.kind !== "peristaltic_pump").map(axis => axis.marlin_axis);
    await refresh();
  } catch (error) {
    message.textContent = `Не удалось прочитать настройки: ${error.message}`;
  }
  setInterval(refresh, 1000);
}

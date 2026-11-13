import { api } from "../api.js";

const $ = (id) => document.getElementById(id);
const buttons = [$("stepcheck-x"), $("stepcheck-y"), $("stepcheck-xy")].filter(Boolean);
const cancel = $("stepcheck-cancel");
const tolerance = $("stepcheck-tolerance");
const statusBox = $("stepcheck-status");
const results = $("stepcheck-results");
let running = false;

function setBusy(value) {
  running = value;
  buttons.forEach((b) => { b.disabled = value; });
  if (cancel) cancel.disabled = !value;
  if (tolerance) tolerance.disabled = value;
}

function message(text, bad = false) {
  if (!statusBox) return;
  statusBox.textContent = text;
  statusBox.classList.toggle("error", bad);
}

function toleranceValue() {
  const value = Number(tolerance?.value ?? 8);
  if (!Number.isFinite(value) || value < 0) throw new Error("Допуск должен быть неотрицательным числом шагов");
  return value;
}

function renderResult(result) {
  if (!results || !result) return;
  const rows = Object.entries(result.axes || {}).map(([axis, item]) => {
    const status = item.status === "OK" ? "✓ OK" : "⚠ STEP_LOSS";
    const cls = item.status === "OK" ? "ok" : "error";
    const delta = Number.isFinite(Number(item.delta_steps)) ? Number(item.delta_steps) : "—";
    const base = item.baseline_count ?? "—";
    const measured = item.measured_count ?? "—";
    const ref = item.referenced ? "REFERENCED" : "нужен Home";
    return `<tr><td>${axis}</td><td class="${cls}">${status}</td><td>${base}</td><td>${measured}</td><td>${delta}</td><td>±${item.tolerance_steps}</td><td>${ref}</td></tr>`;
  }).join("");
  results.innerHTML = `
    <div class="stepcheck-summary ${result.status === "OK" ? "ok" : "error"}">${result.status === "OK" ? "✓ Проверка пройдена" : "⚠ Обнаружена ошибка возврата"}</div>
    <table class="data-table"><thead><tr><th>Ось</th><th>Результат</th><th>Эталон Count</th><th>Факт Count</th><th>Δ шагов</th><th>Допуск</th><th>Reference</th></tr></thead><tbody>${rows}</tbody></table>
    <p class="hint">${result.warning || ""}</p>`;
}

async function run(axes) {
  if (running) return;
  let jobId = null;
  try {
    setBusy(true);
    results.innerHTML = "";
    message(`Проверка ${axes.join("+")}: запускаем…`);
    const started = await api.stepCheckStart(axes, toleranceValue());
    jobId = started.job_id;
    message(started.detail || "Проверка выполняется…");
    while (running && jobId) {
      await new Promise((resolve) => setTimeout(resolve, 150));
      const st = await api.stepCheckStatus(jobId);
      message(st.detail || "Проверка выполняется…", st.state === "failed");
      if (st.state === "completed") {
        renderResult(st.result);
        break;
      }
      if (st.state === "failed" || st.state === "cancelled") {
        throw new Error(st.error || st.detail || "Проверка остановлена");
      }
    }
  } catch (e) {
    message(`Проверка: ${e.message}`, true);
  } finally {
    setBusy(false);
  }
}

$("stepcheck-x")?.addEventListener("click", () => run(["X"]));
$("stepcheck-y")?.addEventListener("click", () => run(["Y"]));
$("stepcheck-xy")?.addEventListener("click", () => run(["X", "Y"]));
$("stepcheck-cancel")?.addEventListener("click", async () => {
  if (!running) return;
  try {
    message("Проверка: останавливаем…");
    await api.stepCheckCancel();
  } catch (e) {
    message(`Stop: ${e.message}`, true);
  }
});

api.state().then((state) => {
  const lines = ["X", "Y"].map((axis) => {
    const cal = state.calibrated?.[axis] ? "CAL" : "—";
    const ref = state.homed?.[axis] ? "REFERENCED" : "нужен Home";
    const count = state.reference_counts?.[axis];
    return `${axis}: ${cal} · ${ref}${count === undefined ? "" : ` · baseline Count ${count}`}`;
  });
  message(lines.join("   |   "));
}).catch((e) => message(`Не удалось загрузить состояние: ${e.message}`, true));

// Procedure browser and runner controls.

import { api } from "../api.js";

const list = document.getElementById("proc-list");
const detail = document.getElementById("proc-detail");

async function reload() {
  list.innerHTML = "loading…";
  try {
    const procs = await api.procedures();
    list.innerHTML = "";
    for (const p of procs) {
      const li = document.createElement("li");
      if (!p.ok) {
        li.innerHTML = `<strong>${p.file}</strong> — <span style="color:var(--error)">${p.error}</span>`;
      } else {
        li.innerHTML = `<a href="#" data-name="${p.name}">${p.name}</a> — ${p.description || ""} <small>(${p.steps} steps)</small> <button type="button" data-run="${p.name}">Run</button>`;
      }
      list.appendChild(li);
    }
  } catch (e) {
    list.innerHTML = "<em>Failed to load procedures: " + e.message + "</em>";
  }
}

list?.addEventListener("click", async (ev) => {
  const a = ev.target.closest("a[data-name]");
  if (!a) return;
  ev.preventDefault();
  try {
    const p = await api.procedure(a.dataset.name);
    detail.textContent = JSON.stringify(p, null, 2);
  } catch (e) {
    detail.textContent = "Error: " + e.message;
  }
});

list?.addEventListener("click", async (ev) => {
  const button = ev.target.closest("button[data-run]");
  if (!button) return;
  button.disabled = true;
  try { detail.textContent = JSON.stringify(await api.runProcedure(button.dataset.run), null, 2); }
  catch (e) { detail.textContent = `Run refused: ${e.message}`; }
  finally { button.disabled = false; }
});

document.getElementById("abort-proc")?.addEventListener("click", async () => {
  detail.textContent = JSON.stringify(await api.abortProcedure(), null, 2);
});

document.getElementById("reload-procs")?.addEventListener("click", reload);
if (list) reload();

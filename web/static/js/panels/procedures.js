// Procedures listing — Phase 1 supports listing + per-procedure detail (lint).
// Running them is Phase 2.

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
        li.innerHTML = `<a href="#" data-name="${p.name}">${p.name}</a> — ${p.description || ""} <small>(${p.steps} steps)</small>`;
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

document.getElementById("reload-procs")?.addEventListener("click", reload);
if (list) reload();

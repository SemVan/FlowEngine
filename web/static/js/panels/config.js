import { api } from "../api.js";

const profileEditor = document.getElementById("config-editor");
const profileResult = document.getElementById("config-result");
const profileList = document.getElementById("profile-list");
const rackEditor = document.getElementById("rack-editor");
const rackResult = document.getElementById("rack-result");
const rackList = document.getElementById("rack-list");
const selectedCells = new Set();

function show(target, value) {
  target.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function fillSelect(select, items) {
  select.replaceChildren();
  for (const item of items) {
    const option = document.createElement("option");
    option.value = item.name;
    option.textContent = item.ok ? item.name : `${item.name} (invalid)`;
    select.append(option);
  }
}

async function reloadProfiles() { fillSelect(profileList, await api.profiles()); }
async function reloadRacks() { fillSelect(rackList, await api.racks()); }

api.config().then((config) => {
  const profile = {
    name: document.getElementById("profile-name").value,
    description: "Saved from FlowEngine",
    ...config,
    rack: null,
  };
  profileEditor.value = JSON.stringify(profile, null, 2);
}).catch((error) => show(profileResult, `Load failed: ${error.message}`));

document.getElementById("save-profile")?.addEventListener("click", async () => {
  try {
    const name = document.getElementById("profile-name").value.trim();
    const value = JSON.parse(profileEditor.value);
    value.name = name;
    show(profileResult, await api.saveProfile(name, value));
    await reloadProfiles();
  } catch (error) { show(profileResult, `Save failed: ${error.message}`); }
});

document.getElementById("load-profile")?.addEventListener("click", async () => {
  try { profileEditor.value = JSON.stringify(await api.profile(profileList.value), null, 2); }
  catch (error) { show(profileResult, `Load failed: ${error.message}`); }
});

function drawRack(rack) {
  const grid = document.getElementById("rack-grid");
  grid.replaceChildren();
  grid.style.gridTemplateColumns = `repeat(${rack.columns}, minmax(2rem, 1fr))`;
  const disabled = new Set((rack.cells || []).filter((c) => !c.enabled).map((c) => c.cell.toUpperCase()));
  for (let row = 0; row < rack.rows; row += 1) {
    for (let column = 1; column <= rack.columns; column += 1) {
      let index = row + 1;
      let letters = "";
      while (index > 0) {
        index -= 1;
        letters = String.fromCharCode(65 + (index % 26)) + letters;
        index = Math.floor(index / 26);
      }
      const cell = `${letters}${column}`;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = cell;
      button.className = disabled.has(cell) ? "rack-cell disabled" : "rack-cell";
      button.dataset.cell = cell;
      button.disabled = disabled.has(cell);
      if (selectedCells.has(cell)) button.classList.add("selected");
      button.title = `x=${rack.origin_x + (column - 1) * rack.column_pitch}, y=${rack.origin_y + row * rack.row_pitch}`;
      grid.append(button);
    }
  }
}

document.getElementById("rack-grid")?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-cell]");
  if (!button || button.disabled) return;
  if (selectedCells.has(button.dataset.cell)) selectedCells.delete(button.dataset.cell);
  else selectedCells.add(button.dataset.cell);
  button.classList.toggle("selected");
  show(rackResult, { selected_cells: [...selectedCells] });
});

async function loadRack(name) {
  const rack = await api.rack(name);
  rackEditor.value = JSON.stringify(rack, null, 2);
  document.getElementById("rack-name").value = rack.name;
  selectedCells.clear();
  drawRack(rack);
}

document.getElementById("save-rack")?.addEventListener("click", async () => {
  try {
    const name = document.getElementById("rack-name").value.trim();
    const rack = JSON.parse(rackEditor.value);
    rack.name = name;
    show(rackResult, await api.saveRack(name, rack));
    drawRack(rack);
    await reloadRacks();
  } catch (error) { show(rackResult, `Save failed: ${error.message}`); }
});

document.getElementById("load-rack")?.addEventListener("click", async () => {
  try { await loadRack(rackList.value); }
  catch (error) { show(rackResult, `Load failed: ${error.message}`); }
});

document.getElementById("plan-rack")?.addEventListener("click", async () => {
  try { show(rackResult, await api.rackPlan(document.getElementById("rack-name").value.trim(), [...selectedCells])); }
  catch (error) { show(rackResult, `Plan failed: ${error.message}`); }
});

document.getElementById("reload-profiles")?.addEventListener("click", reloadProfiles);
document.getElementById("reload-racks")?.addEventListener("click", reloadRacks);

Promise.all([reloadProfiles(), reloadRacks()]).then(async () => {
  if (rackList.value) await loadRack(rackList.value);
}).catch((error) => show(rackResult, error.message));

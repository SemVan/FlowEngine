import { api } from "../api.js";
import { renderHardwareForms, applyHardwareForms } from "./hardware-config.js";

const profileEditor = document.getElementById('config-editor');
const profileResult = document.getElementById('config-result');
const profileList = document.getElementById('profile-list');
const profileName = document.getElementById('profile-name');
const rackEditor = document.getElementById('rack-editor');
const rackResult = document.getElementById('rack-result');
const rackList = document.getElementById('rack-list');
const selectedCells = new Set();
let dirty = false;
let desktop = null;
let loadedName = null;
let busy = false;

function show(target, value) { target.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2); }
function fillSelect(select, items) {
  const selected = select.value;
  select.replaceChildren();
  for (const item of items) {
    const option = document.createElement('option'); option.value = item.name;
    option.textContent = item.name === 'default' ? 'Заводской профиль' : item.name + (item.ok ? '' : ' · ошибка файла');
    option.disabled = !item.ok;
    select.append(option);
  }
  if ([...select.options].some(o => o.value === selected)) select.value = selected;
}
async function reloadProfiles() { fillSelect(profileList, [{name:'default',ok:true}, ...(await api.profiles()).filter(p => p.name !== 'default')]); }
async function reloadRacks() { fillSelect(rackList, await api.racks()); }
function markDirty() { dirty = true; document.body.dataset.profileDirty = 'true'; show(profileResult, 'Есть несохранённые изменения'); }
document.getElementById('hardware-forms').addEventListener('input', markDirty);
profileEditor.addEventListener('input', markDirty);
profileName.addEventListener('input', markDirty);
document.getElementById('calibration-apply').addEventListener('click', markDirty);
window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
function loadEditor(profile) {
  profileEditor.value = JSON.stringify(profile, null, 2);
  loadedName = profile.name;
  profileName.value = profile.name === 'default' ? 'my_instrument' : profile.name;
  renderHardwareForms(); dirty = false; document.body.dataset.profileDirty = 'false';
  show(profileResult, 'Настройки открыты. Измените нужные поля и сохраните профиль.');
}
async function initialize() {
  try {
    const response = await fetch('/api/desktop/session');
    if (response.ok) desktop = await response.json();
  } catch {}
  const [config, state] = await Promise.all([api.config(), api.state()]);
  const saved = state.profile === 'default' ? {description:'Личный профиль',rack:null} : await api.profile(state.profile);
  loadEditor({...saved, ...config, name:state.profile});
  await reloadProfiles(); profileList.value = state.profile;
  if (!desktop) {
    show(profileResult, 'Обычный browser/BAT режим: профиль можно сохранить и применить прямо здесь. При применении reference X/Y сбросится и потребуется Home.');
  }
}

async function apply(name) {
  const endpoint = desktop ? '/api/desktop/apply' : '/api/config/apply';
  const headers = {'Content-Type':'application/json'};
  if (desktop) headers['X-Desktop-Token'] = desktop.token;
  const response = await fetch(endpoint, {
    method:'POST', headers, body:JSON.stringify({name}),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || 'Не удалось применить профиль');
  dirty = false; document.body.dataset.profileDirty = 'false';
  if (desktop) {
    show(profileResult, `Применяем «${name}»… Окно обновится автоматически.`);
  } else {
    show(profileResult, `Профиль «${name}» применён. Привязка X/Y сброшена — перед обычным движением выполните Home.`);
    setTimeout(() => window.location.reload(), 300);
  }
}
async function run(action) {
  if (busy) return;
  busy = true;
  const buttons = ['save-profile','save-apply-profile','load-profile','activate-profile'];
  buttons.forEach(id => document.getElementById(id).disabled = true);
  try { await action(); }
  catch (error) { show(profileResult, `Ошибка: ${error.message}`); }
  finally {
    busy = false;
    buttons.forEach(id => document.getElementById(id).disabled = false);
  }
}
async function save(activate) {
  const name = profileName.value.trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(name)) throw new Error('Название: латинские буквы, цифры, дефис или _. До 64 символов.');
  if (name === 'default') throw new Error('Выберите своё название: default зарезервирован для заводского профиля.');
  applyHardwareForms();
  const value = JSON.parse(profileEditor.value); value.name = name;
  const existing = await api.profiles();
  if (name !== loadedName && existing.some(p => p.name === name) && !confirm(`Заменить существующий профиль «${name}»?`)) return;
  await api.saveProfile(name, value);
  loadedName = name; dirty = false; document.body.dataset.profileDirty = 'false';
  await reloadProfiles(); profileList.value = name;
  show(profileResult, `Профиль «${name}» сохранён. ${activate ? 'Применяем…' : 'Активный сеанс пока не изменён.'}`);
  if (activate) await apply(name);
}
document.getElementById('save-profile').addEventListener('click', () => run(() => save(false)));
document.getElementById('save-apply-profile').addEventListener('click', () => run(() => save(true)));
document.getElementById('activate-profile').addEventListener('click', () => run(async () => {
  if (dirty && !confirm('Применить сохранённый профиль? Несохранённые правки будут потеряны.')) return;
  await apply(profileList.value);
}));
document.getElementById('load-profile').addEventListener('click', () => run(async () => {
  if (dirty && !confirm('Открыть другой профиль и отменить несохранённые правки?')) return;
  const value = profileList.value === 'default' ? await (await fetch('/api/config/default-profile')).json() : await api.profile(profileList.value);
  loadEditor(value);
}));
document.getElementById('export-profile').addEventListener('click', () => {
  try {
    applyHardwareForms(); const value = JSON.parse(profileEditor.value); value.name = profileName.value.trim();
    const url = URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = `${value.name.replace(/[^A-Za-z0-9_-]/g,'_')}-profile.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch(error) { show(profileResult, error.message); }
});
initialize().catch(error => show(profileResult, `Не удалось открыть настройки: ${error.message}`));

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

Promise.all([reloadRacks()]).then(async () => {
  if (rackList.value) await loadRack(rackList.value);
}).catch((error) => show(rackResult, error.message));

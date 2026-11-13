import {api} from "./api.js";
const $ = id => document.getElementById(id);
const source = $("yaml-source"), status = $("yaml-status"), kind = $("yaml-kind"), name = $("yaml-name");
const buffers = new Map();
let current = null, revision = "", dirty = false, busy = false;
const key = () => `${kind.value}/${name.value}`;
function numbers() { $("yaml-numbers").textContent = Array.from({length:source.value.split("\n").length}, (_, i) => i + 1).join("\n"); }
function stash() { if (current) buffers.set(current, {text:source.value, revision, dirty}); }
async function request(path, options) {
  const response = await fetch(path, options); const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data;
}
async function action(fn) {
  if (busy) return; busy = true;
  for (const id of ["yaml-save", "yaml-apply", "yaml-reload", "yaml-kind", "yaml-name"]) $(id).disabled = true;
  try { await fn(); } catch (error) { status.textContent = `Не сохранено: ${error.message}`; }
  finally { busy = false; for (const id of ["yaml-save", "yaml-apply", "yaml-reload", "yaml-kind", "yaml-name"]) $(id).disabled = false; }
}
async function open(reload = false) {
  stash(); current = key();
  const data = !reload && buffers.get(current) || await request(`/api/config/text/${current}`);
  source.value = data.text; revision = data.revision; dirty = Boolean(data.dirty); numbers();
  status.textContent = dirty ? "Есть несохранённый текст в этом окне." : `Открыт ${name.value}.yaml · сохранение не отправляет команды на аппарат.`;
  const url = new URL(location.href); url.searchParams.set("kind", kind.value); url.searchParams.set("name", name.value); history.replaceState(null, "", url);
}
async function list(preferred) {
  const rows = await (kind.value === "profiles" ? api.profiles() : kind.value === "procedures" ? api.procedures() : api.racks());
  name.replaceChildren(...rows.filter(row => row.ok).map(row => { const option = document.createElement("option"); option.value = row.name; option.textContent = row.name + ".yaml"; return option; }));
  if ([...name.options].some(option => option.value === preferred)) name.value = preferred;
  if (name.options.length) await open(); else { source.value = ""; numbers(); status.textContent = "Здесь пока нет сохранённых файлов. Создайте личный профиль или процедуру в основном окне."; }
}
async function save(apply = false) {
  if (!current) throw new Error("Сначала выберите файл");
  const data = await request(`/api/config/text/${current}`, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({text:source.value, revision})});
  source.value = data.text; revision = data.revision; dirty = false; stash(); numbers();
  status.textContent = "Сохранено. Для изменения активного профиля нажмите «Сохранить и применить».";
  if (apply) {
    if (kind.value !== "profiles") { status.textContent = "Сохранено. Новая версия будет использована при следующем запуске процедуры/плана."; return; }
    const result = await request(`/api/config/editor/apply/${encodeURIComponent(name.value)}`, {method:"POST"});
    status.textContent = result.references_preserved ? "Сохранено и применено. Найденные нули сохранены, команды настройки плате не отправлялись." : "Применено. Состояние привязки смотрите в основном окне.";
  }
}
source.addEventListener("input", () => { dirty = true; numbers(); status.textContent = "Изменения в тексте ещё не сохранены · Ctrl+S"; });
source.addEventListener("scroll", () => { $("yaml-numbers").scrollTop = source.scrollTop; });
source.addEventListener("keydown", event => { if (event.key === "Tab") { event.preventDefault(); const start = source.selectionStart; source.setRangeText("  ", start, source.selectionEnd, "end"); source.dispatchEvent(new Event("input")); } });
document.addEventListener("keydown", event => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") { event.preventDefault(); action(() => save()); } });
$("yaml-save").addEventListener("click", () => action(() => save()));
$("yaml-apply").addEventListener("click", () => action(() => save(true)));
$("yaml-reload").addEventListener("click", () => action(() => open(true)));
$("yaml-focus").addEventListener("click", () => document.body.classList.toggle("editor-focus"));
name.addEventListener("change", () => action(() => open()));
kind.addEventListener("change", () => action(() => list()));
await action(async () => {
  const state = await api.state(); const url = new URL(location.href);
  if ([...kind.options].some(option => option.value === url.searchParams.get("kind"))) kind.value = url.searchParams.get("kind");
  await list(url.searchParams.get("name") || state.profile);
});

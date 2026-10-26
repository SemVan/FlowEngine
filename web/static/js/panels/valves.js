import { api } from "../api.js";

const byId = (id) => document.getElementById(id);
const output = byId("valve-result");
const number = (id) => Number(byId(id).value);
let valves = [];

function selected() {
  return valves.find((valve) => valve.name === byId("valve-name").value);
}

function show(value) {
  output.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function body(releaseMotor) {
  return {
    name: byId("valve-name").value,
    channel: byId("valve-channel").value.trim(),
    direction: number("valve-direction"),
    step: number("valve-step"),
    feedrate: number("valve-feedrate"),
    search_distance: number("valve-distance"),
    release_distance: number("valve-release-distance"),
    pull_off: number("valve-pulloff"),
    release_motor: releaseMotor,
  };
}

async function run(label, call) {
  show(`${label}…`);
  document.querySelectorAll(".valve-bench button").forEach((button) => { button.disabled = true; });
  try { show(await call()); }
  catch (error) { show(`FAILED: ${error.message}`); }
  finally {
    byId("valve-read").disabled = false;
    document.querySelectorAll("[data-jog]").forEach((button) => { button.disabled = false; });
    const confirmed = byId("valve-confirm").checked;
    byId("valve-test-home").disabled = !confirmed;
    byId("valve-home").disabled = !confirmed;
  }
}

async function load() {
  const data = await api.valves();
  valves = data.valves;
  const select = byId("valve-name");
  valves.forEach((valve) => select.add(new Option(`${valve.name} (${valve.axis})`, valve.name)));
  const update = () => {
    const valve = selected();
    if (!valve) return;
    byId("valve-distance").value = valve.axis_config.travel;
    byId("valve-direction").value = valve.axis_config.home_direction === "max" ? "1" : "-1";
  };
  select.addEventListener("change", update);
  update();
}

byId("valve-confirm").addEventListener("change", (event) => {
  byId("valve-test-home").disabled = !event.target.checked;
  byId("valve-home").disabled = !event.target.checked;
});
byId("valve-read").addEventListener("click", () => run("Reading M119", async () => {
  const result = await api.valveEndstop(byId("valve-channel").value.trim());
  byId("valve-endstop").textContent = result.triggered ? "TRIGGERED" : "open";
  return result;
}));
document.querySelectorAll("[data-jog]").forEach((button) => button.addEventListener("click", () => {
  const distance = number("valve-jog-distance");
  if (!Number.isFinite(distance) || distance <= 0 || distance > 20) {
    show("FAILED: Service jog distance must be greater than 0 and no more than 20.");
    return;
  }
  const delta = Number(button.dataset.jog) * distance;
  run("Service jog", () => api.valveServiceJog({
    name: byId("valve-name").value,
    channel: byId("valve-channel").value.trim(),
    delta,
    feedrate: number("valve-feedrate"),
  }));
}));
byId("valve-test-home").addEventListener("click", () => run("Test homing", () => api.valveHome(body(true))));
byId("valve-home").addEventListener("click", () => run("Homing", () => api.valveHome(body(false))));

load().catch((error) => show(`FAILED TO LOAD: ${error.message}`));

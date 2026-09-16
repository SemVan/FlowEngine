// Optional browser regression: npm install playwright in a temporary directory.
// ONLY against a separately started --mock instance; refuses real transports.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { firefox } = require(process.env.FLOWENGINE_PLAYWRIGHT || "playwright");
const base = process.env.FLOWENGINE_TEST_URL || "http://127.0.0.1:8877";
const browser = await firefox.launch({ headless: true,
  ...(process.env.FLOWENGINE_FIREFOX ? { executablePath: process.env.FLOWENGINE_FIREFOX } : {}) });
const page = await browser.newPage({ acceptDownloads: true });
const errors = [];
page.on("pageerror", e => errors.push(e.message));
page.on("dialog", d => d.accept());
async function body(response) {
  assert(response.ok(), await response.text()); return response.json();
}
async function clickRequest(selector, suffix) {
  const pending = page.waitForResponse(r => r.url().endsWith(suffix) && r.request().method() === "POST");
  await page.locator(selector).click(); return body(await pending);
}
try {
  const state = await body(await page.request.get(`${base}/api/state`));
  assert.equal(state.transport, "mock", "REFUSING to test on a real controller");
  await page.goto(base);
  await page.locator("#connection-view").filter({ hasText: "mock" }).waitFor();
  await body(await page.request.post(`${base}/api/home`, { data: { axes: ["X"] } }));
  await page.locator("#wb-by").fill("80");
  await page.locator("#wb-units").selectOption("steps");
  await page.locator("#wb-speed").fill("80");
  await page.locator("#wb-speed-units").selectOption("steps/s");
  await page.locator("#wb-accel").fill("50");
  const jog = await clickRequest("#wb-jog", "/api/workbench/jog");
  assert.equal(jog.positions.X, 1); assert.equal(jog.effective_feedrate_axis_min, 60);
  await page.locator("#console-command").fill("M114");
  const consoleResult = await clickRequest("#console-form button", "/api/workbench/console");
  assert(consoleResult.raw.some(s => s.includes("X:1.0000")));
  // Regression for merged Content-Type + Idempotency-Key headers in the legacy wrapper.
  const legacy = await clickRequest('[data-jog="X"][data-dir="+1"]', "/api/jog");
  assert.equal(legacy.positions.X, 2);
  const release = await clickRequest("#wb-release", "/api/workbench/motors");
  assert.equal(release.homed.X, false);
  await clickRequest("#wb-enable", "/api/workbench/motors");
  assert.equal((await body(await page.request.get(`${base}/api/state`))).homed.X, false);
  const download = page.waitForEvent("download"); await page.locator("#wb-export").click();
  assert.equal((await download).suggestedFilename(), "flowengine-feedback.json");
  await page.goto(`${base}/config`);
  await page.locator("#hardware-forms fieldset").first().waitFor();
  await page.locator("#profile-name").fill("browser_calibration");
  await page.locator("#calibration-pump").selectOption("sample_pump");
  await page.locator('[data-measurement="step_pulses"]').fill("80");
  await page.locator('[data-measurement="volume_ul"]').fill("25");
  const coefficient = await clickRequest("#calibration-calculate", "/api/workbench/calibrate-pump");
  assert.equal(coefficient.volume_per_unit_ul, 25);
  await page.locator("#calibration-apply").click();
  const saved = page.waitForResponse(r => r.url().endsWith("/api/config/profiles/browser_calibration") && r.request().method() === "PUT");
  await page.locator("#save-profile").click(); await body(await saved);
  const profile = await body(await page.request.get(`${base}/api/config/profiles/browser_calibration`));
  assert.equal(profile.device_map.pumps[0].calibrated, true);
  assert.equal(profile.device_map.pumps[0].volume_per_unit_ul, 25);
  let exported = page.waitForEvent("download"); await page.locator("#export-profile").click();
  assert.equal((await exported).suggestedFilename(), "browser_calibration-profile.json");
  await page.goto(`${base}/procedures`);
  await page.locator("#step-axis").waitFor();
  await page.locator("#proc-name").fill("browser_move");
  await page.locator("#step-value").fill("80");
  await page.locator("#step-feedrate").fill("80");
  await page.locator("#step-units").selectOption("steps");
  await page.locator("#step-speed-units").selectOption("steps/s");
  await page.locator("#add-step").click();
  const preview = await clickRequest("#preview-proc", "/api/procedures/browser_move/preview");
  assert.equal(preview.steps[0].units, "steps"); assert.deepEqual(preview.execution_issues, []);
  await body(await page.request.post(`${base}/api/home`, { data: { axes: ["X"] } }));
  const step = await clickRequest('button[data-action="run"]', "/api/procedures/browser_move/steps/1/run");
  assert.equal(step.ok, true);
  for (const op of ["move_multi", "pump", "pump_multi", "motors", "read_sensors", "seek", "calibrate_valve", "test_reference"]) {
    await page.locator("#step-op").selectOption(op);
    assert((await page.locator("#step-fields").textContent()).length > 0);
  }
  await page.locator("#step-op").selectOption("pump_multi");
  for (const name of ["sample_pump", "sheath_pump"]) {
    const row = page.locator(`[data-pump-dose="${name}"]`);
    await row.locator('[data-dose-field="selected"]').check();
    await row.locator('[data-dose-field="volume_ul"]').fill("10");
    await row.locator('[data-dose-field="flow_ul_min"]').fill("10");
  }
  await page.locator("#add-step").click();
  const multiPreview = await clickRequest("#preview-proc", "/api/procedures/browser_move/preview");
  assert.equal(multiPreview.steps[1].op, "pump_multi");
  assert.equal(multiPreview.steps[1].pumps.sample_pump.volume_ul, 10);
  assert(multiPreview.execution_issues.some(s => s.includes("uncalibrated")));
  exported = page.waitForEvent("download"); await page.locator("#export-proc").click();
  assert.equal((await exported).suggestedFilename(), "browser_move.json");
  assert.deepEqual(errors, []);
  console.log("PASS: browser connection, pulse units, acceleration, console TX/RX, release/invalidation, export, calibration/profile save, procedure preview/run and operation forms");
} finally { await browser.close(); }

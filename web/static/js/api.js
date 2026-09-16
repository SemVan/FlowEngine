// Thin fetch wrappers. Motion endpoints inject an Idempotency-Key so UI retries
// during transient network blips don't double-move.

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return "id-" + Math.random().toString(16).slice(2) + Date.now().toString(16);
}

function errorMessage(body, fallback) {
  const detail = body && body.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      const location = Array.isArray(item.loc) ? item.loc.join(".") : "request";
      return `${location}: ${item.msg || JSON.stringify(item)}`;
    }).join("; ");
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return fallback;
}

async function request(path, opts = {}) {
  const { headers = {}, ...requestOptions } = opts;
  const res = await fetch(path, {
    ...requestOptions,
    headers: { "Content-Type": "application/json", ...headers },
  });
  const text = await res.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!res.ok) {
    const err = new Error(errorMessage(body, res.statusText));
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

export const api = {
  workbench(action, body) {
    return request(`/api/workbench/${action}`, body === undefined ? {} : {
      method: "POST", body: JSON.stringify(body),
    });
  },
  state() { return request("/api/state"); },
  config() { return request("/api/config"); },
  profiles() { return request("/api/config/profiles"); },
  profile(name) { return request(`/api/config/profiles/${encodeURIComponent(name)}`); },
  saveProfile(name, profile) {
    return request(`/api/config/profiles/${encodeURIComponent(name)}`, {
      method: "PUT", body: JSON.stringify(profile),
    });
  },
  racks() { return request("/api/config/racks"); },
  rack(name) { return request(`/api/config/racks/${encodeURIComponent(name)}`); },
  saveRack(name, rack) {
    return request(`/api/config/racks/${encodeURIComponent(name)}`, {
      method: "PUT", body: JSON.stringify(rack),
    });
  },
  rackPlan(name, cells) {
    return request(`/api/config/racks/${encodeURIComponent(name)}/plan`, {
      method: "POST", body: JSON.stringify(cells),
    });
  },
  firmware() { return request("/api/diagnostics/firmware"); },
  endstops() { return request("/api/diagnostics/endstops"); },
  position() { return request("/api/diagnostics/position"); },
  settings() { return request("/api/diagnostics/settings"); },
  drivers() { return request("/api/diagnostics/drivers"); },
  jog(axis, delta, feedrate) {
    return request("/api/jog", {
      method: "POST",
      headers: { "Idempotency-Key": uuid() },
      body: JSON.stringify({ axis, delta, feedrate }),
    });
  },
  move(axis, target, feedrate) {
    return request("/api/move", {
      method: "POST",
      headers: { "Idempotency-Key": uuid() },
      body: JSON.stringify({ axis, target, feedrate }),
    });
  },
  home(axes) {
    return request("/api/home", { method: "POST", body: JSON.stringify({ axes }) });
  },
  stop() { return request("/api/stop", { method: "POST" }); },
  procedures() { return request("/api/procedures"); },
  procedure(name) { return request(`/api/procedures/${encodeURIComponent(name)}`); },
  saveProcedure(name, procedure) {
    return request(`/api/procedures/${encodeURIComponent(name)}`, {
      method: "PUT", body: JSON.stringify(procedure),
    });
  },
  runProcedure(name, parameters = {}) {
    return request(`/api/procedures/${encodeURIComponent(name)}/run`, {
      method: "POST", body: JSON.stringify(parameters),
    });
  },
  previewProcedure(name, parameters = {}) {
    return request(`/api/procedures/${encodeURIComponent(name)}/preview`, {
      method: "POST", body: JSON.stringify(parameters),
    });
  },
  runProcedureStep(name, stepNumber, parameters = {}) {
    return request(`/api/procedures/${encodeURIComponent(name)}/steps/${stepNumber}/run`, {
      method: "POST",
      headers: { "Idempotency-Key": uuid() },
      body: JSON.stringify(parameters),
    });
  },
  procedureStatus() { return request("/api/procedures/status"); },
  abortProcedure() { return request("/api/procedures/abort", { method: "POST" }); },
};

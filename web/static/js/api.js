// Thin fetch wrappers. Motion endpoints inject an Idempotency-Key so UI retries
// during transient network blips don't double-move.

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return "id-" + Math.random().toString(16).slice(2) + Date.now().toString(16);
}

async function request(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const text = await res.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!res.ok) {
    const err = new Error((body && body.detail) || res.statusText);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

export const api = {
  state() { return request("/api/state"); },
  config() { return request("/api/config"); },
  firmware() { return request("/api/diagnostics/firmware"); },
  endstops() { return request("/api/diagnostics/endstops"); },
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
};

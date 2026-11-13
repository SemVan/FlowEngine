const api = '/api/axis-calibration';
const $ = id => document.getElementById(id);
let job = null;
let readiness = null;
let busy = false;
let reloadTimer = null;

async function request(path, options = {}) {
  const init = {headers:{'Content-Type':'application/json'}, ...options};
  const response = await fetch(path, init);
  let body = null;
  try { body = await response.json(); } catch (_) {}
  if (!response.ok) {
    const detail = body?.detail;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail || `HTTP ${response.status}`));
  }
  return body;
}

function axisReady(axis) {
  const a = readiness?.axes?.[axis];
  return !!a?.direction_verified;
}

function statusText() {
  if (!job || job.state === 'idle') {
    const profile = readiness?.profile || '—';
    const x = axisReady('X') ? 'направления ✓' : 'сначала ручная калибровка';
    const y = axisReady('Y') ? 'направления ✓' : 'сначала ручная калибровка';
    return `Профиль: ${profile}\nX: ${x}\nY: ${y}`;
  }
  const lines = [job.detail || `Автокалибровка: ${job.state}`];
  if (job.axis) lines.push(`Текущая ось: ${job.axis}${job.phase ? ` · ${job.phase}` : ''}`);
  for (const axis of ['X','Y']) {
    const r = job.results?.[axis];
    if (!r) continue;
    lines.push(`${axis}: ход ${Number(r.travel).toFixed(3)} мм · ${Math.round(r.travel_steps)} шагов · ${r.referenced ? 'REFERENCED ✓' : 'reference нет'}`);
  }
  if (job.error) lines.push(`Ошибка: ${job.error}`);
  return lines.join('\n');
}

function render() {
  const running = !!job?.active || ['starting','running'].includes(job?.state);
  const defaultProfile = readiness?.profile === 'default';
  $('auto-cal-x').disabled = busy || running || defaultProfile || !axisReady('X');
  $('auto-cal-y').disabled = busy || running || defaultProfile || !axisReady('Y');
  $('auto-cal-xy').disabled = busy || running || defaultProfile || !axisReady('X') || !axisReady('Y');
  $('auto-cal-cancel').disabled = busy || !running;
  const status = $('auto-cal-status');
  status.textContent = defaultProfile && !running
    ? 'Автокалибровка не перезаписывает default. Выберите личный профиль.'
    : statusText();
  status.classList.toggle('error', job?.state === 'failed');
}

async function refreshReadiness() {
  readiness = await request(`${api}/readiness`);
  render();
}

async function start(axes) {
  if (busy || job?.active) return;
  busy = true; render();
  try {
    job = await request(`${api}/auto/start`, {method:'POST', body:JSON.stringify({axes})});
  } catch (error) {
    job = {state:'failed', active:false, error:error.message, detail:error.message, results:{}};
  } finally {
    busy = false; render();
  }
}

async function cancel() {
  if (busy) return;
  busy = true; render();
  try { job = await request(`${api}/auto/cancel`, {method:'POST', body:'{}'}); }
  catch (error) { job = {...(job || {}), error:error.message, detail:error.message}; }
  finally { busy = false; render(); }
}

$('auto-cal-x')?.addEventListener('click', () => start(['X']));
$('auto-cal-y')?.addEventListener('click', () => start(['Y']));
$('auto-cal-xy')?.addEventListener('click', () => start(['X','Y']));
$('auto-cal-cancel')?.addEventListener('click', cancel);

setInterval(async () => {
  if (busy || document.hidden) return;
  try {
    const next = await request(`${api}/auto/status`);
    const wasRunning = !!job?.active;
    job = next;
    render();
    if (wasRunning && job.state === 'completed' && !reloadTimer) {
      await refreshReadiness().catch(() => {});
      reloadTimer = setTimeout(() => location.reload(), 1500);
    }
  } catch (_) {}
}, 300);

Promise.all([refreshReadiness(), request(`${api}/auto/status`)]).then(([, current]) => {
  job = current;
  render();
}).catch(error => {
  job = {state:'failed', active:false, error:error.message, detail:error.message, results:{}};
  render();
});

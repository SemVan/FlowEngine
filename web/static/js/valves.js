let snapshot = null;
let submitting = false;
let initialized = false;
const el = id => document.getElementById(id);
const message = text => { el('valve-message').textContent = text; };
const escape = text => String(text).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const valveLabel = valve => ({sample_valve:'Сэмплер', wash_valve:'Промывка', sheath_valve:'Шиф'}[valve.name] || valve.name) + ` · ${valve.axis}`;
const releaseLabel = status => ({PENDING:'выполняется',
  ACKNOWLEDGED:'команда M18 подтверждена; фактическое отключение не измерялось',
  REQUESTED_UNCONFIRMED:'снятие удержания запрошено, подтверждения нет',
  UNKNOWN:'снятие удержания не подтверждено, состояние питания неизвестно'}[status] || '');

async function request(path, method = 'GET', body, prefix = '/api/valves') {
  const response = await fetch(`${prefix}${path}`, {method,
    headers: {'Content-Type':'application/json'}, body: body ? JSON.stringify(body) : undefined});
  const result = await response.json();
  if (!response.ok) throw new Error(Array.isArray(result.detail)
    ? result.detail.map(item => item.msg).join('; ') : result.detail || 'Ошибка API');
  return result;
}

function render(data) {
  snapshot = data;
  const shared = data.shared_valve_endstop;
  const busy = submitting || data.busy || data.job?.state === 'running';
  const locked = busy || data.machine_state !== 'connected_idle';
  const open = shared.state === 'OPEN';
  el('valve-signal').textContent = `Shared valve endstop: ${shared.state} · Source: ${shared.source} · ${shared.channel || 'вход не настроен'}`;
  el('valve-signal').style.color = open ? '#239767' : '#df624b';
  el('valve-release-link').hidden = shared.state !== 'TRIGGERED';
  el('valve-home-all').disabled = locked || !open;
  el('valve-job').textContent = busy ? `Выполняется: ${data.active_valve || 'подготовка'} · ${data.phase}` : data.job?.state === 'failed' ? `Ошибка: ${data.job.error}` : data.error || '';
  el('valve-cards').innerHTML = data.valves.map(v => `
    <article class="panel" style="margin:1rem 0;padding:1rem">
      <h3>${escape(valveLabel(v))} <small>(${escape(v.name)})</small></h3>
      <p>Calibration: ${v.calibrated ? 'OK' : 'required'} · Reference: ${v.referenced ? 'REFERENCED' : 'Home required'} · State: ${escape(v.state)}</p>
      <p>Current position: ${escape(v.current)} · от HOME: ${v.coordinate === null ? 'UNKNOWN' : v.coordinate.toFixed(4)} · Shared endstop: ${shared.state}</p>
      <p>A: ${v.config.position_a} · B: ${v.config.position_b} · ${v.error ? escape(v.error) : ''}</p>
      ${releaseLabel(v.service_release) ? `<p>Последний запрос снятия удержания: ${releaseLabel(v.service_release)}</p>` : ''}
      <button data-name="${escape(v.name)}" data-action="test_home" ${locked || !open ? 'disabled' : ''}>Пробный Home · ${escape(valveLabel(v))} · отключить мотор в конце</button>
      <button data-name="${escape(v.name)}" data-action="home" ${locked || !open ? 'disabled' : ''}>Home с удержанием · ${escape(valveLabel(v))}</button>
      <button data-name="${escape(v.name)}" data-action="go_a" ${locked || !open || !v.calibrated || !v.referenced ? 'disabled' : ''}>Go A</button>
      <button data-name="${escape(v.name)}" data-action="go_b" ${locked || !open || !v.calibrated || !v.referenced ? 'disabled' : ''}>Go B</button>
      <button data-name="${escape(v.name)}" data-action="calibrate" ${locked || !open ? 'disabled' : ''}>Calibrate · ${escape(valveLabel(v))}</button>
      ${v.commissioning ? `<p>Выберите рабочее положение на глаз и сохраните A, затем B. Для грубой подстройки есть шаги 1 и 10, для точной — 0.1.</p>
        <button data-name="${escape(v.name)}" data-action="jog" data-delta="${v.direction_away_from_home * 0.1}" ${locked || !open ? 'disabled' : ''}>От HOME · 0.1</button>
        <button data-name="${escape(v.name)}" data-action="position_jog" data-delta="${v.direction_away_from_home}" ${locked || !open ? 'disabled' : ''}>От HOME · 1</button>
        <button data-name="${escape(v.name)}" data-action="position_jog" data-delta="${v.direction_away_from_home * 10}" ${locked || !open ? 'disabled' : ''}>От HOME · 10</button>
        <button data-name="${escape(v.name)}" data-action="position_jog" data-delta="${v.direction_toward_home}" ${locked || !open ? 'disabled' : ''}>Обратно · 1</button>
        <button data-name="${escape(v.name)}" data-action="save_a" ${locked || !open ? 'disabled' : ''}>Сохранить A ${v.saved_a ? '✓' : ''}</button>
        <button data-name="${escape(v.name)}" data-action="save_b" ${locked || !open ? 'disabled' : ''}>Сохранить B ${v.saved_b ? '✓' : ''}</button>` : ''}
    </article>`).join('');
  const selected = data.valves.find(v => v.name === el('valve-service-name').value);
  document.querySelectorAll('[data-service]').forEach(button => {
    button.disabled = locked || shared.state === 'UNKNOWN' || !selected;
    button.textContent = selected ? `${valveLabel(selected)}: ${Number(button.dataset.service) > 0 ? '+' : '−'}` : 'Сначала выберите кран';
  });
  el('valve-save-config').disabled = locked;
  el('valve-inputs').disabled = locked;
  if (!initialized) {
    initialized = true;
    el('valve-service-name').innerHTML = '<option value="">Выберите кран для движения</option>' + data.valves.map(v => `<option value="${escape(v.name)}">${escape(valveLabel(v))} (${escape(v.name)})</option>`).join('');
    el('valve-settings').innerHTML = data.valves.map((v, i) => `<fieldset><legend>${escape(v.name)} · ${escape(v.axis)}</legend>
      <label>Направление к HOME <select data-setting="direction_toward_home" data-index="${i}"><option value="1" ${v.direction_toward_home === 1 ? 'selected' : ''}>+</option><option value="-1" ${v.direction_toward_home === -1 ? 'selected' : ''}>−</option></select></label>
      ${[['home_seek_speed','Подход к микрику, ед/мин'],['home_latch_speed','Точное касание, ед/мин'],['backoff','Backoff, ед'],['pull_off','Pull-off, ед'],['home_timeout_s','Поиск, сек (пусто — до микрика)']].map(([key,label]) => `<label>${label} <input type="number" min="0.001" step="any" data-setting="${key}" data-index="${i}" value="${v.config[key] ?? ''}"></label>`).join('')}</fieldset>`).join('');
    if (shared.channel) el('valve-channel').innerHTML = `<option value="${escape(shared.channel)}">${escape(shared.channel)}</option>`;
  }
}

async function refresh() {
  try {
    const [data, response] = await Promise.all([request(''), fetch('/api/state')]);
    if (!response.ok) throw new Error('Не удалось прочитать состояние оборудования');
    const state = await response.json();
    render({...data, machine_state:state.state});
  } catch (error) { message(error.message); }
}
async function submit(operation) {
  if (submitting) return;
  submitting = true;
  message('');
  if (snapshot) render(snapshot);
  try { await operation(); } catch (error) { message(error.message); }
  finally { submitting = false; await refresh(); }
}
el('valve-cards').addEventListener('click', event => {
  const button = event.target.closest('[data-action]');
  if (!button || button.disabled) return;
  if (button.dataset.action === 'position_jog') {
    const valve = snapshot.valves.find(v => v.name === button.dataset.name);
    submit(() => request('/jog', 'POST', {axis:valve.axis,
      by:Number(button.dataset.delta), feedrate:valve.config.home_seek_speed}, '/api/commissioning'));
    return;
  }
  submit(() => request(`/${encodeURIComponent(button.dataset.name)}`, 'POST',
    {action:button.dataset.action, delta:Number(button.dataset.delta || 0.1)}));
});
el('valve-home-all').onclick = () => submit(() => request('/home-all', 'POST', {}));
el('valve-service-name').onchange = () => { if (snapshot) render(snapshot); };
document.querySelectorAll('[data-service]').forEach(button => {
  button.onclick = () => {
    const name = el('valve-service-name').value;
    if (!name || !snapshot?.valves.some(v => v.name === name)) {
      message('Сначала выберите конкретный кран для движения.');
      return;
    }
    const step = Number(el('valve-service-step').value);
    if (!Number.isFinite(step) || step <= 0 || step > 0.5) {
      message('Для сервисного движения введите шаг больше нуля и не больше 0,5. Мотор не запускался.');
      return;
    }
    submit(() => request(`/${encodeURIComponent(name)}`, 'POST',
      {action:'service_jog', delta:step * Number(button.dataset.service)}));
  };
});
el('valve-inputs').onclick = async () => {
  try {
    const result = await request('/inputs');
    const current = el('valve-channel').value;
    el('valve-input-list').textContent = Object.entries(result.inputs).map(([key, value]) => `${key}: ${value ? 'TRIGGERED' : 'OPEN'}`).join('\n');
    el('valve-channel').innerHTML = '<option value="">Выберите проверенный вход</option>' + Object.keys(result.inputs).map(key => `<option value="${escape(key)}">${escape(key)}${result.z_candidates.includes(key) ? ' · Z' : ''}</option>`).join('');
    if (current in result.inputs) el('valve-channel').value = current;
  } catch (error) { message(error.message); }
};
el('valve-save-config').onclick = () => submit(async () => {
  const valves = snapshot.valves.map(v => ({...v.config}));
  document.querySelectorAll('[data-setting]').forEach(input => { valves[Number(input.dataset.index)][input.dataset.setting] = input.dataset.setting === "home_timeout_s" && input.value.trim() === "" ? null : Number(input.value); });
  await request('/config', 'PUT', {shared_valve_endstop:{channel:el('valve-channel').value || null}, valves});
  message('Сохранено. Выполните Home и сохраните A/B для одного крана.');
});
await refresh();
setInterval(refresh, 500);

const el = id => document.getElementById(`drive-${id}`);
const api = '/api/axis-calibration';
let session = null;
let localRun = null;
let busy = false;
let savedName = null;
let polling = false;
let profileName = 'profile';
let readiness = null;
const storageKey = `flowengine.axis-calibration.${location.origin}`;
let recoveryToken = null;
try { recoveryToken = sessionStorage.getItem(storageKey); } catch (_) {  }

function rememberToken(token) {
  try {
    if (token) sessionStorage.setItem(storageKey, token);
    else sessionStorage.removeItem(storageKey);
  } catch (_) {  }
}

function forgetSession() {
  stopRenew(localRun);
  localRun = null; session = null; recoveryToken = null;
  rememberToken(null);
}

async function request(path, body, keepalive = false) {
  const response = await fetch(path, body === undefined ? {} : {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body), keepalive,
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
    error.status = response.status;
    throw error;
  }
  return data;
}

function message(text, error = false) {
  el('message').textContent = text;
  el('message').classList.toggle('error', error);
}

function suggestedProfileName(axis) {
  return profileName && profileName !== 'default' ? profileName : `calibration_${axis}`;
}

function readinessText(axis) {
  const state = readiness?.axes?.[axis];
  if (!state) return 'Ось отсутствует в профиле';
  if (state.calibrated) {
    const plus = state.positive_toward_motor ? 'к мотору' : 'от мотора';
    return `Откалибрована · «+» ${plus} · ход ${Number(state.travel).toFixed(3)} ед. · ${Math.round(state.travel_steps)} шагов`;
  }
  return 'Не откалибрована: нужно найти оба физических края';
}

function loadAxisDefaults(axis = el('axis').value) {
  const state = readiness?.axes?.[axis];
  if (!state) return;
  el('seek-feedrate').value = Number(state.seek_feedrate ?? state.feedrate_default ?? 1200).toFixed(0);
  el('latch-feedrate').value = Number(state.latch_feedrate ?? 120).toFixed(0);
  el('backoff').value = Number(state.backoff_mm ?? 2).toFixed(2).replace(/\.00$/, '');
}

function isMoving(run = session?.run) {
  return !!run && ['starting','moving','stopping'].includes(run.state);
}

function stopRenew(run) {
  if (run?.renewTimer) clearInterval(run.renewTimer);
  if (run) run.renewTimer = null;
}

function finishLocalRun() {
  if (!localRun) return;
  const sameRun = session?.run?.run_id === localRun.id;
  if (sameRun && !isMoving(session.run)) {
// после остановки на микрике разрешаем обратный ход, даже если кнопку ещё держат
    stopRenew(localRun);
    localRun = null;
  }
}

function useState(next, token) {
  if (!session || session.token !== token) return;
  session = next;
  finishLocalRun();
  render();
}

function runText() {
  if (localRun && session?.run?.run_id !== localRun.id) return localRun.releaseRequested ? 'Останавливается…' : 'Запуск…';
  if (localRun?.held && session?.run?.run_id === localRun.id && !isMoving()) return 'Остановлено · отпустите кнопку';
  if (localRun?.releaseRequested && isMoving()) return 'Останавливается…';
  const run = session?.run;
  if (!run) return 'Остановлено';
  if (isMoving(run)) {
    const phases = {seek:'быстрый поиск', backoff:'отход', latch:'точный подход', release:'выход с концевика'};
    const phase = phases[run.phase] ?? run.phase;
    const speed = ['backoff','latch','release'].includes(run.phase) ? run.latch_feedrate : run.feedrate;
    return `${run.direction < 0 ? '−' : '+'} · ${phase} · ${speed} мм/мин${run.state === 'stopping' ? ' · остановка…' : ''}`;
  }
  const reasons = {
    endstop:'Концевик · остановлено', released:'Остановлено по отпусканию',
    release:'Остановлено по отпусканию', lease_expired:'Связь с кнопкой потеряна',
    timeout:'Лимит удержания · остановлено', hold_timeout:'Лимит удержания · остановлено',
    switch_released:'Концевик отпущен · остановлено', retreat_released:'Концевик отпущен · остановлено',
    retreat_limit:'Лимит отхода · остановлено',
    blocked_endstop:'Это направление ведёт в нажатый концевик · выберите противоположное',
    endstop_side_unknown:'Концевик нажат, но сторона неизвестна · начните калибровку между краями',
    error:'Ошибка · движение прекращено',
  };
  return reasons[run.stop_reason] ?? 'Остановлено';
}

function render() {
  el('x-readiness').textContent = readinessText('X');
  el('y-readiness').textContent = readinessText('Y');
  el('interlock').textContent = readiness
    ? (readiness.motion_enabled
      ? 'Motion interlock включён. Калибровка всё равно использует отдельный режим поиска физических краёв.'
      : 'Motion interlock выключен — это нормально. Калибровка и физический Home X/Y доступны; обычные Jog/процедуры остаются заблокированы до reference.')
    : 'Проверяем состояние motion interlock…';
  const locked = busy || !!recoveryToken || !!localRun || isMoving();
  const hasSession = !!session && !savedName;
  const setupLocked = busy || !!recoveryToken || !!session || !!savedName;
  el('axis').disabled = busy || !!recoveryToken || !!savedName || !!localRun || isMoving();
  for (const axis of ['X','Y']) {
    const tab = el(`tab-${axis.toLowerCase()}`);
    if (!tab) continue;
    tab.classList.toggle('is-active', el('axis').value === axis);
    tab.disabled = busy || !!recoveryToken || !!savedName || !!localRun || isMoving();
  }
  el('seek-feedrate').disabled = setupLocked;
  el('latch-feedrate').disabled = setupLocked;
  el('backoff').disabled = setupLocked;
  el('free-confirm').disabled = busy || !!recoveryToken || !!session || !!savedName;
  el('begin').disabled = busy || !!recoveryToken || !!session || !!savedName || !el('free-confirm').checked;
  el('cancel').disabled = !hasSession || locked;
  el('name').disabled = busy || !!localRun || isMoving() || !hasSession || !!savedName;
  el('position').textContent = session ? `${session.axis} ${Number(session.position).toFixed(3)}` : '—';
  el('switch').textContent = session ? (session.pressed ? 'НАЖАТ' : 'Отпущен') : 'Нет сеанса';
  el('switch').classList.toggle('pressed', !!session?.pressed);
  el('run').textContent = runText();
  el('bounds').textContent = session
    ? (session.measured_travel == null
      ? 'Границ по координате нет: найдите оба физических концевика. Ось пока не имеет абсолютной привязки.'
      : `Измеренный физический ход: ${Number(session.measured_travel).toFixed(3)} ед. · ${Math.round(session.measured_travel_steps)} шагов (${session.measurement_source}).`)
    : 'Сначала подтвердите свободный участок и начните калибровку.';
  const blockedDirection = session?.blocked_direction;
  for (const [id, direction] of [['minus',-1],['plus',1]]) {
    const button = el(id);
    const activeButton = localRun?.held && localRun.direction === direction;
    button.disabled = !activeButton && (!hasSession || locked || !!session?.error || (session?.pressed && (blockedDirection == null || blockedDirection === direction)));
    button.classList.toggle('is-held', !!activeButton);
    button.setAttribute('aria-pressed', activeButton ? 'true' : 'false');
    const mapped = session?.positive_toward_motor;
    const towardMotor = mapped === null || mapped === undefined ? null : (direction > 0 ? mapped : !mapped);
    el(`${id}-label`).textContent = `Держать ${direction < 0 ? '−' : '+'}`;
    el(`${id}-hint`).textContent = towardMotor === null ? 'Направление ещё не названо' : (towardMotor ? 'К мотору' : 'От мотора');
  }
  const pendingContact = session?.last_contact;
  const pendingLabel = session?.last_contact_edge;
  const canRecord = hasSession && !locked && !!pendingContact && !pendingLabel
    && session?.run?.state === 'stopped' && session?.run?.stop_reason === 'endstop';
  if (pendingContact) {
    const pendingText = `${session.axis} · подход ${pendingContact.approach_direction < 0 ? '−' : '+'}`
      + ` · ${Number(pendingContact.position).toFixed(3)} ед.`
      + (pendingContact.step_count == null ? '' : ` · Count ${pendingContact.step_count}`);
    el('last-contact').textContent = pendingLabel
      ? `${pendingText} · уже назначен: ${pendingLabel === 'motor' ? 'У МОТОРА' : 'ОТ МОТОРА'}`
      : `${pendingText} · выберите ниже: «У мотора» или «От мотора»`;
  } else {
    el('last-contact').textContent = 'Ещё не найден. Доедьте до физического концевика.';
  }
  for (const edge of ['motor','far']) {
    const contact = session?.contacts?.[edge];
    el(`${edge}-contact`).textContent = contact
      ? `${Number(contact.position).toFixed(3)} ед. · подход ${contact.approach_direction < 0 ? '−' : '+'}${contact.step_count == null ? '' : ` · Count ${contact.step_count}`}`
      : 'Ещё не отмечен';
    el(`record-${edge}`).disabled = !canRecord;
  }
  const mapped = session?.positive_toward_motor;
  el('mapping').textContent = mapped === null || mapped === undefined
    ? 'Названия направлений появятся после отметки края.'
    : `«+» — ${mapped ? 'к мотору' : 'от мотора'}; «−» — ${mapped ? 'от мотора' : 'к мотору'}.${session?.measured_travel == null ? '' : ` Полный ход ${Number(session.measured_travel).toFixed(3)} ед. / ${Math.round(session.measured_travel_steps)} шагов.`}`;
  const complete = !!session?.contacts?.motor && !!session?.contacts?.far && mapped != null;
  el('home-side').disabled = !hasSession || locked || !complete;
  if (session?.home_side && !el('home-side').dataset.userChanged) el('home-side').value = session.home_side;
  el('map-confirm').disabled = !hasSession || locked || !complete;
  el('save').disabled = busy || !!localRun || isMoving() || (!savedName && (!hasSession || !complete || !el('map-confirm').checked));
  el('save').textContent = 'Сохранить калибровку';
}

async function action(callback) {
  if (busy || recoveryToken || localRun || isMoving()) return;
  busy = true; render();
  try { await callback(); }
  catch (error) { message(error.message, true); }
  finally { busy = false; render(); }
}

async function renew(run) {
  if (localRun !== run || !run.held || run.releaseRequested || run.renewing) return;
  run.renewing = true;
  try {
    await request(`${api}/renew`, {token:run.token, run_id:run.id, sequence:++run.sequence});
  } catch (error) {
    if (localRun === run && run.held && !run.releaseRequested) {
      releaseHold();
      message(`Удержание прекращено: ${error.message}`, true);
    }
  } finally { run.renewing = false; }
}

async function startHold(direction, input) {
  const button = el(direction < 0 ? 'minus' : 'plus');
  if (button.disabled || busy || localRun || isMoving() || !session) return;
  const run = {
    id:crypto.randomUUID(), token:session.token, direction, input,
    held:true, releaseRequested:false, sequence:0, renewing:false, renewTimer:null,
  };
  localRun = run;
  run.renewTimer = setInterval(() => renew(run), 200);
  message('Движение запрашивается. Отпускание кнопки остановит его.');
  render();
  try {
    const next = await request(`${api}/start`, {
      token:run.token, run_id:run.id, direction,
    });
    if (localRun !== run || !session || session.token !== run.token) return;
    useState(next, run.token);
    if (run.held && !run.releaseRequested && isMoving(next.run)) {
      message('Держите кнопку и наблюдайте за кареткой.');
    }
  } catch (error) {
    if (localRun !== run) return;
    stopRenew(run);
    if (!run.releaseRequested) {
      releaseHold();
      message(`Запуск не подтверждён: ${error.message}. Передан запрос остановки.`, true);
    }
    render();
  }
}

function releaseHold(keepalive = false) {
  const run = localRun;
  if (!run) return;
  run.held = false;
  stopRenew(run);
  if (run.releaseRequested) { finishLocalRun(); render(); return; }
  run.releaseRequested = true;
  render();
// отмену отправляем сразу: сервер узнает запуск по его id
  request(`${api}/release`, {token:run.token, run_id:run.id}, keepalive).then(next => {
    if (localRun !== run) return;
    useState(next, run.token);
    if (!isMoving(next.run)) {
      localRun = null;
      message(next.run?.stop_reason === 'endstop' ? 'Концевик сработал. Назовите край, который видите.' : 'Остановлено. Для следующего движения удерживайте кнопку снова.');
      render();
    }
  }).catch(error => {
    if (localRun !== run) return;
    message(`Нет подтверждения остановки: ${error.message}. Удержание больше не продлевается; используйте СТОП, если движение продолжается.`, true);
  });
}

for (const [id, direction] of [['minus',-1],['plus',1]]) {
  const button = el(id);
  button.addEventListener('pointerdown', event => {
    if (!event.isPrimary || event.button !== 0 || button.disabled || localRun) return;
    event.preventDefault();
    button.focus({preventScroll:true});
    button.setPointerCapture(event.pointerId);
    startHold(direction, {type:'pointer', id:event.pointerId});
  });
  for (const name of ['pointerup','pointercancel','lostpointercapture']) button.addEventListener(name, event => {
    if (localRun?.input.type === 'pointer' && localRun.input.id === event.pointerId) releaseHold();
  });
  button.addEventListener('keydown', event => {
    if (![' ','Enter'].includes(event.key)) return;
    event.preventDefault();
    if (!event.repeat) startHold(direction, {type:'keyboard', key:event.key});
  });
  button.addEventListener('click', event => event.preventDefault());
}
window.addEventListener('keyup', event => {
  if (localRun?.input.type === 'keyboard' && localRun.input.key === event.key) {
    event.preventDefault(); releaseHold();
  }
});
window.addEventListener('blur', () => releaseHold(true));
document.addEventListener('visibilitychange', () => { if (document.hidden) releaseHold(true); });
window.addEventListener('pagehide', () => releaseHold(true));
document.getElementById('abort-btn').addEventListener('click', () => {
  releaseHold();
  forgetSession();
  message('Передан общий СТОП. Сеанс закрыт; после остановки используйте «Переподключить».');
  render();
});

el('free-confirm').addEventListener('change', render);
el('map-confirm').addEventListener('change', render);
el('home-side').addEventListener('change', () => { el('home-side').dataset.userChanged = '1'; render(); });
async function switchToAxis(target) {
  const previous = session?.axis ?? el('axis').value;
  if (target === previous) {
    el('axis').value = target;
    render();
    return;
  }
  if (!session) {
    el('axis').value = target;
    el('free-confirm').checked = false;
    loadAxisDefaults(target);
    message(`Выбрана ось ${target}. Подтвердите свободный участок и начните калибровку.`);
    render();
    return;
  }
  if (localRun || isMoving()) {
    message('Сначала остановите движение, затем переключайте X/Y.', true);
    render();
    return;
  }
  await action(async () => {
    const cfg = readiness?.axes?.[target] || {};
    const seek = Number(cfg.seek_feedrate ?? cfg.feedrate_default ?? 1200);
    const latch = Number(cfg.latch_feedrate ?? Math.min(120, seek));
    const backoff = Number(cfg.backoff_mm ?? 2);
    try {
      session = await request(`${api}/switch`, {
        token:session.token, axis:target, free_space_confirmed:true,
        seek_feedrate:seek, latch_feedrate:latch, backoff_mm:backoff,
      });
      el('axis').value = target;
      el('seek-feedrate').value = Number(session.seek_feedrate).toFixed(0);
      el('latch-feedrate').value = Number(session.latch_feedrate).toFixed(0);
      el('backoff').value = Number(session.backoff_mm).toFixed(2).replace(/\.00$/, '');
      el('home-side').value = session.home_side || 'motor';
      delete el('home-side').dataset.userChanged;
      el('map-confirm').checked = false;
      message(`Переключено ${previous} → ${target}. Черновик ${previous} сохранён; можно вернуться без потери найденных краёв.`);
    } catch (error) {
      el('axis').value = previous;
      throw error;
    }
  });
}

el('axis').addEventListener('change', () => { void switchToAxis(el('axis').value); });
for (const axis of ['X','Y']) {
  el(`tab-${axis.toLowerCase()}`)?.addEventListener('click', () => { void switchToAxis(axis); });
}

el('begin').addEventListener('click', () => action(async () => {
  if (!el('free-confirm').checked) throw new Error('Подтвердите свободный участок и отпущенные концевики.');
  const seek = Number(el('seek-feedrate').value);
  const latch = Number(el('latch-feedrate').value);
  const backoff = Number(el('backoff').value);
  if (!(seek >= 30 && seek <= 1200)) throw new Error('Быстрый поиск: укажите 30–1200 мм/мин.');
  if (!(latch >= 15 && latch <= 600 && latch <= seek)) throw new Error('Точный подход: 15–600 мм/мин и не быстрее быстрого поиска.');
  if (!(backoff >= 0.25 && backoff <= 10)) throw new Error('Отход: укажите 0.25–10 мм.');
  session = await request(`${api}/begin`, {
    axis:el('axis').value, free_space_confirmed:true,
    seek_feedrate:seek, latch_feedrate:latch, backoff_mm:backoff,
  });
  rememberToken(session.token);
  el('name').value = suggestedProfileName(session.axis);
  el('home-side').value = session.home_side || 'motor';
  delete el('home-side').dataset.userChanged;
  el('map-confirm').checked = false;
  message('Сеанс начат. Удерживайте «−» или «+» и наблюдайте, куда движется каретка.');
}));
for (const edge of ['motor','far']) el(`record-${edge}`).addEventListener('click', () => action(async () => {
  session = await request(`${api}/record`, {token:session.token, edge, observed_contact_confirmed:true});
  el('map-confirm').checked = false;
  message(`Контакт назначен как «${edge === 'motor' ? 'У МОТОРА' : 'ОТ МОТОРА'}». Теперь удерживайте противоположное направление и найдите второй физический край.`);
}));
el('cancel').addEventListener('click', () => action(async () => {
  await request(`${api}/cancel`, {token:session.token});
  forgetSession(); el('free-confirm').checked = false;
  message('Сеанс завершён без сохранения. Удержание мотора могло сохраниться; привязка оси не установлена.');
}));
el('save').addEventListener('click', () => action(async () => {
  if (!el('map-confirm').checked) throw new Error('Подтвердите проверенные названия обоих направлений.');
  if (!el('name').reportValidity() || !el('name').value.trim()) throw new Error('Укажите имя профиля: латинские буквы, цифры, дефис или подчёркивание.');
  const result = await request(`${api}/save`, {
    token:session.token, name:el('name').value.trim(), observed_direction_confirmed:true,
    home_side:el('home-side').value,
  });
  savedName = result.name; forgetSession();
  readiness = await request(`${api}/readiness`);
  el('free-confirm').checked = false;
  el('map-confirm').checked = false;
  message(`Калибровка сохранена в ${result.name}: ${result.axes?.join(' + ') || result.axis}. Текущая ${result.axis}: ход ${Number(result.travel).toFixed(3)} ед., ${Math.round(result.travel_steps)} шагов; Home = ${result.home_side === 'motor' ? 'у мотора' : 'от мотора'}.`);
  savedName = null;
}));

setInterval(async () => {
  if ((!session && !recoveryToken) || polling || document.hidden) return;
  polling = true;
  const token = session?.token ?? recoveryToken;
  try {
    const next = await request(`${api}/state?token=${encodeURIComponent(token)}`);
    if (recoveryToken === token && !session) {
      recoveryToken = null;
      session = next;
      rememberToken(next.token);
      el('axis').value = next.axis;
      el('seek-feedrate').value = next.seek_feedrate;
      el('latch-feedrate').value = next.latch_feedrate;
      el('backoff').value = next.backoff_mm;
      el('name').value = suggestedProfileName(next.axis);
      el('home-side').value = next.home_side || 'motor';
      delete el('home-side').dataset.userChanged;
      el('map-confirm').checked = false;
      message(isMoving(next.run)
        ? 'Восстановлен сеанс. Ожидайте подтверждения остановки предыдущего удержания; движение не продлевается.'
        : 'Восстановлен сеанс без запуска движения. Для продолжения удерживайте выбранную кнопку.');
      render();
    } else useState(next, token);
    if (session?.error) message(session.error, true);
    else if (session?.run?.stop_reason === 'endstop' && !localRun?.held) message('Концевик сработал. Назовите край, который видите.');
  } catch (error) {
    if (session?.token === token || recoveryToken === token) {
      if ([404,409].includes(error.status)) {
        forgetSession();
        el('free-confirm').checked = false;
        const reconnectHint = /переподключ/i.test(error.message)
          ? ' Нажмите «↻ Переподключить» внизу окна; программу перезапускать не нужно.'
          : '';
        message(error.message + reconnectHint, true);
        render();
      } else {
        releaseHold();
        message(`Не удалось прочитать сеанс: ${error.message}`, true);
      }
    }
  } finally { polling = false; }
}, 200);

Promise.all([
  request('/api/state'),
  request(`${api}/readiness`),
]).then(([state, status]) => {
  profileName = state.profile || status.profile || 'profile';
  readiness = status;
  loadAxisDefaults();
  render();
}).catch(() => {
  request('/api/state').then(state => { profileName = state.profile || 'profile'; render(); }).catch(() => {});
});
render();

import { api } from './api.js';
document.querySelectorAll('.topbar nav a').forEach(a => {
  if (a.pathname === location.pathname) a.classList.add('current');
});
api.state().then(s => {
  document.getElementById('active-profile').textContent = s.profile === 'default' ? 'Заводской' : s.profile;
  document.getElementById('session-mode').textContent = s.transport === 'mock' ? '● СИМУЛЯТОР · без оборудования' : `● ОБОРУДОВАНИЕ · ${s.port || s.transport}`;
}).catch(() => { document.getElementById('session-mode').textContent = 'Нет связи с движком'; });

const reconnect = document.getElementById('reconnect-btn');
const recoveryStatus = document.getElementById('reconnect-status');
let desktopSession;
reconnect.hidden = false;
fetch('/api/desktop/session').then(async response => {
  if (!response.ok) return;
  desktopSession = await response.json();
}).catch(() => {});
reconnect.addEventListener('click', async () => {
  if (document.body.dataset.profileDirty === 'true') {
    recoveryStatus.textContent = 'Сначала сохраните изменения профиля.';
    return;
  }
  reconnect.disabled = true;
  recoveryStatus.textContent = 'Восстанавливаем подключение…';
  try {
    const desktop = !!desktopSession;
    const response = await fetch(desktop ? '/api/desktop/reconnect' : '/api/reconnect', desktop ? {
      method:'POST', headers:{'X-Desktop-Token':desktopSession.token},
    } : {method:'POST'});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Не удалось переподключиться');
    recoveryStatus.textContent = 'Переподключено. Привязки координат сброшены; команды движения не повторяются.';
    if (!desktop) setTimeout(() => location.reload(), 250);
  } catch(error) {
    recoveryStatus.textContent = error.message;
    reconnect.disabled = false;
  }
});

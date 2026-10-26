const $ = id => document.getElementById(id);
const storageKey = "flowengine.commissioning.v1";
const names = { A: "Кран промывки", B: "Кран обжимающей жидкости", C: "Кран сэмплера", X: "Насос суспензии", Y: "Насос обжимающей жидкости", U: "Насос промывки" };
const groups = [];
let latestContext = null;
let reading = false;
let storageWorks = true;
let sheet = loadSheet();

function freshSheet() {
  const now = new Date().toISOString();
  return { schema_version: 1, created_at: now, updated_at: now, context: null, diagnostics: null, input_checks: [], fields: {}, field_times: {} };
}
function loadSheet() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
    if (saved?.schema_version === 1 && saved.fields && !Array.isArray(saved.fields) && typeof saved.fields === "object") return saved;
  } catch { storageWorks = false; }
  return freshSheet();
}
function node(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
function when(value) {
  const date = new Date(value);
  return value && Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "medium", timeZone: "Europe/Moscow" }).format(date) : "нет";
}
function message(text) { $("commission-message").textContent = text; }
function persist() {
  try { localStorage.setItem(storageKey, JSON.stringify(sheet)); }
  catch { storageWorks = false; }
  $("commission-report").value = report();
  renderInputs();
  updateContext();
}
function renderInputs() {
  const initial = sheet.diagnostics?.readings?.M119;
  const previous = sheet.input_checks?.at(-1);
  const last = previous && (!initial || previous.at >= sheet.diagnostics.finished_at) ? previous : null;
  const reading = last?.reading || initial;
  const at = last?.at || sheet.diagnostics?.finished_at;
  $("commission-input-view").textContent = reading
    ? `${when(at)} (Москва)${last?.node ? " · " + last.node : ""}${last?.phase ? " · " + phaseName(last.phase) : ""}\\n` + Object.entries(reading.endstops || {}).map(([key, pressed]) => `${key}: ${pressed ? "TRIGGERED" : "OPEN"}`).join("\\n")
    : "Входы ещё не читались на этом листе.";
}
function phaseName(phase) {
  return { released_before: "отпущен до нажатия", pressed: "удерживаю рукой", released_after: "снова отпущен" }[phase] || "фаза не указана";
}
function updateContext() {
  const ctx = sheet.context;
  const current = latestContext;
  const parts = [`Лист: ${when(sheet.created_at)} (Москва)`, `заполнено полей: ${Object.values(sheet.fields).filter(v => String(v).trim()).length}`];
  if (ctx) parts.push(`снимок ${when(ctx.captured_at)}: ${ctx.profile}, ${ctx.transport}, ${ctx.port || "без COM"}`);
  if (sheet.diagnostics) parts.push(`плата прочитана: ${when(sheet.diagnostics.finished_at)}${sheet.diagnostics.complete ? "" : " (не полностью)"}`);
  if (current && ctx && differentContext(current, ctx)) parts.push("ТЕКУЩИЙ ПРОФИЛЬ/ПОДКЛЮЧЕНИЕ ОТЛИЧАЕТСЯ: выгрузи старый лист и создай новый");
  if (!storageWorks) parts.push("Автосохранение недоступно: скачай JSON перед закрытием");
  $("commission-context").textContent = parts.join(" · ");
}
function differentContext(a, b) {
  return ["profile", "instrument", "transport", "port", "baud"].some(key => a[key] !== b[key]);
}
async function request(path, method = "GET") {
  const response = await fetch(path, { method, cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || response.statusText);
  return body;
}
function field(key, label, kind = "text", choices = null) { return { key, label, kind, choices }; }
function choice(key, label, choices) { return field(key, label, "select", choices); }
function group(id, title, fields, help, open = false) { groups.push({ id, title, fields, help, open }); }

const switches = [["ok", "OPEN → TRIGGERED → OPEN"], ["always_open", "Всё время OPEN"], ["always_triggered", "Всё время TRIGGERED"], ["other", "Другой результат — описал ниже"]];
const directions = [["-", "Минус"], ["+", "Плюс"], ["uncertain", "Не удалось определить"]];
const homeResults = [["complete", "Дошёл, повторно коснулся и отошёл"], ["timeout", "Не дошёл до микрика / тайм-аут"], ["wrong_direction", "Пошёл от микрика"], ["no_motion", "Нет заметного движения"], ["stopped", "Остановил вручную"], ["other", "Другой результат"]];

group("initial", "1. Начальные наблюдения — сначала достаточно этого блока", [
  field("operator", "Кто заполняет / заметка о визите"),
  choice("cool", "До проверки: моторы остыли?", [["yes", "Да"], ["no", "Нет"], ["unknown", "Не проверял"]]),
  field("fluid_state", "Что сейчас в шприцах и линиях; есть ли давление?"),
  field("measurement_tool", "Чем можно измерять: линейка, штангенциркуль, индикатор, транспортир, весы…"),
  ...["C", "A", "B"].map(a => choice(`switch_${a}`, `Микрик ${a} · ${names[a]}: ручное нажатие и отпускание`, switches)),
  field("switch_notes", "Какой вход менялся, что нажимал, что осталось непонятным", "textarea"),
], "Без движения моторов: читать вход при отпущенном микрике, при ручном нажатии и после отпускания. Проверять один микрик за раз, остальные отпущены. Если к микрику нельзя добраться без движения механизма — пропустить.", true);

for (const a of ["C", "A", "B"]) group(`valve_${a}`, `${a === "C" ? "2" : "3"}. ${a} · ${names[a]} — проба и рабочие позиции`, [
  choice(`${a}_toward`, "Какой знак физически ведёт к микрику?", directions),
  field(`${a}_trial`, "Какая проба была выполнена: команда/кнопка, скорость, тайм-аут, время"),
  choice(`${a}_home`, "Результат пробного Home", homeResults),
  field(`${a}_error`, "Точный текст ошибки / необычное поведение", "textarea"),
  choice(`${a}_final_switch`, "Сигнал после пробы", [["open", "OPEN"], ["triggered", "TRIGGERED"], ["unknown", "Неизвестен"]]),
  field(`${a}_release`, "Отключение удержания: что показала программа (ACK / запрос без подтверждения / неизвестно)"),
  field(`${a}_temperature`, "Нагрев и температура, если измеряли"),
  field(`${a}_pos_a`, "Сохранённая координата положения A — единицы Marlin"),
  field(`${a}_label_a`, "Что физически соединяет положение A / его рабочее название"),
  field(`${a}_pos_b`, "Сохранённая координата положения B — единицы Marlin"),
  field(`${a}_label_b`, "Что физически соединяет положение B / его рабочее название"),
  field(`${a}_startup`, "Какое сохранённое положение означает ПРОКАЧКА по таблице инженеров?"),
  field(`${a}_angle_cmd`, "Для масштаба: изменения координаты Marlin, через ;"),
  field(`${a}_angle_real`, "Соответствующие углы выхода редуктора, градусы, через ;"),
], "Записывать после отдельно запущенных проверок. Если Home не завершён, пока достаточно результата и ошибки. A/B и угол заполнять только после фактической проверки; история C− не заменяет наблюдение сегодня.");

for (const a of ["X", "Y"]) group(`pump_${a}`, `4. ${a} · ${names[a]} — миллиметры и объём`, [
  field(`${a}_motor_side`, "С какой стороны моторный микрик; какой знак ведёт к нему?"),
  field(`${a}_minimum`, "Какой физический конец инженеры называют MIN?"),
  field(`${a}_home_result`, "Home: результат / ошибка / выбранная физическая сторона"),
  field(`${a}_working_offset`, "Рабочий отход от MIN: значение и единицы"),
  field(`${a}_scale_cmd`, "Масштаб: заданные перемещения Marlin в одну сторону, через ;"),
  field(`${a}_scale_mm`, "Измеренные ходы поршня в мм в том же порядке, через ;"),
  field(`${a}_scale_method`, "Чем измерял; скорость; была ли перед замером выбрана слабина?"),
  field(`${a}_syringe`, "Шприц: модель / объём / внутренний диаметр, если известны"),
  field(`${a}_dose_steps`, "Для объёма: заданные импульсы STEP, через ;"),
  field(`${a}_dose_volume`, "Полученные объёмы или массы с единицами, через ;"),
  field(`${a}_dose_conditions`, "Жидкость, направление, скорость, давление; плотность при взвешивании"),
], "Прежний опыт пользователя: около 1 мм реального хода на команду 1 мм для X; в памятке программиста — 80 STEP/мм. Масштаб сохраняем до проверки. Размер доступного измеряемого хода выбираем на месте; сюда не заложена команда движения.");

group("backlash", "5. Люфт — только измеренные значения", [
  ...["X", "Y", "C", "A", "B"].flatMap(a => [
    field(`${a}_lash_plus_minus`, `${a}: после + → −, холостой ход в единицах Marlin; повторы через ;`),
    field(`${a}_lash_minus_plus`, `${a}: после − → +, холостой ход в единицах Marlin; повторы через ;`),
  ]),
  field("lash_method", "Как заметил начало движения поршня/выхода; скорость; точность; особенности", "textarea"),
], "Считать ход мотора после реверса до начала движения выхода. M114/Count сами по себе не измеряют движение выхода. Если не удалось различить — написать «не удалось», а не 0. Компенсация от заполнения полей не включается.");

group("wash", "6. U · Насос промывки", [
  choice("U_sensor", "Есть ли датчик оборота / метка?", [["yes", "Да"], ["no", "Нет"], ["unknown", "Неизвестно"]]),
  field("U_sensor_channel", "Имя входа из M119 / куда подключён датчик"),
  field("U_sensor_meaning", "Что означает одно срабатывание: оборот, метка, другое?"),
  field("U_direction", "Рабочий знак вращения и фактическое направление жидкости"),
  field("U_period_steps", "Импульсы между соседними одинаковыми метками, повторы через ;"),
  field("U_period_notes", "Как получены значения, число меток на оборот, скорость"),
  field("U_volume", "Если измеряли подачу: обороты/импульсы и объём или масса с единицами"),
], "Автоматического измерения периода U пока нет. Сейчас можно узнать подключение и устройство датчика у инженеров. Не требуется запускать мотор вслепую ради заполнения листа.");

const routes = [
  ["X_aspirate", "X · забор"], ["X_dispense", "X · подача"],
  ["Y_aspirate", "Y · забор"], ["Y_dispense", "Y · подача"],
  ["U_forward", "U · рабочая промывка"],
];
group("routes", "7. Разрешения насосов — ответ инженеров", routes.flatMap(([id, label]) => [
  field(`${id}_valves`, `${label}: разрешённое сочетание кранов A / B / C (позиции и названия)`),
  choice(`${id}_confirmed`, `${label}: подтверждение`, [["yes", "Инженер подтвердил"], ["forbidden", "Этот режим запрещён"], ["unknown", "Ещё не определено"]]),
]), "В каждом сочетании перечислить все три крана. Если вариантов несколько — записать все. Это сведения для реализации проверок; ввод не разрешает движение и не меняет код блокировок.");
group("notes", "8. Остальное", [field("notes", "Наблюдения, вопросы инженерам, что не удалось измерить", "textarea")], "Можно заполнить только доступные разделы и отправить промежуточный отчёт.");

function render() {
  $("commission-fields").replaceChildren();
  for (const g of groups) {
    const details = node("details"); details.open = g.open;
    details.append(node("summary", g.title), node("p", g.help, "hint"));
    const grid = node("div", undefined, "commission-grid");
    for (const f of g.fields) {
      const label = node("label", f.label);
      const input = node(f.kind === "select" ? "select" : f.kind === "textarea" ? "textarea" : "input");
      input.dataset.field = f.key;
      if (f.kind === "select") {
        for (const [value, title] of [["", "Не заполнял / не проверял"], ...f.choices]) {
          const option = node("option", title); option.value = value; input.append(option);
        }
      } else if (f.kind === "textarea") { input.rows = 3; label.className = "commission-wide"; }
      else input.type = "text";
      input.value = sheet.fields[f.key] || "";
      label.append(input); grid.append(label);
    }
    details.append(grid); $("commission-fields").append(details);
  }
}

function report() {
  const lines = ["FLOWENGINE · РЕЗУЛЬТАТЫ НАЛАДКИ", `Визит: ${when(sheet.created_at)} (Москва)`, "Пустые/неуказанные результаты не измерены; сохранённый профиль не подтверждает механику."];
  const c = sheet.context;
  if (c) {
    lines.push(`Снимок ${when(c.captured_at)}: ${c.instrument}; профиль ${c.profile}; ${c.transport}; ${c.port || "без COM"}/${c.baud || "—"}; ${c.state}`);
    lines.push(`Reference: ${Object.entries(c.referenced || {}).map(([a, value]) => `${a}=${value ? "есть" : "нет"}`).join(", ")}`);
    lines.push(`Профиль STEP/ед.: ${(c.axes || []).map(a => `${a.axis}=${a.steps_per_unit} (${a.units})`).join(", ")}`);
    const shared = c.valves?.shared_valve_endstop;
    if (shared) lines.push(`Общий вход кранов (снимок программы): ${JSON.stringify(shared)}`);
    for (const valve of c.valves?.valves || []) {
      const cfg = valve.config;
      if (cfg) lines.push(`${cfg.axis} профиль: dir=${cfg.direction_toward_home}, seek=${cfg.home_seek_speed}, latch=${cfg.home_latch_speed}, timeout=${cfg.home_timeout_s}; A=${cfg.position_a}, B=${cfg.position_b}, calibrated=${cfg.calibrated}; release=${valve.service_release || "неизвестно"}`);
    }
  }
  if (sheet.diagnostics) {
    const d = sheet.diagnostics;
    lines.push(`Чтение платы ${when(d.finished_at)}: ${d.complete ? "полное" : "частичное"}`);
    for (const [command, data] of Object.entries(d.readings || {})) {
      const raw = Array.isArray(data.raw) ? data.raw : String(data.raw || "").split("\n");
      if (command === "M503") {
        for (const line of raw.filter(s => /\bM(?:92|201|203|204|906)\b/.test(s))) lines.push(line);
      } else if (command === "M119") lines.push(`M119: ${JSON.stringify(data.endstops || {})}`);
      else if (command === "M114") lines.push(`M114: ${raw.join(" | ")}`);
      else if (command === "M211") lines.push(`M211: ${raw.join(" | ")}`);
      else if (command === "M115") lines.push(`M115: ${raw[0] || "нет ответа"}`);
    }
    lines.push(...(d.errors || []).map(e => `Ошибка чтения: ${e}`));
  }
  if (sheet.input_checks?.length) {
    lines.push("", "Проверки микриков — записи M119:");
    for (const check of sheet.input_checks) lines.push(`${when(check.at)}: ${check.node || "узел не указан"}, ${phaseName(check.phase)}: ${Object.entries(check.reading.endstops || {}).map(([key, value]) => `${key}=${value ? "TRIGGERED" : "OPEN"}`).join(", ")}`);
  }
  for (const g of groups) {
    const filled = g.fields.filter(f => String(sheet.fields[f.key] || "").trim());
    if (!filled.length) continue;
    lines.push("", g.title);
    for (const f of filled) {
      const value = String(sheet.fields[f.key]);
      const text = f.choices?.find(([v]) => v === value)?.[1] || value;
      lines.push(`${f.label}: ${text}`);
    }
  }
  if (!Object.values(sheet.fields).some(v => String(v).trim())) lines.push("", "Наблюдения ещё не заполнены.");
  if (!sheet.diagnostics) lines.push("", "Плата на этом листе ещё не прочитана.");
  return lines.join("\n");
}

$("commission-form").addEventListener("submit", e => e.preventDefault());
$("commission-form").addEventListener("input", e => {
  const key = e.target.dataset.field;
  if (!key) return;
  sheet.fields[key] = e.target.value;
  sheet.updated_at = new Date().toISOString();
  sheet.field_times ||= {}; sheet.field_times[key] = sheet.updated_at;
  persist(); message("Наблюдения сохранены в листе. Профиль оборудования не менялся.");
});

async function refreshContext() {
  latestContext = await request("/api/commissioning/snapshot");
  if (sheet.context && differentContext(sheet.context, latestContext)) {
    updateContext(); throw new Error("Лист относится к другому профилю/подключению. Скачай его и создай новый лист.");
  }
  sheet.context = latestContext; sheet.updated_at = new Date().toISOString(); persist();
}
$("commission-snapshot").addEventListener("click", async () => {
  try { await refreshContext(); message("Состояние программы обновлено. Команды плате не отправлялись."); }
  catch (error) { message(error.message); }
});
$("commission-read").addEventListener("click", async () => {
  if (reading) return;
  reading = true; $("commission-read").disabled = true;
  $("commission-input-read").disabled = true;
  try {
    await refreshContext();
    message("Читаем плату без движения…");
    const result = await request("/api/commissioning/diagnostics", "POST");
    sheet.diagnostics = result;
    sheet.context = result.context; latestContext = result.context;
    sheet.updated_at = new Date().toISOString(); persist();
    message(result.complete ? "Данные платы сохранены. Движения не запускались." : `Часть данных сохранена. ${result.errors.join("; ")}`);
  } catch (error) { message(`Чтение не выполнено: ${error.message}`); }
  finally { reading = false; $("commission-read").disabled = false; $("commission-input-read").disabled = false; }
});
$("commission-input-read").addEventListener("click", async () => {
  if (reading) return;
  reading = true; $("commission-input-read").disabled = true; $("commission-read").disabled = true;
  const checkedNode = $("commission-switch").value;
  const phase = $("commission-phase").value;
  try {
    await refreshContext();
    const result = await request("/api/commissioning/endstops", "POST");
    if (!result.complete) throw new Error(result.errors.join("; "));
    sheet.input_checks ||= [];
    sheet.input_checks.push({ at: result.finished_at, node: checkedNode, phase, reading: result.readings.M119 });
    sheet.context = result.context; latestContext = result.context;
    sheet.updated_at = new Date().toISOString(); persist();
    message("Запись M119 сохранена. Для следующего состояния выбери фазу и нажми ещё раз.");
  } catch (error) { message(`M119 не записан: ${error.message}`); }
  finally { reading = false; $("commission-read").disabled = false; $("commission-input-read").disabled = false; }
});
$("commission-copy").addEventListener("click", async () => {
  $("commission-report").value = report();
  try { await navigator.clipboard.writeText(report()); message("Отчёт скопирован. Вставь его одним сообщением в чат."); }
  catch { $("commission-report").closest("details").open = true; $("commission-report").select(); message("Выделен текст отчёта — скопируй его вручную."); }
});
function download(extension, content, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = node("a"); link.href = url;
  link.download = `flowengine-commissioning-${sheet.created_at.replace(/[:.]/g, "-")}.${extension}`;
  link.click(); URL.revokeObjectURL(url);
}
$("commission-text").addEventListener("click", () => download("txt", report(), "text/plain;charset=utf-8"));
$("commission-json").addEventListener("click", () => download("json", JSON.stringify(sheet, null, 2), "application/json"));
$("commission-new").addEventListener("click", () => {
  if (reading) return;
  if (!window.confirm("Начать новый лист? Сначала скачай текущий, если его результаты нужны.")) return;
  sheet = freshSheet(); sheet.context = latestContext; render(); persist(); message("Новый лист создан. Наблюдения ещё не заполнены.");
});

render(); persist();
request("/api/commissioning/snapshot").then(context => {
  latestContext = context;
  if (!sheet.context) { sheet.context = context; persist(); }
  else updateContext();
  const same = !differentContext(sheet.context, context);
  message(same ? "Лист готов. Сначала можно прочитать плату без движения." : "Сохранён лист другого профиля/подключения. Скачай его и создай новый.");
}).catch(error => message(`Лист доступен для заполнения; состояние программы не загружено: ${error.message}`));

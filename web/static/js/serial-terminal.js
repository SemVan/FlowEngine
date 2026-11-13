const panel = document.getElementById("gcode-panel");
if (panel) {
  const lines = document.getElementById("gcode-lines");
  const status = document.getElementById("gcode-status");
  const filter = document.getElementById("gcode-filter");
  const follow = document.getElementById("gcode-follow");
  let history = [], traceId = "", after = 0, busy = false, paused = false;
  if (location.pathname === "/terminal") panel.open = true;
  const format = row => `${row.timestamp.slice(11, 23)} ${row.direction}  ${row.line}`;
  function render() {
    const mode = filter.value;
    lines.textContent = history.filter(row => mode === "all" || mode === row.direction ||
      mode === "motion" && row.direction === "TX" && /^(G(?:0?0|0?1|28|38|90|91|92)\b|M(?:17|18|84|410)\b)/i.test(row.line)).slice(-4000).map(format).join("\n");
    if (follow.checked) lines.scrollTop = lines.scrollHeight;
  }
  async function refresh() {
    if (!panel.open || document.hidden || busy || paused) return;
    busy = true;
    try {
      const response = await fetch(`/api/diagnostics/trace?after=${after}&trace_id=${encodeURIComponent(traceId)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Нет связи с журналом");
      if (data.reset && traceId) history.push({timestamp: new Date().toISOString(), direction: "—", line: "Новый сеанс связи"});
      if (data.gap) history.push({timestamp: new Date().toISOString(), direction: "—", line: "Часть старых записей вышла из буфера платы/сервера"});
      traceId = data.trace_id; after = data.latest;
      history.push(...data.entries); history = history.slice(-20000);
      if (data.entries.length || data.reset) render();
      status.textContent = `Связь с журналом есть · записей в этом окне: ${history.length}`;
    } catch (error) { status.textContent = error.message; }
    finally { busy = false; }
  }
  filter.addEventListener("change", render);
  follow.addEventListener("change", render);
  panel.addEventListener("toggle", refresh);
  document.getElementById("gcode-clear").addEventListener("click", () => { history = []; render(); });
  document.getElementById("gcode-pause").addEventListener("click", event => {
    paused = !paused; event.target.textContent = paused ? "Продолжить просмотр" : "Пауза просмотра";
    status.textContent = paused ? "Просмотр приостановлен, аппарат продолжает работу." : "Продолжаем чтение…";
    refresh();
  });
  document.getElementById("gcode-export").addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([history.map(format).join("\n")], {type:"text/plain;charset=utf-8"}));
    const link = document.createElement("a"); link.href = url; link.download = "flowengine-gcode.log"; link.click(); URL.revokeObjectURL(url);
  });
  setInterval(refresh, 700); refresh();
}

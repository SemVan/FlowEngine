// Scrolling log tail in the footer.

export function mountLog(store) {
  const ul = document.getElementById("log-tail");
  if (!ul) return;

  store.subscribe((state) => {
    const log = state.log || [];
    if (log.length === 0) {
      ul.innerHTML = "";
      return;
    }
    // Render last 60 lines, oldest at top.
    const tail = log.slice(-60);
    ul.innerHTML = "";
    for (const line of tail) {
      const li = document.createElement("li");
      li.className = "lvl-" + (line.level || "INFO");
      li.textContent = line.message;
      ul.appendChild(li);
    }
    ul.scrollTop = ul.scrollHeight;
  });
}

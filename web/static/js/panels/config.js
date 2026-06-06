import { api } from "../api.js";

const out = document.getElementById("config-dump");
if (out) {
  api.config()
    .then((c) => { out.textContent = JSON.stringify(c, null, 2); })
    .catch((e) => { out.textContent = "Error: " + e.message; });
}

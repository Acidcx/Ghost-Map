"use strict";
// Login page. Relative paths so it works behind the IXON HTTP proxy.

document.getElementById("loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target, err = document.getElementById("loginErr");
  err.textContent = "";
  try {
    const res = await fetch("api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Ghostmap": "1" },
      body: JSON.stringify({ user: f.user.value, password: f.password.value }),
    });
    if (res.ok) { location.href = "./"; return; }
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) { /* ignore */ }
    err.textContent = msg;
  } catch (e) {
    err.textContent = e.message;
  }
  f.password.value = "";
});

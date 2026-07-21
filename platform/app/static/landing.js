// Landing signup: POST the email to the magic-link endpoint. External file
// because the CSP forbids inline scripts.
(() => {
  const form = document.getElementById("signup");
  const email = document.getElementById("email");
  const button = document.getElementById("send");
  const status = document.getElementById("status");

  const show = (msg, cls) => {
    status.textContent = msg;
    status.className = cls || "";
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const value = email.value.trim();
    if (!value) return;
    button.disabled = true;
    show("Sending…");
    try {
      const r = await fetch("/api/auth/request-link", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: value }),
      });
      if (r.ok) {
        show("Check your email — the link signs you in on this device and is valid for 15 minutes.", "ok");
        form.reset();
      } else if (r.status === 429) {
        show("Too many links requested — wait a bit and try again.", "err");
        button.disabled = false;
      } else {
        show("That email address doesn't look right.", "err");
        button.disabled = false;
      }
    } catch {
      show("Network error — try again.", "err");
      button.disabled = false;
    }
  });
})();

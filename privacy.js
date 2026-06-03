(() => {
  const KEY = "financeRebalancingPrivacyMode";

  function enabled() {
    return localStorage.getItem(KEY) === "1";
  }

  function apply(on) {
    document.body.classList.toggle("privacy-mode", on);
    localStorage.setItem(KEY, on ? "1" : "0");
    const btn = document.querySelector("[data-privacy-toggle]");
    if (btn) {
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.textContent = on ? "Privacy: on" : "Privacy: off";
      btn.title = on
        ? "Sensitive portfolio amounts are hidden"
        : "Sensitive portfolio amounts are visible";
    }
  }

  function ensureToggle() {
    if (document.querySelector("[data-privacy-toggle]")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "privacy-toggle";
    btn.dataset.privacyToggle = "true";
    btn.addEventListener("click", () => apply(!enabled()));
    document.body.appendChild(btn);
  }

  document.addEventListener("DOMContentLoaded", () => {
    ensureToggle();
    apply(enabled());
  });

  window.financePrivacy = { apply, enabled };
})();

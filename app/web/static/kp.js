// «Робот собирает КП»: шаги идут по таймеру, последний ждёт готовности на стенде; готово — штамп.
// Карточка (data-kp-swap) после штампа подменяет шапку, «Ход сделки» и блок «КП» без перезагрузки.
const forge = document.querySelector("[data-kp-poll]");
if (forge) {
  const steps = [...forge.querySelectorAll(".forge-steps li")];
  const last = steps.length - 1;
  const started = Date.now();
  const cap = last; // последний шаг ждёт, пока стенд не скажет ready
  let now = 0;
  let over = false;

  const show = (i) => {
    now = Math.max(now, Math.min(i, last));
    steps.forEach((li, k) => {
      li.classList.toggle("is-done", k < now);
      li.classList.toggle("is-now", k === now);
    });
  };
  // После «Беру в работу» взгляд и фокус — на сборку КП, а не на верх страницы
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  forge.scrollIntoView({ block: "center", behavior: still ? "auto" : "smooth" });
  forge.focus({ preventScroll: true });
  forge.classList.add("is-focused");
  forge.addEventListener("animationend", function off(e) {
    if (e.target !== forge) return;
    forge.classList.remove("is-focused");
    forge.removeEventListener("animationend", off);
  });

  const tick = setInterval(() => now < cap && show(now + 1), 2200);

  const swap = async () => {
    const html = await (await fetch(location.pathname, { credentials: "same-origin" })).text();
    const doc = new DOMParser().parseFromString(html, "text/html");
    for (const sel of [".dossier .stamp", "#decide", "#kp-panel"]) {
      const fresh = doc.querySelector(sel);
      const old = document.querySelector(sel);
      if (fresh && old) old.replaceWith(fresh);
    }
    document.getElementById("kp-panel")?.classList.add("is-arrived");
  };

  const finish = () => {
    over = true;
    clearInterval(tick);
    steps.forEach((li) => { li.classList.remove("is-now"); li.classList.add("is-done"); });
    forge.classList.add("is-done");
    forge.querySelector(".forge-title").textContent = "КП готово";
    forge.querySelector(".forge-late").hidden = true;
    const done = forge.querySelector(".forge-done-text");
    if (done) done.hidden = false;
    if ("kpSwap" in forge.dataset) setTimeout(() => swap().catch(() => location.reload()), 1400);
  };

  const poll = async () => {
    if (over) return;
    try {
      const res = await fetch(forge.dataset.kpPoll, { credentials: "same-origin", headers: { Accept: "application/json" } });
      const { state } = await res.json();
      if (state === "ready") return finish();
    } catch { /* сеть моргнула — следующий опрос */ }
    if (Date.now() - started > 180000) forge.querySelector(".forge-late").hidden = false;
    setTimeout(poll, 2000);
  };
  setTimeout(poll, 1500);
}

// «Сделки»: раз в минуту спрашивает сервер, сколько сделок разобрано после показа страницы.
// Есть новые — плашка над списком и число в заголовке вкладки; список обновляет кнопка «Показать».
const POLL_MS = 60_000;
const bar = document.getElementById("fresh");

function phrase(n) {
  const d = n % 10, h = n % 100;
  if (d === 1 && h !== 11) return ["Пришла", "новая заявка"];
  if (d >= 2 && d <= 4 && (h < 12 || h > 14)) return ["Пришли", "новые заявки"];
  return ["Пришло", "новых заявок"];
}

if (bar) {
  const since = bar.dataset.since;
  const title = document.title;
  const text = bar.querySelector(".fresh-text");
  let timer = null;

  const show = (n) => {
    if (n < 1) return;
    const [verb, noun] = phrase(n);
    text.replaceChildren(`${verb} `, Object.assign(document.createElement("b"), { textContent: n }), ` ${noun}`);
    bar.hidden = false;
    document.title = `(${n}) ${title}`;
  };

  const poll = async () => {
    clearTimeout(timer);
    try {
      const resp = await fetch(`/deals/fresh?since=${encodeURIComponent(since)}`, {
        headers: { Accept: "application/json" },
        redirect: "error", // сессия истекла — не тянем страницу входа, просто молчим
      });
      if (resp.ok) show((await resp.json()).n);
    } catch {
      // сеть или выход из системы: попробуем в следующий раз
    }
    if (!document.hidden) timer = setTimeout(poll, POLL_MS);
  };

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(timer);
    else poll();
  });
  bar.querySelector("[data-fresh-show]").addEventListener("click", () => location.reload());
  timer = setTimeout(poll, POLL_MS);
}

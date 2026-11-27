// Кнопки «Скопировать»: data-copy="текст" или data-copy-from="id поля". Отдельный файл — CSP запрещает встроенные скрипты.
document.addEventListener("click", async (event) => {
  const btn = event.target.closest("[data-copy], [data-copy-from]");
  if (!btn) return;
  const field = btn.dataset.copyFrom ? document.getElementById(btn.dataset.copyFrom) : null;
  const text = field ? field.value : btn.dataset.copy;
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch {
    if (field) {
      field.select();
      ok = document.execCommand("copy");
    }
  }
  const label = btn.dataset.label || btn.textContent;
  btn.dataset.label = label;
  btn.textContent = ok ? "Скопировано" : "Выделите и скопируйте вручную";
  btn.classList.toggle("is-copied", ok);
  clearTimeout(btn._reset);
  btn._reset = setTimeout(() => {
    btn.textContent = label;
    btn.classList.remove("is-copied");
  }, 2000);
});

// Страница с новым ключом — ответ на POST. Подменяем адрес, чтобы F5 открывал /extension, а не слал форму заново.
if (document.getElementById("ext-key") && location.pathname === "/extension/key") {
  history.replaceState(null, "", "/extension");
}

// Чекбоксы штампов на «Сделках»: отметка сразу применяет фильтр. Отдельный файл — CSP запрещает встроенные скрипты.
document.addEventListener("change", (event) => {
  const box = event.target.closest("[data-autosubmit]");
  if (box) box.form.requestSubmit();
});

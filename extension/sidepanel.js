// Боковая панель: настройка ключа, список сделок, разбор закупки из открытой вкладки, КП и решение менеджера.
import { api, ApiError, cardUrl, getKey } from "./api.js";
import { BASE } from "./config.js";
import { canPickFolder, install } from "./update.js";

const root = document.getElementById("root");
const checked = document.getElementById("checked");
const refreshBtn = document.getElementById("refresh");
const SKIP_REASONS = ["Не наш вендор", "Мало времени", "Цена не проходит", "Не устраивают условия", "Другое"];

let current = null; // id открытого разбора
let pinned = false; // выбран вручную из списка — не сбрасывать при смене вкладки

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function show(...nodes) {
  root.replaceChildren(...nodes);
}

function errorBox(message, retry) {
  return h("div", { class: "alert", role: "alert" }, h("p", {}, message), retry && h("button", { class: "btn btn-quiet", type: "button", onclick: retry }, "Повторить"));
}

// ---------- обновление плагина ----------

const updateBox = document.getElementById("update");

async function renderUpdate() {
  const { update } = await chrome.storage.local.get("update");
  updateBox.hidden = !update;
  if (!update) return;
  if (!canPickFolder()) {
    updateBox.replaceChildren(
      h("p", {}, h("b", {}, "Вышла новая версия плагина."), " Браузер не даёт плагину обновиться самому: скачайте архив, распакуйте поверх старой папки и нажмите «Обновить» на странице chrome://extensions."),
      h("a", { class: "btn btn-quiet btn-small", href: `${BASE}/extension.zip`, target: "_blank", rel: "noopener noreferrer" }, "Скачать архив"),
    );
    return;
  }
  const note = h("p", { class: "muted small" }, "В первый раз браузер попросит папку, куда вы распаковали плагин, — дальше обновления ставятся сами.");
  const btn = h(
    "button",
    {
      class: "btn btn-primary btn-small",
      type: "button",
      onclick: async () => {
        btn.disabled = true;
        note.textContent = "Обновляем…";
        try {
          if (!(await install({ interactive: true }))) note.textContent = "Без доступа к папке обновить не выйдет. Нажмите ещё раз и разрешите.";
        } catch (e) {
          note.textContent = e.name === "AbortError" ? "Папку не выбрали. Нажмите ещё раз." : e.message;
        }
        btn.disabled = false;
      },
    },
    "Обновить",
  );
  updateBox.replaceChildren(h("p", {}, h("b", {}, "Вышла новая версия плагина.")), btn, note);
}

// ---------- настройка ----------

function onboarding(message) {
  refreshBtn.hidden = true;
  checked.textContent = "";
  const input = h("input", { id: "key", type: "password", autocomplete: "off", placeholder: "zk_…", required: true });
  const status = h("p", { class: "muted", role: "status" });
  const form = h(
    "form",
    {
      class: "setup",
      onsubmit: async (e) => {
        e.preventDefault();
        const key = input.value.trim();
        status.textContent = "Проверяем ключ…";
        try {
          await api("/api/ext/inbox", { key });
          await chrome.storage.local.set({ key, error: null });
          await chrome.runtime.sendMessage("poll");
          start();
        } catch (err) {
          status.textContent = err.message;
        }
      },
    },
    h("h1", {}, "Подключите плагин"),
    message && h("div", { class: "alert" }, message),
    h(
      "ol",
      { class: "steps" },
      h("li", {}, h("b", {}, "Выпустите ключ."), " На стенде откройте страницу «Плагин» и нажмите «Выпустить ключ»."),
      h("li", {}, h("b", {}, "Вставьте его сюда."), h("div", { class: "field" }, h("label", { for: "key" }, "Ключ"), input)),
      h("li", {}, h("b", {}, "Откройте закупку на площадке."), " Разбор робота появится в этой панели, о выгодных сделках придёт уведомление."),
    ),
    h("button", { class: "btn btn-primary", type: "submit" }, "Сохранить ключ"),
    status,
  );
  show(form);
  input.focus();
}

// ---------- список ----------

function stampMark(d) {
  return h("span", { class: `mark mark-${d.verdict}`, "aria-label": d.verdict_ru }, d.n === null ? h("small", {}, "разбор") : [h("b", {}, d.n), h("small", {}, "из 6")]);
}

async function renderList() {
  pinned = false;
  current = null;
  const { inbox, error } = await chrome.storage.local.get(["inbox", "error"]);
  const nodes = [];
  if (error) nodes.push(errorBox(error, refresh));
  if (!inbox) {
    nodes.push(h("p", { class: "muted" }, "Загружаем сделки…"));
    show(...nodes);
    return;
  }
  checked.textContent = "проверено " + new Date(inbox.checked_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  nodes.push(h("h1", {}, "Идёт приём КП"));
  nodes.push(h("p", { class: "muted" }, `Уведомляем, когда выполнено ${inbox.notify_min} из 6 условий и больше. Откройте закупку на площадке — разбор появится здесь.`));
  if (!inbox.deals.length) {
    nodes.push(h("p", { class: "empty" }, "Открытых сделок сейчас нет. Робот проверяет площадки каждые несколько минут."));
  } else {
    nodes.push(
      h(
        "ol",
        { class: "list" },
        inbox.deals.map((d) =>
          h(
            "li",
            {},
            h(
              "button",
              { class: "row", type: "button", onclick: () => openDeal(d.id, true) },
              stampMark(d),
              h("span", { class: "row-main" }, h("span", { class: "row-title" }, d.subject), h("span", { class: "muted" }, `${d.amount} · `, h("span", { class: d.urgent ? "urgent" : "" }, d.left))),
              d.has_kp && h("span", { class: "tag tag-kp" }, "КП"),
            ),
          ),
        ),
      ),
    );
  }
  show(...nodes);
}

// ---------- разбор ----------

async function download(dealId, file) {
  const resp = await api(`/api/ext/deals/${dealId}/files/${file.id}`);
  const url = URL.createObjectURL(await resp.blob());
  const a = h("a", { href: url, download: file.name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function fileList(dealId, files) {
  return h(
    "ul",
    { class: "files" },
    files.map((f) =>
      h(
        "li",
        {},
        f.local
          ? h("button", { class: "link", type: "button", onclick: () => download(dealId, f).catch((e) => alert(e.message)) }, f.name)
          : h("span", {}, f.name, h("span", { class: "muted" }, " — на площадке")),
        f.size_kb !== null && h("span", { class: "muted" }, ` ${f.size_kb} КБ`),
      ),
    ),
  );
}

function decision(card) {
  const r = card.row;
  const note = h("p", { class: "muted", role: "status" }, `Сейчас: ${r.status_ru}.`);
  const send = async (action, reason = "") => {
    note.textContent = "Сохраняем…";
    try {
      await api(`/api/ext/deals/${r.id}/status`, { method: "POST", body: { action, reason } });
      await chrome.runtime.sendMessage("poll");
      await openDeal(r.id, pinned, "Решение сохранено и видно на стенде.");
    } catch (e) {
      note.textContent = e.message;
    }
  };
  const select = h("select", { id: "reason", required: true }, h("option", { value: "" }, "Выберите причину"), SKIP_REASONS.map((x) => h("option", { value: x }, x)));
  const other = h("input", { id: "reason-other", maxlength: 450, placeholder: "Своими словами", hidden: true });
  select.addEventListener("change", () => (other.hidden = select.value !== "Другое"));
  return h(
    "section",
    { class: "sheet decide" },
    h("h2", {}, "Ваше решение"),
    note,
    h("button", { class: "btn btn-primary", type: "button", onclick: () => send("taken") }, "Беру в работу"),
    h("button", { class: "btn btn-quiet", type: "button", onclick: () => send("submitted") }, "КП подано"),
    h(
      "details",
      {},
      h("summary", {}, "Не беру"),
      h(
        "form",
        {
          class: "skip",
          onsubmit: (e) => {
            e.preventDefault();
            const reason = select.value === "Другое" ? other.value.trim() || "Другое" : select.value;
            send("skipped", reason);
          },
        },
        h("label", { for: "reason" }, "Почему"),
        select,
        other,
        h("button", { class: "btn btn-quiet", type: "submit" }, "Отметить «не беру»"),
      ),
    ),
    h("p", { class: "muted small" }, "Робот заявку не подаёт и КП заказчику не отправляет."),
  );
}

function renderCard(card, flash) {
  const r = card.row;
  current = r.id;
  checked.textContent = "";
  const catalog = card.catalog_total
    ? card.catalog_total
    : card.conditions.length
      ? h("span", { class: "empty-price" }, "цены нет — ставит менеджер")
      : "не сравнивали";
  show(
    h("button", { class: "link back", type: "button", onclick: renderList }, "Все сделки"),
    flash && h("p", { class: "saved", role: "status" }, flash),
    h("div", { class: `stamp stamp-${r.verdict}` }, h("span", { class: "stamp-main" }, r.verdict_ru), h("span", { class: "stamp-date" }, r.n === null ? r.status_ru : `${r.n} из 6 условий`)),
    h("h1", {}, r.subject),
    h("p", { class: "muted" }, r.customer),
    h(
      "dl",
      { class: "facts" },
      h("dt", {}, "Цена закупки"),
      h("dd", {}, card.nmck),
      h("dt", {}, "По каталогу"),
      h("dd", {}, catalog),
      h("dt", {}, "Приём КП до"),
      h("dd", {}, `${r.end_msk} МСК `, h("span", { class: r.urgent ? "urgent" : "muted" }, r.left)),
    ),
    (card.why.length || card.check) &&
      h("section", {}, h("h2", {}, "Почему так"), h("ul", { class: "why" }, card.why.map((w) => h("li", {}, w))), card.check && h("p", { class: "check" }, h("b", {}, "Проверить: "), card.check)),
    decision(card),
    h(
      "section",
      { class: "sheet" },
      h("h2", {}, "Коммерческое предложение"),
      card.kp_files.length
        ? [fileList(r.id, card.kp_files), h("p", { class: "muted small" }, "Цены по каталогу на сегодня, подтверждает менеджер.")]
        : [h("p", {}, "КП не собрано."), card.kp_blockers.length && h("ul", { class: "why" }, card.kp_blockers.map((b) => h("li", {}, b)))],
    ),
    card.conditions.length &&
      h(
        "section",
        {},
        h("h2", {}, "Пять условий"),
        h("ul", { class: "conditions" }, card.conditions.map((c) => h("li", { class: c.met ? "met" : "unmet" }, h("span", { class: "cond" }, c.met ? "да" : "нет"), h("span", {}, h("b", {}, c.title), " ", h("span", { class: "muted" }, c.reason))))),
      ),
    card.positions.length &&
      h(
        "section",
        {},
        h("h2", {}, "Позиции и каталог"),
        h(
          "ul",
          { class: "positions" },
          card.positions.map((p) =>
            h(
              "li",
              {},
              h("b", {}, p.name),
              h("span", { class: "muted" }, ` ${p.qty}`),
              h("div", { class: "pos" }, h("span", { class: `tag pos-${p.match_kind}` }, p.match), p.sku && h("span", { class: "muted" }, p.sku), p.sku && (p.price ? h("b", {}, p.price) : h("span", { class: "empty-price" }, "нет цены"))),
            ),
          ),
        ),
      ),
    (card.findings.length || card.missing.length) && h("details", {}, h("summary", {}, `Риски и чего не хватает: ${card.findings.length + card.missing.length}`), h("ul", { class: "why" }, [...card.findings, ...card.missing].map((x) => h("li", {}, x)))),
    h("section", { class: "sheet" }, h("h2", {}, "Документы закупки"), card.docs.length ? fileList(r.id, card.docs) : h("p", { class: "muted" }, "Площадка не дала вложений без входа.")),
    h(
      "p",
      { class: "links" },
      h("a", { href: card.url, target: "_blank", rel: "noopener noreferrer" }, "Закупка на площадке"),
      h("a", { href: cardUrl(r.id), target: "_blank", rel: "noopener noreferrer" }, "Карточка на стенде"),
    ),
  );
}

async function openDeal(id, byHand = false, flash = "") {
  pinned = byHand;
  try {
    renderCard(await (await api(`/api/ext/deals/${id}`)).json(), flash);
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return onboarding(e.message);
    show(errorBox(e.message, () => openDeal(id, byHand)));
  }
}

// ---------- открытая вкладка ----------

async function followTab() {
  if (pinned) return;
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab?.url?.startsWith("http")) return current === null ? renderList() : undefined;
  try {
    const { deal_id } = await (await api(`/api/ext/lookup?url=${encodeURIComponent(tab.url)}`)).json();
    if (deal_id !== null) {
      if (deal_id !== current) await openDeal(deal_id);
    } else if (current !== null || !root.childElementCount) {
      await renderList();
    }
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) onboarding(e.message);
  }
}

async function refresh() {
  await chrome.runtime.sendMessage("poll");
  if (current === null) await renderList();
  else await openDeal(current, pinned);
}

async function start() {
  if (!(await getKey())) return onboarding();
  refreshBtn.hidden = false;
  const { focus } = await chrome.storage.local.get("focus");
  if (focus) {
    await chrome.storage.local.remove("focus");
    return openDeal(focus, true);
  }
  await renderList();
  await followTab();
}

refreshBtn.addEventListener("click", refresh);
chrome.tabs.onActivated.addListener(followTab);
chrome.tabs.onUpdated.addListener((_id, info, tab) => tab.active && info.url && followTab());
chrome.storage.onChanged.addListener((changes) => {
  if (changes.update) renderUpdate();
  if (changes.inbox && current === null) renderList();
  if (changes.focus?.newValue) start();
});
renderUpdate();
start();

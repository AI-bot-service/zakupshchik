// Боковая панель: настройка ключа, список сделок, разбор закупки из открытой вкладки, КП и решение менеджера.
import { api, ApiError, cardUrl, getKey } from "./api.js";
import { BASE } from "./config.js";
import { canPickFolder, install } from "./update.js";

const root = document.getElementById("root");
const checked = document.getElementById("checked");
const refreshBtn = document.getElementById("refresh");
const foot = document.getElementById("foot");

// Нижняя строка всегда на виду: версия плагина и его сборки, версия агента разбора и правил на стенде.
function renderFoot(inbox) {
  const plugin = `Плагин ${chrome.runtime.getManifest().version}` + (inbox?.build ? `, сборка ${String(inbox.build).slice(0, 8)}` : "");
  const agent = inbox?.agent ? `агент ${inbox.agent}` : "агент —";
  const rules = inbox?.rules ? `правила ${inbox.rules}` : "";
  foot.textContent = [plugin, agent, rules].filter(Boolean).join(" · ");
}
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
  renderFoot(inbox);
  checked.textContent = "проверено " + new Date(inbox.checked_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  nodes.push(h("h1", {}, "Жду новых предложений на площадках"));
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

// Карточка в панели повторяет карточку стенда: досье → ход сделки → условия → почему так → позиции → риски → документы → КП → журналы.

const QUIET_STEPS = new Set(["Отсеяна", "Приём закрыт"]);
let kpWait = null; // таймер ожидания КП после «Беру в работу»

function panel(title, ...body) {
  return h("section", { class: "panel" }, title && h("h2", {}, title), ...body);
}

function fold(title, ...body) {
  return h("details", { class: "panel fold" }, h("summary", {}, h("h2", {}, title)), ...body);
}

function dossier(card) {
  const r = card.row;
  const catalog = card.catalog_total
    ? card.catalog_total
    : card.conditions.length
      ? h("span", { class: "empty-price" }, "цены нет — ставит менеджер")
      : "не сравнивали";
  return h(
    "header",
    { class: "dossier" },
    h("div", { class: `stamp stamp-${r.stamp}`, role: "status" }, h("span", { class: "stamp-main" }, r.stamp_ru), r.stamp_sub && h("span", { class: "stamp-date" }, r.stamp_sub)),
    h("h1", {}, r.subject),
    h("p", { class: "strong" }, r.customer),
    h("p", { class: "muted small" }, `${r.platform}, ${r.procedure}, размещена ${r.published_msk}`),
    h(
      "dl",
      { class: "facts" },
      h("dt", {}, "Максимальная цена"),
      h("dd", { class: "money" }, card.nmck),
      h("dt", {}, "По каталогу"),
      h("dd", {}, catalog),
      h("dt", {}, "Приём КП до"),
      h("dd", {}, `${r.end_msk} МСК `, h("span", { class: r.urgent ? "urgent" : "muted" }, r.left)),
    ),
  );
}

function track(card) {
  return h(
    "ol",
    { class: "track" },
    card.track.map((s) =>
      h(
        "li",
        { class: `track-${s.state}${QUIET_STEPS.has(s.name) ? " is-quiet" : ""}`, "aria-current": s.state === "now" ? "step" : null },
        h("span", { class: "track-dot", "aria-hidden": "true" }),
        h("b", {}, s.name),
        h("span", { class: "track-when" }, s.when || (s.state === "now" ? "сейчас" : "")),
      ),
    ),
  );
}

function flow(card) {
  const r = card.row;
  const st = r.status;
  const note = h("p", { class: "hint", role: "status" });
  const send = async (action, reason = "") => {
    note.textContent = "Сохраняем…";
    try {
      await api(`/api/ext/deals/${r.id}/status`, { method: "POST", body: { action, reason } });
      await chrome.runtime.sendMessage("poll");
      await openDeal(r.id, pinned);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) return box.replaceChildren(needKp());
      note.textContent = e.message;
    }
  };

  const needKp = () =>
    h(
      "div",
      { class: "need-kp", role: "alert" },
      h("b", {}, "КП в карточке нет"),
      h("p", {}, "«КП отправлено» ставится, когда КП лежит в карточке — так видно, что именно ушло заказчику."),
      st === "taken"
        ? h("p", {}, "Робот уже собирает КП — оно появится в блоке «Коммерческое предложение». Или приложите своё в карточке на стенде.")
        : [h("button", { class: "btn btn-primary", type: "button", onclick: () => send("taken") }, "Сформировать КП"), h("p", {}, "Или приложите своё КП в карточке на стенде.")],
      h("a", { href: cardUrl(r.id), target: "_blank", rel: "noopener noreferrer" }, "Карточка на стенде"),
      h("button", { class: "link", type: "button", onclick: () => openDeal(r.id, pinned) }, "Отмена"),
    );

  let body;
  if (card.actions.length || card.can_skip) {
    const sent =
      st === "submitted" &&
      card.kp &&
      h(
        "div",
        { class: "kp-sent" },
        h("span", {}, `Заказчику ушло КП v${card.kp.version}`, card.kp_files.length ? ": " : ""),
        card.kp_files.length > 0 && fileList(r.id, card.kp_files.slice(0, 1)),
        h("span", { class: "muted small" }, `${card.kp.by_manager ? "своё КП менеджера" : "собрано роботом"}, отмечено ${r.stamp_sub}`),
      );
    const actions = card.actions.map((a) =>
      h("div", { class: "flow-action" }, h("button", { class: `btn ${a.primary ? "btn-primary" : "btn-quiet"}`, type: "button", onclick: () => send(a.to) }, a.label), a.hint && h("p", { class: "hint" }, a.hint)),
    );
    let skip = null;
    if (card.can_skip) {
      const select = h("select", { id: "reason", required: true }, h("option", { value: "" }, "Выберите причину"), SKIP_REASONS.map((x) => h("option", { value: x, selected: x === card.skip_reason }, x)));
      const custom = card.skip_reason && !SKIP_REASONS.includes(card.skip_reason);
      if (custom) select.value = "Другое";
      const other = h("input", { id: "reason-other", maxlength: 450, placeholder: "Например: нет партнёрства с вендором", hidden: !custom, value: custom ? card.skip_reason : null });
      select.addEventListener("change", () => (other.hidden = select.value !== "Другое"));
      skip = h(
        "details",
        { class: "skip-box" },
        h("summary", { class: "btn btn-quiet" }, st === "skipped" ? "Изменить причину отказа" : "Не беру"),
        h(
          "form",
          {
            class: "skip",
            onsubmit: (e) => {
              e.preventDefault();
              send("skipped", select.value === "Другое" ? other.value.trim() || "Другое" : select.value);
            },
          },
          h("label", { for: "reason" }, "Причина отказа"),
          select,
          other,
          h("button", { class: "btn btn-primary", type: "submit" }, "Сохранить"),
          h("p", { class: "hint" }, "Причина попадёт в журнал сделки — по причинам уточняют правила отбора."),
        ),
      );
    }
    body = [sent, h("div", { class: "flow-actions" }, actions, skip), note];
  } else {
    body = h(
      "p",
      { class: "flow-note" },
      st === "filtered_out"
        ? "Робот отсеял сделку по правилам, решение не нужно. Не согласны — поправьте правила в «Админке» на стенде."
        : st === "new"
          ? "Робот ещё разбирает документы. Кнопки появятся после разбора."
          : "Приём КП закрыт — менять статус больше не нужно.",
    );
  }
  const box = h("div", { class: "flow-body" }, body);
  return h(
    "section",
    { class: "panel flow" },
    h("div", { class: "panel-head" }, h("h2", {}, "Ход сделки"), h("p", { class: "muted small" }, "Робот заявку не подаёт и КП заказчику не отправляет — это делаете вы.")),
    track(card),
    box,
  );
}

function conditions(card) {
  const met = card.conditions.filter((c) => c.met).length;
  const terms = h(
    "dl",
    { class: "terms" },
    h("div", {}, h("dt", {}, "Договор"), h("dd", {}, card.contract)),
    card.min_kp_days && h("div", {}, h("dt", {}, "КП должно действовать"), h("dd", {}, `не меньше ${card.min_kp_days} дн.`)),
    !card.conditions.length && h("div", {}, h("dt", {}, "Оплата"), h("dd", {}, card.payment)),
  );
  return h(
    "section",
    { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", {}, "Условия закупки"), card.conditions.length > 0 && h("p", { class: "muted small" }, `Выполнено ${met} из 6`)),
    card.conditions.length
      ? h(
          "ol",
          { class: "conds" },
          card.conditions.map((c) =>
            h(
              "li",
              { class: c.met ? "is-met" : "is-unmet" },
              h("span", { class: "cond-mark", "aria-hidden": "true" }, c.met ? "✓" : "✗"),
              h("b", {}, c.short, h("span", { class: "visually-hidden" }, `: ${c.met ? "выполнено" : "не выполнено"}`)),
              h("span", { class: "cond-text" }, c.text),
            ),
          ),
        )
      : h("p", { class: "muted" }, "Условия посчитаются после разбора документов роботом."),
    terms,
  );
}

function why(card) {
  const r = card.row;
  const items = [];
  if (r.reason && r.status === "filtered_out") items.push(r.reason);
  if (card.not_license_reason) items.push(`Это не поставка лицензий: ${card.not_license_reason}`);
  items.push(...card.why);
  if (!items.length && !card.check) return null;
  return panel(
    "Почему так",
    h("ul", { class: "why" }, items.map((w) => h("li", {}, w))),
    card.check && h("p", { class: "check" }, h("b", {}, "Проверить: "), card.check),
    card.why_stale && h("p", { class: "hint" }, "Правила изменились, итог пересчитан. Разбор моделью обновится в ближайшем цикле."),
  );
}

function positions(card) {
  if (!card.positions.length) return null;
  return panel(
    "Что покупают и что есть в каталоге",
    h(
      "ol",
      { class: "positions" },
      card.positions.map((p) =>
        h(
          "li",
          {},
          h("div", { class: "pos-head" }, h("b", {}, p.name), h("span", { class: "muted" }, p.qty)),
          h(
            "div",
            { class: "pos" },
            h("span", { class: `tag pos-${p.match_kind}` }, p.match),
            p.sku && (p.catalog_url ? h("a", { href: p.catalog_url, target: "_blank", rel: "noopener noreferrer" }, p.catalog_name || p.sku) : h("span", {}, p.catalog_name || "")),
            p.sku && h("span", { class: "muted" }, `артикул ${p.sku}`),
            p.sku && (p.price ? h("b", { class: "price" }, p.price) : h("span", { class: "empty-price" }, "нет цены")),
          ),
          h("p", { class: "muted small" }, `${p.reason} Уверенность ${p.confidence}%.`),
          p.quote && h("details", {}, h("summary", {}, "Как сказано в закупке"), h("blockquote", {}, p.quote)),
        ),
      ),
    ),
  );
}

function risks(card) {
  const items = [...card.findings, ...card.missing];
  return items.length ? panel("Риски и чего не хватает", h("ul", { class: "why" }, items.map((x) => h("li", {}, x)))) : null;
}

function docs(card) {
  return panel(
    "Документы закупки",
    card.docs.length ? fileList(card.row.id, card.docs) : [h("p", { class: "empty-word" }, "Пусто"), h("p", { class: "hint" }, "Площадка не дала вложений без входа.")],
    h("a", { class: "btn btn-quiet", href: card.url, target: "_blank", rel: "noopener noreferrer" }, "Открыть на площадке"),
  );
}

function kp(card) {
  const r = card.row;
  let body;
  if (card.kp_files.length) {
    body = [
      fileList(r.id, card.kp_files),
      h(
        "p",
        { class: "hint" },
        card.kp_by_manager
          ? `Своё КП менеджера, приложено ${card.kp.made_msk}, версия v${card.kp.version}. Робот его не пересобирает. `
          : `Собрано роботом ${card.kp.made_msk}, версия v${card.kp.version}. Цены по каталогу на этот день, подтверждает менеджер. Подпись и печать ставятся вручную. `,
        card.kp.sent ? h("b", {}, "Отправлено заказчику.") : "Заказчику пока не отправлено.",
      ),
    ];
  } else if (r.status === "taken") {
    body = h(
      "div",
      { class: "forge", role: "status" },
      h("span", { class: "forge-sheet", "aria-hidden": "true" }, h("i"), h("i"), h("i")),
      h("p", {}, h("b", {}, "Робот собирает КП"), h("br"), h("span", { class: "muted small" }, "Позиции и цены каталога, DOCX и PDF. Блок обновится сам.")),
    );
  } else {
    const takeFirst = card.actions[0]?.to === "taken";
    body = [
      h("p", { class: "empty-word" }, "Пусто"),
      h("p", { class: "hint" }, takeFirst ? "КП появится здесь после «Беру в работу»." : "Робот собирает КП, когда сделку берут в работу."),
      card.kp_blockers.length > 0 && h("ul", { class: "why" }, card.kp_blockers.map((b) => h("li", {}, b))),
    ];
  }
  return panel("Коммерческое предложение", body);
}

function priority(card) {
  if (!card.priority.length) return null;
  return fold(
    `Как посчитан приоритет${card.score !== null ? `: ${card.score.toFixed(2)}` : ""}`,
    h(
      "ul",
      { class: "bars" },
      card.priority.map(([name, value, weight]) =>
        h("li", {}, h("span", {}, name, h("span", { class: "muted" }, ` вес ${weight}`)), h("progress", { class: "bar", max: 1, value }, `${Math.round(value * 100)}%`), h("span", {}, value.toFixed(2))),
      ),
    ),
    card.rules_version && h("p", { class: "hint" }, `Версия правил ${card.rules_version}, правятся в «Админке» на стенде.`),
  );
}

function history(card) {
  return fold(
    `Журнал сделки: ${card.history_ru || "пусто"}`,
    card.history.length
      ? [
          h(
            "ol",
            { class: "status-log" },
            card.history.slice(0, 8).map((e) =>
              h("li", { class: e.manager ? "is-manager" : "" }, h("span", { class: "muted" }, `${e.at_msk} · ${e.who}`), h("span", {}, h("b", {}, e.move), e.reason ? `: ${e.reason}` : ""), e.facts && h("span", { class: "muted small" }, e.facts)),
            ),
          ),
          card.history.length > 8 && h("p", { class: "muted small" }, "Весь журнал — в карточке на стенде."),
        ]
      : h("p", { class: "muted" }, "Статус ещё не менялся."),
  );
}

function renderCard(card) {
  const r = card.row;
  current = r.id;
  checked.textContent = "";
  clearTimeout(kpWait);
  if (r.status === "taken" && !card.kp_files.length) {
    kpWait = setTimeout(() => current === r.id && openDeal(r.id, pinned), 5000);
  }
  show(
    h("button", { class: "link back", type: "button", onclick: renderList }, "Все сделки"),
    dossier(card),
    flow(card),
    conditions(card),
    why(card),
    positions(card),
    risks(card),
    docs(card),
    kp(card),
    priority(card),
    history(card),
    h("p", { class: "links" }, h("a", { href: cardUrl(r.id), target: "_blank", rel: "noopener noreferrer" }, "Карточка на стенде"), card.steps.length > 0 && h("span", { class: "muted small" }, `Журнал робота: ${card.steps_ru}, ${card.agent_cost}`)),
  );
}

async function openDeal(id, byHand = false) {
  pinned = byHand;
  try {
    renderCard(await (await api(`/api/ext/deals/${id}`)).json());
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
chrome.storage.local.get("inbox").then(({ inbox }) => renderFoot(inbox));
start();

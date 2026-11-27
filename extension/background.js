// Раз в 30 секунд (минимум chrome.alarms) спрашивает стенд о сделках, показывает уведомления о новых выгодных и число на иконке.
import { api, ApiError } from "./api.js";
import { onStandBuild } from "./update.js";

const STAMP = "#4338ca";
const ALARM = "poll";

async function setup() {
  await chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  await poll();
}

chrome.runtime.onInstalled.addListener(setup);
chrome.runtime.onStartup.addListener(setup);
chrome.alarms.onAlarm.addListener((a) => a.name === ALARM && poll());

async function badge(text, color = STAMP) {
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color });
}

async function poll() {
  let inbox;
  try {
    inbox = await (await api("/api/ext/inbox")).json();
  } catch (e) {
    const auth = e instanceof ApiError && e.status === 401;
    await chrome.storage.local.set({ error: e.message, errorAt: new Date().toISOString() });
    await badge(auth ? "ключ" : "!", "#b42318");
    return;
  }
  const { seen = [] } = await chrome.storage.local.get("seen");
  const known = new Set(seen);
  const hot = inbox.deals.filter((d) => d.notify);
  for (const d of hot) {
    if (known.has(d.id)) continue;
    known.add(d.id);
    chrome.notifications.create(`deal-${d.id}`, {
      type: "basic",
      iconUrl: "icons/128.png",
      title: `${d.n} из 6 · ${d.amount} · ${d.verdict_ru}`,
      message: d.subject,
      contextMessage: `${d.platform}, ${d.left}`,
      priority: 2,
      requireInteraction: true,
    });
  }
  await chrome.storage.local.set({ inbox, seen: [...known].slice(-500), error: null });
  await badge(hot.length ? String(hot.length) : "");
  await onStandBuild(inbox.build);
}

chrome.notifications.onClicked.addListener(async (notificationId) => {
  chrome.notifications.clear(notificationId);
  if (notificationId === "update") {
    const win = await chrome.windows.getLastFocused();
    return chrome.sidePanel.open({ windowId: win.id }).catch(() => {});
  }
  const id = Number(notificationId.replace("deal-", ""));
  const { inbox } = await chrome.storage.local.get("inbox");
  const deal = inbox?.deals.find((d) => d.id === id);
  await chrome.storage.local.set({ focus: id });
  if (!deal) return;
  const tab = await chrome.tabs.create({ url: deal.url });
  try {
    await chrome.sidePanel.open({ windowId: tab.windowId });
  } catch {
    // Chrome не дал открыть панель без клика по иконке — разбор уже выбран, откроется по клику.
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg === "poll") poll().then(() => reply(true));
  return true;
});

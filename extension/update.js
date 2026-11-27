// Самообновление распакованного плагина. Chrome такие сам не обновляет, поэтому плагин сверяет свою сборку
// со стендом, перезаписывает файлы в своей папке (доступ к ней менеджер даёт один раз) и перезапускается.
import { api } from "./api.js";
import { BUILD } from "./config.js";

const DB = "zakupshchik";
const STORE = "handles";
const DIR_KEY = "extension-dir";
// Только пути вида icons/128.png: без "..", абсолютных путей и скрытых файлов.
const SAFE_PATH = /^([\w-]+\/)*[\w-]+(\.[\w-]+)+$/;
// Служебные файлы пишутся последними: если запись оборвётся, старая сборка останется рабочей и обновление повторится.
const LAST = ["manifest.json", "config.js"];

export const canPickFolder = () => typeof self.showDirectoryPicker === "function";

function idb(mode, fn) {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open(DB, 1);
    open.onupgradeneeded = () => open.result.createObjectStore(STORE);
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
      const req = fn(open.result.transaction(STORE, mode).objectStore(STORE));
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    };
  });
}

const savedFolder = () => idb("readonly", (s) => s.get(DIR_KEY));

async function isOurFolder(dir) {
  try {
    const file = await (await dir.getFileHandle("manifest.json")).getFile();
    return JSON.parse(await file.text()).name === chrome.runtime.getManifest().name;
  } catch {
    return false;
  }
}

async function folder(interactive) {
  let dir = await savedFolder();
  if (!dir) {
    if (!interactive || !canPickFolder()) return null;
    dir = await self.showDirectoryPicker({ id: "zakupshchik", mode: "readwrite" });
    if (!(await isOurFolder(dir))) throw new Error("В этой папке нет плагина «Закупщик». Выберите папку, куда распаковали архив.");
    await idb("readwrite", (s) => s.put(dir, DIR_KEY));
  }
  const opts = { mode: "readwrite" };
  let perm = await dir.queryPermission(opts);
  if (perm !== "granted" && interactive) perm = await dir.requestPermission(opts);
  return perm === "granted" ? dir : null;
}

async function writeFile(dir, rel, base64) {
  const parts = rel.split("/");
  for (const seg of parts.slice(0, -1)) dir = await dir.getDirectoryHandle(seg, { create: true });
  const out = await (await dir.getFileHandle(parts.at(-1), { create: true })).createWritable();
  await out.write(Uint8Array.from(atob(base64), (c) => c.charCodeAt(0)));
  await out.close();
}

// Ставит сборку со стенда. false — нет доступа к папке (нужен клик менеджера), иначе плагин перезапускается.
export async function install({ interactive }) {
  const dir = await folder(interactive);
  if (!dir) return false;
  const { files } = await (await api("/api/ext/package")).json();
  const paths = Object.keys(files).filter((p) => !LAST.includes(p));
  for (const rel of [...paths, ...LAST]) {
    if (!(rel in files)) continue;
    if (!SAFE_PATH.test(rel)) throw new Error(`Стенд прислал недопустимый путь: ${rel}`);
    await writeFile(dir, rel, files[rel]);
  }
  chrome.runtime.reload();
  return true;
}

// Вызывается фоном после каждого опроса: новая сборка → тихая установка, без доступа — уведомление и кнопка в панели.
export async function onStandBuild(build) {
  if (!build || build === BUILD) {
    await chrome.storage.local.remove("update");
    return;
  }
  const { update } = await chrome.storage.local.get("update");
  if (update?.build === build) return; // эту сборку уже пробовали, ждём клика в панели
  await chrome.storage.local.set({ update: { build } });
  try {
    if (await install({ interactive: false })) return;
  } catch (e) {
    console.warn("Тихое обновление не вышло:", e);
  }
  chrome.notifications.create("update", {
    type: "basic",
    iconUrl: "icons/128.png",
    title: "Вышла новая версия плагина",
    message: "Нажмите на значок «Закупщик» и кнопку «Обновить» — займёт пару секунд.",
    priority: 1,
  });
}

import { BASE } from "./config.js";

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function getKey() {
  const { key } = await chrome.storage.local.get("key");
  return key || "";
}

export async function api(path, { method = "GET", body, key } = {}) {
  const token = key ?? (await getKey());
  if (!token) throw new ApiError(401, "Нет ключа. Вставьте ключ со страницы стенда «Плагин».");
  const headers = { Authorization: `Bearer ${token}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let resp;
  try {
    resp = await fetch(BASE + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "Стенд не ответил. Проверьте интернет, повторим через минуту.");
  }
  if (!resp.ok) {
    let message = `Стенд ответил ошибкой ${resp.status}.`;
    try {
      const data = await resp.json();
      if (typeof data.detail === "string") message = data.detail;
    } catch {}
    throw new ApiError(resp.status, message);
  }
  return resp;
}

export const cardUrl = (id) => `${BASE}/deals/${id}`;

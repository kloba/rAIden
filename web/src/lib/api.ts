// A small fetch client: JSON in and out, the CSRF header on every change, errors as ApiError with the server's message.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-Requested-With"] = "raiden";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api/v1${path}`, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : Array.isArray(data.detail) ? data.detail.map((d: any) => d.msg).join("; ") : detail;
    } catch {
      /* not JSON */
    }
    if (res.status === 401 && onUnauthorized && !path.startsWith("/auth/")) onUnauthorized();
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body ?? {}),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body ?? {}),
  del: <T>(path: string) => request<T>("DELETE", path),
};

export interface StreamEvent {
  event: string;
  data: any;
}

/** POST that answers with Server-Sent Events (chat). Calls onEvent for each event until the stream ends. */
export async function postStream(path: string, body: unknown, onEvent: (e: StreamEvent) => void, signal?: AbortSignal): Promise<void> {
  const res = await fetch(`/api/v1${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "raiden", Accept: "text/event-stream" },
    credentials: "same-origin",
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = "message";
      let data = "";
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) {
        try {
          onEvent({ event, data: JSON.parse(data) });
        } catch {
          onEvent({ event, data });
        }
      }
    }
  }
}

const BASE = "/api/v1";
const DEFAULT_USER_EMAIL = "dev@storybook.local";

export interface AuthenticatedUser {
  email: string;
  auth_type: string;
}

export function getDevUserEmail(): string {
  try {
    return localStorage.getItem("storybook_user_email") || DEFAULT_USER_EMAIL;
  } catch {
    return DEFAULT_USER_EMAIL;
  }
}

function buildHeaders(extra?: Record<string, string>): Record<string, string> {
  return {
    "X-Dev-User-Email": getDevUserEmail(),
    ...extra,
  };
}

export async function getCurrentUser(): Promise<AuthenticatedUser> {
  try {
    const res = await fetch(`${BASE}/me`, {
      credentials: "include",
      headers: buildHeaders(),
    });
    if (res.ok) {
      return res.json();
    }
  } catch {
    // Fall through to default dev identity
  }
  return { email: getDevUserEmail(), auth_type: "dev" };
}

export interface SourceConfig {
  gutenberg_url?: string;
  title?: string;
  author?: string;
}

export interface SessionConfig {
  source: SourceConfig;
  target_age?: string;
  page_count?: number;
  language?: string;
  text_spec?: string;
  image_spec?: string;
  custom_instructions?: string;
}

export interface SessionSummary {
  session_id: string;
  user_email?: string;
  current_stage: string;
  progress_pct: number;
  config?: SessionConfig;
  pdf_signed_url?: string;
  wide_pdf_url?: string;
  trace_url?: string;
  errors: string[];
  resumable?: boolean;
  started_at?: string;
  finished_at?: string;
  adapted_from_source?: boolean;
}

export interface ProgressEvent {
  seq?: number;
  ts?: string;
  stage: string;
  pct?: number;
  message?: string;
  spread?: number;
  page?: number;
  of?: number;
  signed_url?: string;
  session_id?: string;
  attempt?: number;
  reason?: string;
  adapted_from_source?: boolean;
}

export interface LuckyConfig {
  title: string;
  author: string;
  target_age: string;
  page_count: number;
  text_spec: string;
  image_spec: string;
  custom_instructions: string;
}

export interface ListSessionsParams {
  status?: string;
  limit?: number;
  offset?: number;
  sort?: string;
}

export async function getLuckyConfig(): Promise<LuckyConfig> {
  const res = await fetch(`${BASE}/lucky`, {
    credentials: "include",
    headers: buildHeaders(),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export type ShuffleField =
  | "title_author"
  | "text_spec"
  | "image_spec"
  | "custom_instructions"
  | "page_count";

export interface ShuffleResponse {
  title?: string;
  author?: string;
  text_spec?: string;
  image_spec?: string;
  custom_instructions?: string;
  page_count?: number;
}

export interface ShuffleRequest {
  field: ShuffleField;
  title?: string;
  author?: string;
  target_age?: string;
  page_count?: number;
  text_spec?: string;
  image_spec?: string;
  custom_instructions?: string;
}

export async function shuffleField(req: ShuffleRequest): Promise<ShuffleResponse> {
  const res = await fetch(`${BASE}/shuffle`, {
    method: "POST",
    credentials: "include",
    headers: buildHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(req),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function createSession(config: SessionConfig): Promise<SessionSummary> {
  const res = await fetch(`${BASE}/sessions`, {
    method: "POST",
    credentials: "include",
    headers: buildHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ config }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getSession(id: string): Promise<SessionSummary> {
  const res = await fetch(`${BASE}/sessions/${id}`, {
    credentials: "include",
    headers: buildHeaders(),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function cancelSession(id: string): Promise<SessionSummary> {
  const res = await fetch(`${BASE}/sessions/${id}/cancel`, {
    method: "POST",
    credentials: "include",
    headers: buildHeaders(),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function resumeSession(id: string): Promise<SessionSummary> {
  const res = await fetch(`${BASE}/sessions/${id}/resume`, {
    method: "POST",
    credentials: "include",
    headers: buildHeaders(),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function listSessions(params?: ListSessionsParams): Promise<SessionSummary[]> {
  const url = new URL(`${BASE}/sessions`, window.location.origin);
  if (params?.status) url.searchParams.set("status", params.status);
  if (params?.limit !== undefined) url.searchParams.set("limit", String(params.limit));
  if (params?.offset !== undefined) url.searchParams.set("offset", String(params.offset));
  if (params?.sort) url.searchParams.set("sort", params.sort);
  const res = await fetch(url.pathname + url.search, {
    credentials: "include",
    headers: buildHeaders(),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export function streamSession(
  id: string,
  onEvent: (e: ProgressEvent) => void,
  onDone: () => void
): () => void {
  let es: EventSource | null = null;
  let reconnectTimer: number | null = null;
  let closed = false;
  let lastSeq = -1;

  function cleanup() {
    closed = true;
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (es) {
      es.close();
      es = null;
    }
  }

  function scheduleReconnect(delayMs = 1000) {
    if (closed || reconnectTimer !== null) return;
    if (es) {
      es.close();
      es = null;
    }
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      if (!closed) connect();
    }, delayMs);
  }

  function connect() {
    if (closed) return;
    const source = new EventSource(`${BASE}/sessions/${id}/stream?last_seq=${lastSeq}`, {
      withCredentials: true,
    });
    es = source;

    source.onmessage = (msg) => {
      if (closed) return;
      const data: ProgressEvent = JSON.parse(msg.data);
      if (typeof data.seq === "number") {
        if (data.seq <= lastSeq) return;
        lastSeq = data.seq;
      }
      onEvent(data);
      if (data.stage === "done" || data.stage === "error") {
        cleanup();
        onDone();
      }
    };

    source.onerror = async () => {
      if (closed) return;
      try {
        const session = await getSession(id);
        if (closed) return;
        if (session.current_stage === "done" || session.current_stage === "error") {
          onEvent({
            stage: session.current_stage,
            pct: session.progress_pct,
            message:
              session.current_stage === "error"
                ? session.errors?.[0] || "Pipeline failed"
                : undefined,
            adapted_from_source: session.adapted_from_source,
          });
          cleanup();
          onDone();
          return;
        }
      } catch {
        // Transient network error while checking session status — keep reconnecting
      }
      scheduleReconnect(1000);
    };
  }

  connect();
  return cleanup;
}


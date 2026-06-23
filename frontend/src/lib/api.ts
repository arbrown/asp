const BASE = "/api/v1";

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
}

export interface ProgressEvent {
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
  const res = await fetch(`${BASE}/lucky`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export type ShuffleField = "title_author" | "text_spec" | "image_spec" | "custom_instructions";

export interface ShuffleResponse {
  title?: string;
  author?: string;
  text_spec?: string;
  image_spec?: string;
  custom_instructions?: string;
}

export interface ShuffleRequest {
  field: ShuffleField;
  title?: string;
  author?: string;
  target_age?: string;
  text_spec?: string;
  image_spec?: string;
  custom_instructions?: string;
}

export async function shuffleField(req: ShuffleRequest): Promise<ShuffleResponse> {
  const res = await fetch(`${BASE}/shuffle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function createSession(config: SessionConfig): Promise<SessionSummary> {
  const res = await fetch(`${BASE}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getSession(id: string): Promise<SessionSummary> {
  const res = await fetch(`${BASE}/sessions/${id}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function resumeSession(id: string): Promise<SessionSummary> {
  const res = await fetch(`${BASE}/sessions/${id}/resume`, { method: "POST" });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function listSessions(params?: ListSessionsParams): Promise<SessionSummary[]> {
  const url = new URL(`${BASE}/sessions`, window.location.origin);
  if (params?.status) url.searchParams.set("status", params.status);
  if (params?.limit !== undefined) url.searchParams.set("limit", String(params.limit));
  if (params?.offset !== undefined) url.searchParams.set("offset", String(params.offset));
  if (params?.sort) url.searchParams.set("sort", params.sort);
  const res = await fetch(url.pathname + url.search);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export function streamSession(
  id: string,
  onEvent: (e: ProgressEvent) => void,
  onDone: () => void
): () => void {
  const es = new EventSource(`${BASE}/sessions/${id}/stream`);
  es.onmessage = (msg) => {
    const data: ProgressEvent = JSON.parse(msg.data);
    onEvent(data);
    if (data.stage === "done" || data.stage === "error") {
      es.close();
      onDone();
    }
  };
  es.onerror = () => {
    es.close();
    onDone();
  };
  return () => es.close();
}

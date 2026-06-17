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
  errors: string[];
  resumable?: boolean;
}

export interface ProgressEvent {
  stage: string;
  pct: number;
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

export async function getLuckyConfig(): Promise<LuckyConfig> {
  const res = await fetch(`${BASE}/lucky`);
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

export async function listSessions(): Promise<SessionSummary[]> {
  const res = await fetch(`${BASE}/sessions`);
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

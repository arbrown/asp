import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { streamSession, type ProgressEvent } from "../lib/api";

const STAGE_LABELS: Record<string, string> = {
  fetching: "Fetching source text",
  adapting_text: "Adapting story for children",
  splitting_pages: "Splitting into pages",
  building_character_bible: "Building character bible",
  generating_image: "Generating illustrations",
  image_retry: "Retrying illustration",
  composing_pdf: "Composing PDF",
  done: "Complete",
  error: "Error",
};

export default function ProgressPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [events, setEvents] = useState<ProgressEvent[]>([]);
  const [pct, setPct] = useState(0);
  const [stage, setStage] = useState("initializing");
  const [done, setDone] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!id) return;
    const unsub = streamSession(
      id,
      (e) => {
        setEvents((prev) => [...prev, e]);
        setPct(e.pct);
        setStage(e.stage);
        if (e.stage === "done" && e.signed_url) {
          setDone(true);
          setTimeout(() => navigate(`/session/${id}`), 1500);
        }
      },
      () => setDone(true)
    );
    return unsub;
  }, [id, navigate]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

  return (
    <div className="max-w-2xl mx-auto">
      <h1 className="text-2xl font-serif font-bold text-sepia-900 mb-2">Creating Your Storybook</h1>
      <p className="text-sm text-sepia-600 font-mono mb-6">{id}</p>

      {/* Progress bar */}
      <div className="mb-6">
        <div className="flex justify-between text-sm text-sepia-600 mb-1">
          <span>{STAGE_LABELS[stage] ?? stage.replace(/_/g, " ")}</span>
          <span>{pct}%</span>
        </div>
        <div className="h-2 bg-sepia-100 rounded-full overflow-hidden">
          <div
            className="h-full bg-sepia-600 rounded-full transition-all duration-700"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>

      {/* Event log */}
      <div className="border border-sepia-200 rounded-xl bg-white overflow-hidden">
        <div className="px-4 py-2 border-b border-sepia-100 bg-sepia-100">
          <span className="text-xs font-mono text-sepia-600">Pipeline log</span>
        </div>
        <div className="p-4 space-y-1.5 max-h-96 overflow-y-auto font-mono text-xs">
          {events.map((e, i) => (
            <EventLine key={i} event={e} />
          ))}
          {!done && (
            <div className="flex items-center gap-1.5 text-sepia-600">
              <span className="animate-pulse">●</span>
              <span>Processing…</span>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {done && stage === "done" && (
        <p className="mt-6 text-center text-sepia-600 text-sm">
          Done! Redirecting to viewer…
        </p>
      )}
    </div>
  );
}

function EventLine({ event: e }: { event: ProgressEvent }) {
  const isError = e.stage === "error";
  const isRetry = e.stage === "image_retry";
  const cls = isError
    ? "text-red-600"
    : isRetry
      ? "text-amber-600"
      : "text-sepia-700";

  let text = `[${e.pct}%] ${STAGE_LABELS[e.stage] ?? e.stage}`;
  if (e.page) text += ` — page ${e.page}${e.of ? `/${e.of}` : ""}`;
  if (e.attempt) text += ` (attempt ${e.attempt})`;
  if (e.message) text += `: ${e.message}`;
  if (e.reason) text += ` — ${e.reason}`;

  return <div className={cls}>{text}</div>;
}

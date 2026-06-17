import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { streamSession, type ProgressEvent } from "../lib/api";

const BASE = "/api/v1";
const MIN_DISPLAY_MS = 5000;

const STAGE_LABELS: Record<string, string> = {
  fetching: "Fetching source text",
  adapting_text: "Adapting story for children",
  building_character_bible: "Building character bible",
  planning_spreads: "Planning spread layouts",
  generating_image: "Generating illustrations",
  image_retry: "Retrying illustration",
  composing_pdf: "Composing PDF",
  resuming: "Resuming from previous run",
  done: "Complete",
  error: "Error",
};

// Crossfade state lives in a ref so SSE callbacks always read current values.
type CF = { slotA: string | null; slotB: string | null; front: "a" | "b"; shownSince: number };

export default function ProgressPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [events, setEvents] = useState<ProgressEvent[]>([]);
  const [pct, setPct] = useState(0);
  const [stage, setStage] = useState("initializing");
  const [done, setDone] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Crossfade image preview
  const cfRef = useRef<CF>({ slotA: null, slotB: null, front: "a", shownSince: 0 });
  const [cfSnap, setCfSnap] = useState<CF>(cfRef.current);

  function updateCf(patch: Partial<CF>) {
    cfRef.current = { ...cfRef.current, ...patch };
    setCfSnap({ ...cfRef.current });
  }

  function tryShowImage(spreadNumber: number) {
    const url = `${BASE}/sessions/${id}/spreads/${spreadNumber}/image/0`;
    const { slotA, slotB, front, shownSince } = cfRef.current;
    const hasImage = slotA !== null || slotB !== null;
    const elapsed = Date.now() - shownSince;

    if (!hasImage) {
      // First image — preload then show immediately
      preload(url, () => updateCf({ slotA: url, front: "a", shownSince: Date.now() }));
      return;
    }

    if (elapsed < MIN_DISPLAY_MS) return; // current image too fresh — skip

    // Load into the back slot then crossfade
    const back = front === "a" ? "b" : "a";
    preload(url, () => {
      updateCf(back === "b" ? { slotB: url } : { slotA: url });
      // Tiny rAF delay lets React paint the new slot before opacity flips
      requestAnimationFrame(() =>
        requestAnimationFrame(() =>
          updateCf({ front: back, shownSince: Date.now() })
        )
      );
    });
  }

  function preload(url: string, onReady: () => void) {
    const img = new window.Image();
    img.onload = onReady;
    img.onerror = onReady; // show even if something goes wrong
    img.src = url;
  }

  useEffect(() => {
    if (!id) return;
    const unsub = streamSession(
      id,
      (e) => {
        setEvents((prev) => [...prev, e]);
        setPct(e.pct);
        setStage(e.stage);
        if (e.stage === "generating_image" && (e.message === "done" || e.message === "cached") && e.spread != null) {
          tryShowImage(e.spread);
        }
        if (e.stage === "done") {
          setDone(true);
          setTimeout(() => navigate(`/session/${id}`), 1500);
        }
      },
      () => setDone(true)
    );
    return unsub;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, navigate]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

  const { slotA, slotB, front } = cfSnap;
  const hasImage = slotA !== null || slotB !== null;

  return (
    <div className="max-w-6xl mx-auto">
      <h1 className="text-2xl font-serif font-bold text-sepia-900 mb-2">Creating Your Storybook</h1>
      <p className="text-sm text-sepia-600 font-mono mb-6">{id}</p>

      <div className="flex gap-8 items-start">
        {/* Left: progress + log */}
        <div className="flex-1 min-w-0">
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

          <div className="border border-sepia-200 rounded-xl bg-white overflow-hidden">
            <div className="px-4 py-2 border-b border-sepia-100 bg-sepia-100">
              <span className="text-xs font-mono text-sepia-600">Pipeline log</span>
            </div>
            <div className="p-4 space-y-1.5 max-h-[32rem] overflow-y-auto font-mono text-xs">
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

        {/* Right: image preview */}
        <div className="w-80 shrink-0 sticky top-8">
          <div className="relative rounded-2xl overflow-hidden bg-sepia-50 border border-sepia-200 shadow-sm" style={{ aspectRatio: "17/11" }}>
            {!hasImage && (
              <div className="absolute inset-0 flex flex-col items-center justify-center text-sepia-400 gap-3">
                <svg className="w-12 h-12 opacity-30" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                    d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14M8 10h.01M4 6h16a2 2 0 012 2v8a2 2 0 01-2 2H4a2 2 0 01-2-2V8a2 2 0 012-2z" />
                </svg>
                <span className="text-xs font-mono">Illustrations will appear here</span>
              </div>
            )}
            {/* Slot A */}
            {slotA && (
              <img
                src={slotA}
                alt="Generated illustration"
                className="absolute inset-0 w-full h-full object-cover"
                style={{
                  opacity: front === "a" ? 1 : 0,
                  transition: "opacity 700ms ease-in-out",
                }}
              />
            )}
            {/* Slot B */}
            {slotB && (
              <img
                src={slotB}
                alt="Generated illustration"
                className="absolute inset-0 w-full h-full object-cover"
                style={{
                  opacity: front === "b" ? 1 : 0,
                  transition: "opacity 700ms ease-in-out",
                }}
              />
            )}
          </div>
          {hasImage && (
            <p className="mt-2 text-center text-xs text-sepia-500 font-mono">
              Latest illustration
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function EventLine({ event: e }: { event: ProgressEvent }) {
  const isError = e.stage === "error";
  const isRetry = e.stage === "image_retry";
  const cls = isError ? "text-red-600" : isRetry ? "text-amber-600" : "text-sepia-700";

  let text = `[${e.pct}%] ${STAGE_LABELS[e.stage] ?? e.stage}`;
  if (e.spread != null) text += ` — spread ${e.spread}${e.of ? `/${e.of}` : ""}`;
  else if (e.page) text += ` — page ${e.page}${e.of ? `/${e.of}` : ""}`;
  if (e.attempt) text += ` (attempt ${e.attempt})`;
  if (e.message && e.message !== "done" && e.message !== "cached") text += `: ${e.message}`;
  if (e.reason) text += ` — ${e.reason}`;

  return <div className={cls}>{text}</div>;
}

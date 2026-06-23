import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { listSessions, resumeSession, type SessionSummary } from "../lib/api";

type FilterMode = "done" | "error" | "all";

const FILTERS: { label: string; mode: FilterMode }[] = [
  { label: "Completed", mode: "done" },
  { label: "Failed", mode: "error" },
  { label: "All", mode: "all" },
];

function formatTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const diff = Date.now() - d.getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function duration(started?: string, finished?: string): string {
  if (!started || !finished) return "";
  const ms = new Date(finished).getTime() - new Date(started).getTime();
  if (isNaN(ms) || ms < 0) return "";
  const mins = Math.floor(ms / 60_000);
  const secs = Math.floor((ms % 60_000) / 1000);
  return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
}

export default function HistoryPage() {
  const navigate = useNavigate();
  const [filter, setFilter] = useState<FilterMode>("done");

  const { data: sessions = [], isLoading } = useQuery({
    queryKey: ["sessions", filter],
    queryFn: () =>
      listSessions({
        status: filter === "all" ? undefined : filter,
        sort: "created_at_desc",
      }),
    refetchInterval: 5000,
  });

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-3xl font-serif font-bold text-sepia-900">Your Storybooks</h1>
        <button
          onClick={() => navigate("/new")}
          className="bg-sepia-900 text-parchment px-5 py-2.5 rounded-lg hover:bg-sepia-600 transition-colors"
        >
          New Storybook
        </button>
      </div>

      {/* Filter tabs */}
      <div className="flex gap-1 mb-6 bg-sepia-50 border border-sepia-200 rounded-lg p-1 w-fit">
        {FILTERS.map(({ label, mode }) => (
          <button
            key={mode}
            onClick={() => setFilter(mode)}
            className={`px-4 py-1.5 rounded-md text-sm font-medium transition-colors ${
              filter === mode
                ? "bg-white text-sepia-900 shadow-sm border border-sepia-200"
                : "text-sepia-600 hover:text-sepia-900"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {isLoading && <p className="text-sepia-600">Loading…</p>}

      {!isLoading && sessions.length === 0 && (
        <div className="text-center py-20 text-sepia-600">
          <p className="text-xl mb-4">
            {filter === "done"
              ? "No completed storybooks yet."
              : filter === "error"
              ? "No failed runs."
              : "No storybooks yet."}
          </p>
          <button onClick={() => navigate("/new")} className="underline hover:text-sepia-900">
            Create your first one →
          </button>
        </div>
      )}

      <div className="grid gap-4">
        {sessions.map((s) => (
          <SessionCard key={s.session_id} session={s} />
        ))}
      </div>
    </div>
  );
}

function SessionCard({ session }: { session: SessionSummary }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [thumbError, setThumbError] = useState(false);

  const isDone = session.current_stage === "done";
  const isError = session.current_stage === "error";
  const inProgress = !isDone && !isError;

  const title = session.config?.source?.title;
  const author = session.config?.source?.author;
  const thumbUrl = `/api/v1/sessions/${session.session_id}/images/1`;
  const dur = duration(session.started_at, session.finished_at);

  const resume = useMutation({
    mutationFn: () => resumeSession(session.session_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sessions"] });
      navigate(`/session/${session.session_id}/progress`);
    },
  });

  function handleClick() {
    if (isDone) navigate(`/session/${session.session_id}`);
    else if (!isError) navigate(`/session/${session.session_id}/progress`);
  }

  return (
    <div
      onClick={handleClick}
      className={`border border-sepia-200 rounded-xl p-4 transition-all bg-white ${
        !isError ? "cursor-pointer hover:border-sepia-600 hover:shadow-sm" : ""
      }`}
    >
      <div className="flex gap-4">
        {/* Thumbnail */}
        <div className="w-20 h-20 shrink-0 rounded-lg overflow-hidden bg-sepia-50 border border-sepia-100">
          {!thumbError ? (
            <img
              src={thumbUrl}
              alt=""
              className="w-full h-full object-cover"
              onError={() => setThumbError(true)}
            />
          ) : (
            <div className="w-full h-full flex items-center justify-center">
              <svg
                className="w-7 h-7 text-sepia-200"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={1.5}
                  d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253"
                />
              </svg>
            </div>
          )}
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-start justify-between gap-3 mb-1">
            <div className="min-w-0">
              {title ? (
                <p className="font-serif font-semibold text-sepia-900 truncate">{title}</p>
              ) : (
                <p className="font-serif text-sepia-400 italic">Untitled</p>
              )}
              {author && <p className="text-sm text-sepia-600 truncate">{author}</p>}
              <p className="text-xs font-mono text-sepia-400 mt-0.5">
                {session.session_id.slice(0, 8)}…
              </p>
            </div>
            <StatusBadge stage={session.current_stage} />
          </div>

          {/* Timing row */}
          {(session.started_at || dur) && (
            <div className="flex items-center gap-3 mt-1 text-xs text-sepia-400">
              {session.started_at && <span>{formatTime(session.started_at)}</span>}
              {dur && <span className="text-sepia-300">·</span>}
              {dur && <span>{dur}</span>}
            </div>
          )}

          {/* In-progress bar */}
          {inProgress && (
            <div className="mt-2">
              <div className="h-1.5 bg-sepia-100 rounded-full overflow-hidden">
                <div
                  className="h-full bg-sepia-600 rounded-full transition-all duration-500"
                  style={{ width: `${session.progress_pct}%` }}
                />
              </div>
              <p className="text-xs text-sepia-600 mt-1">
                {session.current_stage.replace(/_/g, " ")} — {session.progress_pct}%
              </p>
            </div>
          )}

          {/* Done: quick-access links */}
          {isDone && (
            <div className="flex items-center gap-3 mt-2">
              {session.pdf_signed_url && (
                <a
                  href={session.pdf_signed_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={(e) => e.stopPropagation()}
                  className="text-xs text-sepia-600 hover:text-sepia-900 underline underline-offset-2"
                >
                  PDF
                </a>
              )}
              {session.wide_pdf_url && (
                <a
                  href={session.wide_pdf_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={(e) => e.stopPropagation()}
                  className="text-xs text-sepia-600 hover:text-sepia-900 underline underline-offset-2"
                >
                  Wide PDF
                </a>
              )}
              {session.trace_url && (
                <a
                  href={session.trace_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={(e) => e.stopPropagation()}
                  className="text-xs text-sepia-400 hover:text-sepia-600 underline underline-offset-2"
                >
                  Trace
                </a>
              )}
            </div>
          )}

          {/* Error row */}
          {isError && (
            <div className="mt-2 flex items-start justify-between gap-4">
              {(session.errors?.length ?? 0) > 0 && (
                <p className="text-sm text-red-600 flex-1 truncate">{session.errors[0]}</p>
              )}
              {session.resumable && (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    resume.mutate();
                  }}
                  disabled={resume.isPending}
                  className="shrink-0 text-sm px-3 py-1.5 bg-sepia-900 text-parchment rounded-lg hover:bg-sepia-600 disabled:opacity-50 transition-colors"
                >
                  {resume.isPending ? "Resuming…" : "Resume"}
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function StatusBadge({ stage }: { stage: string }) {
  const cls =
    stage === "done"
      ? "bg-green-100 text-green-800"
      : stage === "error"
      ? "bg-red-100 text-red-800"
      : "bg-sepia-100 text-sepia-800";
  return (
    <span className={`shrink-0 text-xs px-2 py-0.5 rounded-full font-medium ${cls}`}>
      {stage.replace(/_/g, " ")}
    </span>
  );
}

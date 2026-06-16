import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { listSessions, resumeSession, type SessionSummary } from "../lib/api";

export default function HistoryPage() {
  const navigate = useNavigate();
  const { data: sessions = [], isLoading } = useQuery({
    queryKey: ["sessions"],
    queryFn: listSessions,
    refetchInterval: 5000,
  });

  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <h1 className="text-3xl font-serif font-bold text-sepia-900">Your Storybooks</h1>
        <button
          onClick={() => navigate("/new")}
          className="bg-sepia-900 text-parchment px-5 py-2.5 rounded-lg hover:bg-sepia-600 transition-colors"
        >
          New Storybook
        </button>
      </div>

      {isLoading && <p className="text-sepia-600">Loading…</p>}

      {!isLoading && sessions.length === 0 && (
        <div className="text-center py-20 text-sepia-600">
          <p className="text-xl mb-4">No storybooks yet.</p>
          <button
            onClick={() => navigate("/new")}
            className="underline hover:text-sepia-900"
          >
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
      className={`border border-sepia-200 rounded-xl p-4 transition-all bg-white ${!isError ? "cursor-pointer hover:border-sepia-600 hover:shadow-sm" : ""}`}
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
              <svg className="w-7 h-7 text-sepia-200" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                  d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
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
              {author && (
                <p className="text-sm text-sepia-600 truncate">{author}</p>
              )}
              <p className="text-xs font-mono text-sepia-400 mt-0.5">{session.session_id.slice(0, 8)}…</p>
            </div>
            <StatusBadge stage={session.current_stage} />
          </div>

          {inProgress && (
            <div className="mt-2">
              <div className="h-1.5 bg-sepia-100 rounded-full overflow-hidden">
                <div
                  className="h-full bg-sepia-600 rounded-full transition-all duration-500"
                  style={{ width: `${session.progress_pct}%` }}
                />
              </div>
              <p className="text-xs text-sepia-600 mt-1">{session.current_stage.replace(/_/g, " ")} — {session.progress_pct}%</p>
            </div>
          )}

          {isError && (
            <div className="mt-2 flex items-start justify-between gap-4">
              {session.errors.length > 0 && (
                <p className="text-sm text-red-600 flex-1 truncate">{session.errors[0]}</p>
              )}
              {session.resumable && (
                <button
                  onClick={(e) => { e.stopPropagation(); resume.mutate(); }}
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

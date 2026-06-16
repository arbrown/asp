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
  const isDone = session.current_stage === "done";
  const isError = session.current_stage === "error";
  const inProgress = !isDone && !isError;

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
      className={`border border-sepia-200 rounded-xl p-5 transition-all bg-white ${!isError ? "cursor-pointer hover:border-sepia-600 hover:shadow-sm" : ""}`}
    >
      <div className="flex items-center justify-between">
        <span className="font-mono text-xs text-sepia-600">{session.session_id.slice(0, 8)}…</span>
        <StatusBadge stage={session.current_stage} />
      </div>
      {inProgress && (
        <div className="mt-3">
          <div className="h-1.5 bg-sepia-100 rounded-full overflow-hidden">
            <div
              className="h-full bg-sepia-600 rounded-full transition-all duration-500"
              style={{ width: `${session.progress_pct}%` }}
            />
          </div>
          <p className="text-xs text-sepia-600 mt-1">{session.current_stage.replace(/_/g, " ")}</p>
        </div>
      )}
      {isError && (
        <div className="mt-3 flex items-start justify-between gap-4">
          {session.errors.length > 0 && (
            <p className="text-sm text-red-600 flex-1">{session.errors[0]}</p>
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
    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${cls}`}>
      {stage.replace(/_/g, " ")}
    </span>
  );
}

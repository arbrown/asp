import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { getSession } from "../lib/api";

export default function ViewerPage() {
  const { id } = useParams<{ id: string }>();
  const { data: session, isLoading } = useQuery({
    queryKey: ["session", id],
    queryFn: () => getSession(id!),
    enabled: !!id,
  });

  if (isLoading) return <p className="text-sepia-600">Loading…</p>;
  if (!session) return <p className="text-red-600">Session not found.</p>;

  const isReady = session.current_stage === "done" && session.pdf_signed_url;

  return (
    <div className="max-w-2xl mx-auto">
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-serif font-bold text-sepia-900">Your Storybook</h1>
          <p className="text-xs font-mono text-sepia-600 mt-0.5">{id}</p>
        </div>
        {isReady && (
          <a
            href={session.pdf_signed_url}
            download
            className="bg-sepia-900 text-parchment px-5 py-2.5 rounded-lg hover:bg-sepia-600 transition-colors text-sm"
          >
            Download PDF
          </a>
        )}
      </div>

      {isReady ? (
        <div className="border border-sepia-200 rounded-xl overflow-hidden shadow-sm">
          <iframe
            src={session.pdf_signed_url}
            className="w-full h-[80vh]"
            title="Storybook PDF"
          />
        </div>
      ) : (
        <div className="text-center py-20 text-sepia-600">
          <p>Status: {session.current_stage.replace(/_/g, " ")} ({session.progress_pct}%)</p>
          {session.errors.length > 0 && (
            <p className="mt-4 text-red-600">{session.errors[0]}</p>
          )}
        </div>
      )}
    </div>
  );
}

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { getSession } from "../lib/api";

const BASE = "/api/v1";

export default function ViewerPage() {
  const { id } = useParams<{ id: string }>();
  const [coverError, setCoverError] = useState(false);
  const { data: session, isLoading } = useQuery({
    queryKey: ["session", id],
    queryFn: () => getSession(id!),
    enabled: !!id,
  });

  if (isLoading) return <p className="text-sepia-600">Loading…</p>;
  if (!session) return <p className="text-red-600">Session not found.</p>;

  const isReady = session.current_stage === "done" && session.pdf_signed_url;
  const traceUrl = session.trace_url;
  const title = session.config?.source?.title || "Your Storybook";
  const author = session.config?.source?.author;
  const coverUrl = `${BASE}/sessions/${id}/spreads/0/image/0`;

  return (
    <div className="max-w-2xl mx-auto">
      <div className="flex items-start justify-between mb-8 gap-4">
        <div>
          <h1 className="text-2xl font-serif font-bold text-sepia-900">{title}</h1>
          {author && (
            <p className="text-sm text-sepia-600 mt-0.5">Adapted from {author}</p>
          )}
          <p className="text-xs font-mono text-sepia-500 mt-1">{id}</p>
          {traceUrl && (
            <a
              href={traceUrl}
              target="_blank"
              rel="noreferrer"
              className="text-xs text-sepia-400 hover:text-sepia-600 underline mt-0.5 inline-block"
            >
              View trace
            </a>
          )}
        </div>
        {isReady && (
          <div className="flex gap-2 shrink-0">
            <a
              href={session.wide_pdf_url ?? session.pdf_signed_url ?? ""}
              download
              className="bg-sepia-900 text-parchment px-4 py-2.5 rounded-lg hover:bg-sepia-600 transition-colors text-sm"
            >
              Wide PDF
            </a>
            <a
              href={session.pdf_signed_url ?? ""}
              download
              className="bg-sepia-700 text-parchment px-4 py-2.5 rounded-lg hover:bg-sepia-500 transition-colors text-sm"
            >
              Print PDF
            </a>
          </div>
        )}
      </div>

      {isReady ? (
        <>
          {!coverError && (
            <div className="mb-6 rounded-2xl overflow-hidden shadow-md border border-sepia-200 max-w-sm mx-auto">
              <img
                src={coverUrl}
                alt={`Cover illustration for ${title}`}
                className="w-full object-cover"
                onError={() => setCoverError(true)}
              />
            </div>
          )}
          <div className="border border-sepia-200 rounded-xl overflow-hidden shadow-sm">
            <iframe
              src={session.pdf_signed_url}
              className="w-full h-[80vh]"
              title="Storybook PDF"
            />
          </div>
        </>
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

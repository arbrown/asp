import { useQuery } from "@tanstack/react-query";
import { Route, Routes } from "react-router-dom";
import { getCurrentUser, getDevUserEmail } from "./lib/api";
import ConfigPage from "./pages/ConfigPage";
import HistoryPage from "./pages/HistoryPage";
import ProgressPage from "./pages/ProgressPage";
import ViewerPage from "./pages/ViewerPage";

export default function App() {
  const { data: user } = useQuery({
    queryKey: ["currentUser"],
    queryFn: getCurrentUser,
    staleTime: 60_000,
  });

  const email = user?.email || getDevUserEmail();
  const initial = email.charAt(0).toUpperCase();

  return (
    <div className="min-h-screen bg-parchment">
      <header className="border-b border-sepia-200 px-8 py-4 flex items-center justify-between">
        <a href="/" className="text-2xl font-serif font-bold text-sepia-900 hover:text-sepia-600">
          Storybook Agent
        </a>

        <div
          className="inline-flex items-center gap-2.5 px-3.5 py-1.5 rounded-full bg-white border border-sepia-200 shadow-sm text-sm text-sepia-900"
          title={`Signed in as ${email}`}
        >
          <span className="w-6 h-6 rounded-full bg-sepia-900 text-parchment flex items-center justify-center text-xs font-semibold">
            {initial}
          </span>
          <span className="font-mono text-xs text-sepia-700">{email}</span>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-6 py-10">
        <Routes>
          <Route path="/" element={<HistoryPage />} />
          <Route path="/new" element={<ConfigPage />} />
          <Route path="/session/:id/progress" element={<ProgressPage />} />
          <Route path="/session/:id" element={<ViewerPage />} />
        </Routes>
      </main>
    </div>
  );
}

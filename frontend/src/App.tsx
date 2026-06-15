import { Route, Routes } from "react-router-dom";
import ConfigPage from "./pages/ConfigPage";
import HistoryPage from "./pages/HistoryPage";
import ProgressPage from "./pages/ProgressPage";
import ViewerPage from "./pages/ViewerPage";

export default function App() {
  return (
    <div className="min-h-screen bg-parchment">
      <header className="border-b border-sepia-200 px-8 py-4">
        <a href="/" className="text-2xl font-serif font-bold text-sepia-900 hover:text-sepia-600">
          Storybook Agent
        </a>
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

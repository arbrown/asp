import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { createSession, getLuckyConfig, listSessions, type SessionConfig, type SessionSummary } from "../lib/api";

const AGE_OPTIONS = [
  { value: "4-5", label: "Ages 4–5 (Pre-K)" },
  { value: "6-8", label: "Ages 6–8 (Grade 1–2)" },
  { value: "9-12", label: "Ages 9–12 (Grade 3–5)" },
];

function sessionLabel(s: SessionSummary): string {
  const cfg = s.config;
  const title = cfg?.source?.title || cfg?.source?.author || "Untitled";
  const age = cfg?.target_age ?? "?";
  const pages = cfg?.page_count ?? "?";
  return `${title} — ${age}, ${pages} pages (${s.current_stage})`;
}

export default function ConfigPage() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [lucky, setLucky] = useState(false);
  const [error, setError] = useState("");
  const [pastSessions, setPastSessions] = useState<SessionSummary[]>([]);

  useEffect(() => {
    listSessions().then(setPastSessions).catch(() => {});
  }, []);

  const [form, setForm] = useState<SessionConfig>({
    source: { gutenberg_url: "", title: "", author: "" },
    target_age: "4-5",
    page_count: 12,
    language: "en",
    text_spec: "",
    image_spec: "",
    custom_instructions: "",
  });

  function set<K extends keyof SessionConfig>(key: K, value: SessionConfig[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      // Clean up empty optional fields
      const config: SessionConfig = {
        ...form,
        source: {
          gutenberg_url: form.source.gutenberg_url || undefined,
          title: form.source.title || undefined,
          author: form.source.author || undefined,
        },
        text_spec: form.text_spec || undefined,
        image_spec: form.image_spec || undefined,
        custom_instructions: form.custom_instructions || undefined,
      };
      const session = await createSession(config);
      navigate(`/session/${session.session_id}/progress`);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }

  async function handleLucky() {
    setLucky(true);
    setError("");
    try {
      const cfg = await getLuckyConfig();
      setForm({
        source: { gutenberg_url: "", title: cfg.title, author: cfg.author },
        target_age: cfg.target_age,
        page_count: cfg.page_count,
        language: "en",
        text_spec: cfg.text_spec,
        image_spec: cfg.image_spec,
        custom_instructions: cfg.custom_instructions,
      });
    } catch (err) {
      setError(`Lucky generation failed: ${err}`);
    } finally {
      setLucky(false);
    }
  }

  function fillExample() {
    setForm({
      source: { gutenberg_url: "", title: "Eugene Onegin", author: "Alexander Pushkin" },
      target_age: "4-5",
      page_count: 12,
      language: "en",
      text_spec: "Write in Onegin stanzas. Each stanza is 14 lines of iambic tetrameter with rhyme scheme ABABCCDDEFFEGG. Each page is one complete stanza.",
      image_spec: "Pen and ink with fine cross-hatching. No color fills. High contrast black and white. 19th century book illustration style.",
      custom_instructions: "Focus on the Tatiana storyline. Emphasize wonder and nature. Keep the melancholy of the original but make it gentle.",
    });
  }

  return (
    <div className="max-w-2xl mx-auto">
      <div className="flex items-center justify-between mb-8">
        <h1 className="text-3xl font-serif font-bold text-sepia-900">New Storybook</h1>
        <div className="flex items-center gap-3">
          <button type="button" onClick={fillExample}
            className="text-sm text-sepia-500 hover:text-sepia-900 underline underline-offset-2 transition-colors">
            Fill example
          </button>
          <button
            type="button"
            onClick={handleLucky}
            disabled={lucky || loading}
            className="flex items-center gap-2 px-4 py-2 rounded-lg font-medium text-sm text-white disabled:opacity-60 transition-all shadow-sm hover:shadow-md active:scale-95"
            style={{ background: "linear-gradient(135deg, #7c3aed 0%, #4f46e5 50%, #0ea5e9 100%)" }}
          >
            {lucky ? (
              <>
                <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" d="M12 3a9 9 0 1 0 9 9" />
                </svg>
                Generating…
              </>
            ) : (
              <>
                <GemIcon />
                I'm Feeling Lucky
              </>
            )}
          </button>
        </div>
      </div>

      {pastSessions.length > 0 && (
        <div className="mb-6">
          <label className="block text-sm font-medium text-sepia-900 mb-1">Load from previous run</label>
          <select
            className={inputCls}
            defaultValue=""
            onChange={(e) => {
              const s = pastSessions.find((x) => x.session_id === e.target.value);
              if (s?.config) setForm({
                source: { gutenberg_url: "", title: "", author: "", ...s.config.source },
                target_age: s.config.target_age ?? "4-5",
                page_count: s.config.page_count ?? 12,
                language: s.config.language ?? "en",
                text_spec: s.config.text_spec ?? "",
                image_spec: s.config.image_spec ?? "",
                custom_instructions: s.config.custom_instructions ?? "",
              });
            }}
          >
            <option value="" disabled>Choose a past run…</option>
            {pastSessions.map((s) => (
              <option key={s.session_id} value={s.session_id}>{sessionLabel(s)}</option>
            ))}
          </select>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-8">

        {/* Source */}
        <section>
          <h2 className="text-lg font-semibold text-sepia-900 mb-3">Source Work</h2>
          <p className="text-sm text-sepia-600 mb-4">
            Provide a Gutenberg URL, or a title and author — the agent will search automatically.
          </p>
          <div className="space-y-3">
            <Field label="Gutenberg URL (optional)">
              <input
                type="url"
                placeholder="https://www.gutenberg.org/ebooks/NNNN"
                value={form.source.gutenberg_url}
                onChange={(e) => set("source", { ...form.source, gutenberg_url: e.target.value })}
                className={inputCls}
              />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Title">
                <input
                  type="text"
                  placeholder="Eugene Onegin"
                  value={form.source.title}
                  onChange={(e) => set("source", { ...form.source, title: e.target.value })}
                  className={inputCls}
                />
              </Field>
              <Field label="Author">
                <input
                  type="text"
                  placeholder="Alexander Pushkin"
                  value={form.source.author}
                  onChange={(e) => set("source", { ...form.source, author: e.target.value })}
                  className={inputCls}
                />
              </Field>
            </div>
          </div>
        </section>

        {/* Age + pages */}
        <section>
          <h2 className="text-lg font-semibold text-sepia-900 mb-3">Target Audience</h2>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Age group">
              <select
                value={form.target_age}
                onChange={(e) => set("target_age", e.target.value)}
                className={inputCls}
              >
                {AGE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </Field>
            <Field label="Page count">
              <input
                type="number"
                min={6}
                max={64}
                value={form.page_count}
                onChange={(e) => set("page_count", Number(e.target.value))}
                className={inputCls}
              />
            </Field>
          </div>
        </section>

        {/* Text spec */}
        <section>
          <h2 className="text-lg font-semibold text-sepia-900 mb-1">Text Specification</h2>
          <p className="text-sm text-sepia-600 mb-3">
            Optional. Describe any literary form or constraint. The validator will enforce it.
          </p>
          <textarea
            rows={4}
            placeholder={`Example:\nWrite in Onegin stanzas. Each stanza is 14 lines of iambic tetrameter with rhyme scheme ABABCCDDEFFEGG. Each page is one complete stanza.`}
            value={form.text_spec}
            onChange={(e) => set("text_spec", e.target.value)}
            className={`${inputCls} font-mono text-sm`}
          />
        </section>

        {/* Image spec */}
        <section>
          <h2 className="text-lg font-semibold text-sepia-900 mb-1">Illustration Style</h2>
          <p className="text-sm text-sepia-600 mb-3">
            Optional. Describe the visual style. The image validator will enforce consistency.
          </p>
          <textarea
            rows={3}
            placeholder="Example: Pen and ink with fine cross-hatching. No color fills. High contrast black and white. 19th century book illustration style."
            value={form.image_spec}
            onChange={(e) => set("image_spec", e.target.value)}
            className={`${inputCls} font-mono text-sm`}
          />
        </section>

        {/* Custom instructions */}
        <section>
          <h2 className="text-lg font-semibold text-sepia-900 mb-1">Custom Instructions</h2>
          <p className="text-sm text-sepia-600 mb-3">
            Optional. Story focus, themes to emphasize, characters to highlight, etc.
          </p>
          <textarea
            rows={3}
            placeholder="Example: Focus on the Tatiana storyline. Emphasize wonder and nature. Keep the melancholy of the original but make it gentle."
            value={form.custom_instructions}
            onChange={(e) => set("custom_instructions", e.target.value)}
            className={`${inputCls} text-sm`}
          />
        </section>

        {error && <p className="text-red-600 text-sm">{error}</p>}

        <div className="flex gap-3">
          <button
            type="button"
            onClick={() => navigate("/")}
            className="px-5 py-2.5 border border-sepia-200 rounded-lg hover:border-sepia-600 transition-colors"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={loading}
            className="flex-1 bg-sepia-900 text-parchment px-5 py-2.5 rounded-lg hover:bg-sepia-600 transition-colors disabled:opacity-50"
          >
            {loading ? "Starting…" : "Create Storybook"}
          </button>
        </div>
      </form>
    </div>
  );
}

function GemIcon() {
  return (
    <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      {/* Top facets */}
      <path d="M8.5 3h7l3.5 4.5H5L8.5 3z" fillOpacity="0.85" />
      {/* Bottom body */}
      <path d="M5 7.5l7 13.5 7-13.5H5z" fillOpacity="0.95" />
      {/* Inner highlight */}
      <path d="M10 7.5l2 10 2-10h-4z" fill="white" fillOpacity="0.25" />
    </svg>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-sm font-medium text-sepia-900 mb-1">{label}</label>
      {children}
    </div>
  );
}

const inputCls =
  "w-full border border-sepia-200 rounded-lg px-3 py-2 bg-white focus:outline-none focus:border-sepia-600 transition-colors";

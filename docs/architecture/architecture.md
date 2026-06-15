# Storybook Agent — Architecture

A multi-agent pipeline that adapts public-domain literature into illustrated children's storybooks, deployed on Google Cloud.

---

## System Overview

All agents run inside a **single ADK process** (one GKE pod / Cloud Run instance). ADK's `SequentialAgent` and `ParallelAgent` composability handles orchestration in-process — no inter-agent network hops.

The core design principle for custom constraints (poetry forms, art styles, character consistency) is:
> **Free-form natural language specs, injected into prompts, evaluated by the LLM.** No hardcoding of rules. Adding a new constraint requires zero code changes — the user just describes it.

```mermaid
graph TD
    ReactUI["⚛️ React Frontend\n(separate GKE service)"]
    API["🔌 API Service\n(FastAPI + SSE streaming)"]
    Orch["🎭 Orchestrator\n(ADK SequentialAgent)"]

    subgraph AgentPipeline ["ADK Agent Pipeline — single process"]
        Fetch["📚 Literature Fetcher\n(URL or Gutenberg search — agent decides)"]
        Adapt["✏️ Story Adapter\n(gemini-3.1-pro-preview)"]
        TextVal["✅ Text Validator\n(gemini-3.5-flash)\nevaluates against text_spec"]
        PageSplit["📄 Page Splitter\n(deterministic)"]
        CharBible["📖 Character Bible Agent\n(gemini-3.1-pro-preview)\ngenerates visual consistency doc"]
        IllPrompt["🎨 Illustration Prompter\n(gemini-3.5-flash)\ninjects character_bible + image_spec"]
        ImgGen["🖼️ Image Generator\n(gemini-3.1-flash-image — Nano Banana 2)"]
        ImgVal["🔍 Image Validator\n(gemini-3.5-flash vision)\nchecks against bible + prior pages"]
        PDF["📕 PDF Compositor\n(weasyprint)"]
    end

    subgraph Storage ["GCS — storybook-artifacts / sessions / {id}"]
        Orig["original/source_text.txt"]
        Adapted["adapted/story.json"]
        Bible["character_bible.json"]
        Pages["pages/page_N.txt"]
        Prompts["prompts/page_N_prompt.txt"]
        Images["images/page_N.png"]
        Final["final/storybook.pdf"]
    end

    subgraph VertexAI ["Vertex AI / Gemini Enterprise Agent Platform"]
        Gemini31Pro["gemini-3.1-pro-preview\n(complex text + bible generation)"]
        Gemini35Flash["gemini-3.5-flash\n(fast text + vision validation)"]
        NanoBanana2["gemini-3.1-flash-image\n(Nano Banana 2 — image generation)"]
    end

    ReactUI -->|"POST /storybook\n+ SSE progress stream"| API
    API -->|"start_session(config)"| Orch

    Orch --> Fetch
    Fetch --> Orig
    Fetch --> Adapt

    Adapt --> Adapted
    Adapt --> TextVal
    TextVal -->|"pass / retry with specific feedback\ne.g. stanza 3 doesn't scan"| Adapt

    TextVal --> PageSplit
    PageSplit --> Pages
    PageSplit --> CharBible

    CharBible --> Bible
    CharBible --> IllPrompt

    IllPrompt --> Prompts
    IllPrompt --> ImgGen

    ImgGen --> Images
    ImgGen --> ImgVal
    ImgVal -->|"pass / retry with revised prompt\n+ prior page as reference"| ImgGen

    ImgVal --> PDF
    PDF --> Final
    PDF -->|"signed URL"| API
    API -->|"SSE: done + url"| ReactUI

    Adapt <-->|"API"| Gemini31Pro
    CharBible <-->|"API"| Gemini31Pro
    TextVal <-->|"API"| Gemini35Flash
    IllPrompt <-->|"API"| Gemini35Flash
    ImgVal <-->|"API"| Gemini35Flash
    ImgGen <-->|"API"| NanoBanana2
```

---

## Custom Constraint System

Both text and image constraints follow the same pattern: the user writes a plain-English spec, it gets injected into every relevant agent's prompt, and the LLM evaluates conformance. No special-casing in code.

### Text specifications (`text_spec`)

Passed verbatim to the Story Adapter's system prompt and to the Text Validator's evaluation prompt.

**Example — Onegin Stanzas:**
```
text_spec: "Write in Onegin stanzas. Each stanza is 14 lines of iambic tetrameter
with rhyme scheme ABABCCDDEFFEGG. Each page is exactly one complete stanza.
Do not break stanzas across pages."
```

The Text Validator runs each adapted page through Gemini with a prompt like:
> *"Evaluate whether this text conforms to the following specification: {text_spec}. If it does not, return a JSON object with `pass: false` and a `feedback` field listing each violation with line number and specific reason. Be precise enough that the author can fix it on a retry."*

Violations are returned as structured feedback that loops directly back to the Story Adapter for a targeted retry — not a full rewrite.

### Image specifications (`image_spec`)

Free-form style description passed into every Illustration Prompt and Image Validator.

**Examples:**
```
image_spec: "Pen and ink with cross-hatching. No color fills. High contrast black and white only."
image_spec: "Japanese woodblock print style. Bold outlines, flat areas of color, no gradients."
image_spec: "Loose watercolor wash. Soft edges. Muted earth tones. Impressionistic, not photorealistic."
```

### Character Bible (`character_bible.json`)

Generated once by the **Character Bible Agent** after text adaptation, before any images. Reads the full adapted text + `image_spec` and produces a structured document:

```json
{
  "style": "pen and ink with fine cross-hatching, high contrast, no color",
  "palette": ["#1a1a1a", "#f5f0e8", "#8b6914"],
  "world": "Early 19th century rural Russia and St. Petersburg. Birch forests, candlelit dachas, grand ballrooms.",
  "characters": {
    "Eugene Onegin": "Tall young Russian nobleman, early 20s. Dark hair swept back from forehead. Sharp, angular jaw. Slightly bored, melancholic expression. Fitted dark coat, high white collar, tall leather boots.",
    "Tatiana": "Young Russian woman, late teens. Light brown hair worn in two braids. Wide, expressive dark eyes. Simple country dress with embroidered hem. Often shown near windows or outdoors."
  },
  "recurring_motifs": ["birch trees", "candles", "snow", "letters and quill pens"]
}
```

The bible is injected into:
- Every **Illustration Prompter** call (full character descriptions + style)
- Every **Image Validator** call (used as the consistency checklist)

### Image Validator — multimodal consistency check

The Image Validator calls `gemini-3.5-flash` with:
1. The generated image (current page)
2. Page 1's image (style anchor reference — first page sets the visual standard)
3. The `character_bible.json`
4. The `image_spec`

Evaluation prompt:
> *"Compare the generated image against the character bible and style spec. Check: (1) does each visible character match their physical description? (2) does the overall style match the image_spec? (3) does the style match the reference image? Return `pass: true` or `pass: false` with specific, actionable `feedback` for each violation."*

On failure, the feedback is appended to the original prompt and the Image Generator retries with: *"Previous attempt failed validation: {feedback}. Revised prompt: ..."*

---

## Agent Responsibilities

| Agent | Role | Model |
|---|---|---|
| **Orchestrator** | Runs the full pipeline; holds session context; emits SSE progress events | ADK `SequentialAgent` |
| **Literature Fetcher** | URL provided → fetches directly; title/author only → uses Gutenberg search tool; agent chooses; strips boilerplate | HTTP tool + GCS |
| **Story Adapter** | Rewrites source text for target age group; conforms to `text_spec` if provided | `gemini-3.1-pro-preview` |
| **Text Validator** | Evaluates adapted text against `text_spec` using LLM; returns structured pass/fail with per-violation feedback | `gemini-3.5-flash` |
| **Page Splitter** | Segments adapted story into pages per word-count budget; respects stanza/section boundaries from `text_spec` | deterministic |
| **Character Bible Agent** | Reads full adapted text + `image_spec`; generates `character_bible.json` with character descriptions, style, palette, motifs | `gemini-3.1-pro-preview` |
| **Illustration Prompter** | Writes a page-specific image prompt; injects character bible + image_spec for full consistency | `gemini-3.5-flash` |
| **Image Generator** | Calls Nano Banana 2; prompt includes character bible excerpt + image_spec | `gemini-3.1-flash-image` |
| **Image Validator** | Multimodal: checks generated image against bible, image_spec, and page-1 style anchor; returns structured feedback on retry | `gemini-3.5-flash` (vision) |
| **PDF Compositor** | Combines page text + images into formatted storybook PDF | `weasyprint` |

---

## Session Input Schema

```json
{
  "source": {
    "gutenberg_url": "https://www.gutenberg.org/ebooks/XXXX",
    "title": "Eugene Onegin",
    "author": "Alexander Pushkin"
  },
  "target_age": "4-5",
  "page_count": 12,
  "language": "en",
  "text_spec": "Write in Onegin stanzas. Each stanza is 14 lines of iambic tetrameter with rhyme scheme ABABCCDDEFFEGG. Each page is exactly one complete stanza.",
  "image_spec": "Pen and ink with fine cross-hatching. No color fills. High contrast black and white only. 19th century illustration style.",
  "custom_instructions": "Focus on the Tatiana storyline. Emphasize wonder and nature."
}
```

All spec fields are optional. Defaults: no `text_spec` (prose), no `image_spec` (agent chooses style appropriate to the work).

**Age group → pipeline parameters:**

| Age Group | Max words/page | Sentence length | Gemini reading level target |
|---|---|---|---|
| 4–5 | 20 | 5–7 words | Pre-K / Primer |
| 6–8 | 50 | 8–12 words | Grade 1–2 |
| 9–12 | 100 | 12–20 words | Grade 3–5 |

---

## Frontend Architecture

A separate React service with a config form that exposes `text_spec` and `image_spec` as plain text areas — no dropdowns or preset lists, just free-form input.

```mermaid
graph LR
    subgraph Frontend ["React App — storybook-ui"]
        Home["Home / Gutenberg Search"]
        Config["Session Config Form\n(age · text_spec · image_spec\n· custom_instructions)"]
        Progress["Live Progress View\n(SSE stream + stage indicators)"]
        Viewer["Storybook Viewer\n(page flip · download PDF)"]
        History["Session History"]
    end

    subgraph API ["storybook-api (FastAPI)"]
        EP1["POST /sessions"]
        EP2["GET /sessions/{id}/stream (SSE)"]
        EP3["GET /sessions/{id}"]
        EP4["GET /sessions"]
    end

    Home --> Config
    Config -->|"POST /sessions"| EP1
    EP1 -->|"session_id"| Progress
    Progress -->|"SSE"| EP2
    Progress -->|"done"| Viewer
    Viewer -->|"GET /sessions/{id}"| EP3
    Home --> History
```

**Tech stack:**

| Concern | Choice |
|---|---|
| Framework | React 19 + TypeScript |
| Build | Vite |
| Styling | Tailwind CSS |
| Real-time | SSE (one-way server push, simpler than WebSockets) |
| Page flip | `react-pageflip` |
| State | Zustand |
| HTTP | `@tanstack/react-query` |

**SSE event shapes:**
```json
{ "event": "progress", "stage": "building_character_bible", "pct": 35 }
{ "event": "progress", "stage": "generating_image", "page": 3, "of": 12, "pct": 58 }
{ "event": "progress", "stage": "image_retry", "page": 3, "attempt": 2, "reason": "Tatiana's hair color inconsistent with character bible" }
{ "event": "done", "signed_url": "https://storage.googleapis.com/...", "session_id": "abc123" }
{ "event": "error", "stage": "text_validation", "message": "Retry limit reached. Stanza 4 does not scan as iambic tetrameter." }
```

---

## Deployment Topology

Three GKE services:
- `storybook-ui` — React app served by nginx
- `storybook-api` — FastAPI, SSE stream management
- `storybook-agent` — ADK pipeline, single pod (personal project)

```mermaid
graph TD
    Internet --> LB["GKE Ingress"]
    LB --> UI["storybook-ui\n(React, nginx)"]
    LB --> APIGW["storybook-api\n(FastAPI)"]
    APIGW --> AgentPod["storybook-agent\n(ADK pipeline)"]

    subgraph GCP
        AgentPod --> Vertex["Vertex AI\n(Gemini 3.x + Nano Banana 2)"]
        AgentPod --> GCS["Cloud Storage\n(session artifacts)"]
        AgentPod --> SecretMgr["Secret Manager"]
        CloudBuild["Cloud Build"] --> AR["Artifact Registry"]
        AR --> LB
    end
```

---

## GCS Artifact Layout

```
gs://storybook-artifacts-{project_id}/
└── sessions/
    └── {session_id}/
        ├── config.json
        ├── original/
        │   └── source_text.txt
        ├── adapted/
        │   └── story.json
        ├── character_bible.json          ← generated once, used by all image agents
        ├── pages/
        │   ├── page_01.txt … page_N.txt
        ├── prompts/
        │   ├── page_01_prompt.txt … page_N_prompt.txt
        ├── images/
        │   ├── page_01.png … page_N.png  ← page_01 also serves as style anchor
        └── final/
            └── storybook.pdf
```

---

## Session Lifecycle

```mermaid
sequenceDiagram
    actor User
    participant UI as React UI
    participant API as storybook-api
    participant Agent as ADK Pipeline
    participant GCS
    participant Vertex as Vertex AI

    User->>UI: Config form (Eugene Onegin · age 4-5 · Onegin stanzas · pen and ink)
    UI->>API: POST /sessions {config}
    API->>Agent: start_session(config)
    API-->>UI: 200 {session_id}
    UI->>API: GET /sessions/{id}/stream (SSE)

    Agent->>Vertex: Literature Fetcher (URL→direct / title→Gutenberg search)
    Agent->>GCS: original/source_text.txt
    API-->>UI: SSE fetching 10%

    loop until text_spec passes
        Agent->>Vertex: gemini-3.1-pro-preview — adapt + conform to text_spec
        Agent->>Vertex: gemini-3.5-flash — validate against text_spec
        Note over Agent,Vertex: Returns structured violation feedback on fail
    end
    Agent->>GCS: adapted/story.json + pages/
    API-->>UI: SSE adapting_text 30%

    Agent->>Vertex: gemini-3.1-pro-preview — build character_bible.json
    Agent->>GCS: character_bible.json
    API-->>UI: SSE building_character_bible 40%

    loop For each page 1…N
        Agent->>Vertex: gemini-3.5-flash — illustration prompt (bible + image_spec injected)
        Agent->>GCS: prompts/page_N_prompt.txt
        loop until image passes validation
            Agent->>Vertex: gemini-3.1-flash-image — generate image
            Agent->>Vertex: gemini-3.5-flash (vision) — validate vs bible + page_01 anchor
            Note over Agent,Vertex: Feedback: "Onegin's coat is brown, should be dark"
        end
        Agent->>GCS: images/page_N.png
        API-->>UI: SSE generating_image page N
    end

    Agent->>Agent: weasyprint — compose PDF
    Agent->>GCS: final/storybook.pdf
    API-->>UI: SSE done + signed_url
    UI->>User: Storybook viewer + download
```

---

## Resolved Decisions

| Decision | Choice |
|---|---|
| Pod lifecycle | Single pod — personal project, one user |
| Gutenberg source | Agent decides: URL → direct fetch; title/author → Gutenberg search tool |
| Auth | Anonymous — no login required |
| Image model | `gemini-3.1-flash-image` (Nano Banana 2); `gemini-3-pro-image` (Nano Banana Pro) for higher fidelity |
| Text model | `gemini-3.1-pro-preview` for adaptation + bible; `gemini-3.5-flash` for everything else |
| Custom constraints | Free-form `text_spec` + `image_spec` fields; LLM evaluates — no hardcoded rules |
| Image consistency | Character Bible Agent (runs once) + page-1 style anchor in every validation call |
| Art style input | Free-form `image_spec` string — no preset dropdown |

## Open Questions

- [ ] **Retry limits**: How many text/image validation retries before surfacing an error to the user? (Suggest: 3 for text, 2 for images)

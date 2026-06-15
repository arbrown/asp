# Storybook Agent — Architecture

A multi-agent pipeline that adapts public-domain literature into illustrated children's storybooks, deployed on Google Cloud.

---

## System Overview

All agents run inside a **single ADK process** (one GKE pod / Cloud Run instance). ADK's `SequentialAgent` and `ParallelAgent` composability handles orchestration in-process — no inter-agent network hops. If the image generation step becomes a scaling bottleneck later, it can be extracted to a separate worker pool, but the pipeline is sequential so splitting now adds ops complexity with no benefit.

```mermaid
graph TD
    ReactUI["⚛️ React Frontend\n(separate GKE service)"]
    API["🔌 API Service\n(FastAPI + SSE streaming)"]
    Orch["🎭 Orchestrator\n(ADK SequentialAgent)"]

    subgraph AgentPipeline ["ADK Agent Pipeline — single process"]
        Fetch["📚 Literature Fetcher\n(Gutenberg → raw text)"]
        Adapt["✏️ Story Adapter\n(Gemini 2.5 Pro)"]
        TextVal["✅ Text Validator\n(Gemini 2.5 Flash)"]
        PageSplit["📄 Page Splitter\n(deterministic tool)"]
        IllPrompt["🎨 Illustration Prompter\n(Gemini 2.5 Flash)"]
        ImgGen["🖼️ Image Generator\n(Imagen 4.0)"]
        ImgVal["🔍 Image Validator\n(Gemini 2.5 Flash vision)"]
        PDF["📕 PDF Compositor\n(weasyprint)"]
    end

    subgraph Storage ["GCS — storybook-artifacts"]
        Orig["original/source_text.txt"]
        Adapted["adapted/story.json"]
        Pages["pages/page_N.txt"]
        Prompts["prompts/page_N_prompt.txt"]
        Images["images/page_N.png"]
        Final["final/storybook.pdf"]
    end

    subgraph VertexAI ["Vertex AI / Gemini Enterprise Agent Platform"]
        Gemini31Pro["gemini-3.1-pro-preview\n(complex text tasks)"]
        Gemini35Flash["gemini-3.5-flash\n(fast text + vision)"]
        NanoBanana2["gemini-3.1-flash-image\n(Nano Banana 2 — image generation)"]
    end

    ReactUI -->|"POST /storybook\n+ SSE progress stream"| API
    API -->|"start_session(config)"| Orch

    Orch --> Fetch
    Fetch --> Orig
    Fetch --> Adapt

    Adapt --> Adapted
    Adapt --> TextVal
    TextVal -->|"pass / retry with feedback"| Adapt

    TextVal --> PageSplit
    PageSplit --> Pages
    PageSplit --> IllPrompt

    IllPrompt --> Prompts
    IllPrompt --> ImgGen

    ImgGen --> Images
    ImgGen --> ImgVal
    ImgVal -->|"pass / retry with revised prompt"| ImgGen

    ImgVal --> PDF
    PDF --> Final
    PDF -->|"signed URL"| API
    API -->|"SSE: done + url"| ReactUI

    Adapt <-->|"API"| Gemini31Pro
    TextVal <-->|"API"| Gemini35Flash
    IllPrompt <-->|"API"| Gemini35Flash
    ImgVal <-->|"API"| Gemini35Flash
    ImgGen <-->|"API"| NanoBanana2
```

---

## Agent Responsibilities

| Agent | Role | Model |
|---|---|---|
| **Orchestrator** | Runs the full pipeline; holds session context; emits SSE progress events | ADK `SequentialAgent` |
| **Literature Fetcher** | Given a title/author or URL, uses Gutenberg search tool or fetches directly — agent decides which is more appropriate; strips boilerplate | HTTP tool + GCS |
| **Story Adapter** | Rewrites source text for the target age group; respects user's custom instructions | `gemini-3.1-pro-preview` |
| **Text Validator** | Checks adapted text: reading level, age-appropriateness, completeness; sends feedback to Adapter if retry needed | `gemini-3.5-flash` |
| **Page Splitter** | Segments adapted story into pages using word-count budget derived from age group | deterministic |
| **Illustration Prompter** | Writes a rich, style-consistent image prompt for each page; maintains character/world continuity | `gemini-3.5-flash` |
| **Image Generator** | Calls Nano Banana 2 to produce one illustration per page | `gemini-3.1-flash-image` (Nano Banana 2) |
| **Image Validator** | Uses Gemini vision to check each image: style consistency, content safety, match to page text; sends revised prompt back if needed | `gemini-3.5-flash` |
| **PDF Compositor** | Combines page text + images into a formatted PDF storybook | `weasyprint` |

---

## Session Input Schema

A session is started with a structured config. The user can provide as much or as little as they want — sensible defaults fill the rest.

```json
{
  "source": {
    "gutenberg_url": "https://www.gutenberg.org/ebooks/XXXX",
    "title": "Eugene Onegin",
    "author": "Alexander Pushkin"
  },
  "target_age": "4-5",
  "art_style": "watercolor",
  "page_count": 12,
  "custom_instructions": "Focus on the friendship themes. Use simple rhyming language.",
  "language": "en"
}
```

**Age group → pipeline parameters mapping:**

| Age Group | Max words/page | Sentence length | Gemini reading level target |
|---|---|---|---|
| 4–5 | 20 | 5–7 words | Pre-K / Primer |
| 6–8 | 50 | 8–12 words | Grade 1–2 |
| 9–12 | 100 | 12–20 words | Grade 3–5 |

---

## Frontend Architecture

A separate React service — not ADK's built-in debug UI.

```mermaid
graph LR
    subgraph Frontend ["React App (GKE service: storybook-ui)"]
        Home["Home / Search\n(Gutenberg lookup)"]
        Config["Session Config Form\n(age, style, instructions)"]
        Progress["Live Progress View\n(SSE stream)"]
        Viewer["Storybook Viewer\n(page flip, download)"]
        History["Session History\n(past runs)"]
    end

    subgraph API ["API Service (GKE service: storybook-api)"]
        EP1["POST /sessions"]
        EP2["GET /sessions/{id}/stream (SSE)"]
        EP3["GET /sessions/{id}"]
        EP4["GET /sessions"]
    end

    Home --> Config
    Config -->|"POST /sessions"| EP1
    EP1 -->|"session_id"| Progress
    Progress -->|"GET /sessions/{id}/stream"| EP2
    EP2 -->|"progress events"| Progress
    Progress -->|"done"| Viewer
    Viewer -->|"GET /sessions/{id}"| EP3
    Home -->|"GET /sessions"| History
```

**Tech choices for the frontend:**

| Concern | Choice |
|---|---|
| Framework | React 19 + TypeScript |
| Build tool | Vite |
| Styling | Tailwind CSS |
| Real-time progress | Server-Sent Events (SSE) — simpler than WebSockets for one-way server push |
| Page flip animation | `react-pageflip` |
| State management | Zustand (lightweight) |
| HTTP client | `@tanstack/react-query` |

**SSE progress event shape:**
```json
{ "event": "progress", "stage": "adapting_text", "pct": 30, "message": "Adapting prose for ages 4–5…" }
{ "event": "progress", "stage": "generating_image", "page": 3, "pct": 65 }
{ "event": "done", "signed_url": "https://storage.googleapis.com/...", "session_id": "abc123" }
{ "event": "error", "stage": "image_validation", "message": "Image retry limit exceeded on page 2" }
```

---

## Deployment Topology

```mermaid
graph TD
    Internet["Internet"] --> LB["GKE Ingress / Load Balancer"]
    LB --> UI["storybook-ui\n(React, nginx)"]
    LB --> APIGW["storybook-api\n(FastAPI)"]
    APIGW --> AgentPod["storybook-agent\n(ADK pipeline)"]

    subgraph GCP ["Google Cloud"]
        AgentPod --> Vertex["Vertex AI\n(Gemini 2.5 + Imagen 4.0)"]
        AgentPod --> GCS["Cloud Storage\n(session artifacts)"]
        AgentPod --> SecretMgr["Secret Manager"]
        AR["Artifact Registry"] --> LB
        CloudBuild["Cloud Build"] --> AR
    end
```

Three GKE services:
- `storybook-ui` — React app served by nginx, scales to 0 off-hours
- `storybook-api` — FastAPI, receives requests and manages SSE streams
- `storybook-agent` — ADK process, one pod per in-flight session (or queue-based)

---

## GCS Artifact Layout

```
gs://storybook-artifacts-{project_id}/
└── sessions/
    └── {session_id}/
        ├── config.json              ← full session input
        ├── original/
        │   └── source_text.txt
        ├── adapted/
        │   └── story.json           ← title, author, adapted_text, metadata
        ├── pages/
        │   ├── page_01.txt
        │   └── page_N.txt
        ├── prompts/
        │   ├── page_01_prompt.txt
        │   └── page_N_prompt.txt
        ├── images/
        │   ├── page_01.png
        │   └── page_N.png
        └── final/
            └── storybook.pdf
```

---

## Technology Summary

| Concern | Choice | Notes |
|---|---|---|
| Agent framework | Google ADK (Python) | Single process, SequentialAgent + ParallelAgent |
| Complex text tasks | `gemini-3.1-pro-preview` | Story adaptation (highest capability) |
| Fast text + vision | `gemini-3.5-flash` | Validation, prompts, vision checks |
| Image generation | `gemini-3.1-flash-image` | Nano Banana 2 (latest fast image model); `gemini-3-pro-image` (Nano Banana Pro) for higher fidelity |
| Frontend | React 19 + Vite + Tailwind | Separate GKE service |
| Real-time updates | SSE | Server → client progress stream |
| Serving | GKE | Three services: ui, api, agent |
| Artifacts | GCS | Session-scoped folders, signed URLs for delivery |
| PDF generation | `weasyprint` | HTML/CSS → PDF, good typography |
| Source texts | Project Gutenberg | Free public-domain library |
| IaC | Terraform | `infra/terraform/` |

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

    User->>UI: Fill config form (Eugene Onegin, age 4-5, watercolor)
    UI->>API: POST /sessions {config}
    API->>Agent: start_session(config)
    API-->>UI: 200 {session_id}
    UI->>API: GET /sessions/{id}/stream (SSE)

    Agent->>GCS: save config.json
    Agent->>Vertex: Literature Fetcher — URL given? fetch directly; title only? Gutenberg search tool
    Agent->>GCS: save original/source_text.txt
    API-->>UI: SSE: {stage: fetching, pct: 10}

    Agent->>Vertex: gemini-3.1-pro-preview — adapt for age 4-5
    Agent->>Vertex: gemini-3.5-flash — validate text
    API-->>UI: SSE: {stage: adapting_text, pct: 30}

    Agent->>GCS: save adapted/story.json + pages/
    API-->>UI: SSE: {stage: splitting_pages, pct: 40}

    loop For each page (12 pages)
        Agent->>Vertex: gemini-3.5-flash — illustration prompt
        Agent->>GCS: save prompts/page_N_prompt.txt
        Agent->>Vertex: gemini-3.1-flash-image (Nano Banana 2) — generate image
        Agent->>Vertex: gemini-3.5-flash (vision) — validate image
        Agent->>GCS: save images/page_N.png
        API-->>UI: SSE: {stage: generating_image, page: N, pct: ...}
    end

    Agent->>Agent: compose PDF (weasyprint)
    Agent->>GCS: save final/storybook.pdf
    API-->>UI: SSE: {event: done, signed_url: "..."}
    UI->>User: Show storybook viewer + download button
```

---

## Resolved Decisions

| Decision | Choice |
|---|---|
| Pod lifecycle | Single pod — personal project, one user |
| Gutenberg source | Agent decides: URL provided → direct fetch; title/author only → Gutenberg search tool |
| Auth | Anonymous — no login required |
| Image model | `gemini-3.1-flash-image` (Nano Banana 2); swap to `gemini-3-pro-image` (Nano Banana Pro) for higher fidelity |
| Text models | `gemini-3.1-pro-preview` for adaptation; `gemini-3.5-flash` for everything else |

## Open Questions

- [ ] **Retry policy**: Max retries for text/image validation before surfacing error to user?
- [ ] **Art style presets**: Curated list (watercolor, gouache, pencil sketch, flat vector, linocut) or free-form string?

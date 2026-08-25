# Storybook Agent — Architecture

A multi-agent pipeline that adapts public-domain literature into illustrated children's storybooks, deployed on Google Cloud.

---

## System Overview

All agents run inside a **single backend process** (one GKE pod running FastAPI and the ADK execution runtime). ADK's `LlmAgent` and `LoopAgent` composability handles orchestration in-process with `asyncio` queues and concurrency semaphores — no inter-agent network hops.

The core design principle for custom constraints (poetry forms, art styles, character consistency) is:
> **Free-form natural language specs, injected into prompts, evaluated by the LLM.** No hardcoding of rules. Adding a new constraint requires zero code changes — the user just describes it.

```mermaid
graph TD
    ReactUI["⚛️ React Frontend\n(separate GKE service)"]
    API["🔌 API Service\n(FastAPI + SSE streaming)"]
    Orch["🎭 Pipeline Orchestrator\n(asyncio + ADK Agents)"]

    subgraph AgentPipeline ["ADK Agent Pipeline — single backend process"]
        Fetch["📚 Literature Fetcher\n(Gutenberg search + mirror/OPDS rotation)"]
        
        subgraph TwoPassAdaptation ["Two-Pass Adaptation & Bible Generation"]
            DraftAdapt["✏️ Draft Adapter\n(gemini-3.5-flash)\nfast structural draft"]
            BibleSeed["🌱 Bible Seeder\n(gemini-3.1-pro-preview)\nper-chunk draft bible"]
            BibleMerge["🔀 Bible Merger\n(gemini-3.1-pro-preview)\nreconciles multi-chunk seeds"]
            CraftAdapt["🖋️ Craft Adapter\n(gemini-3.1-pro-preview)\nbible-aware spread adaptation"]
            TextVal["✅ Text Validator\n(gemini-3.5-flash)\nevaluates against text_spec"]
            FinalBible["📖 Finalize Bible\n(gemini-3.1-pro-preview)\nrefreshes bible against final text"]
        end

        subgraph SpreadLayout ["Spread & Layout Planning"]
            SpreadPlan["📐 Spread Planner\n(gemini-3.5-flash)\nplans coverage, aspect ratios, typography"]
            LayoutExt["🎨 Layout Extractor\n(gemini-3.5-flash)\nderives color & layout directives"]
        end

        subgraph IllustrationPipeline ["Illustration & Vision Verification (Parallel)"]
            IllPrompt["🎨 Illustration Prompter\n(gemini-3.5-flash)\ninjects character_bible + image_spec"]
            ImgGen["🖼️ Image Generator\n(gemini-3.1-flash-image — Nano Banana 2)"]
            ImgVal["🔍 Image Validator\n(gemini-3.5-flash vision)\nchecks vs bible + spread anchors"]
            PageVerify["👁️ Spread Layout Verifier\n(gemini-3.5-flash vision)\nverifies rendered HTML legibility"]
        end

        PDF["📕 PDF Compositor\n(weasyprint + jinja2)"]
    end

    subgraph Storage ["Durable Storage & Persistence"]
        subgraph GCS ["GCS — storybook-artifacts / sessions / {id}"]
            Orig["original/source_text.txt"]
            Adapted["adapted/story.json"]
            BibleDoc["character_bible.json"]
            Spreads["spreads/spread_plan.json"]
            Prompts["prompts/spread_N_prompt.txt"]
            Images["images/spread_N.png"]
            FinalPDF["final/storybook.pdf"]
        end
        subgraph DB ["rqlite — Database Cluster (port 4001)"]
            SessionsTable["sessions table\n(state, metadata, history, errors)"]
        end
    end

    subgraph VertexAI ["Vertex AI / Gemini Enterprise Platform"]
        Gemini31Pro["gemini-3.1-pro-preview\n(complex craft text + bible generation)\n*Aspirational: gemini-3.5-pro when GA*"]
        Gemini35Flash["gemini-3.5-flash\n(fast draft, prompts & multimodal vision)"]
        NanoBanana2["gemini-3.1-flash-image\n(Nano Banana 2 — image generation)\n*Aspirational: gemini-3-pro-image for HD*"]
    end

    ReactUI -->|"POST /api/v1/sessions\n+ SSE progress stream"| API
    API -->|"persist session state"| SessionsTable
    API -->|"run_pipeline(config)"| Orch

    Orch --> Fetch
    Fetch --> Orig
    Fetch --> DraftAdapt

    DraftAdapt --> BibleSeed
    BibleSeed --> BibleMerge
    BibleMerge --> BibleDoc
    BibleMerge --> CraftAdapt

    CraftAdapt --> TextVal
    TextVal -->|"pass / retry with violation feedback"| CraftAdapt
    CraftAdapt --> FinalBible
    FinalBible --> BibleDoc
    CraftAdapt --> Adapted

    CraftAdapt --> SpreadPlan
    SpreadPlan --> LayoutExt
    LayoutExt --> Spreads

    SpreadPlan --> IllPrompt
    IllPrompt --> Prompts
    IllPrompt --> ImgGen

    ImgGen --> Images
    ImgGen --> ImgVal
    ImgVal -->|"pass / retry with revised prompt\n+ anchor reference"| ImgGen

    ImgVal --> PageVerify
    PageVerify --> PDF
    PDF --> FinalPDF
    PDF --> Orch
    Orch -->|"update completed state"| SessionsTable

    DraftAdapt -.-> Gemini35Flash
    BibleSeed -.-> Gemini31Pro
    BibleMerge -.-> Gemini31Pro
    CraftAdapt -.-> Gemini31Pro
    TextVal -.-> Gemini35Flash
    FinalBible -.-> Gemini31Pro
    SpreadPlan -.-> Gemini35Flash
    LayoutExt -.-> Gemini35Flash
    IllPrompt -.-> Gemini35Flash
    ImgGen -.-> NanoBanana2
    ImgVal -.-> Gemini35Flash
    PageVerify -.-> Gemini35Flash
```

---

## Frontend Architecture

The frontend is a lightweight **React 18 SPA** built with Vite, TypeScript, and Tailwind CSS. It communicates with the backend via REST and a persistent Server-Sent Events (SSE) connection.

```mermaid
graph LR
    subgraph UI ["React SPA (Vite + TypeScript + Tailwind)"]
        Form["Config Form\n• Title & Author Search\n• 5 Target Age Bands (2-3, 4-5, 6-7, 8-9, 10-12)\n• Spread Count (10-20)\n• Text Spec (free-form)\n• Image Spec (free-form)\n• Custom Instructions (free-form)"]
        Assistants["Creative Assistants\n• I'm Feeling Lucky (full config dice roll)\n• Per-Section Shuffle Buttons (context-aware)"]
        Progress["Progress View\n• Monotonic SSE EventSource\n• Live sub-stage progress (0-100%)\n• Error recovery & Resume trigger"]
        Viewer["Storybook Viewer\n• Two-page spread flip viewer\n• PDF download\n• Raw assets & prompts inspector"]
        History["History View\n• Past session list from rqlite\n• Status badges & resumability"]
    end

    subgraph Backend ["FastAPI Backend Routes (/api/v1)"]
        Lucky["GET /lucky\n(AI-generated complete config)"]
        Shuffle["POST /shuffle\n(field-specific re-rolls)"]
        PostSession["POST /sessions\n(starts background async task)"]
        ResumeSession["POST /sessions/{id}/resume\n(re-triggers errored session)"]
        StreamSSE["GET /sessions/{id}/stream\n(text/event-stream SSE)"]
        GetSession["GET /sessions/{id}\n(state + signed asset URLs)"]
        ListSessions["GET /sessions\n(paginated session history)"]
    end

    Form --> Assistants
    Assistants -->|"GET /lucky"| Lucky
    Assistants -->|"POST /shuffle"| Shuffle
    Form -->|"POST /sessions"| PostSession
    Progress -->|"GET /sessions/{id}/stream"| StreamSSE
    Progress -->|"POST /sessions/{id}/resume"| ResumeSession
    Viewer -->|"GET /sessions/{id}"| GetSession
    History -->|"GET /sessions"| ListSessions
```

### Key UI Features
- **5 Age Bands**: Granular targeting for `2-3`, `4-5`, `6-7`, `8-9`, and `10-12` with age-appropriate vocabulary, sentence length, and spread composition.
- **Creative Exploration**: "I'm Feeling Lucky" produces end-to-end creative seeds, while per-section **Shuffle buttons** regenerate title/author, text forms, illustration specs, or custom rules with prior fields as context.
- **Monotonic Live Progress**: The SSE stream delivers smooth sub-stage percentages across drafting (0–10%), bible creation (10–20%), craft adaptation (20–40%), layout planning (40–43%), parallel image generation (43–91%), and PDF compilation (91–100%).
- **Resilience & Resumption**: Errored runs can be resumed in-place via `POST /sessions/{id}/resume`, picking up from the last completed checkpoint.

---

## Agent Pipeline Details

The pipeline uses a **two-pass adaptation** approach to achieve literary quality and strong visual consistency.

```
Literature Fetcher
      │
      ▼
Draft Adapter (gemini-3.5-flash)  ─── Fast structural chunk draft
      │
      ▼
Character Bible Seeder & Merger (gemini-3.1-pro-preview)  ─── Character & visual world definition
      │
      ▼
Craft Adapter (gemini-3.1-pro-preview)  ─── LoopAgent with Text Validator
      │
      ▼
Finalize Bible (gemini-3.1-pro-preview)  ─── Synchronize bible with final craft text
      │
      ▼
Spread Planner & Layout Extractor (gemini-3.5-flash)  ─── Spreads, aspect ratios & typography
      │
      ▼
Illustration Prompter (gemini-3.5-flash)  ─── Injects character bible + art style + spread context
      │
      ▼
Image Generator (gemini-3.1-flash-image)  ─── Parallel generation (concurrency-limited)
      │
      ▼
Image Validator (gemini-3.5-flash vision)  ─── Multimodal check vs bible + style anchor
      │
      ▼
Spread Layout Verifier (gemini-3.5-flash vision)  ─── Multimodal legibility check on rendered HTML
      │
      ▼
PDF Compositor (weasyprint)  ─── Final print-ready PDF
```

### Agent Roster

| Agent | Role | Model | Validation / Feedback |
|---|---|---|---|
| **Literature Fetcher** | Fetches public-domain works via Gutenberg API, OPDS Atom catalog, or mirror rotation (`pglaf.org`, `aleph.gutenberg.org`) | Tool execution / deterministic | Validates non-empty text, strips headers |
| **Draft Adapter** | Fast structural draft of story text chunk | `gemini-3.5-flash` | Checks word counts and age-appropriate structure |
| **Character Bible Seeder & Merger** | Extracts character traits, attire, physical features, and setting rules; reconciles multi-chunk seeds | `gemini-3.1-pro-preview` | Structured JSON output conforming to bible schema |
| **Craft Adapter** | Adapts story into two-page spreads with verse/meter and literary craft rules | `gemini-3.1-pro-preview` *(Aspirational: `gemini-3.5-pro`)* | Paired with Text Validator in ADK `LoopAgent` |
| **Text Validator** | Evaluates adapted text against `text_spec` constraints (meter, rhyme, vocabulary, cadence) | `gemini-3.5-flash` | Returns structured pass/fail with exact violation notes |
| **Spread Planner & Layout Extractor** | Determines verso/recto content, illustration coverage (full, verso, recto), aspect ratio (16:9, 3:4, 1:1), and color themes | `gemini-3.5-flash` | Validates layout balance and page count constraints |
| **Illustration Prompter** | Crafts comprehensive image prompts embedding bible rules, scene cues, and art style | `gemini-3.5-flash` | Injects style anchors and character definitions |
| **Image Generator** | Generates high-resolution spread illustrations | `gemini-3.1-flash-image` (Nano Banana 2) *(Aspirational: `gemini-3-pro-image`)* | Handled with exponential backoff & content policy recovery |
| **Image Validator** | Multimodal evaluation against character bible, spread 0/1 style anchor, and previous spread | `gemini-3.5-flash` (vision) | Returns structured pass/fail with corrective prompt feedback |
| **Spread Layout Verifier** | Multimodal check of rendered HTML spread + image for typography contrast and legibility | `gemini-3.5-flash` (vision) | Checks text placement, overlay readability, and clipping |
| **PDF Compositor** | Assembles spreads into two-page landscape / portrait PDF with HTML/CSS styling | `weasyprint` + Jinja2 | Validates page geometry and font embedding |

---

## Deployment Topology

The application is deployed to **Google Kubernetes Engine (GKE) Autopilot** in `us-central1`.

```mermaid
graph TD
    User([User Browser])

    subgraph GCP ["Google Cloud Platform (us-central1)"]
        subgraph GKE ["GKE Autopilot Cluster — storybook-cluster"]
            Ingress["GKE Managed Ingress\n(storybook-ingress / GCE)"]

            subgraph PodUI ["storybook-ui Pod"]
                Nginx["nginx (port 80)"]
                StaticAssets["React SPA Static Files"]
            end

            subgraph PodBackend ["storybook-backend Pod (Combined API + ADK Runtime)"]
                FastAPIApp["FastAPI Service (port 8080)\n• REST Endpoints\n• SSE Event Stream"]
                ADKRuntime["Pipeline Runner (in-process asyncio)\n• ADK Agents & LoopAgents\n• Concurrency Semaphores"]
                WorkloadID["Workload Identity\n(storybook-backend SA)"]
            end

            subgraph PodRqlite ["rqlite StatefulSet"]
                RqliteSvc["rqlite ClusterIP (port 4001)\n• Replicated SQLite Store\n• Durable Session State"]
            end
        end

        subgraph GCSBucket ["Cloud Storage Bucket — storybook-artifacts-{id}"]
            RawSource["/sessions/{id}/original/"]
            Adaptations["/sessions/{id}/adapted/"]
            Bibles["/sessions/{id}/character_bible.json"]
            RenderedImages["/sessions/{id}/images/"]
            CompiledPDF["/sessions/{id}/final/storybook.pdf"]
        end

        subgraph VertexGCP ["Vertex AI / Gemini API (global endpoint)"]
            VertexPro["gemini-3.1-pro-preview\n(Craft Text & Character Bible)"]
            VertexFlash["gemini-3.5-flash\n(Draft, Prompter, Validator, Verifier)"]
            VertexImage["gemini-3.1-flash-image\n(Nano Banana 2 Image Gen)"]
        end

        subgraph ArtifactReg ["Artifact Registry"]
            BackendImg["storybook-images/backend:latest"]
            UIImg["storybook-images/frontend:latest"]
        end
    end

    User -->|"HTTPS /"| Ingress
    Ingress -->|"path: /"| Nginx
    Ingress -->|"path: /api"| FastAPIApp

    FastAPIApp <-->|"HTTP /db query & execute"| RqliteSvc
    FastAPIApp -->|"enqueue session"| ADKRuntime
    ADKRuntime <-->|"google-genai / Vertex AI SDK"| VertexGCP
    ADKRuntime <-->|"gcsfs / google-cloud-storage"| GCSBucket
    WorkloadID -.->|"IAM OAuth Token"| VertexGCP
    WorkloadID -.->|"Storage Object Admin"| GCSBucket

    ArtifactReg -.->|"deploy image"| PodUI
    ArtifactReg -.->|"deploy image"| PodBackend
```

---

## Session Lifecycle

```mermaid
sequenceDiagram
    actor User
    participant UI as React Frontend
    participant API as FastAPI Backend
    participant DB as rqlite Store
    participant Agent as ADK Pipeline
    participant GCS as GCS Bucket
    participant Vertex as Vertex AI

    User->>UI: Configure story or click Lucky / Shuffle
    UI->>API: POST /api/v1/sessions
    API->>DB: Insert initial session record (pending)
    API->>Agent: Spawn background asyncio pipeline task
    API-->>UI: 202 Accepted {session_id}
    UI->>API: GET /api/v1/sessions/{id}/stream (SSE)

    Note over Agent: Phase 1: Literature Acquisition
    Agent->>Agent: Gutenberg search / mirror rotation
    Agent->>GCS: original/source_text.txt
    API-->>UI: SSE fetching_literature (10%)

    Note over Agent: Phase 2: Two-Pass Adaptation & Bible Generation
    Agent->>Vertex: Draft Adapter (gemini-3.5-flash) — structural chunk draft
    Agent->>Vertex: Bible Seeder & Merger (gemini-3.1-pro-preview) — build character bible
    Agent->>GCS: character_bible.json
    API-->>UI: SSE building_character_bible (20%)

    loop until text_spec passes (LoopAgent max 3 retries)
        Agent->>Vertex: Craft Adapter (gemini-3.1-pro-preview) — adapt into spreads
        Agent->>Vertex: Text Validator (gemini-3.5-flash) — validate meter/rules
        Note over Agent,Vertex: Returns structured violation feedback on fail
    end
    Agent->>Vertex: Finalize Bible (gemini-3.1-pro-preview)
    Agent->>GCS: adapted/story.json
    API-->>UI: SSE adapting_text (40%)

    Note over Agent: Phase 3: Spread Planning & Layout
    Agent->>Vertex: Spread Planner & Layout Extractor (gemini-3.5-flash)
    Agent->>GCS: spreads/spread_plan.json
    API-->>UI: SSE planning_spreads (43%)

    Note over Agent: Phase 4: Parallel Image Generation & Vision Validation
    par Across Spreads (concurrency limited)
        Agent->>Vertex: Illustration Prompter (gemini-3.5-flash)
        Agent->>GCS: prompts/spread_N_prompt.txt
        loop until image passes validation (max 2 retries)
            Agent->>Vertex: gemini-3.1-flash-image — generate illustration
            Agent->>Vertex: gemini-3.5-flash vision — validate vs bible + spread_01 anchor
            Note over Agent,Vertex: Structured corrective feedback on retry
        end
        Agent->>GCS: images/spread_N.png
        API-->>UI: SSE generating_image spread N (43% -> 91%)
    end

    Note over Agent: Phase 5: Layout Verification & PDF Assembly
    Agent->>Vertex: Spread Layout Verifier (gemini-3.5-flash vision) — check rendered HTML contrast & legibility
    Agent->>Agent: weasyprint — compose two-page landscape/portrait PDF
    Agent->>GCS: final/storybook.pdf
    Agent->>DB: Update session state to "done"
    API-->>UI: SSE done (100%) + signed_urls
    UI->>User: Display storybook viewer + PDF download
```

---

## Resolved Decisions & Architecture Annotations

| Decision | Implemented Choice | Aspirational / Future State Annotation |
|---|---|---|
| **Pod Lifecycle** | Single `storybook-backend` pod running FastAPI + ADK pipeline execution via in-process `asyncio` tasks | Aspirational for multi-tenant scale: decoupled Celery / Cloud Tasks queue with independent autoscaling worker pods |
| **Gutenberg Source** | Gutenberg search (`gutendex`) with fallback to OPDS Atom catalog (`m.gutenberg.org`) and mirror rotation (`pglaf.org`, `aleph.gutenberg.org`) | Standalone local Gutenberg mirror cache on persistent disk |
| **Target Age Granularity** | 5 distinct age bands (`2-3`, `4-5`, `6-7`, `8-9`, `10-12`) with calibrated vocabulary and spread geometry | Custom reading-level lexile score tuning |
| **Creative Controls** | "I'm Feeling Lucky" full-form prompt generation + per-field context-aware Shuffle buttons | Community prompt gallery and fine-tuned style presets |
| **Image Model** | `gemini-3.1-flash-image` (Nano Banana 2) via `google-genai` Vertex AI SDK | Aspirational: `gemini-3-pro-image` (Nano Banana Pro) when broadly available for higher artistic fidelity |
| **Craft Text Model** | `gemini-3.1-pro-preview` for craft adaptation, bible seeding/merging/finalizing | Aspirational: `gemini-3.5-pro` when GA |
| **Fast Text & Vision** | `gemini-3.5-flash` for drafting, text validation, illustration prompts, image validation, and HTML page verification | Continuous upgrade to newest Flash checkpoints |
| **Layout & Spreads** | True two-page spread architecture (verso/recto) with dynamic aspect ratios (16:9, 3:4, 1:1) and layout verifier | Interactive in-browser visual spread designer |
| **Session Persistence** | **rqlite** replicated SQLite cluster on GKE for durable relational session state & history | Managed Cloud SQL (PostgreSQL) if enterprise multi-cluster scale is needed |
| **Object Storage** | Google Cloud Storage (`storybook-artifacts-{id}`) with session folder partitioning | Cloud CDN signed URL caching for high-traffic public reading |
| **PDF Engine** | `weasyprint` + Jinja2 HTML/CSS templates | Headless Chrome print-to-PDF pipeline for advanced CSS paged media support |

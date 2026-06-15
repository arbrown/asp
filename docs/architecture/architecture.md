# Storybook Agent — Architecture

A multi-agent pipeline that adapts public-domain literature into illustrated children's storybooks, deployed on Google Cloud.

---

## System Overview

```mermaid
graph TD
    User["👤 User / Client"]
    API["API Gateway\n(Cloud Run or GKE Ingress)"]
    Orch["🎭 Orchestrator Agent\n(ADK SequentialAgent)"]

    subgraph Agents ["Agent Pipeline"]
        Fetch["📚 Literature Fetcher Agent\n(Fetches & validates source text)"]
        Adapt["✏️ Story Adapter Agent\n(Gemini: rewrites for children)"]
        Page["📄 Page Splitter Agent\n(Segments story into pages)"]
        Prompt["🎨 Illustration Prompter Agent\n(Gemini: crafts image prompts)"]
        Image["🖼️ Image Generator Agent\n(Imagen 3 via Vertex AI)"]
        PDF["📕 PDF Compositor Agent\n(Assembles final storybook)"]
    end

    subgraph Storage ["GCS Bucket — storybook-artifacts"]
        direction LR
        Orig["session/{id}/original/\nsource_text.txt"]
        Adapted["session/{id}/adapted/\nstory.json"]
        Pages["session/{id}/pages/\npage_01.txt … page_N.txt"]
        Prompts["session/{id}/prompts/\npage_01_prompt.txt …"]
        Images["session/{id}/images/\npage_01.png … page_N.png"]
        Final["session/{id}/final/\nstorybook.pdf"]
    end

    subgraph VertexAI ["Vertex AI / Gemini Enterprise Agent Platform"]
        Gemini["Gemini 2.0 Flash / Pro\n(Text generation)"]
        Imagen["Imagen 3\n(Image generation)"]
    end

    User -->|"POST /storybook {title, author, style}"| API
    API --> Orch

    Orch --> Fetch
    Fetch -->|"raw text"| Orig
    Fetch --> Adapt

    Adapt -->|"child-friendly prose"| Adapted
    Adapt --> Page

    Page -->|"page segments"| Pages
    Page --> Prompt

    Prompt -->|"illustration prompts"| Prompts
    Prompt --> Image

    Image -->|"page illustrations"| Images
    Image --> PDF

    PDF -->|"final PDF"| Final
    PDF -->|"signed URL"| User

    Adapt <-->|"Gemini API"| Gemini
    Prompt <-->|"Gemini API"| Gemini
    Image <-->|"Imagen API"| Imagen
```

---

## Agent Responsibilities

| Agent | Role | Model / Tool |
|---|---|---|
| **Orchestrator** | Coordinates the full pipeline; passes session context between agents | ADK `SequentialAgent` |
| **Literature Fetcher** | Accepts a Project Gutenberg URL or title, downloads raw text, strips boilerplate | HTTP tool + GCS write |
| **Story Adapter** | Rewrites the source text as age-appropriate prose (target reading level configurable) | Gemini 2.0 Flash |
| **Page Splitter** | Segments the adapted story into discrete pages (word-count budget per page) | Deterministic tool |
| **Illustration Prompter** | Generates a rich image prompt for each page, consistent style/character descriptions | Gemini 2.0 Flash |
| **Image Generator** | Calls Imagen 3 to produce one illustration per page | Imagen 3 via Vertex AI |
| **PDF Compositor** | Combines page text + images into a formatted PDF storybook | Python (`reportlab` or `weasyprint`) |

---

## GCS Artifact Layout

```
gs://storybook-artifacts-{project_id}/
└── sessions/
    └── {session_id}/          # UUID generated per run
        ├── original/
        │   └── source_text.txt
        ├── adapted/
        │   └── story.json     # title, author, reading_level, full_text
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

## Deployment Topology

```mermaid
graph LR
    subgraph GKE ["GKE Cluster (or Cloud Run)"]
        API2["API Service\n(FastAPI)"]
        AgentSvc["Agent Runner Service\n(ADK runtime)"]
    end

    subgraph GCP ["Google Cloud"]
        GCS["Cloud Storage\n(Artifacts)"]
        Vertex["Vertex AI\n(Gemini + Imagen)"]
        SecretMgr["Secret Manager\n(API keys, config)"]
        AR["Artifact Registry\n(Container images)"]
        CloudBuild["Cloud Build\n(CI/CD)"]
    end

    Client["Client"] --> API2
    API2 --> AgentSvc
    AgentSvc --> GCS
    AgentSvc --> Vertex
    AgentSvc --> SecretMgr
    CloudBuild --> AR
    AR --> GKE
```

---

## Technology Choices

| Concern | Choice | Rationale |
|---|---|---|
| Agent framework | Google ADK (Python) | Native Vertex AI integration, supports multi-agent orchestration |
| LLM | Gemini 2.0 Flash / Pro | Cost-efficient for text; Pro for complex adaptation tasks |
| Image generation | Imagen 3 | Highest quality Google-native image model |
| Serving | Cloud Run (MVP) → GKE (scale) | Cloud Run for fast iteration; GKE for long-running pipelines |
| Artifact storage | GCS | Durable, session-scoped, easy signed URL generation |
| PDF generation | `weasyprint` (HTML→PDF) | CSS-based layout, good typography control |
| Source texts | Project Gutenberg API | Largest free public-domain library |

---

## Session Lifecycle

```mermaid
sequenceDiagram
    actor User
    participant API
    participant Orch as Orchestrator
    participant GCS
    participant Gemini
    participant Imagen

    User->>API: POST /storybook {title, reading_level, art_style}
    API->>Orch: start_session(config)
    Orch->>GCS: create session folder
    Orch->>Orch: fetch source text (Gutenberg)
    Orch->>GCS: save original/source_text.txt
    Orch->>Gemini: adapt text for children
    Orch->>GCS: save adapted/story.json + pages/
    loop For each page
        Orch->>Gemini: generate illustration prompt
        Orch->>GCS: save prompts/page_N_prompt.txt
        Orch->>Imagen: generate image
        Orch->>GCS: save images/page_N.png
    end
    Orch->>Orch: compose PDF
    Orch->>GCS: save final/storybook.pdf
    Orch->>API: signed_url + session_id
    API->>User: 200 OK {signed_url, session_id}
```

---

## Open Questions / Decisions Pending

- [ ] **Async vs sync API**: Long pipeline (~minutes) — use async job + polling endpoint, or streaming SSE?
- [ ] **Art style control**: Fixed styles (watercolor, cartoon, pencil sketch) or free-form prompt injection?
- [ ] **Reading level targeting**: Fixed tiers (ages 4–6, 7–9, 10–12) or Flesch-Kincaid scoring loop?
- [ ] **Multi-language**: English-only MVP or i18n from the start?
- [ ] **Source selection UI**: URL paste only, or a built-in Gutenberg search?
- [ ] **Page count**: Fixed (12 pages standard picture book) or derived from source length?

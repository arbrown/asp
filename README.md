# Storybook Agent

A multi-agent pipeline that adapts public-domain literature into illustrated children's storybooks, built on Google ADK and Vertex AI (Gemini + Imagen), deployable on GKE or Cloud Run.

## Project Structure

```
.
├── docs/
│   ├── architecture/       # Architecture diagrams and design docs
│   └── runbooks/           # Operational runbooks
├── src/
│   ├── agents/             # ADK agent definitions
│   ├── tools/              # Shared tool implementations
│   └── api/                # FastAPI service layer
├── k8s/                    # Kubernetes manifests
├── infra/
│   └── terraform/          # GCP infrastructure as code
└── scripts/                # Dev and operational scripts
```

## Architecture

```mermaid
flowchart TB
    %% ========================================================
    %% USER
    %% ========================================================
    User(["👤 <b>User / Reader</b>"])

    %% ========================================================
    %% INSIDE GKE CLUSTER
    %% ========================================================
    subgraph GKE ["☸️ <b>Inside GKE Cluster</b>"]
        direction TB

        UI["💻 <b>Frontend</b><br/>Web UI"]

        subgraph BACKEND ["<b>Backend Service</b>"]
            direction TB
            Main["🎭 <b>Main Orchestrator Agent</b><br/><i>Coordinates workflow & lifecycle</i>"]

            subgraph SUBAGENTS ["<b>Specialized In-Process Sub-Agents</b>"]
                direction LR
                Story["✍️ <b>Story Agent</b><br/>Adaptation & Bible"]
                Art["🎨 <b>Art Agent</b><br/>Illustration & Prompts"]
                Val["🔍 <b>Validator Agent</b><br/>Text & Vision Review"]
                Book["📕 <b>Compositor Agent</b><br/>PDF Book Assembly"]
                
                Story --> Art --> Val --> Book
            end

            Main ==>|"Dispatches & coordinates"| SUBAGENTS
        end

        UI -->|"Starts story generation"| Main
    end

    %% ========================================================
    %% OUTSIDE CLUSTER (GOOGLE CLOUD)
    %% ========================================================
    subgraph OUTSIDE ["☁️ <b>Outside Cluster (Google Cloud)</b>"]
        direction LR
        GCS[("🪣 <b>Cloud Storage (GCS)</b><br/>Intermediate drafts, bibles,<br/>images & final PDF")]
        Gemini["🧠 <b>Gemini & Imagen</b><br/>LLM reasoning, multimodal<br/>critique & image generation"]
    end

    %% ========================================================
    %% DATA & MODEL FLOWS
    %% ========================================================
    User --> UI
    
    SUBAGENTS <-.->|"Read / write intermediate state"| GCS
    SUBAGENTS <-.->|"Prompts, vision & generation"| Gemini

    %% ========================================================
    %% STYLING & CLASSES
    %% ========================================================
    classDef userStyle fill:#EFF6FF,stroke:#3B82F6,stroke-width:2px,color:#1E3A8A;
    classDef uiStyle fill:#F0FDFA,stroke:#0D9488,stroke-width:2px,color:#134E4A;
    classDef mainStyle fill:#4F46E5,stroke:#312E81,stroke-width:2.5px,color:#FFFFFF;
    classDef agentStyle fill:#FFFFFF,stroke:#6366F1,stroke-width:1.5px,color:#1E293B;
    classDef gcsStyle fill:#FEF3C7,stroke:#D97706,stroke-width:2px,color:#78350F;
    classDef aiStyle fill:#FAF5FF,stroke:#9333EA,stroke-width:2px,color:#581C87;

    class User userStyle;
    class UI uiStyle;
    class Main mainStyle;
    class Story,Art,Val,Book agentStyle;
    class GCS gcsStyle;
    class Gemini aiStyle;

    style GKE fill:#F8FAFC,stroke:#0284C7,stroke-width:2px;
    style BACKEND fill:#FFFFFF,stroke:#CBD5E1,stroke-width:1.5px;
    style SUBAGENTS fill:#F1F5F9,stroke:#E2E8F0,stroke-width:1px;
    style OUTSIDE fill:#FAF5FF,stroke:#A855F7,stroke-width:2px,stroke-dasharray: 4 4;
```

See [docs/architecture/architecture.md](docs/architecture/architecture.md) for the detailed pipeline diagrams and design notes.

## Quick Start

_Coming soon._

## Tech Stack

- **Agents**: [Google ADK](https://google.github.io/adk-docs/) (Python)
- **LLM**: Gemini 2.0 Flash / Pro via Vertex AI
- **Image generation**: Imagen 3 via Vertex AI
- **Storage**: Google Cloud Storage (session-scoped artifact folders)
- **Serving**: Cloud Run (MVP) / GKE (production)

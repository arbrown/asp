import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gcp_project_id: str
    gcp_region: str = "us-central1"
    gcs_artifacts_bucket: str
    app_domain: str = ""

    # Text models
    model_adapter: str = "gemini-3.5-flash"
    # 3.5 Pro hasn't shipped; 3.1 is still under the -preview suffix. Override via env when GA.
    model_craft: str = "gemini-3.1-pro-preview"
    model_fast: str = "gemini-3.5-flash"

    # Image model (Nano Banana 2)
    model_image: str = "gemini-3.1-flash-image"

    # Retry limits
    text_max_retries: int = 3
    image_max_retries: int = 2

    # Two-pass adaptation: draft → bible → craft re-adapt. Set false to skip the
    # draft pass and run the legacy single-adapter flow (cheaper, lower craft).
    text_two_pass: bool = True

    # Parallel image generation — max simultaneous generate_image() calls
    image_concurrency: int = 5

    # Max simultaneous LLM calls (prompt generation + validation combined)
    llm_concurrency: int = 6

    # rqlite database URL — defaults to the k8s ClusterIP service name
    rqlite_url: str = "http://rqlite:4001"

    # Agent Substrate configuration
    substrate_enabled: bool = True
    substrate_api_addr: str = "api.ate-system.svc:443"
    substrate_atespace: str = "asp"
    substrate_template: str = "asp-runner"
    substrate_ca_file: str = "/run/servicedns-ca/ca.crt"
    substrate_cred_bundle: str = "/run/podidentity.podcert.ate.dev/credential-bundle.pem"

    # Authentication & Multi-tenancy
    dev_auth_enabled: bool = True
    dev_default_email: str = "dev@storybook.local"
    legacy_owner_email: str = "owner@storybook.local"


settings = Settings()

# Direct google-genai (and ADK) to use Vertex AI instead of the Gemini API.
# Must be set before any agent module is imported — config.py is always first.
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"
os.environ["GOOGLE_CLOUD_PROJECT"] = settings.gcp_project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"  # newer Gemini models only on global endpoint

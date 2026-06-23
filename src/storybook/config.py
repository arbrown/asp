import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    gcp_project_id: str
    gcp_region: str = "us-central1"
    gcs_artifacts_bucket: str

    # Text models
    model_adapter: str = "gemini-3.5-flash"
    model_fast: str = "gemini-3.5-flash"

    # Image model (Nano Banana 2)
    model_image: str = "gemini-3.1-flash-image"

    # Retry limits
    text_max_retries: int = 3
    image_max_retries: int = 2

    # Parallel image generation — max simultaneous generate_image() calls
    image_concurrency: int = 3

    # Max simultaneous LLM calls (prompt generation + validation combined)
    llm_concurrency: int = 5

    # rqlite database URL — defaults to the k8s ClusterIP service name
    rqlite_url: str = "http://rqlite:4001"


settings = Settings()

# Direct google-genai (and ADK) to use Vertex AI instead of the Gemini API.
# Must be set before any agent module is imported — config.py is always first.
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"
os.environ["GOOGLE_CLOUD_PROJECT"] = settings.gcp_project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"  # newer Gemini models only on global endpoint

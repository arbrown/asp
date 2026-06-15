import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from storybook.api.routes import router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Storybook Agent API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tightened via GKE Ingress in production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


@app.get("/healthz")
async def health() -> dict:
    return {"status": "ok"}

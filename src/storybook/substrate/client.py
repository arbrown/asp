"""gRPC client for Agent Substrate (ateapi.Control)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import grpc
from google.protobuf import empty_pb2

from storybook.config import settings

_PROTO_DIR = str(Path(__file__).resolve().parent)
if _PROTO_DIR not in sys.path:
    sys.path.insert(0, _PROTO_DIR)
ateapi_pb2, ateapi_pb2_grpc = grpc.protos_and_services("ateapi.proto")

log = logging.getLogger(__name__)

SERVER_NAME = "api.ate-system.svc"

_PEM_BLOCK = re.compile(
    rb"-----BEGIN (?P<kind>[A-Z ]+)-----.*?-----END (?P=kind)-----\n?",
    re.DOTALL,
)


def _split_cred_bundle(bundle: bytes) -> tuple[bytes, bytes]:
    """Split a Kubernetes pod-certificate bundle into (private_key, cert_chain)."""
    key = None
    chain: list[bytes] = []
    for m in _PEM_BLOCK.finditer(bundle):
        if m.group("kind") == b"PRIVATE KEY":
            key = m.group(0)
        else:
            chain.append(m.group(0))
    if key is None:
        raise ValueError("credential bundle contains no PRIVATE KEY block")
    if not chain:
        raise ValueError("credential bundle contains no CERTIFICATE block")
    return key, b"".join(chain)


class SubstrateClient:
    """Thin async wrapper around the Agent Substrate Control gRPC service."""

    def __init__(self) -> None:
        self._local_procs: dict[str, subprocess.Popen] = {}

    def _open_channel(self) -> grpc.Channel:
        target = (
            os.environ.get("SUBSTRATE_API_ADDR")
            or settings.substrate_api_addr
        ).replace("http://", "").replace("https://", "")

        ca_file = os.environ.get("SUBSTRATE_CA_FILE") or settings.substrate_ca_file
        cred_bundle = (
            os.environ.get("SUBSTRATE_CRED_BUNDLE") or settings.substrate_cred_bundle
        )
        bearer_token = os.environ.get("SUBSTRATE_BEARER_TOKEN", "")

        options = [("grpc.ssl_target_name_override", SERVER_NAME)]

        if os.path.exists(ca_file) and os.path.exists(cred_bundle):
            with open(ca_file, "rb") as f:
                ca_cert = f.read()
            with open(cred_bundle, "rb") as f:
                private_key, cert_chain = _split_cred_bundle(f.read())
            creds = grpc.ssl_channel_credentials(
                root_certificates=ca_cert,
                private_key=private_key,
                certificate_chain=cert_chain,
            )
            return grpc.secure_channel(target, creds, options=options)

        if os.path.exists(ca_file) and bearer_token:
            with open(ca_file, "rb") as f:
                ca_cert = f.read()
            ssl_creds = grpc.ssl_channel_credentials(root_certificates=ca_cert)
            call_creds = grpc.access_token_call_credentials(bearer_token)
            creds = grpc.composite_channel_credentials(ssl_creds, call_creds)
            return grpc.secure_channel(target, creds, options=options)

        return grpc.insecure_channel(target)

    def _delete_actor_cleanly(self, stub: Any, actor_ref: Any) -> bool:
        try:
            actor = stub.GetActor(
                ateapi_pb2.GetActorRequest(actor=actor_ref),
                timeout=10.0,
            )
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                return False
            raise

        state_str = ateapi_pb2.ActorState.Name(actor.status.state)
        if state_str == "ACTOR_STATE_RUNNING":
            try:
                stub.SuspendActor(
                    ateapi_pb2.SuspendActorRequest(actor=actor_ref),
                    timeout=30.0,
                )
            except grpc.RpcError as exc:
                log.debug("SuspendActor before delete returned: %s", exc)

        try:
            stub.DeleteActor(
                ateapi_pb2.DeleteActorRequest(actor=actor_ref),
                timeout=30.0,
            )
            return True
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                return False
            raise

    def _create_actor_sync(
        self,
        template: str,
        atespace: str,
        name: str,
        resume: bool = False,
        user_email: str = "",
        downscoped_token: str | None = None,
    ) -> dict:
        if not settings.substrate_enabled:
            cmd = [sys.executable, "-m", "storybook.runner", "--session-id", name]
            if resume:
                cmd.append("--resume")
            env = os.environ.copy()
            if user_email:
                env["ASP_USER_EMAIL"] = user_email
            if downscoped_token:
                env["GCS_DOWNSCOPED_TOKEN"] = downscoped_token
            proc = subprocess.Popen(cmd, env=env)
            self._local_procs[name] = proc
            return {"name": name, "atespace": atespace, "state": "ACTOR_STATE_RUNNING"}

        with self._open_channel() as channel:
            stub = ateapi_pb2_grpc.ControlStub(channel)
            actor_ref = ateapi_pb2.ObjectRef(atespace=atespace, name=name)

            # 1. Ensure atespace exists
            try:
                stub.CreateAtespace(
                    ateapi_pb2.CreateAtespaceRequest(
                        atespace=ateapi_pb2.Atespace(
                            metadata=ateapi_pb2.ResourceMetadata(name=atespace)
                        )
                    ),
                    timeout=10.0,
                )
            except grpc.RpcError as exc:
                if exc.code() != grpc.StatusCode.ALREADY_EXISTS:
                    raise

            # 2. If actor already exists (e.g. on resume or re-run), delete it first
            try:
                self._delete_actor_cleanly(stub, actor_ref)
            except grpc.RpcError as exc:
                log.warning("Pre-create check for actor %s/%s: %s", atespace, name, exc)

            # 3. Create Actor from ActorTemplate
            t_create = time.monotonic()
            stub.CreateActor(
                ateapi_pb2.CreateActorRequest(
                    actor=ateapi_pb2.Actor(
                        metadata=ateapi_pb2.ResourceMetadata(
                            atespace=atespace,
                            name=name,
                        ),
                        actor_template=ateapi_pb2.ObjectRef(
                            atespace=atespace,
                            name=template,
                        ),
                    )
                ),
                timeout=30.0,
            )

            # 4. Attach EgressPolicy so the actor can reach Vertex AI, GCS, Gutenberg, and rqlite
            try:
                stub.CreateActorEgressPolicy(
                    ateapi_pb2.CreateActorEgressPolicyRequest(
                        actor=actor_ref,
                        egress_policy=ateapi_pb2.EgressPolicy(
                            metadata=ateapi_pb2.ResourceMetadata(
                                atespace=atespace,
                                name="default",
                            ),
                            rules=[ateapi_pb2.EgressRule(all=empty_pb2.Empty())],
                        ),
                    ),
                    timeout=10.0,
                )
            except grpc.RpcError as exc:
                if exc.code() != grpc.StatusCode.ALREADY_EXISTS:
                    raise

            # 5. Resume Actor onto a warm worker pod in WorkerPool
            t_resume = time.monotonic()
            resp = stub.ResumeActor(
                ateapi_pb2.ResumeActorRequest(actor=actor_ref),
                timeout=60.0,
            )
            now_mono = time.monotonic()
            elapsed_ms = (now_mono - t_create) * 1000.0
            resume_ms = (now_mono - t_resume) * 1000.0
            actor = resp.actor
            worker_pod = (
                actor.status.worker_assignment.worker_pod
                if actor.status.HasField("worker_assignment")
                else ""
            )
            state_str = ateapi_pb2.ActorState.Name(actor.status.state)
            log.info(
                "Provisioned and resumed Substrate actor %s/%s from template %s in %.1f ms (resume_ms=%.1f, state=%s, worker_pod=%s, resume=%s)",
                atespace,
                name,
                template,
                elapsed_ms,
                resume_ms,
                state_str,
                worker_pod,
                resume,
            )
            return {
                "name": name,
                "atespace": atespace,
                "uid": actor.metadata.uid,
                "state": state_str,
                "worker_pod": worker_pod,
                "startup_ms": round(elapsed_ms, 1),
                "resume_ms": round(resume_ms, 1),
            }

    async def create_actor(
        self,
        template: str = "asp-runner",
        atespace: str = "asp",
        name: str = "",
        resume: bool = False,
        user_email: str = "",
        downscoped_token: str | None = None,
    ) -> dict:
        """Create and resume a dedicated Substrate Actor for the given session."""
        if settings.substrate_enabled and name and (user_email or downscoped_token):
            from storybook.db import store

            try:
                await store.save_actor_credentials(name, user_email, downscoped_token)
            except Exception as exc:
                log.warning("Could not persist actor credentials for %s: %s", name, exc)

        return await asyncio.to_thread(
            self._create_actor_sync,
            template,
            atespace,
            name,
            resume,
            user_email,
            downscoped_token,
        )

    def _stop_actor_sync(self, name: str, atespace: str) -> bool:
        if not settings.substrate_enabled:
            proc = self._local_procs.pop(name, None)
            if proc and proc.poll() is None:
                proc.terminate()
                return True
            return False

        with self._open_channel() as channel:
            stub = ateapi_pb2_grpc.ControlStub(channel)
            actor_ref = ateapi_pb2.ObjectRef(atespace=atespace, name=name)
            deleted = self._delete_actor_cleanly(stub, actor_ref)
            if deleted:
                log.info("Stopped and deleted Substrate actor %s/%s", atespace, name)
            return deleted

    async def stop_actor(self, name: str, atespace: str = "asp") -> bool:
        """Stop and delete an active Substrate Actor."""
        return await asyncio.to_thread(self._stop_actor_sync, name, atespace)


ate = SubstrateClient()

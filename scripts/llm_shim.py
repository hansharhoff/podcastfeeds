"""Tiny HTTP wrapper around the local `claude` CLI, for the podcastfeeds
container (which has no Claude auth of its own).

Runs on the WSL2 host:  .venv/bin/python scripts/llm_shim.py
Listens on 0.0.0.0:8765 (so the container can reach it via
http://host.docker.internal:8765) but _is_local() rejects every caller outside
loopback and the Docker bridge (172.16/12) — so LAN and tailnet peers get 403
even though the port is bound on those interfaces.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import tempfile

import uvicorn
from fastapi import FastAPI, HTTPException, Request

MODEL = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")
PORT = int(os.environ.get("LLM_SHIM_PORT", "8765"))

# Isolate the CLI from Hans' personal Claude Code setup. Without this the CLI
# loads ~/.claude: the SessionStart hook ("if there is even a 1% chance a skill
# applies you MUST invoke it") plus the whole skills roster. A book-brief prompt
# reads like a creative-work request, so the brainstorming skill fired and the
# CLI answered ABOUT the skill instead of writing the brief — that meta-text was
# narrated as episode 337 (2026-07-28).
#   --setting-sources ""      no user/project/local settings, so no hooks
#   --disable-slash-commands  no skills
# NOT --bare: it reads auth strictly from ANTHROPIC_API_KEY/apiKeyHelper and
# never OAuth, and this host has no API key — the shim runs on the subscription.
ISOLATION = ["--setting-sources", "", "--disable-slash-commands"]
PIPELINE_ROLE = (
    "You are a text generation service for an automated podcast pipeline. "
    "Return only the requested text, ready to be spoken aloud. Never mention "
    "skills, tools, files, permissions, or your own configuration, and never "
    "ask the caller a question — there is no interactive user on the other end."
)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _is_local(host: str) -> bool:
    """Allow only loopback and the Docker bridge range (172.16.0.0/12), which is
    where the podcastfeeds container's requests originate. This deliberately
    excludes the LAN (192.168/16, 10/8) and the tailnet (100.64/10) — the shim
    binds 0.0.0.0 so it is reachable on those interfaces, but must not serve them."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip in ipaddress.ip_network("172.16.0.0/12")


@app.post("/v1/complete")
async def complete(request: Request):
    if not _is_local(request.client.host):
        raise HTTPException(status_code=403)
    data = await request.json()
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt required")
    cmd = [
        "claude", "-p", prompt, "--model", data.get("model") or MODEL,
        *ISOLATION, "--append-system-prompt", PIPELINE_ROLE,
    ]
    tools = data.get("allowed_tools") or []
    if tools:  # e.g. ["WebSearch"] — nothing else is ever granted
        cmd += ["--allowedTools", ",".join(str(t) for t in tools)]
    env = dict(os.environ)
    if data.get("thinking"):
        env["MAX_THINKING_TOKENS"] = str(data.get("thinking_tokens") or 10000)
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail="claude CLI timed out") from None
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=stderr.decode()[-300:])
    return {"text": stdout.decode().strip()}


@app.post("/v1/vision")
async def vision(request: Request):
    """{prompt, image_b64, mime} -> {text}. Writes the image to a temp file and
    lets the claude CLI read it (--allowedTools Read)."""
    if not _is_local(request.client.host):
        raise HTTPException(status_code=403)
    data = await request.json()
    prompt = (data.get("prompt") or "").strip()
    image_b64 = data.get("image_b64") or ""
    if not prompt or not image_b64:
        raise HTTPException(status_code=400, detail="prompt and image_b64 required")
    suffix = ".png" if "png" in (data.get("mime") or "") else ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(base64.b64decode(image_b64))
        path = f.name
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", f"First use the Read tool on the image file {path}, then:\n{prompt}",
            "--model", data.get("model") or MODEL, "--allowedTools", "Read",
            *ISOLATION, "--append-system-prompt", PIPELINE_ROLE,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except TimeoutError:
            proc.kill()
            raise HTTPException(status_code=504, detail="claude CLI timed out") from None
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=stderr.decode()[-300:])
    return {"text": stdout.decode().strip()}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")

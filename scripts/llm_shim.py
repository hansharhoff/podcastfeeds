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
import logging
import os
import tempfile
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request

MODEL = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")
PORT = int(os.environ.get("LLM_SHIM_PORT", "8765"))

# Every call is a subprocess spawn, so shim latency is invisible from inside
# the container — and stayed invisible for weeks while an unclosed stdin cost
# 3 seconds per call (found by accident, 2026-07-28). Log to a FILE as well as
# stderr: this host has no usable session D-Bus, so `journalctl --user` is not
# a reliable way to read the service's output.
LOG_FILE = Path(__file__).resolve().parent.parent / "data" / "llm_shim.log"
log = logging.getLogger("llm_shim")


def _setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    log.addHandler(stream)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(LOG_FILE)
        file_handler.setFormatter(fmt)
        log.addHandler(file_handler)
    except OSError as exc:  # read-only dir etc. — stderr logging still stands
        log.warning("no shim log file at %s: %s", LOG_FILE, exc)

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


async def _run_cli(cmd: list[str], *, kind: str, model: str, prompt_chars: int,
                   timeout: int, env: dict | None = None) -> str:
    """Spawn the claude CLI, timing the call and logging the outcome.

    stdin is DEVNULL, not inherited: the CLI otherwise waits on input that
    never comes and every call pays a flat 3-second stall."""
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        log.error("%s TIMEOUT after %.1fs (model=%s, prompt=%d chars)",
                  kind, time.monotonic() - started, model, prompt_chars)
        raise HTTPException(status_code=504, detail="claude CLI timed out") from None
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        detail = stderr.decode()[-300:]
        log.error("%s FAILED rc=%s in %.1fs (model=%s, prompt=%d chars): %s",
                  kind, proc.returncode, elapsed, model, prompt_chars, detail)
        raise HTTPException(status_code=502, detail=detail)
    text = stdout.decode().strip()
    log.info("%s ok in %.1fs (model=%s, prompt=%d chars, reply=%d chars)",
             kind, elapsed, model, prompt_chars, len(text))
    return text


@app.post("/v1/complete")
async def complete(request: Request):
    if not _is_local(request.client.host):
        raise HTTPException(status_code=403)
    data = await request.json()
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt required")
    model = data.get("model") or MODEL
    cmd = [
        "claude", "-p", prompt, "--model", model,
        *ISOLATION, "--append-system-prompt", PIPELINE_ROLE,
    ]
    tools = data.get("allowed_tools") or []
    if tools:  # e.g. ["WebSearch"] — nothing else is ever granted
        cmd += ["--allowedTools", ",".join(str(t) for t in tools)]
    env = dict(os.environ)
    if data.get("thinking"):
        env["MAX_THINKING_TOKENS"] = str(data.get("thinking_tokens") or 10000)
    text = await _run_cli(cmd, kind="complete", model=model,
                          prompt_chars=len(prompt), timeout=600, env=env)
    return {"text": text}


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
    model = data.get("model") or MODEL
    cmd = [
        "claude", "-p",
        f"First use the Read tool on the image file {path}, then:\n{prompt}",
        "--model", model, "--allowedTools", "Read",
        *ISOLATION, "--append-system-prompt", PIPELINE_ROLE,
    ]
    try:
        text = await _run_cli(cmd, kind="vision", model=model,
                              prompt_chars=len(prompt), timeout=300)
    finally:
        os.unlink(path)
    return {"text": text}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


if __name__ == "__main__":
    _setup_logging()
    log.info("llm shim listening on :%d (model=%s, log=%s)", PORT, MODEL, LOG_FILE)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")

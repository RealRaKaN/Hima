"""Minimal client for Ollama POST /api/generate (non-streaming, structured output).

Docs:
  https://docs.ollama.com/api/generate
  https://docs.ollama.com/capabilities/structured-outputs
keep_alive, loading and unloading a model (generate with no prompt), GET /api/ps: same docs, checked
against the server itself (Ollama 0.40.5).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from hima.config import Llm


class LlmUnavailable(RuntimeError):
    """Server unreachable, timed out, or returned something unusable."""


@dataclass(frozen=True)
class LlmReply:
    text: str
    latency_s: float
    prompt_tokens: int | None = None      # Ollama's prompt_eval_count: how much of the prompt the model read


def build_payload(prompt: str, schema: dict[str, Any], llm: Llm, images: list[str] | None = None,
                  num_ctx: int | None = None, keep_alive_s: float | None = None) -> dict[str, Any]:
    """images: base64 strings (Ollama /api/generate "images" field, for vision models); num_ctx: context size
    option (None = the model's default); keep_alive_s: how long the model stays loaded after the call (None =
    the server's default, 5 min)."""
    payload = {
        "model": llm.model,
        "prompt": prompt,
        "format": schema,
        "stream": False,
        "options": {"temperature": llm.temperature, "seed": llm.seed},
    }
    if images:
        payload["images"] = list(images)
    if num_ctx is not None:
        payload["options"]["num_ctx"] = int(num_ctx)
    if keep_alive_s is not None:
        payload["keep_alive"] = f"{int(keep_alive_s)}s"
    return payload


def generate(prompt: str, schema: dict[str, Any], llm: Llm, *,
             post: Callable[..., Any] = requests.post, images: list[str] | None = None,
             num_ctx: int | None = None, keep_alive_s: float | None = None) -> LlmReply:
    payload = build_payload(prompt, schema, llm, images, num_ctx, keep_alive_s)
    t0 = time.perf_counter()
    try:
        r = post(llm.url, json=payload, timeout=llm.timeout_s)
    except requests.Timeout as e:
        raise LlmUnavailable(f"timeout after {llm.timeout_s} s") from e
    except requests.RequestException as e:
        raise LlmUnavailable(f"cannot reach {llm.url}: {type(e).__name__}") from e
    latency = time.perf_counter() - t0
    if r.status_code != 200:
        raise LlmUnavailable(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        body = r.json()
    except ValueError as e:
        raise LlmUnavailable("server reply is not JSON") from e
    text = body.get("response") if isinstance(body, dict) else None
    if not isinstance(text, str):
        raise LlmUnavailable("server reply has no 'response' text")
    n = body.get("prompt_eval_count")
    return LlmReply(text, latency, n if isinstance(n, int) else None)


def base_url(llm: Llm) -> str:
    """http://host:port of the server, from [llm].url (.../api/generate)."""
    return llm.url.split("/api/", 1)[0]


def loaded_models(llm: Llm, *, get: Callable[..., Any] = requests.get) -> list[dict[str, Any]]:
    """GET /api/ps: the models in memory now, as [{"name", "vram_mib"}]."""
    try:
        r = get(base_url(llm) + "/api/ps", timeout=10)
        models = r.json()["models"] if r.status_code == 200 else None
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        raise LlmUnavailable(f"cannot list loaded models: {type(e).__name__}") from e
    if not isinstance(models, list):
        raise LlmUnavailable(f"cannot list loaded models: HTTP {r.status_code}")
    return [{"name": m.get("name") or m.get("model"), "vram_mib": int(m.get("size_vram", 0)) // 2**20}
            for m in models if isinstance(m, dict)]


def set_loaded(model: str, llm: Llm, keep_alive_s: float, *, post: Callable[..., Any] = requests.post,
               timeout_s: float = 120.0) -> float:
    """Load a model and keep it keep_alive_s (generate with no prompt), or unload it (keep_alive_s = 0).
    Returns the seconds it took; raises LlmUnavailable."""
    t0 = time.perf_counter()
    try:
        r = post(llm.url, json={"model": model, "keep_alive": f"{int(keep_alive_s)}s"}, timeout=timeout_s)
    except requests.RequestException as e:
        raise LlmUnavailable(f"cannot {'load' if keep_alive_s else 'unload'} {model}: {type(e).__name__}") from e
    if r.status_code != 200:
        raise LlmUnavailable(f"{model}: HTTP {r.status_code}: {r.text[:200]}")
    return time.perf_counter() - t0

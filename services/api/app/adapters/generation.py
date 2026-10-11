import base64
import io
import logging
import time
from dataclasses import dataclass

import httpx

from app.core.config import Settings
from app.core.errors import AppError
from app.services.images import render_mock_edit

logger = logging.getLogger("streetcloud.generation")


@dataclass(frozen=True)
class GenerationInput:
    job_id: str
    source_bytes: bytes
    compiled_prompt: str
    negative_prompt: str | None
    resolution_preset: str
    model_id: str
    quality_preset: str
    provider: str


@dataclass(frozen=True)
class GenerationOutput:
    image_bytes: bytes
    mime_type: str
    provider: str
    model_id: str
    width: int
    height: int
    provider_request_id: str | None
    generation_duration_ms: int
    demo_labeled: bool


class MockGenerationAdapter:
    async def generate(self, req: GenerationInput, version_no: int) -> GenerationOutput:
        started = time.perf_counter()
        image = render_mock_edit(req.source_bytes, version_no, worker_label=None)
        return GenerationOutput(
            image_bytes=image,
            mime_type="image/jpeg",
            provider="mock",
            model_id="mock-demo",
            width=0,
            height=0,
            provider_request_id=None,
            generation_duration_ms=int((time.perf_counter() - started) * 1000),
            demo_labeled=True,
        )

    async def health(self) -> dict:
        return {"provider": "mock", "configured": True, "demoOnly": True}


class OpenAIGenerationAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def generate(self, req: GenerationInput, version_no: int) -> GenerationOutput:
        if not self.settings.openai_image_ready:
            raise AppError(
                "PROVIDER_NOT_CONFIGURED",
                "Set OPENAI_API_KEY and OPENAI_IMAGE_MODEL on the API server.",
                409,
            )
        from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

        quality = {"fast": "low", "balanced": "medium", "higher": "high"}.get(req.quality_preset, "medium")
        model = req.model_id or self.settings.openai_image_model
        kwargs: dict = {
            "model": model,
            "image": ("street.jpg", io.BytesIO(req.source_bytes), "image/jpeg"),
            "prompt": req.compiled_prompt[:32000],
            "size": "auto",
            "quality": quality,
            "output_format": "jpeg",
            "n": 1,
        }
        # input_fidelity is documented for gpt-image-1 family, not gpt-image-2.
        if model.startswith("gpt-image-1") and "mini" not in model:
            kwargs["input_fidelity"] = "high"
        started = time.perf_counter()
        client = AsyncOpenAI(api_key=self.settings.openai_api_key, timeout=180)
        try:
            result = await client.images.edit(**kwargs)
        except APITimeoutError as exc:
            raise AppError("GENERATION_TIMED_OUT", "The image provider timed out. You can retry.", 504, retryable=True) from exc
        except APIConnectionError as exc:
            raise AppError("NETWORK_UNAVAILABLE", "The image provider could not be reached.", 503, retryable=True) from exc
        except APIStatusError as exc:
            code = "PROVIDER_RATE_LIMITED" if exc.status_code == 429 else "GENERATION_INVALID_OUTPUT"
            if exc.status_code in {401, 403}:
                code = "PROVIDER_NOT_CONFIGURED"
            logger.warning("openai image edit failed status=%s", exc.status_code)
            raise AppError(code, "The image provider rejected this edit.", exc.status_code, retryable=exc.status_code == 429) from exc
        data = result.data[0]
        raw = data.b64_json
        if not raw:
            raise AppError("GENERATION_INVALID_OUTPUT", "The provider returned no image.", 502)
        return GenerationOutput(
            image_bytes=base64.b64decode(raw),
            mime_type="image/jpeg",
            provider="openai",
            model_id=model,
            width=0,
            height=0,
            provider_request_id=getattr(result, "created", None) and str(result.created),
            generation_duration_ms=int((time.perf_counter() - started) * 1000),
            demo_labeled=False,
        )

    async def health(self) -> dict:
        return {"provider": "openai", "configured": self.settings.openai_image_ready}


class GeminiGenerationAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def generate(self, req: GenerationInput, version_no: int) -> GenerationOutput:
        if not self.settings.gemini_image_ready:
            raise AppError(
                "PROVIDER_NOT_CONFIGURED",
                "Set GOOGLE_API_KEY and GEMINI_IMAGE_MODEL on the API server.",
                409,
            )
        started = time.perf_counter()
        model = req.model_id or self.settings.gemini_image_model
        use_interactions = self.settings.gemini_image_api == "interactions" or (
            self.settings.gemini_image_api == "auto" and "interaction" in model
        )
        try:
            if use_interactions:
                image = await self._interactions(model, req)
            else:
                image = await self._generate_content(model, req)
        except AppError:
            raise
        except Exception as exc:
            logger.warning("gemini image edit failed: %s", exc.__class__.__name__)
            raise AppError("GENERATION_INVALID_OUTPUT", "Gemini did not return an edited image.", 502) from exc
        return GenerationOutput(
            image_bytes=image,
            mime_type="image/jpeg",
            provider="gemini",
            model_id=model,
            width=0,
            height=0,
            provider_request_id=None,
            generation_duration_ms=int((time.perf_counter() - started) * 1000),
            demo_labeled=False,
        )

    async def _generate_content(self, model: str, req: GenerationInput) -> bytes:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.settings.google_api_key)
        response = await client.aio.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=req.source_bytes, mime_type="image/jpeg"),
                req.compiled_prompt,
            ],
            config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]),
        )
        return _first_inline_image(response)

    async def _interactions(self, model: str, req: GenerationInput) -> bytes:
        from google import genai

        client = genai.Client(api_key=self.settings.google_api_key)
        interaction = await client.aio.interactions.create(
            model=model,
            input=[
                {"type": "text", "text": req.compiled_prompt},
                {
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(req.source_bytes).decode("ascii"),
                },
            ],
            response_modalities=["IMAGE", "TEXT"],
        )
        return _first_inline_image(interaction)

    async def health(self) -> dict:
        return {"provider": "gemini", "configured": self.settings.gemini_image_ready}


class HostedGpuAdapter:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client

    async def generate(self, req: GenerationInput, version_no: int) -> GenerationOutput:
        if not self.settings.hosted_gpu_ready:
            raise AppError(
                "PROVIDER_NOT_CONFIGURED",
                "Configure hosted GPU to enable.",
                409,
            )
        started = time.perf_counter()
        headers = {}
        if self.settings.hosted_gpu_api_key:
            headers["Authorization"] = f"Bearer {self.settings.hosted_gpu_api_key}"
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=180)
        try:
            response = await client.post(
                self.settings.hosted_gpu_endpoint.rstrip("/") + "/generate",
                files={"image": ("street.jpg", req.source_bytes, "image/jpeg")},
                data={"prompt": req.compiled_prompt, "model": req.model_id},
                headers=headers,
            )
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code in {401, 403}:
            raise AppError("PROVIDER_NOT_CONFIGURED", "Hosted GPU rejected the credentials.", 502)
        if response.status_code >= 400:
            raise AppError("GENERATION_INVALID_OUTPUT", "Hosted GPU did not return an image.", 502, retryable=response.status_code >= 500)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("image/"):
            payload = response.content
        else:
            body = response.json()
            encoded = body.get("imageBase64") or body.get("image_base64")
            if not encoded:
                raise AppError("GENERATION_INVALID_OUTPUT", "Hosted GPU response had no image.", 502)
            payload = base64.b64decode(encoded)
        return GenerationOutput(
            image_bytes=payload,
            mime_type="image/jpeg",
            provider="comfyui_hosted",
            model_id=req.model_id or "hosted",
            width=0,
            height=0,
            provider_request_id=None,
            generation_duration_ms=int((time.perf_counter() - started) * 1000),
            demo_labeled=False,
        )

    async def health(self) -> dict:
        return {
            "provider": "comfyui_hosted",
            "configured": self.settings.hosted_gpu_ready,
            "notice": None if self.settings.hosted_gpu_ready else "Configure hosted GPU to enable.",
        }


def _first_inline_image(response: object) -> bytes:
    candidates = getattr(response, "candidates", None) or []
    parts = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts.extend(getattr(content, "parts", None) or [])
    parts.extend(getattr(response, "parts", None) or [])
    outputs = getattr(response, "outputs", None) or getattr(response, "output", None) or []
    if isinstance(outputs, list):
        parts.extend(outputs)
    for part in parts:
        inline = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
        data = None
        if inline is not None:
            data = getattr(inline, "data", None)
        if data is None:
            data = getattr(part, "data", None)
        if isinstance(data, str) and data:
            return base64.b64decode(data)
        if isinstance(data, (bytes, bytearray)) and data:
            return bytes(data)
    raise AppError("GENERATION_INVALID_OUTPUT", "The model response did not include an image.", 502)


def adapter_for(provider: str, settings: Settings):
    if provider == "mock":
        return MockGenerationAdapter()
    if provider == "openai":
        return OpenAIGenerationAdapter(settings)
    if provider == "gemini":
        return GeminiGenerationAdapter(settings)
    if provider == "comfyui_hosted":
        return HostedGpuAdapter(settings)
    raise AppError("PROVIDER_NOT_CONFIGURED", f"No in-process adapter for {provider}.", 400)


async def run_adapter(provider: str, settings: Settings, req: GenerationInput, version_no: int) -> GenerationOutput:
    adapter = adapter_for(provider, settings)
    return await adapter.generate(req, version_no)


def provider_catalog(settings: Settings) -> dict:
    mock_ok = settings.environment != "production"
    return {
        "image": [
            {
                "id": "mock",
                "label": "Local mock",
                "configured": mock_ok,
                "demoOnly": True,
                "notice": "DEMO / MOCK RESULT. This does not call an image model.",
            },
            {
                "id": "openai",
                "label": "OpenAI",
                "configured": settings.openai_image_ready,
                "demoOnly": False,
                "model": settings.openai_image_model or None,
                "notice": None
                if settings.openai_image_ready
                else "Set OPENAI_API_KEY and OPENAI_IMAGE_MODEL on the API server.",
            },
            {
                "id": "gemini",
                "label": "Gemini",
                "configured": settings.gemini_image_ready,
                "demoOnly": False,
                "model": settings.gemini_image_model or None,
                "notice": None
                if settings.gemini_image_ready
                else "Set GOOGLE_API_KEY and GEMINI_IMAGE_MODEL on the API server.",
            },
            {
                "id": "comfyui_hosted",
                "label": "Hosted GPU",
                "configured": settings.hosted_gpu_ready,
                "demoOnly": False,
                "notice": None if settings.hosted_gpu_ready else "Configure hosted GPU to enable.",
            },
        ],
        "evaluation": [
            {
                "id": "demo_fixture",
                "label": "Demo fixture",
                "configured": True,
                "demoOnly": True,
                "notice": "Authored demonstration scores. Not a vision model.",
            },
            {
                "id": "openai_vision",
                "label": "Cloud vision (OpenAI)",
                "configured": settings.openai_vision_ready,
                "demoOnly": False,
                "model": settings.openai_vision_model or None,
                "notice": None
                if settings.openai_vision_ready
                else "Set OPENAI_API_KEY and OPENAI_VISION_MODEL.",
            },
            {
                "id": "gemini_vision",
                "label": "Cloud vision (Gemini)",
                "configured": settings.gemini_vision_ready,
                "demoOnly": False,
                "model": settings.gemini_vision_model or None,
                "notice": None
                if settings.gemini_vision_ready
                else "Set GOOGLE_API_KEY and GEMINI_VISION_MODEL.",
            },
            {
                "id": "ollama_vision",
                "label": "Local vision model",
                "configured": settings.ollama_vision_ready,
                "demoOnly": False,
                "model": settings.ollama_vision_model or None,
                "notice": None
                if settings.ollama_vision_ready
                else "Set OLLAMA_VISION_MODEL to a vision model served on this computer.",
            },
            {
                "id": "private_vlm",
                "label": "Private vision model",
                "configured": False,
                "demoOnly": False,
                "notice": "Enabled only after a worker reports a tested vision model.",
            },
            {
                "id": "disabled",
                "label": "Evaluation disabled",
                "configured": True,
                "demoOnly": False,
                "notice": "No scores will be requested until you turn evaluation on.",
            },
        ],
    }


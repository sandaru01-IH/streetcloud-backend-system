import base64
import json
import logging

import httpx

from app.core.config import Settings
from app.core.errors import AppError
from app.services.rubric import (
    LIMITATION_BANNER,
    PROPERTIES,
    PROPERTY_BY_KEY,
    RUBRIC_VERSION,
    mean_assessable,
    validate_assessments,
)

logger = logging.getLogger("streetcloud.evaluation")

EVAL_PROMPT = """You are scoring ONE street photograph against Christopher Alexander's
15 properties of living structure. Judge only what is visible in the TARGET
image. A second image, if present, is the previous photograph for context.
Every score describes the TARGET.

This is a visual reading, not a survey. Do not invent measurements, traffic
counts, or legal compliance. Use each criterion below as the definition of
that property. Compare the street you see with that definition.

Score every property from 0 to 5. applicability is always "assessable".
score is always a number, never null.
0 = the property is visually absent.
1 = a weak trace.
2 = partly present.
3 = clearly present.
4 = strong and organizing.
5 = the property structures the whole view.
Be critical. More greenery alone is not a high score. A plain road can score
high on the void and low on thick boundaries.

Return ONLY JSON with rubric_version, assessments, summary, limitations.
assessments is an array of exactly 15 objects, one per key, each with its own
key field. Do not nest properties inside one object.
Each object: key, applicability ("assessable"), score (number 0-5),
confidence (number 0-1 or null), visual_evidence (one sentence about this
photo), limitation (one sentence).

Criteria:
{criteria}
"""


def evaluation_prompt() -> str:
    criteria = "\n".join(f"- {item['key']}: {item['name']}. {item['cue']}" for item in PROPERTIES)
    return EVAL_PROMPT.format(criteria=criteria)


def fixture_evaluation() -> dict:
    """Fixed authored panel so the demo pipeline can render 15 rows.

    These numbers are not a vision assessment of any photograph.
    """
    assessments = []
    for index, prop in enumerate(PROPERTIES):
        assessments.append(
            {
                "key": prop["key"],
                "applicability": "assessable",
                "score": round(2 + (index % 3) * 0.5, 2),
                "confidence": None,
                "visual_evidence": (
                    f"Fixture: placeholder note for {prop['name']}. "
                    "Not a vision-model reading of this image."
                ),
                "limitation": "Demonstration fixture. No photograph was assessed.",
            }
        )
    validated = validate_assessments(assessments)
    mean, count = mean_assessable(validated)
    return {
        "rubric_version": RUBRIC_VERSION,
        "origin": "authored_fixture",
        "model": "authored-demo-fixture",
        "summary": (
            "Demonstration fixture attached so the evaluation panel can be reviewed. "
            "These numbers are not a vision assessment of this photograph."
        ),
        "limitations": [LIMITATION_BANNER, "Origin: authored fixture, not a vision model."],
        "assessments": validated,
        "mean": mean,
        "assessed_count": count,
    }


def evaluation_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["rubric_version", "assessments", "summary", "limitations"],
        "properties": {
            "rubric_version": {"type": "string"},
            "summary": {"type": "string"},
            "limitations": {"type": "array", "items": {"type": "string"}},
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "applicability", "score", "confidence", "visual_evidence", "limitation"],
                    "properties": {
                        "key": {"type": "string"},
                        "applicability": {"type": "string", "enum": ["assessable"]},
                        "score": {"type": "number", "minimum": 0, "maximum": 5},
                        "confidence": {"type": ["number", "null"]},
                        "visual_evidence": {"type": "string"},
                        "limitation": {"type": "string"},
                    },
                },
            },
        },
    }


def _alias_key(raw_key: str) -> str:
    text = str(raw_key or "").strip().lower().replace("-", " ").replace("_", " ")
    text = "_".join(text.split())
    if text in PROPERTY_BY_KEY:
        return text
    for prop in PROPERTIES:
        if prop["name"].lower() == str(raw_key or "").strip().lower():
            return prop["key"]
    return text


def _coerce_number(value: object) -> object:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped.lower() in {"null", "none", "n/a", "na"}:
        return None
    try:
        return float(stripped)
    except ValueError:
        return value


def _as_assessment(key: str, value: object) -> dict:
    if isinstance(value, dict):
        row = dict(value)
        row.setdefault("key", key)
        return row
    return {"key": key, "score": value}


def _flatten_assessments(raw: object) -> list[dict]:
    if isinstance(raw, dict):
        if any(field in raw for field in ("key", "score", "visual_evidence", "evidence", "applicability")):
            return [raw]
        return [_as_assessment(str(key), value) for key, value in raw.items()]
    if isinstance(raw, list):
        flat: list[dict] = []
        for item in raw:
            flat.extend(_flatten_assessments(item))
        return flat
    raise ValueError("assessments must be a list")


def _normalize_assessments(raw: object) -> list[dict]:
    normalized: list[dict] = []
    for item in _flatten_assessments(raw):
        row = dict(item)
        row["key"] = _alias_key(str(row.get("key") or ""))
        row["score"] = _coerce_number(row.get("score"))
        row["confidence"] = _coerce_number(row.get("confidence"))
        if row.get("applicability") not in {"assessable", "not_assessable"}:
            row["applicability"] = "not_assessable" if row["score"] is None else "assessable"
        normalized.append(row)
    return normalized


def parse_model_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.removeprefix("json").strip()
    data = json.loads(cleaned)
    assessments = validate_assessments(_normalize_assessments(data.get("assessments")))
    mean, count = mean_assessable(assessments)
    limitations = [str(item) for item in data.get("limitations") or []]
    if LIMITATION_BANNER not in limitations:
        limitations.append(LIMITATION_BANNER)
    return {
        "rubric_version": RUBRIC_VERSION,
        "origin": "vision_model",
        "model": "",
        "summary": str(data.get("summary") or "").strip() or "The model did not provide a summary.",
        "limitations": limitations,
        "assessments": assessments,
        "mean": mean,
        "assessed_count": count,
    }


class OpenAIVisionAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def evaluate(self, target: bytes, baseline: bytes | None, model_id: str) -> dict:
        if not self.settings.openai_vision_ready:
            raise AppError(
                "PROVIDER_NOT_CONFIGURED",
                "Set OPENAI_API_KEY and OPENAI_VISION_MODEL on the API server.",
                409,
            )
        from openai import AsyncOpenAI

        prompt = evaluation_prompt()
        content: list[dict] = [{"type": "input_text", "text": prompt + "\nTARGET image follows."}]
        content.append(
            {
                "type": "input_image",
                "detail": "auto",
                "image_url": "data:image/jpeg;base64," + base64.b64encode(target).decode("ascii"),
            }
        )
        if baseline:
            content.append({"type": "input_text", "text": "BASELINE image follows for context only."})
            content.append(
                {
                    "type": "input_image",
                    "detail": "auto",
                    "image_url": "data:image/jpeg;base64," + base64.b64encode(baseline).decode("ascii"),
                }
            )
        client = AsyncOpenAI(api_key=self.settings.openai_api_key, timeout=120)
        model = model_id or self.settings.openai_vision_model
        response = await client.responses.create(
            model=model,
            input=[{"role": "user", "content": content}],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "alexander_street_v1",
                    "strict": True,
                    "schema": evaluation_schema(),
                }
            },
        )
        try:
            parsed = parse_model_json(response.output_text)
        except Exception as exc:
            logger.warning("openai vision JSON rejected: %s", exc.__class__.__name__)
            raise AppError("VISION_EVALUATION_FAILED", "The vision model returned an invalid rubric.", 502) from exc
        parsed["model"] = model
        return parsed


class GeminiVisionAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def evaluate(self, target: bytes, baseline: bytes | None, model_id: str) -> dict:
        if not self.settings.gemini_vision_ready:
            raise AppError(
                "PROVIDER_NOT_CONFIGURED",
                "Set GOOGLE_API_KEY and GEMINI_VISION_MODEL on the API server.",
                409,
            )
        from google import genai
        from google.genai import types

        prompt = evaluation_prompt()
        parts = [prompt, types.Part.from_bytes(data=target, mime_type="image/jpeg")]
        if baseline:
            parts.append("Baseline photograph for context only. Score the target.")
            parts.append(types.Part.from_bytes(data=baseline, mime_type="image/jpeg"))
        client = genai.Client(api_key=self.settings.google_api_key)
        model = model_id or self.settings.gemini_vision_model
        response = await client.aio.models.generate_content(
            model=model,
            contents=parts,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        try:
            parsed = parse_model_json(response.text or "")
        except Exception as exc:
            logger.warning("gemini vision JSON rejected: %s", exc.__class__.__name__)
            raise AppError("VISION_EVALUATION_FAILED", "The vision model returned an invalid rubric.", 502) from exc
        parsed["model"] = model
        return parsed


class OllamaVisionAdapter:
    def __init__(self, settings: Settings, client: object | None = None) -> None:
        self.settings = settings
        self.client = client

    async def evaluate(self, target: bytes, baseline: bytes | None, model_id: str) -> dict:
        if not self.settings.ollama_vision_ready:
            raise AppError(
                "PROVIDER_NOT_CONFIGURED",
                "Set OLLAMA_VISION_MODEL to a vision model served on this computer.",
                409,
            )
        model = model_id or self.settings.ollama_vision_model
        await self._free_comfy()
        prompt = evaluation_prompt()
        prompt += "\nThe first image is the TARGET redesign. Score only that image."
        images = [_jpeg_for_vision(target)]
        if baseline:
            prompt += " The second image is the baseline photograph for context only."
            images.append(_jpeg_for_vision(baseline))
        url = self.settings.ollama_base_url.rstrip("/") + "/api/chat"
        body = {
            "model": model,
            "stream": False,
            "format": evaluation_schema(),
            "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 1800},
            "messages": [{"role": "user", "content": prompt, "images": images}],
        }
        try:
            content = await self._chat(url, body)
            parsed = parse_model_json(content)
        except AppError:
            raise
        except Exception as exc:
            logger.warning("ollama vision JSON rejected: %s", exc.__class__.__name__)
            repair = {
                "model": model,
                "stream": False,
                "format": evaluation_schema(),
                "messages": [
                    {"role": "user", "content": prompt, "images": images},
                    {
                        "role": "user",
                        "content": (
                            "That reply was not valid JSON for all 15 property keys. "
                            "Return only the JSON object. Every assessment needs visual_evidence from the target image."
                        ),
                        "images": images,
                    },
                ],
                "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 1800},
            }
            try:
                content = await self._chat(url, repair)
                parsed = parse_model_json(content)
            except AppError:
                raise
            except Exception as second_exc:
                logger.warning("ollama vision retry rejected: %s", second_exc.__class__.__name__)
                raise AppError(
                    "VISION_EVALUATION_FAILED",
                    "The local vision model returned an invalid rubric.",
                    502,
                ) from second_exc
        parsed["model"] = model
        note = f"Read by the local vision model {model}."
        if note not in parsed["limitations"]:
            parsed["limitations"].append(note)
        return parsed

    async def _chat(self, url: str, body: dict, *, allow_free: bool = True) -> str:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=300)
        try:
            response = await client.post(url, json=body)
        except Exception as exc:
            logger.warning("ollama vision unreachable: %s", exc.__class__.__name__)
            raise AppError(
                "VISION_EVALUATION_FAILED",
                "The local vision model could not be reached. Check that Ollama is running.",
                502,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code == 404:
            raise AppError(
                "PROVIDER_NOT_CONFIGURED",
                "That local vision model is not installed.",
                409,
            )
        if response.status_code >= 400:
            detail = _ollama_detail(response)
            if allow_free and _needs_memory(detail) and await self._free_comfy():
                logger.warning("unloaded the local image model so vision scoring can fit in memory")
                return await self._chat(url, body, allow_free=False)
            message = "The local vision model did not answer."
            if detail:
                message = f"{message} {detail}"
            raise AppError("VISION_EVALUATION_FAILED", message, 502)
        payload = response.json()
        return str((payload.get("message") or {}).get("content") or "")

    async def _free_comfy(self) -> bool:
        if not self.settings.comfy_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    self.settings.comfy_url.rstrip("/") + "/free",
                    json={"unload_models": True, "free_memory": True},
                )
        except Exception as exc:
            logger.warning("could not unload the image model: %s", exc.__class__.__name__)
            return False
        return response.status_code < 400


def _ollama_detail(response: httpx.Response) -> str:
    try:
        detail = str(response.json().get("error") or "")
    except Exception:
        detail = response.text or ""
    return " ".join(detail.split())[:180]


async def unload_ollama_models(settings: Settings) -> None:
    """Drop local vision weights so image generation can use the GPU."""
    if not settings.ollama_base_url:
        return
    base = settings.ollama_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            listed = await client.get(f"{base}/api/ps")
            names = []
            if listed.status_code < 400:
                for item in listed.json().get("models") or []:
                    name = item.get("name") or item.get("model")
                    if name:
                        names.append(name)
            for name in names:
                await client.post(f"{base}/api/generate", json={"model": name, "keep_alive": 0})
    except Exception as exc:
        logger.warning("could not unload the local vision model: %s", exc.__class__.__name__)


def _needs_memory(detail: str) -> bool:
    text = detail.lower()
    return "memory" in text or "vram" in text


def _jpeg_for_vision(data: bytes) -> str:
    from io import BytesIO

    from PIL import Image

    image = Image.open(BytesIO(data)).convert("RGB")
    image.thumbnail((768, 768))
    encoded = BytesIO()
    image.save(encoded, format="JPEG", quality=85)
    return base64.b64encode(encoded.getvalue()).decode("ascii")


async def run_evaluator(provider: str, settings: Settings, target: bytes, baseline: bytes | None, model_id: str) -> dict:
    if provider == "demo_fixture":
        return fixture_evaluation()
    if provider == "openai_vision":
        return await OpenAIVisionAdapter(settings).evaluate(target, baseline, model_id)
    if provider == "gemini_vision":
        return await GeminiVisionAdapter(settings).evaluate(target, baseline, model_id)
    if provider == "ollama_vision":
        return await OllamaVisionAdapter(settings).evaluate(target, baseline, model_id)
    if provider in {"unconfigured", "private_vlm", "disabled"}:
        message = {
            "unconfigured": "Choose a configured vision provider before evaluation can run.",
            "private_vlm": "This worker has not reported a tested private vision model.",
            "disabled": "Evaluation is disabled for this session.",
        }[provider]
        raise AppError("PROVIDER_NOT_CONFIGURED", message, 409)
    raise AppError("PROVIDER_NOT_CONFIGURED", "Unknown evaluation provider.", 400)

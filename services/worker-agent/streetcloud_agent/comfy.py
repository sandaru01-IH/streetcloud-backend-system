import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "workflows" / "manifest.json"
WORKFLOW = ROOT / "workflows" / "street-img2img-v1.json"


def workflow_status() -> dict:
    if not MANIFEST.exists():
        return {"workflowId": "street-img2img-v1", "ready": False, "reason": "Workflow manifest is missing."}
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


async def generate(comfy_url: str, source: bytes, prompt: str, negative: str, params: dict) -> bytes:
    status = workflow_status()
    if not status.get("ready"):
        reason = status.get("reason") or "The private workflow is not marked ready."
        raise RuntimeError(reason)
    graph = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    base = comfy_url.rstrip("/")
    async with httpx.AsyncClient(timeout=180) as client:
        uploaded = await client.post(f"{base}/upload/image", files={"image": ("street.jpg", source, "image/jpeg")})
        uploaded.raise_for_status()
        name = uploaded.json()["name"]
        graph["1"]["inputs"]["image"] = name
        graph["3"]["inputs"]["text"] = prompt
        graph["4"]["inputs"]["text"] = negative
        graph["6"]["inputs"]["steps"] = int(params.get("steps") or 20)
        graph["6"]["inputs"]["cfg"] = float(params.get("cfg") or 6.5)
        graph["6"]["inputs"]["denoise"] = float(params.get("denoise") or 0.45)
        queued = await client.post(f"{base}/prompt", json={"prompt": graph})
        queued.raise_for_status()
        prompt_id = queued.json()["prompt_id"]
        history = {}
        for _ in range(60):
            found = await client.get(f"{base}/history/{prompt_id}")
            found.raise_for_status()
            history = found.json()
            if prompt_id in history:
                break
            await _sleep()
        entry = history.get(prompt_id) or {}
        images = []
        for node in (entry.get("outputs") or {}).values():
            images.extend(node.get("images") or [])
        if not images:
            raise RuntimeError("ComfyUI finished without an image.")
        image = images[0]
        viewed = await client.get(
            f"{base}/view",
            params={"filename": image["filename"], "subfolder": image.get("subfolder", ""), "type": image.get("type", "output")},
        )
        viewed.raise_for_status()
        await _release(client, base)
        return viewed.content


async def _release(client: httpx.AsyncClient, base: str) -> None:
    try:
        await client.post(f"{base}/free", json={"unload_models": True, "free_memory": True})
    except (httpx.HTTPError, OSError):
        return


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(2)

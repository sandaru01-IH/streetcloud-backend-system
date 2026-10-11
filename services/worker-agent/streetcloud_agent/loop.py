import asyncio
from pathlib import Path

import httpx

from streetcloud_agent.comfy import generate, workflow_status
from streetcloud_agent.config import load_config
from streetcloud_agent.images import mock_result

LOG = Path.home() / ".streetcloud" / "agent.log"


def log(line: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def once(client: httpx.AsyncClient, config: dict) -> None:
    token = config.get("deviceToken") or ""
    base = str(config["apiBase"]).rstrip("/")
    status = workflow_status()
    capabilities = {
        "workflowId": status.get("workflowId"),
        "workflowReady": bool(status.get("ready")),
        "mock": bool(config.get("mock")),
        "comfy": bool(config.get("comfyUrl")),
    }
    beat = await client.post(
        f"{base}/api/v1/agent/v1/heartbeat",
        headers=_headers(token),
        json={"available": True, "agentVersion": "1.0.0", "capabilities": capabilities},
    )
    beat.raise_for_status()
    leased = await client.post(f"{base}/api/v1/agent/v1/jobs/lease", headers=_headers(token), json={})
    leased.raise_for_status()
    job = leased.json().get("job")
    if not job:
        return
    log(f"leased {job['jobId']}")
    try:
        image = await _run(client, base, config, job)
    except Exception as exc:
        log(f"failed {job['jobId']}: {exc}")
        await client.post(
            f"{base}/api/v1/agent/v1/jobs/{job['jobId']}/fail",
            headers=_headers(token),
            json={"errorCode": "GENERATION_INVALID_OUTPUT", "message": str(exc)[:400]},
        )
        return
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Lease-Token": job["leaseToken"],
        "Content-Type": "image/jpeg",
    }
    if config.get("mock"):
        headers["X-Streetcloud-Demo"] = "1"
    sent = await client.post(f"{base}{job['outputPath']}", headers=headers, content=image)
    sent.raise_for_status()
    log(f"uploaded {job['jobId']}")


async def _run(client: httpx.AsyncClient, base: str, config: dict, job: dict) -> bytes:
    source_path = job.get("sourceImagePath")
    if not source_path:
        raise RuntimeError("The control plane did not provide a source image.")
    source = await client.get(f"{base}{source_path}")
    source.raise_for_status()
    if config.get("mock"):
        return mock_result(source.content)
    comfy_url = config.get("comfyUrl") or ""
    if not comfy_url:
        raise RuntimeError("Set COMFY_URL, or set STREETCLOUD_AGENT_MOCK=1 for a labeled demo. This agent does not call a cloud model.")
    await _unload_vision_model()
    return await generate(comfy_url, source.content, job.get("prompt") or "", job.get("negativePrompt") or "", job.get("params") or {})


async def _unload_vision_model() -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            listed = await client.get("http://127.0.0.1:11434/api/ps")
            if listed.status_code >= 400:
                return
            for item in listed.json().get("models") or []:
                name = item.get("name") or item.get("model")
                if name:
                    await client.post("http://127.0.0.1:11434/api/generate", json={"model": name, "keep_alive": 0})
    except (httpx.HTTPError, OSError):
        return


async def connect() -> None:
    config = load_config()
    if not config.get("deviceToken"):
        raise SystemExit("Run setup with the device token shown once in the app.")
    async with httpx.AsyncClient(timeout=180) as client:
        while True:
            try:
                await once(client, config)
            except Exception as exc:
                log(f"loop error: {exc}")
            await asyncio.sleep(3)

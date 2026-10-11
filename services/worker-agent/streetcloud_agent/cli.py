import argparse
import asyncio
import json

import httpx

from streetcloud_agent.comfy import workflow_status
from streetcloud_agent.config import CONFIG_PATH, load_config, save_config
from streetcloud_agent.loop import LOG, connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="streetcloud-agent", description="Outbound Street Cloud GPU agent")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="Store the API address and one-time device token")
    setup.add_argument("--api", required=True)
    setup.add_argument("--token", required=True)
    setup.add_argument("--name", default="Planning lab GPU")
    setup.add_argument("--comfy", default="")
    setup.add_argument("--mock", action="store_true", help="Label every result DEMO / MOCK RESULT")

    sub.add_parser("doctor", help="Check API, token, workflow, and ComfyUI")
    sub.add_parser("connect", help="Heartbeat and lease jobs until stopped")
    sub.add_parser("status", help="Print the local config without the token")
    sub.add_parser("logs", help="Print the local agent log")

    args = parser.parse_args(argv)
    if args.command == "setup":
        save_config(
            {
                "apiBase": args.api.rstrip("/"),
                "deviceToken": args.token,
                "friendlyName": args.name,
                "comfyUrl": args.comfy.rstrip("/"),
                "mock": args.mock,
            }
        )
        print(f"Saved {CONFIG_PATH}")
        return 0
    if args.command == "status":
        config = load_config()
        print(
            json.dumps(
                {
                    "apiBase": config.get("apiBase"),
                    "friendlyName": config.get("friendlyName"),
                    "tokenStored": bool(config.get("deviceToken")),
                    "comfyUrl": config.get("comfyUrl") or None,
                    "mock": bool(config.get("mock")),
                    "workflow": workflow_status(),
                },
                indent=2,
            )
        )
        return 0
    if args.command == "logs":
        print(LOG.read_text(encoding="utf-8") if LOG.exists() else "No log yet.")
        return 0
    if args.command == "doctor":
        return asyncio.run(_doctor())
    if args.command == "connect":
        asyncio.run(connect())
        return 0
    return 1


async def _doctor() -> int:
    config = load_config()
    base = str(config.get("apiBase") or "").rstrip("/")
    print(f"API {base}")
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            health = await client.get(f"{base}/health")
            print(f"health {health.status_code} {health.text[:180]}")
        except Exception as exc:
            print(f"health failed: {exc}")
            return 1
        token = config.get("deviceToken")
        if not token:
            print("device token missing")
            return 1
        beat = await client.post(
            f"{base}/api/v1/agent/v1/heartbeat",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"available": True, "agentVersion": "1.0.0", "capabilities": workflow_status()},
        )
        print(f"heartbeat {beat.status_code} {beat.text[:180]}")
        comfy = config.get("comfyUrl")
        if comfy:
            try:
                stats = await client.get(f"{str(comfy).rstrip('/')}/system_stats")
                print(f"comfy {stats.status_code}")
            except Exception as exc:
                print(f"comfy unreachable: {exc}")
        else:
            print("COMFY_URL is not set.")
    status = workflow_status()
    print(json.dumps(status))
    if config.get("mock"):
        print("Mock mode is explicit. Results are labeled DEMO / MOCK RESULT.")
    elif not status.get("ready"):
        print("Workflow is not ready. Jobs will fail on this agent until checkpoints are installed, or until mock mode is turned on.")
    return 0 if beat.status_code == 200 else 1

import json
import os
from pathlib import Path

CONFIG_PATH = Path.home() / ".streetcloud" / "agent.json"


def load_config() -> dict:
    data = {}
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    data.setdefault("apiBase", os.environ.get("STREETCLOUD_API_URL", "http://127.0.0.1:8000"))
    if os.environ.get("STREETCLOUD_DEVICE_TOKEN"):
        data["deviceToken"] = os.environ["STREETCLOUD_DEVICE_TOKEN"]
    if os.environ.get("COMFY_URL"):
        data["comfyUrl"] = os.environ["COMFY_URL"]
    data["mock"] = os.environ.get("STREETCLOUD_AGENT_MOCK", "").strip() in {"1", "true", "TRUE"} or bool(data.get("mock"))
    return data


def save_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stored = {key: data.get(key) for key in ("apiBase", "deviceToken", "friendlyName", "comfyUrl", "mock")}
    CONFIG_PATH.write_text(json.dumps(stored, indent=2), encoding="utf-8")

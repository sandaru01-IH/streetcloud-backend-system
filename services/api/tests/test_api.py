import io
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from app.adapters.generation import GenerationInput, HostedGpuAdapter, OpenAIGenerationAdapter
from app.core.config import Settings
from app.core.errors import AppError
from app.db.base import Base
from app.main import create_app
from app.services.jobs import process_until_idle


def jpeg_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (80, 64), (70, 110, 90)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
async def api(tmp_path):
    settings = Settings(
        database_url="sqlite+aiosqlite://",
        embed_worker=False,
        auto_create_tables=False,
        jwt_secret="test-secret-value-not-production",
        environment="local",
        storage_backend="local",
        storage_local_dir=str(tmp_path / "objects"),
        pairing_ttl_seconds=600,
        ollama_vision_model="",
        openai_vision_model="",
        gemini_vision_model="",
    )
    application = create_app(settings)
    async with application.state.ctx.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, application
    await application.state.ctx.engine.dispose()


async def register(client: AsyncClient, email: str = "alpha@example.com") -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse", "displayName": "Alpha"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_health(api):
    client, _app = api
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


async def test_demo_submission_updates_cloud_and_mock_version(api):
    client, application = api
    guest = await client.post("/api/v1/auth/guest")
    token = guest.json()["accessToken"]
    created = await client.post("/api/v1/sessions", headers=auth(token), json={"title": "Sample", "demo": True, "mode": "individual"})
    assert created.status_code == 200, created.text
    session = created.json()
    assert session["demo"] is True
    assert session["provider"] == "mock"
    submitted = await client.post(
        f"/api/v1/sessions/{session['sessionId']}/feedback",
        headers=auth(token),
        json={"text": "Add shade trees and a protected cycle lane", "clientSubmissionId": "sub-1", "expectedSessionRevision": 0},
    )
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["generation"]["policy"] == "automatic"
    assert body["generation"]["status"] == "queued"
    replay = await client.post(
        f"/api/v1/sessions/{session['sessionId']}/feedback",
        headers=auth(token),
        json={"text": "Add shade trees and a protected cycle lane", "clientSubmissionId": "sub-1", "expectedSessionRevision": 0},
    )
    assert replay.json()["feedbackId"] == body["feedbackId"]
    await process_until_idle(application.state.ctx)
    cloud = await client.get(f"/api/v1/sessions/{session['sessionId']}/wordcloud", headers=auth(token))
    labels = {item["label"] for item in cloud.json()["concepts"]}
    assert "Shade trees" in labels
    assert "Cycle infrastructure" in labels
    assert cloud.json()["label"] == "My design priorities"
    versions = await client.get(f"/api/v1/sessions/{session['sessionId']}/versions", headers=auth(token))
    numbers = [item["versionNumber"] for item in versions.json()["versions"]]
    assert numbers == [0, 1]
    generated = next(item for item in versions.json()["versions"] if item["versionNumber"] == 1)
    assert generated["demoLabeled"] is True
    image = await client.get(generated["imagePath"])
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/jpeg")
    evaluation = await client.get(f"/api/v1/versions/{generated['versionId']}/evaluations", headers=auth(token))
    payload = evaluation.json()
    assert payload["origin"] == "authored_fixture"
    assert len(payload["assessments"]) == 15
    other = await register(client, "other@example.com")
    hidden = await client.get(f"/api/v1/sessions/{session['sessionId']}", headers=auth(other["accessToken"]))
    assert hidden.status_code == 403


async def test_upload_mock_and_rapid_submissions_do_not_drop_text(api):
    client, application = api
    account = await register(client)
    token = account["accessToken"]
    created = await client.post(
        "/api/v1/sessions",
        headers=auth(token),
        json={"title": "My street", "demo": False, "mode": "individual", "processingMode": "cloud_api", "provider": "mock"},
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["sessionId"]
    presign = await client.post(f"/api/v1/sessions/{session_id}/source-upload", headers=auth(token))
    upload_path = presign.json()["uploadPath"]
    parsed = urlparse(upload_path)
    query = parse_qs(parsed.query)
    put = await client.put(upload_path, content=jpeg_bytes(), headers={"Content-Type": "image/jpeg"})
    assert put.status_code == 204, put.text
    done = await client.post(
        f"/api/v1/sessions/{session_id}/source-upload/complete",
        headers=auth(token),
        json={"assetId": presign.json()["assetId"], "consent": True},
    )
    assert done.status_code == 200, done.text
    snapshot = await client.get(f"/api/v1/sessions/{session_id}", headers=auth(token))
    assert snapshot.json()["status"] == "ready", snapshot.text
    texts = ["Add shade trees", "Add a sidewalk", "Add benches"]
    revision = 0
    for index, text in enumerate(texts):
        response = await client.post(
            f"/api/v1/sessions/{session_id}/feedback",
            headers=auth(token),
            json={"text": text, "clientSubmissionId": f"rapid-{index}", "expectedSessionRevision": revision},
        )
        assert response.status_code == 200, response.text
        revision = response.json()["sessionRevision"]
    listed = await client.get(f"/api/v1/sessions/{session_id}/feedback", headers=auth(token))
    assert len(listed.json()["feedback"]) == 3
    await process_until_idle(application.state.ctx)
    versions = await client.get(f"/api/v1/sessions/{session_id}/versions", headers=auth(token))
    numbers = [item["versionNumber"] for item in versions.json()["versions"]]
    assert 0 in numbers and 1 in numbers and 2 in numbers
    current = (await client.get(f"/api/v1/sessions/{session_id}", headers=auth(token))).json()
    evaluation = await client.get(
        f"/api/v1/versions/{current['currentVersion']['versionId']}/evaluations",
        headers=auth(token),
    )
    assert evaluation.json()["status"] == "failed"
    assert evaluation.json()["errorCode"] == "PROVIDER_NOT_CONFIGURED"
    assert query["exp"]


async def test_hosted_gpu_is_disabled_until_configured(api):
    client, _app = api
    account = await register(client, "hosted@example.com")
    response = await client.post(
        "/api/v1/sessions",
        headers=auth(account["accessToken"]),
        json={"demo": False, "processingMode": "hosted_gpu", "provider": "comfyui_hosted"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROVIDER_NOT_CONFIGURED"
    assert "Configure hosted GPU" in response.json()["error"]["message"]


async def test_pairing_code_expires_and_cannot_be_reused(tmp_path):
    settings = Settings(
        database_url="sqlite+aiosqlite://",
        embed_worker=False,
        auto_create_tables=False,
        jwt_secret="test-secret-value-not-production",
        environment="local",
        storage_local_dir=str(tmp_path / "objects"),
        pairing_ttl_seconds=-1,
    )
    application = create_app(settings)
    async with application.state.ctx.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        account = await register(client, "gpu@example.com")
        headers = auth(account["accessToken"])
        worker = await client.post("/api/v1/workers/register", headers=headers, json={"friendlyName": "Lab GPU"})
        assert worker.status_code == 200, worker.text
        challenge = await client.post(f"/api/v1/workers/{worker.json()['workerId']}/pairing-challenges", headers=headers)
        assert challenge.status_code == 200, challenge.text
        paired = await client.post(
            "/api/v1/workers/pair",
            headers=headers,
            json={"workerId": worker.json()["publicId"], "pairingCode": challenge.json()["pairingCode"]},
        )
        assert paired.status_code == 400
        assert paired.json()["error"]["code"] == "PAIRING_CODE_EXPIRED"
    await application.state.ctx.engine.dispose()

    settings.pairing_ttl_seconds = 600
    application = create_app(settings)
    async with application.state.ctx.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        owner = await register(client, "owner@example.com")
        other = await register(client, "guest-user@example.com")
        headers = auth(owner["accessToken"])
        worker = await client.post("/api/v1/workers/register", headers=headers, json={"friendlyName": "Lab GPU"})
        challenge = await client.post(f"/api/v1/workers/{worker.json()['workerId']}/pairing-challenges", headers=headers)
        payload = {"workerId": worker.json()["workerId"], "pairingCode": challenge.json()["pairingCode"]}
        first = await client.post("/api/v1/workers/pair", headers=auth(other["accessToken"]), json=payload)
        assert first.status_code == 200, first.text
        second = await client.post("/api/v1/workers/pair", headers=auth(other["accessToken"]), json=payload)
        assert second.status_code == 400
        assert second.json()["error"]["code"] == "PAIRING_CODE_INVALID"
        listed = await client.get("/api/v1/workers", headers=headers)
        assert any(row["publicId"] == worker.json()["publicId"] for row in listed.json()["workers"])
        removed = await client.post(f"/api/v1/workers/{worker.json()['publicId']}/unpair", headers=headers)
        assert removed.status_code == 200, removed.text
        listed = await client.get("/api/v1/workers", headers=headers)
        assert worker.json()["publicId"] not in {row["publicId"] for row in listed.json()["workers"]}
        rejected = await client.post(
            "/api/v1/agent/v1/heartbeat",
            headers={"Authorization": f"Bearer {worker.json()['deviceToken']}"},
            json={"available": True},
        )
        assert rejected.status_code == 401
    await application.state.ctx.engine.dispose()


async def test_private_worker_lease_stores_labeled_output(api):
    client, application = api
    account = await register(client, "operator@example.com")
    headers = auth(account["accessToken"])
    worker = await client.post("/api/v1/workers/register", headers=headers, json={"friendlyName": "PC A"})
    device = worker.json()["deviceToken"]
    beat = await client.post(
        "/api/v1/agent/v1/heartbeat",
        headers={"Authorization": f"Bearer {device}"},
        json={"available": True, "agentVersion": "1.0.0", "capabilities": {"workflows": [{"id": "street-img2img-v1", "ready": False}]}},
    )
    assert beat.status_code == 200, beat.text
    created = await client.post(
        "/api/v1/sessions",
        headers=headers,
        json={"demo": False, "processingMode": "private_gpu", "workerId": worker.json()["workerId"], "visionProvider": "disabled"},
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["sessionId"]
    presign = await client.post(f"/api/v1/sessions/{session_id}/source-upload", headers=headers)
    await client.put(presign.json()["uploadPath"], content=jpeg_bytes())
    await client.post(
        f"/api/v1/sessions/{session_id}/source-upload/complete",
        headers=headers,
        json={"assetId": presign.json()["assetId"], "consent": True},
    )
    submitted = await client.post(
        f"/api/v1/sessions/{session_id}/feedback",
        headers=headers,
        json={"text": "Add shade trees", "clientSubmissionId": "gpu-1", "expectedSessionRevision": 0},
    )
    assert submitted.json()["generation"]["status"] == "queued"
    leased = await client.post("/api/v1/agent/v1/jobs/lease", headers={"Authorization": f"Bearer {device}"})
    assert leased.status_code == 200, leased.text
    job = leased.json()["job"]
    assert job["workflowId"] == "street-img2img-v1"
    output = await client.post(
        job["outputPath"],
        headers={"Authorization": f"Bearer {device}", "X-Lease-Token": job["leaseToken"], "X-Streetcloud-Demo": "1"},
        content=jpeg_bytes(),
    )
    assert output.status_code == 200, output.text
    assert output.json()["demoLabeled"] is True
    await process_until_idle(application.state.ctx)
    versions = await client.get(f"/api/v1/sessions/{session_id}/versions", headers=headers)
    generated = [item for item in versions.json()["versions"] if item["versionNumber"] == 1][0]
    assert generated["provider"] == "comfyui_private"
    assert generated["demoLabeled"] is True


async def test_provider_guards_do_not_call_network():
    settings = Settings(environment="local")
    with pytest.raises(AppError) as caught:
        await OpenAIGenerationAdapter(settings).generate(
            GenerationInput("j", b"x", "prompt", None, "832x512", "", "balanced", "openai"),
            1,
        )
    assert caught.value.code == "PROVIDER_NOT_CONFIGURED"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/generate")
        return httpx.Response(200, content=jpeg_bytes(), headers={"content-type": "image/jpeg"})

    hosted_settings = Settings(hosted_gpu_endpoint="https://gpu.example.test/v1", hosted_gpu_api_key="secret")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await HostedGpuAdapter(hosted_settings, client=http).generate(
            GenerationInput("j", jpeg_bytes(), "Add trees", None, "832x512", "hosted", "balanced", "comfyui_hosted"),
            1,
        )
    assert result.provider == "comfyui_hosted"
    assert result.image_bytes.startswith(b"\xff\xd8")

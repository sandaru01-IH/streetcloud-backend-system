import io

import pytest
from PIL import Image

from app.adapters.evaluation import OllamaVisionAdapter, fixture_evaluation, parse_model_json
from app.core.config import Settings
from app.core.errors import AppError
from app.services.brief import compile_prompt, mutate_brief
from app.services.concepts import extract_concepts
from app.services.providers import resolve_vision
from app.services.rubric import validate_assessments
from app.services.state_machine import can_transition


def test_concepts_group_synonyms_and_keep_sinhala():
    concepts = extract_concepts("Add shade trees and a protected cycle lane")
    slugs = {item["slug"] for item in concepts}
    assert "shade_trees" in slugs
    assert "cycle_infrastructure" in slugs
    sinhala = extract_concepts("ගස් සෙවන")
    assert any("ගස්" in item["label"] or "සෙවන" in item["label"] for item in sinhala)


def test_brief_replaces_removed_feature():
    first = extract_concepts("Add shade trees and a protected cycle lane")
    assert first
    brief = mutate_brief(None, "Add shade trees and a protected cycle lane", "f1")
    updated = mutate_brief(brief, "Remove the cycle lane", "f2")
    retain_categories = {item["category"] for item in updated["retain"]}
    remove_categories = {item["category"] for item in updated["remove"]}
    assert "cycling" in remove_categories
    assert "cycling" not in retain_categories
    assert "trees" in retain_categories
    prompt = compile_prompt(updated)
    assert "EXISTING STREET" in prompt
    assert "cycle" in prompt.lower()


def test_state_machine_blocks_skipping_to_success():
    assert can_transition("queued", "preparing")
    assert not can_transition("queued", "succeeded")
    assert can_transition("running", "worker_disconnected")


def test_rubric_rejects_incomplete_or_illegal_scores():
    good = fixture_evaluation()
    assert len(good["assessments"]) == 15
    assert good["origin"] == "authored_fixture"
    assert good["assessments"][0]["evidence"].startswith("Fixture:")
    broken = [dict(item) for item in good["assessments"][:-1]]
    with pytest.raises(ValueError):
        validate_assessments(broken)
    illegal = [dict(item) for item in good["assessments"]]
    illegal[0]["score"] = 9
    with pytest.raises(ValueError):
        validate_assessments(illegal)
    with pytest.raises(ValueError):
        parse_model_json('{"assessments": [], "summary": "x", "limitations": []}')


def test_normalize_rejects_tiny_and_non_images():
    from app.services.images import normalize_image

    with pytest.raises(AppError):
        normalize_image(b"not-an-image", 2_000_000, 40)
    tiny = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(tiny, format="JPEG")
    with pytest.raises(AppError):
        normalize_image(tiny.getvalue(), 2_000_000, 40)


def test_parse_accepts_nested_property_objects():
    nested = {
        "summary": "A wide paved street with trees along one edge.",
        "limitations": ["Single viewpoint."],
        "assessments": [
            {
                "levels_of_scale": {
                    "applicability": "assessable",
                    "score": 3,
                    "confidence": 0.4,
                    "visual_evidence": "Trees, verge, and the road form nested bands.",
                    "limitation": "One viewpoint.",
                },
                "strong_centers": {
                    "applicability": "assessable",
                    "score": 2,
                    "confidence": 0.4,
                    "visual_evidence": "The road corridor is the main focal band.",
                    "limitation": "One viewpoint.",
                },
            }
        ],
    }
    from app.services.rubric import PROPERTIES

    first = nested["assessments"][0]
    for prop in PROPERTIES:
        if prop["key"] not in first:
            first[prop["key"]] = {
                "applicability": "assessable",
                "score": 2,
                "confidence": 0.4,
                "visual_evidence": f"The photograph shows a cue for {prop['name'].lower()}.",
                "limitation": "One viewpoint.",
            }
    import json

    parsed = parse_model_json(json.dumps(nested))
    assert len(parsed["assessments"]) == 15
    assert parsed["assessments"][0]["key"] == "levels_of_scale"


def test_auto_vision_uses_a_configured_local_model():
    ready = Settings(ollama_vision_model="qwen2.5vl:3b", openai_api_key="", openai_vision_model="", google_api_key="", gemini_vision_model="")
    assert resolve_vision(ready, "auto", demo=False) == "ollama_vision"
    bare = Settings(ollama_vision_model="", openai_vision_model="", gemini_vision_model="")
    assert resolve_vision(bare, "auto", demo=False) == "unconfigured"
    assert resolve_vision(ready, "auto", demo=True) == "demo_fixture"


async def test_ollama_adapter_reads_model_json():
    sample = fixture_evaluation()
    body = {
        "summary": "Trees line one side of a wide paved street.",
        "limitations": ["Single viewpoint."],
        "assessments": [
            {
                "key": item["key"],
                "applicability": item["applicability"],
                "score": str(item["score"]),
                "confidence": None,
                "visual_evidence": f"Visible structure related to {item['key'].replace('_', ' ')}.",
                "limitation": "One photograph cannot settle this property.",
            }
            for item in sample["assessments"]
        ],
    }
    import json

    class Response:
        status_code = 200

        def json(self):
            return {"message": {"content": json.dumps(body)}}

    class Client:
        async def post(self, url, json):
            assert url.endswith("/api/chat")
            assert json["model"] == "qwen2.5vl:3b"
            assert json["messages"][0]["images"]
            return Response()

    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), (40, 90, 60)).save(buffer, format="JPEG")
    settings = Settings(ollama_vision_model="qwen2.5vl:3b", ollama_base_url="http://127.0.0.1:11434", comfy_url="")
    parsed = await OllamaVisionAdapter(settings, client=Client()).evaluate(buffer.getvalue(), None, "qwen2.5vl:3b")
    assert parsed["origin"] == "vision_model"
    assert parsed["model"] == "qwen2.5vl:3b"
    assert len(parsed["assessments"]) == 15
    assert parsed["assessments"][0]["evidence"]

import re

from app.services.concepts import extract_concepts

RULES_VERSION = "brief-v1"
MAX_COMMUNITY_CONCEPTS = 8
PRESERVATION_RULES = """Edit the supplied photograph of the EXISTING STREET; do not create a new location.
Preserve its camera viewpoint, perspective, visible building outlines, building
facades, street orientation, horizon, lighting consistency and identifiable
surroundings wherever possible. Apply only these conceptual urban design changes:
{structured_interventions}. Maintain existing interventions carried forward:
{retained_interventions}. Do not add unrelated buildings, imaginary landmarks,
false signage or unrelated people. Render a believable planning visualization.
Treat all visible road dimensions as approximate; do not imply verified
construction feasibility. Respond with the edited image, not a separate diagram."""
NEGATIVE_PROMPT = (
    "unrelated location, new skyline, warped facades, illegible signage, "
    "extra crowds, technical diagram, collage, watermark, text overlay"
)
_REMOVE = re.compile(r"\b(remove|delete|reduce|without|less|fewer|no more|get rid of)\b", re.I)
_SPLIT = re.compile(r",|\band\b|\bbut\b", re.I)


def empty_brief(revision: int = 0) -> dict:
    return {
        "revision": revision,
        "retain": [],
        "remove": [],
        "replace": [],
        "constraints": ["Keep the building facades and camera viewpoint"],
        "communityConcepts": [],
        "conflicts": [],
    }


def _categories_in(text: str) -> list[dict]:
    return extract_concepts(text)


def mutate_brief(
    previous: dict | None,
    text: str,
    feedback_id: str,
    community_labels: list[str] | None = None,
) -> dict:
    base = previous or empty_brief(0)
    brief = {
        "revision": int(base.get("revision", 0)) + 1,
        "retain": [dict(item) for item in base.get("retain", [])],
        "remove": [dict(item) for item in base.get("remove", [])],
        "replace": list(base.get("replace", [])),
        "constraints": list(base.get("constraints") or ["Keep the building facades and camera viewpoint"]),
        "communityConcepts": list(community_labels or []),
        "conflicts": [],
    }
    clauses = [part.strip() for part in _SPLIT.split(text) if part.strip()] or [text.strip()]
    for clause in clauses:
        polarity = "remove" if _REMOVE.search(clause) else "retain"
        matches = _categories_in(clause)
        if not matches:
            matches = [
                {
                    "slug": "general",
                    "label": "General intervention",
                    "category": "general",
                    "source_phrase": clause[:160],
                }
            ]
        seen_categories: set[str] = set()
        for match in matches:
            category = match["category"]
            if category in seen_categories:
                continue
            seen_categories.add(category)
            instruction = clause[:400]
            _apply(brief, polarity, category, instruction, feedback_id, match["label"])
    brief["communityConcepts"] = list(community_labels or [])[:MAX_COMMUNITY_CONCEPTS]
    return brief


def _apply(brief: dict, polarity: str, category: str, instruction: str, feedback_id: str, label: str) -> None:
    other = "remove" if polarity == "retain" else "retain"
    opposite = [item for item in brief[other] if item["category"] == category]
    if opposite:
        brief[other] = [item for item in brief[other] if item["category"] != category]
        brief["conflicts"].append(
            {
                "category": category,
                "message": (
                    f"Latest request {polarity}s {label}. "
                    "The previous opposing instruction was replaced."
                ),
            }
        )
    bucket = brief[polarity]
    existing = next((item for item in bucket if item["category"] == category), None)
    if existing:
        if feedback_id not in existing["sourceFeedbackIds"]:
            existing["sourceFeedbackIds"].append(feedback_id)
        existing["instruction"] = instruction
        existing["label"] = label
        return
    bucket.append(
        {
            "category": category,
            "label": label,
            "instruction": instruction,
            "sourceFeedbackIds": [feedback_id],
        }
    )


def compile_prompt(brief: dict) -> str:
    retain = brief.get("retain") or []
    remove = brief.get("remove") or []
    structured = "\n".join(f"- {item['instruction']}" for item in retain) or "- No additions yet."
    retained = "\n".join(f"- Keep: {item['instruction']}" for item in retain) or "- None yet."
    if remove:
        retained += "\n" + "\n".join(f"- Do not include / reduce: {item['instruction']}" for item in remove)
    prompt = PRESERVATION_RULES.format(
        structured_interventions=structured,
        retained_interventions=retained,
    )
    community = brief.get("communityConcepts") or []
    if community:
        prompt += (
            "\n\nCommunity themes that informed this collaborative brief (top concepts only): "
            + ", ".join(community)
            + ". Direct participant instructions above still take priority."
        )
    if brief.get("conflicts"):
        prompt += "\n\nResolved conflicts, latest instruction wins:\n" + "\n".join(
            f"- {item['message']}" for item in brief["conflicts"]
        )
    return prompt

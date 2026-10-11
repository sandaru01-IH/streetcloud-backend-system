"""Christopher Alexander street rubric, alexander-street-v2.

Scores are provisional image-based indicators, not validated urban-quality measures.
Each cue is the visual test for that property in one street photograph.
"""

RUBRIC_VERSION = "alexander-street-v2"

PROPERTIES: list[dict[str, str]] = [
    {
        "key": "levels_of_scale",
        "name": "Levels of scale",
        "cue": "A hierarchy of similar shapes at different sizes, from the whole street down to trees, edges, and small details. Steps should be close enough to cohere and distinct enough not to blur, like a logarithmic or Fibonacci spacing near 2.7.",
    },
    {
        "key": "strong_centers",
        "name": "Strong centers",
        "cue": "Coherent regions tied together. A defined center has a visible focal point. An implied center has a boundary that draws attention to its interior. Centers should reinforce each other across scales.",
    },
    {
        "key": "thick_boundaries",
        "name": "Thick boundaries",
        "cue": "The edge of a space is itself a smaller center, such as a planted verge, wall, or shaded margin. It both bounds the larger area and focuses it, instead of leaving a gap between scales.",
    },
    {
        "key": "alternating_repetition",
        "name": "Alternating repetition",
        "cue": "Repeated elements paired with contrast, so trees, lights, or bays alternate rather than repeat the same unit monotonously.",
    },
    {
        "key": "positive_space",
        "name": "Positive space",
        "cue": "Convex, graspable volumes in both the street space and the building mass. The open space and the solid forms should both read as comfortable, well-shaped figures.",
    },
    {
        "key": "good_shape",
        "name": "Good shape",
        "cue": "Simple geometric figures with internal symmetry that the eye can grasp at once. Avoid shapes that look broken, arbitrary, or hard to parse.",
    },
    {
        "key": "local_symmetries",
        "name": "Local symmetries",
        "cue": "Balance inside smaller nested parts, such as a gateway, a pair of trees, or a facade bay. Do not require one symmetry of the entire street.",
    },
    {
        "key": "deep_interlock_ambiguity",
        "name": "Deep interlock and ambiguity",
        "cue": "Edges that interpenetrate, such as planting, porches, or paths crossing between road and buildings, forming a semi-permeable zone instead of a hard cut.",
    },
    {
        "key": "contrast",
        "name": "Contrast",
        "cue": "Clear separation between adjoining parts and between figure and ground, such as light against dark, solid against void, or pavement against planting. Weak, glassy, or washed-out boundaries score low.",
    },
    {
        "key": "gradients",
        "name": "Gradients",
        "cue": "A gradual change that leaves uniformity without snapping into unrelated pieces, such as a shift from open street to shaded edge to private frontage.",
    },
    {
        "key": "roughness",
        "name": "Roughness",
        "cue": "Small irregularities and approximate, not perfect, repetition. Repeated units such as trees, tiles, or windows vary slightly so the scene is not mechanical.",
    },
    {
        "key": "echoes",
        "name": "Echoes",
        "cue": "Similar forms repeating across the view, either side by side at one size or echoed at a larger and a smaller size.",
    },
    {
        "key": "the_void",
        "name": "The void",
        "cue": "A large calm open area that balances zones of detail. The empty middle should feel held by a more articulated boundary.",
    },
    {
        "key": "simplicity_inner_calm",
        "name": "Simplicity and inner calm",
        "cue": "Nothing extraneous. The parts cooperate so a complex street still looks quiet, balanced, and effortless rather than cluttered.",
    },
    {
        "key": "not_separateness",
        "name": "Not-separateness",
        "cue": "Every visible part belongs to the surroundings. No object, facade, or patch of paving looks pasted on or demands attention by itself.",
    },
]

PROPERTY_KEYS = [item["key"] for item in PROPERTIES]
PROPERTY_BY_KEY = {item["key"]: item for item in PROPERTIES}

LIMITATION_BANNER = (
    "Image-only assessment. These figures are exploratory model or fixture indicators, "
    "not engineered proof of safety, access, or regulatory compliance."
)


def validate_assessments(raw_items: list[dict]) -> list[dict]:
    if len(raw_items) != 15:
        raise ValueError("Exactly 15 property assessments are required.")
    seen: set[str] = set()
    by_key: dict[str, dict] = {}
    for item in raw_items:
        key = item.get("key")
        if key not in PROPERTY_BY_KEY:
            raise ValueError(f"Unknown property key: {key}")
        if key in seen:
            raise ValueError(f"Duplicate property key: {key}")
        seen.add(key)
        applicability = item.get("applicability")
        if applicability not in {"assessable", "not_assessable"}:
            raise ValueError(f"Invalid applicability for {key}")
        score = item.get("score")
        evidence = str(item.get("visual_evidence") or item.get("evidence") or "").strip()
        caveat = str(item.get("limitation") or item.get("caveat") or "").strip()
        if not evidence:
            raise ValueError(f"Missing evidence for {key}")
        if applicability == "assessable":
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                raise ValueError(f"Assessable property {key} needs a numeric score.")
            if score < 0 or score > 5:
                raise ValueError(f"Score out of range for {key}")
        elif score is not None:
            raise ValueError(f"Not-assessable property {key} must have a null score.")
        confidence = item.get("confidence")
        if confidence is not None and (
            not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1
        ):
            raise ValueError(f"Confidence out of range for {key}")
        by_key[key] = {
            "key": key,
            "applicability": applicability,
            "score": None if score is None else round(float(score), 2),
            "confidence": None if confidence is None else round(float(confidence), 2),
            "evidence": evidence[:500],
            "caveat": (caveat or "Single-view photograph; hidden conditions are unknown.")[:400],
        }
    if seen != set(PROPERTY_KEYS):
        raise ValueError("Property set does not match the v1 rubric.")
    return [by_key[key] for key in PROPERTY_KEYS]


def mean_assessable(items: list[dict]) -> tuple[float | None, int]:
    scores = [item["score"] for item in items if item["applicability"] == "assessable" and item["score"] is not None]
    if not scores:
        return None, 0
    return round(sum(scores) / len(scores), 2), len(scores)

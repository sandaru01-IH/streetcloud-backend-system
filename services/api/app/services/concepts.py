import hashlib
import re
import unicodedata

EXTRACTOR_VERSION = "street-v1"
TAXONOMY_VERSION = "street-v1"

# phrase, slug, label, category
_TAXONOMY: list[tuple[str, str, str, str]] = [
    ("shade trees", "shade_trees", "Shade trees", "trees"),
    ("street trees", "shade_trees", "Shade trees", "trees"),
    ("tree canopy", "shade_trees", "Shade trees", "trees"),
    ("trees", "shade_trees", "Shade trees", "trees"),
    ("protected cycle track", "cycle_infrastructure", "Cycle infrastructure", "cycling"),
    ("protected cycle lane", "cycle_infrastructure", "Cycle infrastructure", "cycling"),
    ("cycle track", "cycle_infrastructure", "Cycle infrastructure", "cycling"),
    ("cycle lane", "cycle_infrastructure", "Cycle infrastructure", "cycling"),
    ("cycle lanes", "cycle_infrastructure", "Cycle infrastructure", "cycling"),
    ("bike lane", "cycle_infrastructure", "Cycle infrastructure", "cycling"),
    ("bike track", "cycle_infrastructure", "Cycle infrastructure", "cycling"),
    ("bicycle path", "cycle_infrastructure", "Cycle infrastructure", "cycling"),
    ("pedestrian crossing", "pedestrian_safety", "Pedestrian safety", "pedestrian"),
    ("pedestrian safety", "pedestrian_safety", "Pedestrian safety", "pedestrian"),
    ("safe sidewalk", "pedestrian_safety", "Pedestrian safety", "pedestrian"),
    ("sidewalk", "pedestrian_safety", "Pedestrian safety", "pedestrian"),
    ("footpath", "pedestrian_safety", "Pedestrian safety", "pedestrian"),
    ("crossing", "pedestrian_safety", "Pedestrian safety", "pedestrian"),
    ("street seating", "seating", "Seating", "seating"),
    ("benches", "seating", "Seating", "seating"),
    ("bench", "seating", "Seating", "seating"),
    ("seating", "seating", "Seating", "seating"),
    ("street light", "lighting", "Lighting", "lighting"),
    ("lighting", "lighting", "Lighting", "lighting"),
    ("car space", "parking", "Parking", "parking"),
    ("car parking", "parking", "Parking", "parking"),
    ("parking", "parking", "Parking", "parking"),
    ("bus stop", "transit", "Transit", "transit"),
    ("bus lane", "transit", "Transit", "transit"),
    ("rain garden", "water", "Water and drainage", "water"),
    ("drainage", "water", "Water and drainage", "water"),
    ("stormwater", "water", "Water and drainage", "water"),
    ("public space", "public_space", "Public space", "public_space"),
    ("plaza", "public_space", "Public space", "public_space"),
    ("traffic calming", "traffic_calming", "Traffic calming", "safety"),
    ("fewer cars", "traffic_calming", "Traffic calming", "safety"),
]

_STOPWORDS = {
    "the", "and", "with", "for", "from", "that", "this", "into", "onto", "over",
    "under", "than", "then", "keep", "add", "more", "less", "remove", "delete",
    "reduce", "without", "please", "would", "could", "should", "make", "want",
    "along", "beside", "across", "street", "road", "area", "some", "very",
    "have", "has", "been", "being", "also", "just", "only", "both", "each",
}

_PHRASES = sorted(_TAXONOMY, key=lambda row: len(row[0]), reverse=True)


def detect_language(text: str) -> str:
    if re.search(r"[\u0D80-\u0DFF]", text):
        return "si"
    if re.search(r"[\u0B80-\u0BFF]", text):
        return "ta"
    return "en"


def _custom_slug(label: str) -> str:
    digest = hashlib.sha256(label.casefold().encode("utf-8")).hexdigest()[:10]
    return f"custom-{digest}"


def extract_concepts(text: str) -> list[dict]:
    original = unicodedata.normalize("NFKC", text).strip()
    folded = original.casefold()
    found: list[dict] = []
    seen: set[str] = set()
    occupied = [False] * len(folded)
    if len(folded) == len(original):
        for phrase, slug, label, category in _PHRASES:
            start = 0
            while True:
                idx = folded.find(phrase, start)
                if idx < 0:
                    break
                end = idx + len(phrase)
                left_ok = idx == 0 or not folded[idx - 1].isalnum()
                right_ok = end == len(folded) or not folded[end].isalnum()
                if left_ok and right_ok and not any(occupied[idx:end]):
                    for pos in range(idx, end):
                        occupied[pos] = True
                    if slug not in seen:
                        seen.add(slug)
                        found.append(
                            {
                                "slug": slug,
                                "label": label,
                                "category": category,
                                "source_phrase": phrase,
                                "confidence": 0.9,
                                "language": "en",
                            }
                        )
                    break
                start = idx + 1
        remainder = "".join(" " if occupied[i] else ch for i, ch in enumerate(original))
    else:
        remainder = original

    script_runs = re.findall(r"[\u0D80-\u0DFF\u0B80-\u0BFF][\u0D80-\u0DFF\u0B80-\u0BFF\s]{0,80}", original)
    for run in script_runs:
        label = " ".join(run.split())
        if len(label) < 2:
            continue
        slug = _custom_slug(label)
        if slug in seen:
            continue
        seen.add(slug)
        found.append(
            {
                "slug": slug,
                "label": label,
                "category": "general",
                "source_phrase": label,
                "confidence": 0.7,
                "language": detect_language(label),
            }
        )
        remainder = remainder.replace(run, " ")

    for token in re.findall(r"[^\W\d_]{2,}", remainder, flags=re.UNICODE):
        if token.casefold() in _STOPWORDS:
            continue
        if not token.isascii():
            continue
        if len(token) < 4:
            continue
        label = token
        slug = _custom_slug(label)
        if slug in seen:
            continue
        seen.add(slug)
        found.append(
            {
                "slug": slug,
                "label": label,
                "category": "general",
                "source_phrase": label,
                "confidence": 0.55,
                "language": "en",
            }
        )
    return found

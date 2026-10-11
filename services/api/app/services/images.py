import hashlib
import io

from PIL import Image, ImageDraw, ImageFont

from app.core.errors import AppError

Image.MAX_IMAGE_PIXELS = 40_000_000


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def normalize_image(data: bytes, max_bytes: int, max_megapixels: int) -> tuple[bytes, int, int, str]:
    if not data:
        raise AppError("PHOTO_UNSUPPORTED_MIME", "The upload was empty.", 400)
    if len(data) > max_bytes:
        raise AppError("PHOTO_TOO_LARGE", f"Photos must be {max_bytes // (1024 * 1024)} MB or smaller.", 413)
    if sniff_mime(data) is None:
        raise AppError(
            "PHOTO_UNSUPPORTED_MIME",
            "Use a JPEG, PNG, or WebP photo. HEIC should be exported as JPEG first.",
            415,
        )
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        image = Image.open(io.BytesIO(data))
        image.load()
    except AppError:
        raise
    except Exception as exc:
        raise AppError("PHOTO_UNSUPPORTED_MIME", "The file could not be decoded as an image.", 415) from exc
    width, height = image.size
    if width < 32 or height < 32:
        raise AppError("PHOTO_UNSUPPORTED_MIME", "The photo is too small to use as a street scene.", 415)
    if width * height > max_megapixels * 1_000_000:
        raise AppError("PHOTO_TOO_LARGE", "The photo has too many pixels.", 413)
    rgb = image.convert("RGB")
    longest = max(rgb.size)
    if longest > 2048:
        scale = 2048 / longest
        rgb = rgb.resize((max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))))
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=90)
    payload = out.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    return payload, rgb.width, rgb.height, digest


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render_mock_edit(source: bytes, version_no: int, worker_label: str | None = None) -> bytes:
    image = Image.open(io.BytesIO(source)).convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    banner_h = max(56, image.height // 7)
    draw.rectangle((0, image.height - banner_h, image.width, image.height), fill=(18, 109, 89, 235))
    label = "DEMO / MOCK RESULT"
    if worker_label:
        label += f" · {worker_label}"
    label += f" · v{version_no}"
    draw.text((16, image.height - banner_h + 16), label, fill=(255, 255, 255, 255), font=_font(18))
    composed = Image.alpha_composite(image, overlay).convert("RGB")
    out = io.BytesIO()
    composed.save(out, format="JPEG", quality=85)
    return out.getvalue()


def render_sample_street() -> bytes:
    width, height = 960, 640
    image = Image.new("RGB", (width, height), "#D7E4EA")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 280), fill="#B9D4E0")
    draw.rectangle((0, 430, width, height), fill="#6E7672")
    draw.rectangle((0, 360, width, 430), fill="#C5B59A")
    for index, x in enumerate(range(40, width, 160)):
        top = 150 + (index % 2) * 20
        draw.rectangle((x, top, x + 110, 360), fill="#8C8378" if index % 2 == 0 else "#6E7C86")
        draw.rectangle((x + 20, top + 40, x + 48, top + 80), fill="#E7E1D2")
        draw.rectangle((x + 62, top + 40, x + 90, top + 80), fill="#E7E1D2")
    draw.polygon([(0, 470), (width, 470), (width - 80, height), (80, height)], fill="#4E5552")
    draw.text((24, 24), "SAMPLE STREET — DEMO", fill="#15221F", font=_font(22))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=90)
    return out.getvalue()


def render_export(source: bytes) -> bytes:
    image = Image.open(io.BytesIO(source)).convert("RGB")
    banner_h = max(42, image.height // 12)
    canvas = Image.new("RGB", (image.width, image.height + banner_h), "#15221F")
    canvas.paste(image, (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (12, image.height + 12),
        "AI-generated concept — not a surveyed or approved design.",
        fill="#F4F7F4",
        font=_font(16),
    )
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=88)
    return out.getvalue()

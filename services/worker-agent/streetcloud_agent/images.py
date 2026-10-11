from io import BytesIO

from PIL import Image, ImageDraw


def mock_result(source: bytes) -> bytes:
    try:
        image = Image.open(BytesIO(source)).convert("RGB")
    except Exception:
        image = Image.new("RGB", (832, 512), (40, 70, 60))
    image.thumbnail((832, 512))
    canvas = Image.new("RGB", image.size, (18, 48, 40))
    canvas.paste(image, (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, canvas.width, 36), fill=(18, 48, 40))
    draw.text((12, 10), "DEMO / MOCK RESULT", fill=(255, 255, 255))
    out = BytesIO()
    canvas.save(out, format="JPEG", quality=85)
    return out.getvalue()

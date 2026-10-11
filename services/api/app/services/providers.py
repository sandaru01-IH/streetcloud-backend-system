from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import User


def resolve_vision(settings: Settings, requested: str | None, *, demo: bool) -> str:
    if demo:
        return "demo_fixture"
    choice = requested or "auto"
    if choice == "disabled":
        return "disabled"
    if choice == "demo_fixture":
        raise AppError(
            "PROVIDER_NOT_CONFIGURED",
            "The demonstration fixture is only used on the labeled sample street.",
            400,
        )
    if choice == "openai_vision":
        if not settings.openai_vision_ready:
            raise AppError("PROVIDER_NOT_CONFIGURED", "OpenAI vision is not configured on the server.", 409)
        return choice
    if choice == "gemini_vision":
        if not settings.gemini_vision_ready:
            raise AppError("PROVIDER_NOT_CONFIGURED", "Gemini vision is not configured on the server.", 409)
        return choice
    if choice == "ollama_vision":
        if not settings.ollama_vision_ready:
            raise AppError(
                "PROVIDER_NOT_CONFIGURED",
                "Set OLLAMA_VISION_MODEL to a vision model served on this computer.",
                409,
            )
        return choice
    if choice == "private_vlm":
        return "private_vlm"
    if choice == "auto":
        if settings.openai_vision_ready:
            return "openai_vision"
        if settings.gemini_vision_ready:
            return "gemini_vision"
        if settings.ollama_vision_ready:
            return "ollama_vision"
        return "unconfigured"
    raise AppError("PROVIDER_NOT_CONFIGURED", "Unknown evaluation provider.", 400)


def resolve_generation(
    settings: Settings,
    user: User,
    *,
    processing_mode: str,
    provider: str | None,
    demo: bool,
    worker_ok: bool,
) -> tuple[str, str, str, str]:
    """Return processing_mode, provider, model_id, vision is resolved separately."""
    if user.role == "guest" and not demo:
        raise AppError("NOT_AUTHORIZED", "Guest access is limited to the labeled sample street.", 403)
    if demo:
        return "demo", "mock", "mock-demo"
    if processing_mode == "cloud_api":
        chosen = provider or ""
        if chosen == "mock":
            if settings.environment == "production":
                raise AppError(
                    "PROVIDER_NOT_CONFIGURED",
                    "Mock generation is disabled in production.",
                    409,
                )
            return processing_mode, "mock", "mock-demo"
        if chosen == "openai":
            if not settings.openai_image_ready:
                raise AppError(
                    "PROVIDER_NOT_CONFIGURED",
                    "Set OPENAI_API_KEY and OPENAI_IMAGE_MODEL on the API server.",
                    409,
                )
            return processing_mode, "openai", settings.openai_image_model
        if chosen == "gemini":
            if not settings.gemini_image_ready:
                raise AppError(
                    "PROVIDER_NOT_CONFIGURED",
                    "Set GOOGLE_API_KEY and GEMINI_IMAGE_MODEL on the API server.",
                    409,
                )
            return processing_mode, "gemini", settings.gemini_image_model
        raise AppError("PROVIDER_NOT_CONFIGURED", "Choose OpenAI, Gemini, or the local mock.", 400)
    if processing_mode == "hosted_gpu":
        if not settings.hosted_gpu_ready:
            raise AppError("PROVIDER_NOT_CONFIGURED", "Configure hosted GPU to enable.", 409)
        return processing_mode, "comfyui_hosted", "hosted-configured"
    if processing_mode == "private_gpu":
        if not worker_ok:
            raise AppError("WORKER_UNAUTHORIZED", "Pair a private GPU server before using it.", 403)
        return processing_mode, "comfyui_private", "street-img2img-v1"
    raise AppError("PROVIDER_NOT_CONFIGURED", "Unknown processing mode.", 400)

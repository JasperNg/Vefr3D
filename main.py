from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, Security
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from starlette.middleware.trustedhost import TrustedHostMiddleware
from collections import deque
from contextlib import asynccontextmanager
import os
import uuid
import hmac
import asyncio
import logging
import threading
import time
import warnings
import comfyuiservice
from fastapi.concurrency import run_in_threadpool
from PIL import Image, ImageOps, UnidentifiedImageError
from pathlib import Path


ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg"}
ALLOWED_IMAGE_FORMATS = ("JPEG", "PNG")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
CHUNK = 1024 * 1024
Image.MAX_IMAGE_PIXELS = 50_000_000


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be at least 1.")
    return value


MAX_ACTIVE_REQUESTS = _positive_int_env("CUI_MAX_ACTIVE_REQUESTS", 2)
RATE_LIMIT_PER_MINUTE = _positive_int_env("CUI_RATE_LIMIT_PER_MINUTE", 6)
STALE_ARTIFACT_SECONDS = _positive_int_env("CUI_STALE_ARTIFACT_SECONDS", 86400)

logger = logging.getLogger(__name__)

_trellis = Path(__file__).parent / "Trellis2"
IMAGEDIR = _trellis / "images"


_API_KEY = os.environ.get("CUI_API_KEY")
if not _API_KEY:
    raise RuntimeError("CUI_API_KEY environment variable is not set.")

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

_rate_limit_lock = threading.Lock()
_recent_requests = deque()


def _enforce_rate_limit() -> None:
    now = time.monotonic()
    cutoff = now - 60
    with _rate_limit_lock:
        while _recent_requests and _recent_requests[0] <= cutoff:
            _recent_requests.popleft()
        if len(_recent_requests) >= RATE_LIMIT_PER_MINUTE:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded.",
                headers={"Retry-After": "60"},
            )
        _recent_requests.append(now)

def verify_api_key(key: str = Security(_api_key_header)):
    if not key or not hmac.compare_digest(key, _API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")
    _enforce_rate_limit()

_ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("CUI_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()
]

_generation_semaphore = asyncio.Semaphore(1)
_admission_lock = asyncio.Lock()
_active_requests = 0


async def _try_acquire_request_slot() -> bool:
    global _active_requests
    async with _admission_lock:
        if _active_requests >= MAX_ACTIVE_REQUESTS:
            return False
        _active_requests += 1
        return True


async def _release_request_slot() -> None:
    global _active_requests
    async with _admission_lock:
        _active_requests -= 1


def cleanup_files(*filepaths: str) -> None:
    for raw_path in dict.fromkeys(path for path in filepaths if path):
        path = Path(raw_path)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove temporary artifact %s: %s", path.name, exc)


def validate_and_sanitize_image(input_path: Path, extension: str) -> None:
    """Decode only JPEG/PNG, enforce the real pixel cap, and strip metadata."""
    expected_format = "PNG" if extension == ".png" else "JPEG"
    sanitized_path = input_path.with_name(
        f".{input_path.name}.{uuid.uuid4().hex}.sanitized"
    )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(input_path, formats=ALLOWED_IMAGE_FORMATS) as image:
                if image.format != expected_format:
                    raise ValueError("Image content does not match its extension.")
                if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                    raise Image.DecompressionBombError("Image exceeds the pixel limit.")

                image.load()
                transposed = ImageOps.exif_transpose(image)
                sanitized = transposed.copy()
                if transposed is not image:
                    transposed.close()

        try:
            transparency = sanitized.info.get("transparency")
            sanitized.info.clear()
            if expected_format == "PNG":
                if transparency is not None:
                    sanitized.info["transparency"] = transparency
                sanitized.save(sanitized_path, format="PNG")
            else:
                if sanitized.mode not in {"RGB", "L"}:
                    converted = sanitized.convert("RGB")
                    sanitized.close()
                    sanitized = converted
                sanitized.save(sanitized_path, format="JPEG", quality=95)
        finally:
            sanitized.close()
        os.replace(sanitized_path, input_path)
    finally:
        sanitized_path.unlink(missing_ok=True)


def cleanup_stale_artifacts() -> None:
    """Remove app-owned artifacts left behind by a previous process crash."""
    cutoff = time.time() - STALE_ARTIFACT_SECONDS
    candidates = list(IMAGEDIR.glob("vefr3d-*"))
    candidates.extend(Path(comfyuiservice.model_path).glob("vefr3d-*"))
    stale_paths = []
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_mtime <= cutoff:
                stale_paths.append(str(path))
        except OSError as exc:
            logger.warning("Failed to inspect temporary artifact %s: %s", path.name, exc)
    cleanup_files(*stale_paths)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(cleanup_stale_artifacts)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)



@app.post("/gen-model/", dependencies=[Security(verify_api_key)])
async def create_upload_file(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not await _try_acquire_request_slot():
        raise HTTPException(
            status_code=503,
            detail="Generation queue is full.",
            headers={"Retry-After": "60"},
        )

    unique_id = f"vefr3d-{uuid.uuid4()}"
    input_path = None
    response_ready = False
    try:
        _, extension = os.path.splitext(file.filename or "")
        extension = extension.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported file type '{extension}'. Allowed: "
                    f"{', '.join(sorted(ALLOWED_EXTENSIONS))}"
                ),
            )

        IMAGEDIR.mkdir(parents=True, exist_ok=True)
        input_path = IMAGEDIR / f"{unique_id}{extension}"
        written = 0
        try:
            with input_path.open("wb") as destination:
                while True:
                    chunk = await file.read(CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"File too large. Max "
                                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
                            ),
                        )
                    destination.write(chunk)
        except HTTPException:
            raise
        except OSError as exc:
            logger.warning("Failed to save an uploaded image: %s", exc)
            raise HTTPException(
                status_code=500, detail="Failed to save uploaded image."
            ) from exc

        async with _generation_semaphore:
            try:
                await run_in_threadpool(
                    validate_and_sanitize_image, input_path, extension
                )
            except (
                UnidentifiedImageError,
                OSError,
                ValueError,
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
            ) as exc:
                raise HTTPException(
                    status_code=400,
                    detail="File is not a valid JPEG/PNG image or is too large.",
                ) from exc

            model_path = await run_in_threadpool(
                comfyuiservice.fetch_model_from_comfy, unique_id, extension
            )

        generated_artifacts = comfyuiservice.get_generated_artifacts(unique_id)
        if model_path is None:
            await run_in_threadpool(cleanup_files, *generated_artifacts)
            raise HTTPException(
                status_code=500, detail="Model generation failed or timed out."
            )

        if model_path not in generated_artifacts:
            generated_artifacts.append(model_path)
        background_tasks.add_task(cleanup_files, *generated_artifacts)

        response = FileResponse(
            path=model_path,
            filename=f"{unique_id}.glb",
            media_type="model/gltf-binary",
        )
        response_ready = True
        return response
    finally:
        await file.close()
        if input_path is not None:
            await run_in_threadpool(cleanup_files, str(input_path))
        if not response_ready:
            generated_artifacts = comfyuiservice.get_generated_artifacts(unique_id)
            await run_in_threadpool(cleanup_files, *generated_artifacts)
        await _release_request_slot()

@app.get("/hello")
def read_hello():
    return {"message": "Server is up and running!"}

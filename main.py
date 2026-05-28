from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, Security
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from starlette.middleware.trustedhost import TrustedHostMiddleware
import os
import uuid
import hmac
import asyncio
import comfyuiservice
from fastapi.concurrency import run_in_threadpool
from PIL import Image, UnidentifiedImageError
from pathlib import Path


ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
CHUNK = 1024 * 1024
CLEANUP_DELAY = 120
Image.MAX_IMAGE_PIXELS = 50_000_000



_trellis = Path(__file__).parent / "Trellis2"
IMAGEDIR = _trellis / "images"


_API_KEY = os.environ.get("CUI_API_KEY")
if not _API_KEY:
    raise RuntimeError("CUI_API_KEY environment variable is not set.")

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_api_key(key: str = Security(_api_key_header)):
    if not key or not hmac.compare_digest(key, _API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")

_ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("CUI_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()
]

app = FastAPI()
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)


_generation_semaphore = asyncio.Semaphore(1)

async def cleanup_files_task(*filepaths: str):
    if CLEANUP_DELAY > 0:
        await asyncio.sleep(CLEANUP_DELAY)
    
    for path in filepaths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
                print(f"Successfully cleaned up")
        except Exception as e:
            print(f"Error during cleanup")



@app.post("/gen-model/", dependencies=[Security(verify_api_key)])
async def create_upload_file(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    unique_id = str(uuid.uuid4())
    _, ext = os.path.splitext(file.filename or "")
    ext = ext.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")

    # Cheap pre-check using the header-reported size (may be None)
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max {MAX_UPLOAD_BYTES // (1024*1024)} MB."
        )

    inputpath = f"{IMAGEDIR}/{unique_id}{ext}"

    # Stream to disk in chunks, enforcing the cap as we go
    written = 0
    try:
        with open(inputpath, "wb") as f:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    f.close()
                    try:
                        os.remove(inputpath)
                    except OSError:
                        pass
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Max {MAX_UPLOAD_BYTES // (1024*1024)} MB."
                    )
                f.write(chunk)
    except HTTPException:
        raise
    except Exception:
        try:
            os.remove(inputpath)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail="Failed to save uploaded image. Image is too large")
    
    try:
        with Image.open(inputpath) as img:
            img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        try:
            os.remove(inputpath)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail="File is not a valid image or is too large.")

    #generate (limit to 1 concurrent generation)
    async with _generation_semaphore:
        model_path = await run_in_threadpool(
            comfyuiservice.fetch_model_from_comfy, unique_id, ext
        )
    
    if model_path is None:
        background_tasks.add_task(cleanup_files_task, inputpath)
        raise HTTPException(status_code=500, detail="Model generation failed or timed out.")

        
    background_tasks.add_task(cleanup_files_task, inputpath, model_path)
    
    print(f"Sending model to user: {model_path}")
    return FileResponse(
        path=model_path, 
        filename=f"{unique_id}.glb", 
        media_type="model/gltf-binary"
    )

@app.get("/hello")
def read_hello():
    return {"message": "Server is up and running!"}
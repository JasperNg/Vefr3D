import bpy
import os
import tempfile
import threading
import urllib.request
import uuid
import json
import ipaddress
import struct
from urllib.parse import urlsplit


_ADDON_ID = __package__ or __name__
MAX_MODEL_BYTES = 256 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 64 * 1024
GLB_HEADER = struct.Struct("<4sII")


def _validated_server_base_url(server_url: str) -> str:
    """Require HTTPS except for an explicitly loopback development server."""
    raw_url = server_url.strip().rstrip("/")
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
        # Accessing port validates malformed/out-of-range port values.
        parsed.port
    except ValueError as exc:
        raise ValueError("Server URL is malformed.") from exc

    if not hostname or parsed.scheme not in {"http", "https"}:
        raise ValueError("Server URL must use HTTPS.")
    if parsed.username or parsed.password:
        raise ValueError("Server URL must not contain credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("Server URL must not contain a query string or fragment.")

    is_loopback = hostname.lower() == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass

    if parsed.scheme != "https" and not is_loopback:
        raise ValueError("HTTPS is required for non-loopback servers.")

    return raw_url


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent redirects from forwarding the API key to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _response_content_length(response):
    raw_length = response.headers.get("Content-Length")
    if raw_length is None:
        return None
    try:
        content_length = int(raw_length)
    except ValueError as exc:
        raise RuntimeError("Server returned an invalid Content-Length header.") from exc
    if content_length < 0:
        raise RuntimeError("Server returned an invalid Content-Length header.")
    return content_length


def _validate_glb_header(header: bytes, actual_length: int, http_length=None) -> None:
    if len(header) < GLB_HEADER.size:
        raise RuntimeError("Server returned a truncated GLB file.")

    magic, version, declared_length = GLB_HEADER.unpack(header[:GLB_HEADER.size])
    if magic != b"glTF":
        raise RuntimeError("Server response is not a GLB file.")
    if version != 2:
        raise RuntimeError(f"Unsupported GLB version: {version}.")
    if declared_length != actual_length:
        raise RuntimeError("GLB header length does not match the downloaded file.")
    if declared_length > MAX_MODEL_BYTES:
        raise RuntimeError("Generated model exceeds the download size limit.")
    if http_length is not None and http_length != actual_length:
        raise RuntimeError("HTTP Content-Length does not match the downloaded file.")


def _read_server_error(response, content_type: str):
    response_body = response.read(MAX_ERROR_RESPONSE_BYTES + 1)
    if len(response_body) > MAX_ERROR_RESPONSE_BYTES:
        return f"Unexpected oversized response type: {content_type}"
    try:
        parsed = json.loads(response_body)
    except (ValueError, UnicodeDecodeError):
        return f"Unexpected response type: {content_type}"
    if not isinstance(parsed, dict):
        return f"Unexpected response type: {content_type}"
    return parsed.get("detail") or parsed.get("error") or response_body[:200]


def _download_glb_response(response) -> str:
    content_type = response.headers.get("Content-Type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "model/gltf-binary":
        raise RuntimeError(f"Server error: {_read_server_error(response, content_type)}")

    http_length = _response_content_length(response)
    if http_length is not None and http_length > MAX_MODEL_BYTES:
        raise RuntimeError("Generated model exceeds the download size limit.")

    temp_file = tempfile.NamedTemporaryFile(suffix=".glb", delete=False)
    temp_path = temp_file.name
    total_bytes = 0
    header = bytearray()
    try:
        while True:
            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_MODEL_BYTES:
                raise RuntimeError("Generated model exceeds the download size limit.")
            if len(header) < GLB_HEADER.size:
                needed = GLB_HEADER.size - len(header)
                header.extend(chunk[:needed])
            temp_file.write(chunk)

        temp_file.close()
        _validate_glb_header(bytes(header), total_bytes, http_length)
        return temp_path
    except Exception:
        if not temp_file.closed:
            temp_file.close()
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

# state_lock is a dictionary with the status of and the information from the daemon so that the main thread can reference it
_state_lock = threading.Lock()
_state = {
    "running": False,   # thread is running
    "done": False,      # thread os done
    "error": None,      # error message
    "glb_path": None,   # Path to temp GLB file
}


#Check value
def _state_get(key):
    with _state_lock:
        return _state[key]

#Write value
def _state_update(**kwargs):
    with _state_lock:
        _state.update(kwargs)

#Background thread - daemon that sends image to server and recieves the generated file
def _upload_and_fetch(image_path: str, server_url: str, api_key: str) -> None:
    try:
        url = _validated_server_base_url(server_url) + "/gen-model/"
        filename = os.path.basename(image_path)
        ext = os.path.splitext(filename)[1].lower()

        # Define accepted file types
        types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
        }.get(ext, "application/octet-stream")

        with open(image_path, "rb") as fh:
            image_bytes = fh.read()

        # ASCII-safe filename
        safe_name = filename.encode("ascii", "replace").decode("ascii")

        # Boundary to dilineate between file data and other info + uuid to generate random bytes
        boundary = f"----BlenderComfyUIBoundary{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {types}\r\n\r\n"
        ).encode("utf-8") + image_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

        # POST the image
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-API-Key": api_key,
            },
            method="POST",
        )

        # Timeout and check for JSON error
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(req, timeout=600) as resp:
            glb_path = _download_glb_response(resp)

        _state_update(glb_path=glb_path, done=True, running=False)

    except Exception as exc:
        _state_update(error=str(exc), done=True, running=False)

#-------------------Blender Classes-------------------------


def _addon_preferences(context):
    addon = context.preferences.addons.get(_ADDON_ID)
    if addon is None:
        raise RuntimeError("Vefr3D add-on preferences are unavailable.")
    return addon.preferences


class COMFYUI_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = _ADDON_ID

    comfyui_server_url: bpy.props.StringProperty(
        name="Server URL",
        description="HTTPS base URL of the FastAPI server (HTTP is allowed only on loopback)",
        default="http://localhost:8000",
    )
    comfyui_api_key: bpy.props.StringProperty(
        name="API Key",
        description="X-API-Key sent to the FastAPI server; never saved by Blender",
        default="",
        subtype="PASSWORD",
        options={"SKIP_SAVE"},
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "comfyui_server_url")
        layout.prop(self, "comfyui_api_key")

# Blender File Dialog Picker
class COMFYUI_OT_pick_image(bpy.types.Operator):
    bl_idname = "comfyui.pick_image"
    bl_label = "Pick Image"
    bl_options = {"INTERNAL"}

    filepath: bpy.props.StringProperty(
        subtype="FILE_PATH",
        options={"SKIP_SAVE"},
    )
    filter_glob: bpy.props.StringProperty(
        default="*.png;*.jpg;*.jpeg",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    
    #Opens file browser
    def invoke(self, context, event):
        default_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "testimages",
        )
        if os.path.isdir(default_dir):
            self.filepath = default_dir + os.sep
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}
    
    #Adds path to the plugin after user clicks accept
    def execute(self, context):
        context.scene.comfyui_image_path = self.filepath
        return {"FINISHED"}

# Main operator
class COMFYUI_OT_generate(bpy.types.Operator):
    bl_idname = "comfyui.generate"
    bl_label = "Generate 3D Model"
    bl_options = {"REGISTER"}

    _timer = None

    # Modal Status cheking loop

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        # Force a redraw if generation is not done
        if not _state_get("done"):
            self._set_header(context, "ComfyUI: generating 3D model, please wait…")
            for area in context.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
            return {"PASS_THROUGH"}

        # Generation finished (success or failure)
        self._remove_timer(context)
        self._set_header(context, None)

        # 
        error = _state_get("error")
        if error:
            self.report({"ERROR"}, f"Generation failed: {error}")
            self._redraw_view3d(context)
            return {"CANCELLED"}

        glb_path = _state_get("glb_path")
        if not glb_path or not os.path.exists(glb_path):
            self.report({"ERROR"}, "Server returned no model file.")
            self._redraw_view3d(context)
            return {"CANCELLED"}

        # Try to import and catch the error
        try:
            bpy.ops.import_scene.gltf(filepath=glb_path)
        except Exception as exc:
            self.report({"ERROR"}, f"GLB import failed: {exc}")
            return {"CANCELLED"}
        finally:
            try:
                os.unlink(glb_path)
            except OSError:
                pass
            self._redraw_view3d(context)

        self.report({"INFO"}, "3D model imported successfully.")
        return {"FINISHED"}

    #Main Thread
    def execute(self, context):
        if _state_get("running"):
            self.report({"WARNING"}, "A generation is already in progress.")
            return {"CANCELLED"}

        # Checks online access
        if not bpy.app.online_access:
            self.report(
                {"ERROR"},
                "Online access is disabled. Enable it in Preferences > System > Network.",
            )
            return {"CANCELLED"}

        # Checks if path and server url exists
        image_path = bpy.path.abspath(context.scene.comfyui_image_path)
        if not image_path:
            self.report({"ERROR"}, "No image selected.")
            return {"CANCELLED"}
        if not os.path.isfile(image_path):
            self.report({"ERROR"}, f"File not found: {image_path}")
            return {"CANCELLED"}

        try:
            preferences = _addon_preferences(context)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        server_url = preferences.comfyui_server_url
        if not server_url:
            self.report({"ERROR"}, "Server URL is empty.")
            return {"CANCELLED"}
        try:
            server_url = _validated_server_base_url(server_url)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        api_key = preferences.comfyui_api_key
        if not api_key:
            self.report({"ERROR"}, "API key is empty.")
            return {"CANCELLED"}

        # Reset state
        _state_update(running=True, done=False, error=None, glb_path=None)

        # Kick off background thread
        thread = threading.Thread(
            target=_upload_and_fetch,
            args=(image_path, server_url, api_key),
            daemon=True,
        )
        thread.start()

        # Register modal timer (polls every second)
        wm = context.window_manager
        self._timer = wm.event_timer_add(1.0, window=context.window)
        wm.modal_handler_add(self)

        self.report({"INFO"}, "Uploading image — waiting for server…")
        self._redraw_view3d(context)
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        self._remove_timer(context)
        self._set_header(context, None)
        _state_update(running=False)
        self._redraw_view3d(context)

    def _remove_timer(self, context):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

    @staticmethod
    def _set_header(context, text):
        try:
            if context.area is not None:
                context.area.header_text_set(text)
        except Exception:
            pass

    @staticmethod
    def _redraw_view3d(context):
        if context.screen is None:
            return
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()

# Blender UI Panel
class COMFYUI_PT_panel(bpy.types.Panel):
    bl_label = "Vefr3D"
    bl_idname = "COMFYUI_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Vefr3D"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Server URL + API key
        box = layout.box()
        box.label(text="Server", icon="WORLD")
        try:
            preferences = _addon_preferences(context)
        except RuntimeError as exc:
            box.label(text=str(exc), icon="ERROR")
            return
        box.prop(preferences, "comfyui_server_url", text="URL")
        box.prop(preferences, "comfyui_api_key", text="API Key")

        layout.separator()

        # Image picker
        box = layout.box()
        box.label(text="Input Image", icon="IMAGE_DATA")
        row = box.row(align=True)
        row.prop(scene, "comfyui_image_path", text="")
        row.operator("comfyui.pick_image", text="", icon="FILE_FOLDER")

        layout.separator()

        # Generate button / running indicator
        if _state_get("running"):
            col = layout.column()
            col.enabled = False
            col.operator("comfyui.generate", text="Generating…", icon="SORTTIME")
        else:
            layout.operator("comfyui.generate", text="Generate 3D Model", icon="MESH_CUBE")


# Registration - 
_classes = (
    COMFYUI_AddonPreferences,
    COMFYUI_OT_pick_image,
    COMFYUI_OT_generate,
    COMFYUI_PT_panel,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.comfyui_image_path = bpy.props.StringProperty(
        name="Image Path",
        description="Path to the input image (.png / .jpg / .jpeg)",
        default="",
        options={"SKIP_SAVE"},
    )


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.comfyui_image_path


if __name__ == "__main__":
    register()

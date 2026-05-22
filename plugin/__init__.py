import bpy
import os
import tempfile
import threading
import urllib.request
import uuid
import json

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
        url = server_url.rstrip("/") + "/gen-model/"
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
        with urllib.request.urlopen(req, timeout=600) as resp:
            content_type = resp.headers.get("Content-Type", "")
            response_body = resp.read()
            if "model/gltf-binary" not in content_type:
                # Try to decode JSON error from server
                try:
                    parsed = json.loads(response_body)
                    err = parsed.get("detail") or parsed.get("error") or response_body[:200]
                except ValueError:
                    err = f"Unexpected response type: {content_type}"
                raise RuntimeError(f"Server error: {err}")
            glb_bytes = response_body

        # Write to a named temp file that the main thread can import
        tmp = tempfile.NamedTemporaryFile(suffix=".glb", delete=False)
        tmp.write(glb_bytes)
        tmp.close()

        _state_update(glb_path=tmp.name, done=True, running=False)

    except Exception as exc:
        _state_update(error=str(exc), done=True, running=False)

#-------------------Blender Classes-------------------------

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

        server_url = context.scene.comfyui_server_url
        if not server_url:
            self.report({"ERROR"}, "Server URL is empty.")
            return {"CANCELLED"}

        api_key = context.scene.comfyui_api_key
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
        box.prop(scene, "comfyui_server_url", text="URL")
        box.prop(scene, "comfyui_api_key", text="API Key")

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
    COMFYUI_OT_pick_image,
    COMFYUI_OT_generate,
    COMFYUI_PT_panel,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.comfyui_server_url = bpy.props.StringProperty(
        name="Server URL",
        description="Base URL of the FastAPI server (main.py)",
        default="http://localhost:8000",
    )
    bpy.types.Scene.comfyui_image_path = bpy.props.StringProperty(
        name="Image Path",
        description="Path to the input image (.png / .jpg / .jpeg / .webp)",
        default="",
    )
    bpy.types.Scene.comfyui_api_key = bpy.props.StringProperty(
        name="API Key",
        description="X-API-Key sent to the FastAPI server",
        default="",
        subtype="PASSWORD",
    )


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.comfyui_server_url
    del bpy.types.Scene.comfyui_image_path
    del bpy.types.Scene.comfyui_api_key


if __name__ == "__main__":
    register()
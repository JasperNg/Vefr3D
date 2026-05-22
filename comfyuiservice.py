import websocket
import uuid
import json
import time
import urllib.request
from pathlib import Path



server_address = "127.0.0.1:8188"
trellis = Path(__file__).parent / "Trellis2"
imgpath = trellis / "images"
model_path = trellis / "ComfyUI-Easy-Install" / "ComfyUI" / "output"
workflow_path = Path(__file__).parent / "workflow.json"

GENERATION_TIMEOUT = 600
WS_RECV_TIMEOUT = 30

def get_prompt_with_workflow(input, ext):
    with open(workflow_path, "r", encoding="utf-8") as f:
        prompt_json = json.load(f)
    prompt_json["176"]["inputs"]["image"] = f'"{imgpath}\{input}{ext}"'
    prompt_json["166"]["inputs"]["value"] = input
    return prompt_json

def queue_prompt(prompt, client_id):
    p = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(p).encode('utf-8')
    req =  urllib.request.Request("http://{}/prompt".format(server_address), data=data)
    return json.loads(urllib.request.urlopen(req).read())

def get_history(prompt_id):
    with urllib.request.urlopen(f"http://{server_address}/history/{prompt_id}") as resp:
        return json.loads(resp.read())

def get_model(ws, prompt, filename_to_find, client_id):
    prompt_id = queue_prompt(prompt, client_id)['prompt_id']
    ws.settimeout(WS_RECV_TIMEOUT)
    deadline = time.monotonic() + GENERATION_TIMEOUT
    completed = False

    while time.monotonic() < deadline:
        try:
            out = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except Exception as e:
            print(f"WebSocket error while waiting for prompt {prompt_id}: {e}")
            return None

        if not isinstance(out, str):
            # Binary preview frames; ignore.
            continue

        try:
            message = json.loads(out)
        except ValueError:
            continue

        msg_type = message.get('type')
        data = message.get('data', {})

        # ComfyUI signals failure via these messages.
        if msg_type in ('execution_error', 'execution_interrupted'):
            if data.get('prompt_id') == prompt_id:
                print(f"ComfyUI reported {msg_type} for prompt {prompt_id}: {data}")
                return None

        if msg_type == 'executing':
            if data.get('node') is None and data.get('prompt_id') == prompt_id:
                completed = True
                break

    if not completed:
        print(f"Timed out waiting for prompt {prompt_id} after {GENERATION_TIMEOUT}s")
        return None

    # Poll the history endpoint to confirm completion and pick up the file
    # once it's been flushed to disk (avoids race with the executing message).
    poll_deadline = time.monotonic() + 30
    while time.monotonic() < poll_deadline:
        try:
            history = get_history(prompt_id)
        except Exception as e:
            print(f"Failed to fetch history for {prompt_id}: {e}")
            return None

        entry = history.get(prompt_id)
        if entry and entry.get('status', {}).get('completed'):
            files = list(Path(model_path).glob(f'{filename_to_find}*.glb'))
            if files:
                return str(files[0])
            # Completed but file not visible yet; brief retry.
            time.sleep(0.5)
            files = list(Path(model_path).glob(f'{filename_to_find}*.glb'))
            return str(files[0]) if files else None

        time.sleep(0.5)

    print(f"History never reported completion for {prompt_id}")
    return None

def fetch_model_from_comfy(input_name, ext):
    client_id = str(uuid.uuid4())
    ws = websocket.WebSocket()
    try:
        ws.connect(f"ws://{server_address}/ws?clientId={client_id}", timeout=10)
    except Exception as e:
        print(f"Failed to connect to ComfyUI websocket: {e}")
        return None
    try:
        prompt = get_prompt_with_workflow(input_name, ext)
        modelpath = get_model(ws, prompt, input_name, client_id)
    except Exception as e:
        print(f"Error during ComfyUI generation: {e}")
        modelpath = None
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return str(modelpath) if modelpath else None
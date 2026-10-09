import asyncio
import json
from collections import defaultdict
from fastapi import WebSocket

class ScreencastBroadcaster:
    def __init__(self):
        self.clients = defaultdict(list)
        # task_id -> {page_id: latest_base64_frame}
        self.latest_frames = defaultdict(dict)
        
    async def connect(self, task_id: str, websocket: WebSocket):
        await websocket.accept()
        self.clients[task_id].append(websocket)
        # Send latest frames for all active pages in this task
        for page_id, frame in self.latest_frames[task_id].items():
            try:
                payload = json.dumps({"page_id": page_id, "data": frame})
                await websocket.send_text(payload)
            except Exception:
                pass
            
    def disconnect(self, task_id: str, websocket: WebSocket):
        if websocket in self.clients[task_id]:
            self.clients[task_id].remove(websocket)
        
    async def broadcast(self, task_id: str, page_id: str, base64_image: str):
        self.latest_frames[task_id][page_id] = base64_image
        payload = json.dumps({"page_id": page_id, "data": base64_image})
        for ws in list(self.clients[task_id]):
            try:
                await ws.send_text(payload)
            except Exception:
                self.disconnect(task_id, ws)

    def clear(self, task_id: str):
        self.latest_frames.pop(task_id, None)
        self.clients.pop(task_id, None)

screencast_stream = ScreencastBroadcaster()

import socketserver
import threading
from typing import Optional

from ..config import WFDMediaConfig
from ..constants import WFD_RTSP_PORT
from ..media.pipeline import WFDMediaPipeline
from .handler import _WFDRTSPHandler


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

class WFDRTSPServer:
    def __init__(
        self,
        media_config: WFDMediaConfig,
        host: str = "0.0.0.0",
        port: int = WFD_RTSP_PORT,
    ) -> None:
        self.host = host
        self.port = port
        self.media_config = media_config
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.has_connected_client = False
        self._media_lock = threading.Lock()
        self._active_media: list[WFDMediaPipeline] = []
        self._uibc_server = None  # opt-in UIBC input server; None unless enabled

    def _register_media(self, media: WFDMediaPipeline) -> None:
        with self._media_lock:
            self._active_media.append(media)

    def _unregister_media(self, media: WFDMediaPipeline) -> None:
        with self._media_lock:
            try:
                self._active_media.remove(media)
            except ValueError:
                pass

    def stop_all_media(self) -> None:
        with self._media_lock:
            pipelines = list(self._active_media)
        for pipeline in pipelines:
            pipeline.stop()

    def start(self) -> None:
        self._server = _ThreadingTCPServer((self.host, self.port), _WFDRTSPHandler)
        self._server.media_config = self.media_config  # type: ignore[attr-defined]
        self._server.parent_server = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        print(f"[FluxCast WFD RTSP] Server listening on {self.host}:{self.port}")

    def stop(self) -> None:
        if self._uibc_server is not None:
            self._uibc_server.stop()
            self._uibc_server = None
        if self._server:
            self._server.shutdown()
            self._server.server_close()

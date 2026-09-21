import socketserver
import threading
import time
from typing import Optional

from ..config import WFDMediaConfig
from ..constants import WFD_RTSP_PORT
from ..media.pipeline import WFDMediaPipeline
from ..p2p.addressing import (
    _is_expected_peer_ip,
    _is_p2p_group_iface,
    _valid_interface,
)
from .handler import _WFDRTSPHandler


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def verify_request(self, request, client_address) -> bool:
        """Reject other hosts before ThreadingMixIn creates a handler thread."""
        parent = getattr(self, "parent_server", None)
        client_ip = client_address[0]
        if parent is not None and parent.authenticate_client(client_ip):
            return True
        print(f"[FluxCast WFD RTSP] Rejected unverified client from {client_ip}")
        return False


class WFDRTSPServer:
    def __init__(
        self,
        media_config: WFDMediaConfig,
        peer_address: str,
        interface: Optional[str] = None,
        host: str = "0.0.0.0",
        port: int = WFD_RTSP_PORT,
    ) -> None:
        self.host = host
        self.port = port
        self.media_config = media_config
        self.peer_address = peer_address
        self.interface: Optional[str] = None
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        # A socket reserves ownership first. The flag only becomes true after
        # that owner produces meaningful WFD negotiation traffic.
        self.has_connected_client = False
        self._auth_lock = threading.Lock()
        self._group_interface_ready = threading.Event()
        if interface is not None:
            self.set_group_interface(interface)
        self._client_lock = threading.Lock()
        self._connected_client: Optional[str] = None
        self._client_claim = 0
        self._media_lock = threading.Lock()
        self._active_media: list[WFDMediaPipeline] = []
        self._uibc_server = None  # opt-in UIBC input server; None unless enabled

    def set_group_interface(self, interface: str) -> bool:
        """Set the session's P2P group interface."""
        if not _valid_interface(interface) or not _is_p2p_group_iface(interface):
            return False
        with self._auth_lock:
            if self.interface is not None and self.interface != interface:
                return False
            self.interface = interface
            self._group_interface_ready.set()
            return True

    def authenticate_client(self, client_ip: str) -> bool:
        """Check the receiver address, serializing neighbour lookups."""
        # The listener starts before P2P activation so passive receivers do not
        # race a closed port. If one connects as the group comes up, briefly
        # wait for the backend to publish the exact group interface.
        if not self._group_interface_ready.wait(timeout=2.0):
            return False
        # The neighbour entry can lag behind the group interface. Share one
        # deadline across lock acquisition, queries and retry sleeps.
        deadline = time.monotonic() + 2.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._auth_lock.acquire(timeout=remaining):
                return False
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if _is_expected_peer_ip(
                    client_ip,
                    self.peer_address,
                    self.interface,
                    timeout=remaining,
                ):
                    return time.monotonic() < deadline
            finally:
                self._auth_lock.release()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # Let another receiver check its identity between retries.
            time.sleep(min(0.1, remaining))

    def claim_client(
        self,
        client_ip: str,
        *,
        replace_unconfirmed: bool = False,
    ) -> Optional[int]:
        """Reserve the selected receiver and return an ownership generation."""
        if not self.authenticate_client(client_ip):
            return None
        with self._client_lock:
            if self._connected_client is not None:
                if not replace_unconfirmed or self.has_connected_client:
                    return None
            self._client_claim += 1
            self._connected_client = client_ip
            self.has_connected_client = False
            return self._client_claim

    def confirm_client(self, client_ip: str, claim: int) -> bool:
        """Confirm that the current claim produced valid negotiation traffic."""
        with self._client_lock:
            if self._connected_client != client_ip or self._client_claim != claim:
                return False
            self.has_connected_client = True
            return True

    def release_client(self, client_ip: str, claim: int) -> None:
        with self._client_lock:
            if self._connected_client == client_ip and self._client_claim == claim:
                self._connected_client = None
                self.has_connected_client = False

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

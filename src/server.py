import http.server
import os
import socketserver
import threading
import time
from typing import Optional
from urllib.parse import unquote, urlsplit


HLS_DIR = "/tmp/fluxcast"
HLS_SEGMENT_SECONDS = 1.0
LIVE_TS_SIZE = 1_000_000_000_000
LIVE_TS_PREROLL_SEGMENTS = 1
PROGRESSIVE_TS_SIZE = 10_000_000_000
PROGRESSIVE_TS_PATH = os.path.join(HLS_DIR, "progressive.ts")
PROGRESSIVE_TS_CHUNK_SIZE = 128 * 1024
PROGRESSIVE_TS_SEEK_TOLERANCE = 8 * 1024 * 1024

DLNA_FLAGS = "01700000000000000000000000000000"
HLS_CONTENT_FEATURES = (
    "DLNA.ORG_OP=01;"
    "DLNA.ORG_CI=0;"
    f"DLNA.ORG_FLAGS={DLNA_FLAGS}"
)
TS_CONTENT_FEATURES = (
    "DLNA.ORG_PN=AVC_TS_MP_HD_AAC;"
    "DLNA.ORG_OP=01;"
    "DLNA.ORG_CI=0;"
    f"DLNA.ORG_FLAGS={DLNA_FLAGS}"
)


def _url_path(raw_path: str) -> str:
    return unquote(urlsplit(raw_path).path)


def _content_type(path: str) -> str:
    if path.endswith(".m3u8"):
        return "application/x-mpegurl"
    if path.endswith(".ts"):
        return "video/mp2t"
    return "application/octet-stream"


def _content_features(path: str) -> str:
    if path.endswith(".ts"):
        return TS_CONTENT_FEATURES
    return HLS_CONTENT_FEATURES


class ProgressiveTS:

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._segments: set[str] = set()
        self._size = 0
        self._generation = 0
        self._active_readers = 0

    def reset(self) -> None:
        with self._cond:
            self._generation += 1
            self._started = False
            self._segments.clear()
            self._size = 0
            self._thread = None
            self._active_readers = 0
            self._cond.notify_all()
        try:
            os.remove(PROGRESSIVE_TS_PATH)
        except FileNotFoundError:
            pass

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            generation = self._generation
            self._thread = threading.Thread(
                target=self._run,
                args=(generation,),
                daemon=True,
            )
            self._thread.start()

    def size(self) -> int:
        with self._lock:
            return self._size

    def restart_at_live_edge_if_idle(self) -> bool:
        with self._lock:
            should_restart = self._size > 0 and self._active_readers == 0
        if not should_restart:
            return False

        self.reset()
        self.start()
        return True

    def acquire_reader(self) -> None:
        with self._lock:
            self._active_readers += 1

    def release_reader(self) -> None:
        with self._lock:
            self._active_readers = max(0, self._active_readers - 1)

    def wait_for_size(self, size: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._size < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=min(remaining, 0.25))
            return True

    def read_at(self, offset: int, max_bytes: int) -> bytes:
        if offset < 0 or max_bytes <= 0:
            return b""
        with self._lock:
            available = self._size
        if offset >= available:
            return b""

        length = min(max_bytes, available - offset)
        try:
            with open(PROGRESSIVE_TS_PATH, "rb") as file:
                file.seek(offset)
                return file.read(length)
        except OSError:
            return b""

    @staticmethod
    def _playlist_segments() -> list[str]:
        playlist = os.path.join(HLS_DIR, "stream.m3u8")
        try:
            with open(playlist, "r", encoding="utf-8", errors="replace") as file:
                lines = file.read().splitlines()
        except OSError:
            return []

        return [
            line.strip() for line in lines
            if line.strip().endswith(".ts") and "/" not in line.strip()
        ]

    def _append_segment(self, segment: str, generation: int) -> None:
        local_path = os.path.join(HLS_DIR, segment)
        try:
            with open(local_path, "rb") as source:
                data = source.read()
        except OSError:
            return
        if not data:
            return

        with self._cond:
            if not (self._started and self._generation == generation):
                return
            with open(PROGRESSIVE_TS_PATH, "ab") as output:
                output.write(data)
            self._segments.add(segment)
            self._size += len(data)
            self._cond.notify_all()

    def _is_current_generation(self, generation: int) -> bool:
        with self._lock:
            return self._started and self._generation == generation

    def _run(self, generation: int) -> None:
        os.makedirs(HLS_DIR, exist_ok=True)
        try:
            open(PROGRESSIVE_TS_PATH, "wb").close()
        except OSError:
            return

        while self._is_current_generation(generation):
            segments = self._playlist_segments()
            if not segments:
                time.sleep(0.05)
                continue

            with self._lock:
                empty = self._size == 0
                known = set(self._segments)

            if empty:
                pending = segments[-1:]
            else:
                pending = [segment for segment in segments if segment not in known]

            if not pending:
                time.sleep(0.05)
                continue

            for segment in pending:
                self._append_segment(segment, generation)


_progressive_ts = ProgressiveTS()


class HLSRequestHandler(http.server.BaseHTTPRequestHandler):
    """Serves HLS files and the progressive TS bridge for Samsung DLNA."""

    protocol_version = "HTTP/1.1"
    _log_lock = threading.Lock()
    _playlist_log_count = 0

    def log_message(self, format, *args):
        return

    def _local_path(self) -> Optional[str]:
        path = _url_path(self.path).lstrip("/")
        if not path:
            path = "stream.m3u8"
        normalized = os.path.normpath(path)
        if normalized.startswith("..") or os.path.isabs(normalized):
            return None
        basename = os.path.basename(normalized)
        if basename == "stream.m3u8" or (
            basename.startswith("stream") and basename.endswith(".ts")
        ):
            normalized = basename
        return os.path.join(HLS_DIR, normalized)

    def _send_common_headers(self, local_path: str) -> None:
        self.send_header("Content-Type", _content_type(local_path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Connection", "close")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", _content_features(local_path))

    def _send_empty(self, status: int) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def _parse_range(self, size: int) -> Optional[tuple[int, int, bool]]:
        raw = self.headers.get("Range")
        if not raw:
            return None
        if not raw.startswith("bytes=") or "," in raw:
            return None

        start_text, _, end_text = raw[6:].partition("-")
        try:
            if start_text == "":
                suffix_len = int(end_text)
                if suffix_len <= 0:
                    return None
                return max(0, size - suffix_len), size - 1, True

            start = int(start_text)
            has_explicit_end = bool(end_text)
            end = int(end_text) if has_explicit_end else size - 1
        except ValueError:
            return None

        if start >= size:
            return None
        return start, min(end, size - 1), has_explicit_end

    def _playlist_segments(self) -> list[str]:
        playlist = os.path.join(HLS_DIR, "stream.m3u8")
        try:
            with open(playlist, "r", encoding="utf-8", errors="replace") as file:
                lines = file.read().splitlines()
        except OSError:
            lines = []

        segments = [
            line.strip() for line in lines
            if line.strip().endswith(".ts") and "/" not in line.strip()
        ]
        if segments:
            return segments

        paths = sorted(
            path for path in os.listdir(HLS_DIR)
            if path.startswith("stream") and path.endswith(".ts")
        )
        return paths[-3:]

    @staticmethod
    def _segment_index(segment: str) -> Optional[int]:
        name = os.path.basename(segment)
        if not (name.startswith("stream") and name.endswith(".ts")):
            return None
        try:
            return int(name[len("stream"):-len(".ts")])
        except ValueError:
            return None

    def _log_file_response(
        self,
        local_path: str,
        status: int,
        length: int,
        request_range: Optional[str],
    ) -> None:
        if local_path.endswith(".m3u8"):
            with self._log_lock:
                type(self)._playlist_log_count += 1
                count = type(self)._playlist_log_count
            if count > 5 and count % 20 != 0 and not request_range:
                return

            segments = self._playlist_segments()
            last_segment = segments[-1] if segments else "none"
            range_suffix = f", Range: {request_range}" if request_range else ""
            print(
                f"[FluxCast Server] HLS playlist #{count} -> {self.client_address[0]} "
                f"({status}, {length} bytes, last={last_segment}{range_suffix})"
            )
            return

        if local_path.endswith(".ts"):
            segment_name = os.path.basename(local_path)
            segment_idx = self._segment_index(segment_name)
            live_segments = self._playlist_segments()
            live_idx = self._segment_index(live_segments[-1]) if live_segments else None
            lag_suffix = ""
            if segment_idx is not None and live_idx is not None:
                behind_segments = live_idx - segment_idx
                if 0 <= behind_segments < 1000:
                    behind_seconds = behind_segments * HLS_SEGMENT_SECONDS
                    lag_suffix = f", behind={behind_segments} seg/{behind_seconds:.1f}s"
                else:
                    lag_suffix = ", behind=unknown (mixed playlist cache)"
            range_suffix = f", Range: {request_range}" if request_range else ""
            print(
                f"[FluxCast Server] HLS segment {segment_name} -> {self.client_address[0]} "
                f"({status}, {length // 1024} KiB{lag_suffix}{range_suffix})"
            )
            return

        if request_range:
            print(
                f"[FluxCast Server] {self.client_address[0]} -> {self.path} "
                f"({status}, {length} bytes, Range: {request_range})"
            )

    def _playlist_body(self, local_path: str) -> bytes:
        try:
            with open(local_path, "r", encoding="utf-8", errors="replace") as file:
                lines = file.read().splitlines()
        except OSError:
            return b""

        rewritten = []
        inserted_start = False
        for line in lines:
            if line == "#EXT-X-TARGETDURATION:0":
                rewritten.append("#EXT-X-TARGETDURATION:1")
                continue
            if line.startswith("#EXT-X-START:"):
                continue
            rewritten.append(line)
            if line.startswith("#EXT-X-INDEPENDENT-SEGMENTS"):
                rewritten.append("#EXT-X-START:TIME-OFFSET=-1.0,PRECISE=YES")
                inserted_start = True

        if not inserted_start:
            for idx, line in enumerate(rewritten):
                if line.startswith("#EXT-X-TARGETDURATION:"):
                    rewritten.insert(idx + 1, "#EXT-X-START:TIME-OFFSET=-1.0,PRECISE=YES")
                    break

        return ("\n".join(rewritten) + "\n").encode("utf-8")

    def _send_live_ts_headers(self, status: int, start: int, end: int) -> None:
        length = end - start + 1
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "video/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Connection", "close")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", TS_CONTENT_FEATURES)
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{LIVE_TS_SIZE}")
        self.end_headers()

    def _serve_live_ts(self, send_body: bool) -> None:
        byte_range = self._parse_range(LIVE_TS_SIZE)
        if byte_range is None:
            start, end, has_explicit_end = 0, LIVE_TS_SIZE - 1, False
            status = 200
        else:
            start, end, has_explicit_end = byte_range
            status = 206

        self._send_live_ts_headers(status, start, end)
        if not send_body:
            return

        request_range = self.headers.get("Range")
        range_suffix = f", Range: {request_range}" if request_range else ""
        print(
            f"[FluxCast Server] EXPERIMENTAL live-ts {self.client_address[0]} -> /live.ts "
            f"({status}{range_suffix})"
        )

        sent = 0
        last_segment = None
        try:
            while sent <= end - start:
                segments = self._playlist_segments()
                if not segments:
                    time.sleep(0.05)
                    continue

                if last_segment is None:
                    pending_segments = segments[-LIVE_TS_PREROLL_SEGMENTS:]
                elif last_segment in segments:
                    pending_segments = segments[segments.index(last_segment) + 1:]
                else:
                    pending_segments = segments[-LIVE_TS_PREROLL_SEGMENTS:]

                if not pending_segments:
                    time.sleep(0.05)
                    continue

                for segment in pending_segments:
                    local_path = os.path.join(HLS_DIR, segment)
                    try:
                        with open(local_path, "rb") as file:
                            data = file.read()
                    except OSError:
                        continue
                    if not data:
                        continue

                    if has_explicit_end:
                        remaining = end - start + 1 - sent
                        data = data[:remaining]

                    self.wfile.write(data)
                    self.wfile.flush()
                    sent += len(data)
                    last_segment = segment

                    if has_explicit_end and sent >= end - start + 1:
                        return
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _parse_progressive_range(
        self,
    ) -> Optional[tuple[int, int, bool, int]]:
        raw = self.headers.get("Range")
        if not raw:
            return None
        if not raw.startswith("bytes=") or "," in raw:
            return None

        start_text, _, end_text = raw[6:].partition("-")
        advertised_size = PROGRESSIVE_TS_SIZE
        try:
            if start_text == "":
                suffix_len = int(end_text)
                if suffix_len <= 0:
                    return None
                return max(0, advertised_size - suffix_len), advertised_size - 1, True, advertised_size

            start = int(start_text)
            has_explicit_end = bool(end_text)
            if start < 0:
                return None

            if start >= advertised_size:
                advertised_size = start + PROGRESSIVE_TS_SIZE
            end = int(end_text) if has_explicit_end else advertised_size - 1
        except ValueError:
            return None

        if end < start:
            return None
        return start, end, has_explicit_end, advertised_size

    def _send_progressive_ts_headers(
        self,
        status: int,
        start: int,
        end: int,
        advertised_size: int,
    ) -> None:
        length = end - start + 1
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "video/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Connection", "close")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", TS_CONTENT_FEATURES)
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{advertised_size}")
        self.end_headers()

    def _send_progressive_range_error(self, current_size: int) -> None:
        self.close_connection = True
        self.send_response(416)
        self.send_header("Content-Type", "video/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes */{current_size}")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", TS_CONTENT_FEATURES)
        self.end_headers()

    def _serve_progressive_ts(self, send_body: bool) -> None:
        _progressive_ts.start()

        request_range = self.headers.get("Range")
        byte_range = self._parse_progressive_range()
        current_size = _progressive_ts.size()

        if request_range and byte_range is None:
            self._send_progressive_range_error(current_size)
            print(
                f"[FluxCast Server] progressive-ts rejected invalid range from "
                f"{self.client_address[0]} (416, current={current_size // 1024} KiB, "
                f"Range: {request_range})"
            )
            return

        if byte_range is None:
            start, end, has_explicit_end = 0, PROGRESSIVE_TS_SIZE - 1, False
            advertised_size = PROGRESSIVE_TS_SIZE
            status = 200
        else:
            start, end, has_explicit_end, advertised_size = byte_range
            status = 206

        read_start = start
        live_seek_suffix = ""
        is_future_seek = start > current_size + PROGRESSIVE_TS_SEEK_TOLERANCE
        if is_future_seek:
            read_start = current_size
            live_seek_suffix = f", live-seek {start}->{read_start}"

        restart_suffix = ""
        if send_body and (start == 0 or is_future_seek) and _progressive_ts.restart_at_live_edge_if_idle():
            current_size = _progressive_ts.size()
            read_start = 0 if is_future_seek else start
            restart_suffix = ", live-edge restart"

        reader_acquired = False
        if send_body:
            _progressive_ts.acquire_reader()
            reader_acquired = True
            wait_before_headers = not is_future_seek
            if wait_before_headers and not _progressive_ts.wait_for_size(read_start + 1, timeout=10.0):
                _progressive_ts.release_reader()
                reader_acquired = False
                self._send_empty(503)
                print(
                    f"[FluxCast Server] progressive-ts has no data for "
                    f"{self.client_address[0]} (503, start={start}, read_start={read_start})"
                )
                return

        self._send_progressive_ts_headers(status, start, end, advertised_size)
        if not send_body:
            return

        range_suffix = f", Range: {request_range}" if request_range else ""
        print(
            f"[FluxCast Server] progressive-ts -> {self.client_address[0]} "
            f"({status}, start={start}, current={_progressive_ts.size() // 1024} KiB"
            f"{restart_suffix}{live_seek_suffix}{range_suffix})"
        )

        sent = 0
        total_length = end - start + 1
        started_at = time.monotonic()
        try:
            while sent < total_length:
                offset = read_start + sent
                if not _progressive_ts.wait_for_size(offset + 1, timeout=15.0):
                    break

                chunk = _progressive_ts.read_at(
                    offset,
                    min(PROGRESSIVE_TS_CHUNK_SIZE, total_length - sent),
                )
                if not chunk:
                    time.sleep(0.03)
                    continue

                self.wfile.write(chunk)
                self.wfile.flush()
                sent += len(chunk)

                if has_explicit_end and sent >= total_length:
                    return
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if reader_acquired:
                _progressive_ts.release_reader()
            elapsed = time.monotonic() - started_at
            print(
                f"[FluxCast Server] progressive-ts closed for {self.client_address[0]} "
                f"(sent={sent // 1024} KiB, {elapsed:.1f}s{live_seek_suffix})"
            )

    def _serve_file(self, send_body: bool) -> None:
        local_path = self._local_path()
        if local_path is None or not os.path.isfile(local_path):
            self._send_empty(404)
            return

        playlist_body = self._playlist_body(local_path) if local_path.endswith(".m3u8") else None
        size = len(playlist_body) if playlist_body is not None else os.path.getsize(local_path)
        byte_range = self._parse_range(size)
        self.close_connection = True

        if byte_range is None:
            start, end = 0, size - 1
            status = 200
        else:
            start, end, _ = byte_range
            status = 206

        length = max(0, end - start + 1)
        self.send_response(status)
        self._send_common_headers(local_path)
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if not send_body:
            return

        request_range = self.headers.get("Range")
        self._log_file_response(local_path, status, length, request_range)

        if playlist_body is not None:
            self.wfile.write(playlist_body[start:start + length])
            return

        with open(local_path, "rb") as file:
            file.seek(start)
            remaining = length
            while remaining > 0:
                chunk = file.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_HEAD(self):
        path = _url_path(self.path)
        if path.endswith("/progressive.ts"):
            self._serve_progressive_ts(send_body=False)
            return
        if path.endswith("/live.ts"):
            self._serve_live_ts(send_body=False)
            return
        self._serve_file(send_body=False)

    def do_GET(self):
        path = _url_path(self.path)
        if path.endswith("/progressive.ts"):
            self._serve_progressive_ts(send_body=True)
            return
        if path.endswith("/live.ts"):
            self._serve_live_ts(send_body=True)
            return
        self._serve_file(send_body=True)


class StreamServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self._server: Optional[socketserver.TCPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, capture_process=None) -> None:
        os.makedirs(HLS_DIR, exist_ok=True)
        _progressive_ts.reset()
        self._server = http.server.ThreadingHTTPServer(
            (self.host, self.port), HLSRequestHandler
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        _progressive_ts.reset()

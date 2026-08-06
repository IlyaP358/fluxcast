import shutil
import subprocess
from typing import Optional

from capture.portal_capture import PortalCaptureSession, close_portal_capture
from ..config import WFDMediaConfig, WFDNotReady
from ..env import _is_hyprland_session, _is_wayland_session, _wfd_capture_backend_order
from ..gst import _gst_wfd_sender_available
from ..net import _interface_for_ip, _netdev_tx_bytes, _rtp_url
from .portal import PortalMixin
from .testpattern import TestPatternMixin
from .wlroots import WlrootsMixin
from .x11 import X11Mixin


class WFDMediaPipeline(TestPatternMixin, PortalMixin, X11Mixin, WlrootsMixin):
    def __init__(
        self,
        config: WFDMediaConfig,
        tv_ip: str,
        local_ip: str,
        sink_rtp_port: int,
    ) -> None:
        self.config = config
        self.tv_ip = tv_ip
        self.local_ip = local_ip
        self.sink_rtp_port = sink_rtp_port
        self.processes: list[subprocess.Popen[bytes]] = []
        self.tx_interface: Optional[str] = None
        self.tx_baseline: Optional[int] = None
        self.portal_session: Optional[PortalCaptureSession] = None
        self._portal_gst_cmd: Optional[list[str]] = None
        self._portal_pw_fd: Optional[int] = None
        self._lpcm_muxer = None   # WFDLPCMMuxer instance for Microsoft adapter

    def start(self) -> None:
        if self.processes:
            return

        self.tx_interface = _interface_for_ip(self.local_ip)
        self.tx_baseline = _netdev_tx_bytes(self.tx_interface)

        requested_pipeline = self.config.media_pipeline
        pipeline = requested_pipeline
        if pipeline == "auto":
            pipeline = "gst" if self.config.test_pattern and _gst_wfd_sender_available() else "ffmpeg"
        # mpegtsmux has no way to set the PSI version_number, so the AOSP
        # layout is only reachable through ffmpeg (#84).
        if self.config.aosp_pmt_pid and pipeline == "gst":
            if requested_pipeline == "auto":
                print("[FluxCast WFD Media] AOSP TS layout requested; using the ffmpeg sender "
                      "(mpegtsmux cannot set the PAT/PMT version).")
                pipeline = "ffmpeg"
            else:
                print("[FluxCast WFD Media] WARNING --wfd-media-pipeline gst cannot set the "
                      "PAT/PMT version to AOSP's 1; the sink may ignore the tables.")

        if pipeline == "gst":
            if not self.config.test_pattern:
                raise WFDNotReady("GStreamer WFD sender is currently implemented for --wfd-test-pattern only.")
            try:
                self._start_gst_test_pattern()
            except WFDNotReady:
                if requested_pipeline == "auto":
                    print("[FluxCast WFD Media] GStreamer sender failed to start; falling back to ffmpeg.")
                    self._start_test_pattern()
                else:
                    raise
        elif self.config.test_pattern:
            self._start_test_pattern()
        else:
            self._start_desktop()

    def tx_summary(self) -> str:
        current = _netdev_tx_bytes(self.tx_interface)
        if self.tx_baseline is None or current is None:
            return "tx=unknown"
        delta = max(0, current - self.tx_baseline)
        return f"tx+{delta // 1024} KiB on {self.tx_interface}"

    def stop(self) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        self.processes.clear()
        if self._lpcm_muxer is not None:
            self._lpcm_muxer.stop()
            self._lpcm_muxer = None
        close_portal_capture(self.portal_session)
        self.portal_session = None

    def restart_video(self) -> None:
        if self._portal_gst_cmd is None or self._portal_pw_fd is None:
            return
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        self.processes.clear()
        new_proc = subprocess.Popen(
            self._portal_gst_cmd,
            stderr=None,
            pass_fds=(self._portal_pw_fd,),
        )
        self.processes = [new_proc]
        print("[FluxCast WFD Media] Pipeline restarted for IDR request.")

    def _rtp_output(self) -> str:
        return _rtp_url(self.tv_ip, self.sink_rtp_port, self.config.source_port, self.local_ip)

    def _common_output_args(self) -> list[str]:
        """
        Low-latency RTP/MPEG-TS output args.
        """
        if self.config.dump_ts_path:
            # A second ffmpeg output would get its own encoder (mpeg2video), so
            # the dump would not be the stream we transmit. Only gst can tee it.
            print(
                "[FluxCast WFD TS] --wfd-dump-ts is not supported on the ffmpeg "
                "pipeline; re-run with --wfd-media-pipeline gst to capture it."
            )

        aosp_args: list[str] = []
        if self.config.aosp_pmt_pid:
            # AOSP's TSPacketizer stamps version_number=1 on PAT and PMT
            # (0xc3); ffmpeg and mpegtsmux both default to 0 (0xc1). A sink
            # that seeds its "last seen version" at 0 treats our tables as
            # already-parsed and never picks up the program (#84).
            # sdt_period only thins the SDT out, it cannot be removed.
            aosp_args = ["-tables_version", "1", "-sdt_period", "1000000"]

        return [
            "-muxdelay", "0",
            "-muxpreload", "0",
            "-flush_packets", "1",
            # WFD receivers (notably Samsung) are sensitive to MPEG-TS layout.
            # Keep PMT/video/audio PID values aligned with the working gst path!!!
            # PMT PID 0x1000, video PID 0x1011, audio PID 0x1100.
            "-mpegts_pmt_start_pid", "256" if self.config.aosp_pmt_pid else "4096",
            "-mpegts_start_pid", "4113",
            "-streamid", "0:4113",
            "-mpegts_flags", "resend_headers+pat_pmt_at_frames",
            "-pat_period", "0.1",
            "-pcr_period", "20",
            *aosp_args,
            "-f", "rtp_mpegts",
            self._rtp_output(),
        ]



    def _start_desktop(self) -> None:
        if not shutil.which("ffmpeg"):
            raise WFDNotReady("ffmpeg is required for WFD desktop streaming.")

        backends = _wfd_capture_backend_order(self.config)
        if self.config.aosp_pmt_pid and "gst-x11" in backends:
            print("[FluxCast WFD Media] WARNING the gst-x11 backend muxes with mpegtsmux, "
                  "which cannot set the PAT/PMT version to AOSP's 1.")
        errors: list[str] = []
        for idx, backend in enumerate(backends):
            try:
                if backend == "x11grab":
                    self._start_desktop_x11grab()
                elif backend == "gst-x11":
                    self._start_desktop_gst_x11()
                elif backend == "portal":
                    # mpegtsmux cannot stamp the PSI version, so the AOSP
                    # layout needs ffmpeg to do the muxing (#84).
                    if self.config.aosp_pmt_pid:
                        self._start_desktop_portal_ffmpeg()
                    else:
                        self._start_desktop_portal()
                else:
                    self._start_desktop_wf_recorder()
                return
            except WFDNotReady as exc:
                errors.append(f"{backend}: {exc}")
                if idx < len(backends) - 1:
                    print(f"[FluxCast WFD Media] Backend {backend} failed, trying fallback...")
        detail = "; ".join(errors) if errors else "No usable capture backend"
        if self.config.capture_backend == "auto" and _is_wayland_session() and not _is_hyprland_session():
            detail += (
                "; KDE/GNOME Wayland desktop capture uses portal backend in this build. "
                "Install dbus-next + xdg-desktop-portal stack + gst-launch-1.0, "
                "then allow screen-share in the portal picker dialog."
            )
        raise WFDNotReady(detail)








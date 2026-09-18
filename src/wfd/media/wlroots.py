import os
import shutil
import subprocess
import threading
import time

from ..config import WFDNotReady
from ..wf_recorder import find_wf_recorder, wf_recorder_supports_icc
from ..encoding import (
    _bitrate_to_kbits, _calculate_gop, _kbits_to_bitrate_text, _letterbox_vf,
    _parse_resolution, _quality_floor_kbits, _vbv_bufsize,
)
from ..env import _detect_audio_monitor
from ..hw_encode import (
    apply_bitrate_plan,
    build_encode_plan,
    capture_encode_attempts,
    capture_encode_mode,
    capture_encode_preference,
    prefer_wf_recorder_vaapi_dmabuf,
)
from ..power_plan import active_power_plan, encode_throttled
from ..latency import _append_latency_log
from ..modes import _h264_level_for_mode
from ..net import _ffmpeg_sender_args


class WlrootsMixin:
    @staticmethod
    def _enlarge_pipe_fd(fd: int, size: int | None = None) -> int:
        """Raise Linux pipe capacity (both ends). Default 8 MiB for raw NV12 nut.

        The wf-recorder → ffmpeg raw pipe used to stay at the kernel default
        (64 KiB). One brief VAAPI pause filled it, wf-recorder's buffer pool
        exploded (``bufs_size`` 5→16), and video RTP died while audio continued.
        H264→muxer was already 1 MiB; enlarge the raw pipe to match real frame
        pressure. Honors ``FLUXCAST_WFD_RAW_PIPE_BYTES``; falls back if
        ``fs.pipe-max-size`` is lower.
        """
        import fcntl

        if size is None:
            raw = (os.environ.get("FLUXCAST_WFD_RAW_PIPE_BYTES", "") or "").strip()
            if raw.isdigit():
                size = max(65536, int(raw))
            else:
                size = 8 << 20
        try:
            fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, size)
            return size
        except OSError:
            for candidate in (1 << 20, 512 * 1024, 256 * 1024):
                if candidate >= size:
                    continue
                try:
                    fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, candidate)
                    return candidate
                except OSError:
                    continue
        return 0

    def _start_ffmpeg_io_watchdog(
        self,
        ffmpeg_proc: subprocess.Popen,
        *,
        stall_seconds: float = 4.0,
        poll_seconds: float = 1.0,
    ) -> None:
        """Rebind capture if ffmpeg IO freezes while the process stays alive.

        Catches VAAPI pipe wedges faster than waiting for bufs_size / TX stalls.
        Disabled with FLUXCAST_WFD_FFMPEG_IO_WATCHDOG=0.
        """
        flag = (os.environ.get("FLUXCAST_WFD_FFMPEG_IO_WATCHDOG") or "1").strip().lower()
        if flag in ("0", "false", "no", "off"):
            return
        pid = getattr(ffmpeg_proc, "pid", None)
        if not pid:
            return

        def _io_counters(p: int) -> tuple[int, int] | None:
            try:
                data = {}
                with open(f"/proc/{p}/io", encoding="utf-8") as fh:
                    for line in fh:
                        if ":" not in line:
                            continue
                        k, v = line.split(":", 1)
                        data[k.strip()] = int(v.strip())
                return data.get("rchar", 0), data.get("wchar", 0)
            except (OSError, ValueError):
                return None

        def _run() -> None:
            last = _io_counters(pid)
            flat_for = 0.0
            while ffmpeg_proc.poll() is None:
                time.sleep(poll_seconds)
                if getattr(self, "restarting", False):
                    flat_for = 0.0
                    last = _io_counters(pid)
                    continue
                cur = _io_counters(pid)
                if cur is None or last is None:
                    last = cur
                    flat_for = 0.0
                    continue
                if cur[0] == last[0] and cur[1] == last[1]:
                    flat_for += poll_seconds
                else:
                    flat_for = 0.0
                    last = cur
                if flat_for >= stall_seconds:
                    print(
                        "[FluxCast WFD Media] ffmpeg IO watchdog: "
                        f"rchar/wchar flat for {flat_for:.0f}s "
                        f"(pid={pid}); rebinding desktop capture",
                        flush=True,
                    )
                    try:
                        self.restart_video()
                    except Exception as exc:  # noqa: BLE001
                        print(
                            "[FluxCast WFD Media] ffmpeg IO watchdog "
                            f"rebind failed: {exc}",
                            flush=True,
                        )
                    return

        threading.Thread(
            target=_run,
            name="ffmpeg-io-watchdog",
            daemon=True,
        ).start()

    def _emit_capture_encode(
        self,
        *,
        capture_path: str,
        encoder: str,
        preference: str,
        fallback: bool,
    ) -> None:
        # Remember path so mid-session restart_video can leave a wedged DMA-BUF
        # session for VAAPI pipe instead of recreating the same freeze.
        self._last_capture_path = capture_path
        _append_latency_log(
            getattr(self.config, "latency_log_path", None),
            "capture_encode",
            capture_path=capture_path,
            encoder=encoder,
            preference=preference,
            fallback=bool(fallback),
        )

    def _start_desktop_wf_recorder(self) -> None:
        wf_recorder = find_wf_recorder()
        if not wf_recorder:
            raise WFDNotReady("wf-recorder is required for WFD desktop streaming.")

        monitor = self.config.monitor
        if monitor is None:
            raise WFDNotReady("wf-recorder backend requires a selected monitor.")

        preference = capture_encode_preference()
        # Mid-session rebind may lock to pipe VAAPI/CPU so a hang does not
        # silently fall through the full engine chain.
        engine_lock = getattr(self, "_restart_engine_lock", None)
        if engine_lock in ("vaapi", "pipe"):
            attempts = ["vaapi"]
            print(
                "[FluxCast WFD Media] Mid-session rebind locked to GPU · VAAPI "
                "(pipe); skipping engine fallback chain",
                flush=True,
            )
        elif engine_lock == "cpu":
            attempts = ["cpu"]
            print(
                "[FluxCast WFD Media] Mid-session rebind locked to CPU "
                "(libx264); skipping engine fallback chain",
                flush=True,
            )
        else:
            attempts = capture_encode_attempts(preference)
        last_exc: Exception | None = None

        for index, attempt in enumerate(attempts):
            fallback = index > 0 and engine_lock is None
            try:
                if attempt == "dmabuf":
                    if not prefer_wf_recorder_vaapi_dmabuf(monitor):
                        raise WFDNotReady(
                            "DMA-BUF not available (VAAPI missing, scaled deny, "
                            "or RENDER ENGINE is not dmabuf)"
                        )
                    if getattr(self.config, "prefer_lpcm", False) and not self.config.no_audio:
                        self._start_wf_recorder_lpcm(wf_recorder, monitor)
                    else:
                        self._start_wf_recorder_vaapi_dmabuf(wf_recorder, monitor)
                    self._emit_capture_encode(
                        capture_path="dmabuf",
                        encoder="h264_vaapi",
                        preference=preference,
                        fallback=fallback,
                    )
                    if fallback:
                        print(
                            "[FluxCast WFD Media] RENDER ENGINE fell back to "
                            "GPU · DMA-BUF"
                        )
                    return

                encoder = "vaapi" if attempt == "vaapi" else "libx264"
                if attempt == "vaapi" and fallback:
                    print(
                        "[FluxCast WFD Media] DMA-BUF failed; trying GPU · VAAPI "
                        "(pipe hwupload)"
                    )
                elif attempt == "cpu" and fallback:
                    print(
                        "[FluxCast WFD Media] RENDER ENGINE fell back to CPU "
                        "(libx264)"
                    )
                # LPCM-only sinks cannot play AAC from the plain pipe mux; use
                # annex-B + WFDLPCMMuxer while keeping the raw NV12 pipe encode.
                if getattr(self.config, "prefer_lpcm", False) and not self.config.no_audio:
                    plan_name = self._start_wf_recorder_raw_pipe_lpcm(
                        wf_recorder, monitor, encoder_override=encoder
                    )
                else:
                    plan_name = self._start_wf_recorder_raw_pipe(
                        wf_recorder, monitor, encoder_override=encoder
                    )
                resolved = plan_name or encoder
                if resolved == "vaapi":
                    resolved = "h264_vaapi"
                elif resolved == "qsv":
                    resolved = "h264_qsv"
                self._emit_capture_encode(
                    capture_path="pipe",
                    encoder=resolved,
                    preference=preference,
                    fallback=fallback,
                )
                return
            except WFDNotReady as exc:
                last_exc = exc
                print(
                    f"[FluxCast WFD Media] RENDER ENGINE {attempt} failed ({exc})"
                )
                continue

        if last_exc is not None:
            raise last_exc
        raise WFDNotReady("No RENDER ENGINE capture path succeeded.")

    def _desktop_bitrate_plan(self, monitor):
        src_res = f"{monitor.width}x{monitor.height}"
        out_res = self.config.output_resolution or src_res
        gop = _calculate_gop(self.config)
        parsed_out = _parse_resolution(out_res) or (monitor.width, monitor.height)
        requested_kbits = _bitrate_to_kbits(self.config.bitrate)
        floor_kbits = _quality_floor_kbits(parsed_out[0], parsed_out[1], self.config.fps)
        plan = active_power_plan()
        throttled = encode_throttled(plan)
        # AOSP/Miracast adaptive bitrate (encode.env / congestion-cut) must not
        # be silently raised back to the 8 Mbps "desktop clarity" floor — that
        # fought every ×0.6 cut and left artifacts while settings said 3M.
        honor = (
            (os.environ.get("FLUXCAST_WFD_HONOR_BITRATE") or "").strip().lower()
            in ("1", "true", "yes", "on")
            or (os.environ.get("FLUXCAST_WFD_ENCODE_ENV_FILE") or "").strip() != ""
        )
        if throttled or honor:
            effective_kbits = requested_kbits
        else:
            effective_kbits = max(requested_kbits, floor_kbits)
        effective_bitrate = _kbits_to_bitrate_text(effective_kbits)
        effective_bitrate = apply_bitrate_plan(effective_bitrate, throttled=throttled)
        if effective_kbits > requested_kbits and not throttled and not honor:
            print(
                "[FluxCast WFD Media] Raising bitrate for desktop clarity: "
                f"{self.config.bitrate} -> {effective_bitrate}"
            )
        elif honor and requested_kbits < floor_kbits:
            print(
                "[FluxCast WFD Media] Honoring adaptive bitrate "
                f"{effective_bitrate} (below clarity floor {_kbits_to_bitrate_text(floor_kbits)})"
            )
        return {
            "src_res": src_res,
            "out_res": out_res,
            "gop": gop,
            "parsed_out": parsed_out,
            "power_plan": plan.id,
            "power_plan_name": plan.name,
            "throttled": throttled,
            # Deprecated alias for older log formatters.
            "bias": plan.id,
            "effective_bitrate": effective_bitrate,
            "effective_kbits": _bitrate_to_kbits(effective_bitrate),
            "level": _h264_level_for_mode(self.config),
            "vf_scale": None if out_res == src_res else _letterbox_vf(out_res),
        }

    def _wf_damage_flag(self) -> list[str]:
        # Default: -D (continuous). FLUXCAST_WFD_WF_RECORDER_DAMAGE=1 omits -D.
        damage_aware = os.environ.get("FLUXCAST_WFD_WF_RECORDER_DAMAGE", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        return [] if damage_aware else ["-D"]

    def _wf_capture_rate_args(
        self, wf_recorder: str, *, vaapi_dmabuf: bool = False
    ) -> list[str]:
        """Capture cadence flags for this wf-recorder build.

        Stock wlr-screencopy DMA: omit ``-r`` (it appends ``fps=N`` after
        ``scale_vaapi`` and forces VAAPI→software conversion).

        VAAPI DMA encode (``vaapi_dmabuf=True``): also omit ``-r``. Encoder
        BRC still gets ``-p framerate=`` from ``_vaapi_rc_wf_params``. ICC
        without ``-r`` defaults to 60 Hz client pacing — same target fps
        without the post-``scale_vaapi`` fps filter tax.

        ICC raw/pipe paths: pass ``-r`` as the client request rate.
        """
        if vaapi_dmabuf:
            return []
        if wf_recorder_supports_icc(wf_recorder):
            # -r matches config.fps (encode framerate). Override:
            # FLUXCAST_WFD_ICC_CAPTURE_FPS.
            icc_fps = (os.environ.get("FLUXCAST_WFD_ICC_CAPTURE_FPS") or "").strip()
            if not icc_fps:
                try:
                    icc_fps = str(int(self.config.fps))
                except (TypeError, ValueError, AttributeError):
                    icc_fps = "30"
            return ["-r", icc_fps]
        return []

    def _maybe_rebind_for_impairment(self, reason: str) -> None:
        """Restart desktop capture after DTS/pool spikes (debounced)."""
        now = time.monotonic()
        last = float(getattr(self, "_last_impairment_rebind", 0.0) or 0.0)
        # Pipe + continuous -D storms need faster recovery than generic DTS.
        pipe_fast = reason in ("buffer-pool", "bufs-size-storm")
        debounce = 8.0 if pipe_fast else 20.0
        if now - last < debounce:
            return
        if getattr(self, "restarting", False):
            return
        self._last_impairment_rebind = now
        print(
            f"[FluxCast WFD Media] Auto-rebind after {reason} "
            "(buffer-pool / DTS stutter recovery)",
            flush=True,
        )
        try:
            self.restart_video()
        except Exception as exc:
            print(f"[FluxCast WFD Media] Auto-rebind failed: {exc}", flush=True)

    def _watch_sender_stderr(self, proc: subprocess.Popen, label: str) -> None:
        """Relay sender stderr and trigger rebind on known impairment lines."""

        def _run() -> None:
            if proc.stderr is None:
                return
            import re as _re

            pool_hits = 0
            window_start = time.monotonic()
            bufs_vals: list[tuple[float, int]] = []
            try:
                for raw in iter(proc.stderr.readline, b""):
                    try:
                        line = raw.decode(errors="replace").rstrip()
                    except Exception:
                        continue
                    if line:
                        print(line, flush=True)
                    now = time.monotonic()
                    if now - window_start > 5.0:
                        pool_hits = 0
                        window_start = now
                        bufs_vals = [v for v in bufs_vals if now - v[0] <= 5.0]
                    low = line.lower()
                    if "non monotonically" in low or "non-monotonically" in low:
                        self._maybe_rebind_for_impairment("dts")
                    elif "buffer pool full" in low:
                        pool_hits += 1
                        if pool_hits >= 2:
                            self._maybe_rebind_for_impairment("buffer-pool")
                    else:
                        m = _re.search(r"bufs_size:\s*(\d+)", line)
                        if m:
                            n = int(m.group(1))
                            bufs_vals.append((now, n))
                            # Keep ~2s of samples for climb detection.
                            recent = [v for v in bufs_vals if now - v[0] <= 2.0]
                            bufs_vals = recent
                            peak = max(v for _, v in recent) if recent else n
                            start = recent[0][1] if recent else n
                            storm = peak >= 14 or (peak - start) >= 6
                            if storm:
                                _append_latency_log(
                                    getattr(self.config, "latency_log_path", None),
                                    "bufs_size_storm",
                                    peak=peak,
                                    start=start,
                                    samples=len(recent),
                                    label=label,
                                )
                                # One strong storm is enough on continuous -D.
                                self._maybe_rebind_for_impairment("bufs-size-storm")
            except Exception:
                pass
            finally:
                try:
                    proc.stderr.close()
                except Exception:
                    pass

        threading.Thread(
            target=_run, name=f"fluxcast-{label}-stderr", daemon=True
        ).start()

    def _wf_vaapi_h264_profile(self) -> str:
        """wf-recorder ``-p profile=`` matching negotiated WFD profile (CHP→high)."""
        from ..hw_encode import _map_h264_profile

        mapped = _map_h264_profile(getattr(self.config, "h264_profile", None) or "baseline")
        if mapped in ("high", "main", "constrained_baseline"):
            return mapped
        return "constrained_baseline"

    def _vaapi_rc_wf_params(self, meta: dict) -> tuple[list[str], str]:
        """Build wf-recorder ``-p`` rate-control args for h264_vaapi.

        Env:
          FLUXCAST_WFD_VAAPI_RC — CQP (default), CBR, or VBR
          FLUXCAST_WFD_VAAPI_QP — CQP quantizer (default 18)
          FLUXCAST_WFD_VAAPI_BITRATE — CBR/VBR target (e.g. 12M); falls back
            to the desktop bitrate plan
          FLUXCAST_WFD_VAAPI_GOP — GOP frames (default = stream fps ≈ 1s)
        """
        rc = (os.environ.get("FLUXCAST_WFD_VAAPI_RC", "") or "CQP").strip().upper()
        if rc not in ("CQP", "CBR", "VBR", "AVBR", "QVBR", "ICQ"):
            rc = "CQP"
        quality = (os.environ.get("FLUXCAST_WFD_VAAPI_QUALITY", "") or "4").strip() or "4"
        _gop_env = (os.environ.get("FLUXCAST_WFD_VAAPI_GOP", "") or "").strip()
        try:
            gop = max(1, int(_gop_env)) if _gop_env else max(meta["gop"], int(self.config.fps))
        except (TypeError, ValueError):
            gop = max(meta["gop"], int(self.config.fps))

        # Prefer caller async_depth (High uses 2); wf-recorder defaults to 1.
        _async = (os.environ.get("FLUXCAST_WFD_VAAPI_ASYNC_DEPTH", "") or "2").strip() or "2"
        try:
            async_depth = str(max(1, min(4, int(_async))))
        except ValueError:
            async_depth = "2"

        # Match M4 WFD profile: CHP → high, CBP → constrained_baseline.
        profile = self._wf_vaapi_h264_profile()

        if rc == "CQP":
            qp = (os.environ.get("FLUXCAST_WFD_VAAPI_QP", "") or "18").strip() or "18"
            i_qfactor = (os.environ.get("FLUXCAST_WFD_VAAPI_I_QFACTOR", "") or "").strip()
            params = [
                "-p", f"rc_mode={rc}",
                "-p", f"qp={qp}",
                "-p", f"gop_size={gop}",
                "-p", f"quality={quality}",
                "-p", f"async_depth={async_depth}",
                "-p", "bf=0",
                "-p", f"profile={profile}",
                "-p", f"framerate={self.config.fps}",
            ]
            if i_qfactor:
                params.extend(["-p", f"i_qfactor={i_qfactor}"])
            desc = (
                f"{rc} qp={qp}, gop={gop}, quality={quality}, async={async_depth}, "
                f"profile={profile}"
                + (f", i_qfactor={i_qfactor}" if i_qfactor else "")
            )
        else:
            # Target bitrate: stream/config bitrate; VAAPI_BITRATE is the peak
            # cap for QVBR/VBR (matches pipe ffmpeg -b:v / -maxrate split).
            # Always set maxrate — without it, busy scenes can exceed a 20 MHz
            # Miracast P2P budget (~12 Mbps reliable peak).
            target = (os.environ.get("FLUXCAST_WFD_BITRATE", "") or "").strip()
            if not target:
                target = meta.get("effective_bitrate") or self.config.bitrate or "10M"
            peak = (os.environ.get("FLUXCAST_WFD_VAAPI_BITRATE", "") or "").strip()
            if not peak:
                peak = target
            # AVOption name is ``b`` (bits/s), not ``bitrate``. Set before
            # rc_mode so CBR validation sees a target.
            br_bits = _bitrate_to_kbits(target) * 1000
            peak_bits = _bitrate_to_kbits(peak) * 1000
            if peak_bits < br_bits:
                peak_bits = br_bits
                peak = target
            # Intel HRD needs bufsize or BRC undershoots (~2–3 Mbps). QVBR
            # needs a larger VBV than CBR or it sits at ~3 Mbps despite b=.
            try:
                default_vbv = "2.0" if rc == "QVBR" else "0.5"
                vbv_mult = float(
                    (os.environ.get("FLUXCAST_WFD_VBV_MULTIPLIER", "") or default_vbv).strip()
                    or default_vbv
                )
            except ValueError:
                vbv_mult = 2.0 if rc == "QVBR" else 0.5
            vbv_mult = max(0.25, min(4.0, vbv_mult))
            buf_bits = max(br_bits // 4, int(peak_bits * vbv_mult))
            params = [
                "-p", f"b={br_bits}",
                "-p", f"rc_mode={rc}",
                "-p", f"gop_size={gop}",
                "-p", f"quality={quality}",
                "-p", f"async_depth={async_depth}",
                "-p", "bf=0",
                "-p", f"profile={profile}",
                "-p", f"framerate={self.config.fps}",
                "-p", f"bufsize={buf_bits}",
            ]
            if rc == "QVBR":
                # Fixed qp= makes Intel QVBR treat quality as already met and
                # ignore b=/maxrate (~3 Mbps air). Use Samsung qmin/qmax bounds
                # so bitrate drives; qp env is only a soft mid hint via qfactor skip.
                qmin = (os.environ.get("FLUXCAST_WFD_VAAPI_QMIN", "") or "").strip()
                qmax = (os.environ.get("FLUXCAST_WFD_VAAPI_QMAX", "") or "").strip()
                if not qmin:
                    qmin = (os.environ.get("FLUXCAST_WFD_ENCODE_QP_MIN", "") or "15").strip() or "15"
                if not qmax:
                    qmax = (os.environ.get("FLUXCAST_WFD_ENCODE_QP_MAX", "") or "44").strip() or "44"
                # ~70% minrate floor so Intel QVBR cannot sit at ~3 Mbps.
                min_bits = max(br_bits // 4, (br_bits * 7) // 10)
                params.extend(
                    [
                        "-p", f"qmin={qmin}",
                        "-p", f"qmax={qmax}",
                        "-p", f"maxrate={peak_bits}",
                        "-p", f"minrate={min_bits}",
                    ]
                )
                desc = (
                    f"{rc} b={target} max={peak} min≈{min_bits} "
                    f"qmin={qmin} qmax={qmax} buf={buf_bits}, gop={gop}, "
                    f"quality={quality}, async={async_depth}, profile={profile}"
                )
            elif rc in ("VBR", "AVBR"):
                params.extend(["-p", f"maxrate={peak_bits}"])
                desc = (
                    f"{rc} b={target} max={peak} buf={buf_bits}, "
                    f"gop={gop}, quality={quality}, async={async_depth}, "
                    f"profile={profile}"
                )
            elif rc == "CBR":
                # HRD: maxrate == target.
                params.extend(["-p", f"maxrate={br_bits}"])
                desc = (
                    f"{rc} bitrate={target} buf={buf_bits}, "
                    f"gop={gop}, quality={quality}, async={async_depth}, "
                    f"profile={profile}"
                )
            else:
                desc = (
                    f"{rc} bitrate={target}, gop={gop}, quality={quality}, "
                    f"profile={profile}"
                )
        return params, desc

    def _start_wf_recorder_lpcm(self, wf_recorder: str, monitor) -> None:
        """DMA-BUF H.264 + WFD LPCM mux for LPCM-only sinks (e.g. many TVs).

        ffmpeg mpegtsmux cannot emit WFD stream_type 0x83; reuse WFDLPCMMuxer
        with wf-recorder annex-B video and Pulse *sink monitor* PCM (same
        device string as ``wf-recorder --audio=``).
        """
        meta = self._desktop_bitrate_plan(monitor)
        rc_params, rc_desc = self._vaapi_rc_wf_params(meta)
        audio_monitor = self._normalize_sink_monitor(
            self.config.audio_device or _detect_audio_monitor() or ""
        )

        try:
            from drivers.wfd_lpcm_mux import WFDLPCMMuxer
        except ImportError as exc:
            raise WFDNotReady(f"WFDLPCMMuxer unavailable: {exc}") from exc

        out_w, out_h = meta["parsed_out"]
        device = os.environ.get("FLUXCAST_VAAPI_DEVICE", "").strip() or "/dev/dri/renderD128"
        if meta["out_res"] != meta["src_res"]:
            vf = f"scale_vaapi=w={out_w}:h={out_h}:format=nv12:out_range=tv"
        else:
            vf = "scale_vaapi=format=nv12:out_range=tv"

        r_fd, w_fd = os.pipe()
        os.set_inheritable(r_fd, True)
        os.set_inheritable(w_fd, True)
        # Enlarge pipe capacity so a slow muxer does not block the recorder.
        try:
            import fcntl

            pipe_sz = 1 << 20  # 1 MiB
            fcntl.fcntl(r_fd, fcntl.F_SETPIPE_SZ, pipe_sz)
            fcntl.fcntl(w_fd, fcntl.F_SETPIPE_SZ, pipe_sz)
        except OSError:
            pass
        ar_fd, aw_fd = os.pipe()
        os.set_inheritable(ar_fd, True)
        os.set_inheritable(aw_fd, True)

        print(f"[FluxCast WFD Media] Capturing screen : {monitor.name} ({meta['src_res']})")
        print(
            f"[FluxCast WFD Media] Capturing audio  : {audio_monitor} "
            "(sink monitor via pw-cat — not mic)"
        )
        print(
            "[FluxCast WFD Media] Video encoder   : h264_vaapi DMA-BUF + "
            f"WFDLPCMMuxer (stream_type=0x83, {rc_desc})"
        )
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        # Bind RTP before spawning capture (fail cleanly without orphans).
        muxer = WFDLPCMMuxer(
            self.tv_ip,
            self.sink_rtp_port,
            local_ip=self.local_ip,
            local_port=self.config.source_port,
            bind_iface=self.tx_interface,
        )

        # appsink: max-buffers=1 drop=true (drop whole AUs; no intermediate queue).
        vid_pipeline = (
            f"fdsrc fd={r_fd} do-timestamp=true ! "
            "h264parse config-interval=-1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=sink sync=false max-buffers=1 drop=true"
        )
        # pw-cat → S16LE pipe; convert to S16BE for WFD LPCM.
        aud_pipeline = (
            f"fdsrc fd={ar_fd} do-timestamp=true ! "
            "audio/x-raw,format=S16LE,rate=48000,channels=2,"
            "layout=interleaved ! "
            "audioconvert ! audio/x-raw,format=S16BE ! "
            "audiobuffersplit output-buffer-size=1920 ! "
            "appsink name=sink sync=false max-buffers=32 drop=false"
        )

        try:
            muxer.start(vid_pipeline, aud_pipeline)
        except Exception:
            os.close(r_fd)
            os.close(w_fd)
            os.close(ar_fd)
            os.close(aw_fd)
            muxer.stop()
            raise

        # Same -D / DAMAGE policy as the DMA paths (_wf_damage_flag).
        # Omit -r on VAAPI DMA (see _wf_capture_rate_args vaapi_dmabuf=True).
        wf_cmd = [
            wf_recorder,
            "-y",
            *self._wf_damage_flag(),
            *self._wf_capture_rate_args(wf_recorder, vaapi_dmabuf=True),
            "-o", monitor.name,
            "-c", "h264_vaapi",
            "-d", device,
            "-b", "0",
            "-F", vf,
            *rc_params,
            "-m", "h264",
            "-f", "/dev/stdout",
        ]
        wf_proc = subprocess.Popen(
            wf_cmd,
            stdout=w_fd,
            stderr=subprocess.PIPE,
            pass_fds=(w_fd,),
        )
        os.close(w_fd)
        self._watch_sender_stderr(wf_proc, "wf-recorder-lpcm")

        # pw-cat --target binds the sink .monitor node (S16LE).
        # media.role=Abstract avoids Pulse stream-restore remapping.
        aud_cmd = self._lpcm_audio_capture_cmd(audio_monitor)
        aud_proc = subprocess.Popen(
            aud_cmd,
            stdout=aw_fd,
            stderr=None,
            pass_fds=(aw_fd,),
        )
        os.close(aw_fd)

        time.sleep(2.0)
        if (
            wf_proc.poll() is not None
            or aud_proc.poll() is not None
            or not muxer._mux_thread
            or not muxer._mux_thread.is_alive()
        ):
            muxer.stop()
            for proc in (wf_proc, aud_proc):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()
            os.close(r_fd)
            os.close(ar_fd)
            raise WFDNotReady("WFD LPCM muxer failed to stay up")

        self._lpcm_muxer = muxer
        self.processes.append(wf_proc)
        self.processes.append(aud_proc)
        self._lpcm_video_fd = r_fd
        self._lpcm_audio_fd = ar_fd
        print(
            f"[FluxCast WFD Media] LPCM muxer up "
            f"(audio_frames={muxer.audio_frames_sent}, video_frames={muxer.frames_sent})"
        )

    def _start_wf_recorder_vaapi_dmabuf(self, wf_recorder: str, monitor) -> None:
        """Capture+encode on GPU (DMA-BUF); ffmpeg only remuxes to RTP.

        Encode flags:
        - scale_vaapi → NV12 with out_range=tv (limited).
        - Stock DMA: omit ``-r``; ICC: ``-r`` via ``_wf_capture_rate_args``.
        - ``bf=0`` + profile from negotiated WFD (CHP→high, else CBP).
        """
        meta = self._desktop_bitrate_plan(monitor)
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        # RC from _vaapi_rc_wf_params (CQP default; CBR via FLUXCAST_WFD_VAAPI_RC).
        rc_params, rc_desc = self._vaapi_rc_wf_params(meta)
        out_w, out_h = meta["parsed_out"]
        device = os.environ.get("FLUXCAST_VAAPI_DEVICE", "").strip() or "/dev/dri/renderD128"

        # out_range=tv (limited) for Miracast sinks.
        if meta["out_res"] != meta["src_res"]:
            vf = f"scale_vaapi=w={out_w}:h={out_h}:format=nv12:out_range=tv"
        else:
            vf = "scale_vaapi=format=nv12:out_range=tv"

        icc = wf_recorder_supports_icc(wf_recorder)
        wf_cmd = [
            wf_recorder,
            "-y",
            *self._wf_damage_flag(),
            *self._wf_capture_rate_args(wf_recorder, vaapi_dmabuf=True),
            # -b 0 = max b-frames (not bitrate).
            "-o", monitor.name,
            "-c", "h264_vaapi",
            "-d", device,
            "-b", "0",
            "-F", vf,
            *rc_params,
            "-m", "nut",
            "-f", "/dev/stdout",
        ]
        # AAC in the same nut container as video (single ffmpeg input).
        if not self.config.no_audio and audio_monitor:
            wf_cmd[1:1] = [f"--audio={audio_monitor}", "-C", "aac", "-R", "48000"]

        # Keep demux queue small — 4096 buffered nut packets can add hundreds of
        # ms of glass-to-glass lag. Override with FLUXCAST_WFD_THREAD_QUEUE_SIZE.
        _tqs = (os.environ.get("FLUXCAST_WFD_THREAD_QUEUE_SIZE", "") or "64").strip() or "64"
        ffmpeg_cmd = [
            *_ffmpeg_sender_args(self.config.ffmpeg_stats),
            "-fflags", "+genpts",
            "-thread_queue_size", _tqs,
            "-f", "nut",
            "-i", "pipe:0",
            "-map", "0:v:0",
            "-c:v", "copy",
        ]
        if not self.config.no_audio and audio_monitor:
            ffmpeg_cmd += [
                "-map", "0:a:0",
                "-c:a", "copy",
                "-streamid", "1:4352",
            ]

        ffmpeg_cmd += self._common_output_args()

        print(f"[FluxCast WFD Media] Capturing screen : {monitor.name} ({meta['src_res']})")
        if not self.config.no_audio:
            print(f"[FluxCast WFD Media] Capturing audio  : {audio_monitor} (via wf-recorder)")
        if meta["out_res"] != meta["src_res"]:
            print(f"[FluxCast WFD Media] Scaling output  : {meta['out_res']}")
        proto = "icc" if icc else "wlr-screencopy"
        print(
            f"[FluxCast WFD Media] Video encoder   : h264_vaapi via wf-recorder "
            f"DMA-BUF on {device} ({meta['power_plan']} / {meta['power_plan_name']}, "
            f"{rc_desc}, tv-range, no-bframes, proto={proto})"
        )
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        self._spawn_wf_ffmpeg(wf_cmd, ffmpeg_cmd)

    def _normalize_sink_monitor(self, audio_monitor: str) -> str:
        """Require a sink ``.monitor`` node (not a mic / default source)."""
        am = (audio_monitor or "").strip()
        if not am:
            raise WFDNotReady("LPCM path requires a Pulse/PipeWire audio monitor")
        if am in ("default", "auto") or "input" in am.lower():
            raise WFDNotReady(
                f"Refusing LPCM capture from {audio_monitor!r}; "
                "need a sink monitor like 'miracast.monitor'"
            )
        if not am.endswith(".monitor"):
            am = f"{am}.monitor"
            print(
                f"[FluxCast WFD Media] Audio device {audio_monitor!r} is not a "
                f"monitor; using {am!r}"
            )
        return am

    def _lpcm_audio_capture_cmd(self, audio_monitor: str) -> list[str]:
        """pw-cat (preferred) or parec → S16LE for WFDLPCMMuxer."""
        if shutil.which("pw-cat") is not None:
            return [
                "pw-cat",
                "-r",
                "-a",
                "--target", audio_monitor,
                "--rate", "48000",
                "--channels", "2",
                "--format", "s16",
                "-P", "application.name=fluxcast-wfd-capture",
                "-P", f"media.name={audio_monitor}",
                "-P", "media.role=Abstract",
                "-P", "media.category=Capture",
                "-P", f"target.object={audio_monitor}",
                "-",
            ]
        return [
            "parec",
            f"--device={audio_monitor}",
            "--client-name=fluxcast-wfd-capture",
            f"--stream-name={audio_monitor}",
            "--rate=48000",
            "--channels=2",
            "--format=s16le",
            "--raw",
            "--property=media.role=Abstract",
            f"--property=module-stream-restore.id=fluxcast-wfd:{audio_monitor}",
        ]

    def _start_wf_recorder_raw_pipe_lpcm(
        self,
        wf_recorder: str,
        monitor,
        encoder_override: str | None = None,
    ) -> str:
        """Raw NV12 pipe → ffmpeg annex-B H.264 + pw-cat PCM → WFDLPCMMuxer.

        Same WFD LPCM (stream_type 0x83) contract as DMA-BUF LPCM, but video
        encode stays on the RENDER ENGINE ``vaapi`` / ``cpu`` pipe path.
        """
        meta = self._desktop_bitrate_plan(monitor)
        audio_monitor = self._normalize_sink_monitor(
            self.config.audio_device or _detect_audio_monitor() or ""
        )
        try:
            from drivers.wfd_lpcm_mux import WFDLPCMMuxer
        except ImportError as exc:
            raise WFDNotReady(f"WFDLPCMMuxer unavailable: {exc}") from exc

        input_pix = "nv12"
        plan = build_encode_plan(
            h264_profile=self.config.h264_profile,
            level=meta["level"],
            fps=self.config.fps,
            gop=meta["gop"],
            bitrate=meta["effective_bitrate"],
            bufsize=_vbv_bufsize(meta["effective_bitrate"], self.config),
            vf_scale=meta["vf_scale"],
            output_height=meta["parsed_out"][1],
            input_pix_fmt=input_pix,
            encoder_override=encoder_override,
        )

        r_fd, w_fd = os.pipe()
        os.set_inheritable(r_fd, True)
        os.set_inheritable(w_fd, True)
        try:
            import fcntl

            pipe_sz = 1 << 20
            fcntl.fcntl(r_fd, fcntl.F_SETPIPE_SZ, pipe_sz)
            fcntl.fcntl(w_fd, fcntl.F_SETPIPE_SZ, pipe_sz)
        except OSError:
            pass
        ar_fd, aw_fd = os.pipe()
        os.set_inheritable(ar_fd, True)
        os.set_inheritable(aw_fd, True)

        pipe_note = (
            f"pipe {input_pix}+hwupload"
            if plan.name in ("vaapi", "qsv")
            else f"pipe {input_pix}+software"
        )
        print(f"[FluxCast WFD Media] Capturing screen : {monitor.name} ({meta['src_res']})")
        print(
            f"[FluxCast WFD Media] Capturing audio  : {audio_monitor} "
            "(sink monitor via pw-cat — not mic)"
        )
        print(
            f"[FluxCast WFD Media] Video encoder   : {plan.note} [{pipe_note}] + "
            "WFDLPCMMuxer (stream_type=0x83)"
        )
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        muxer = WFDLPCMMuxer(
            self.tv_ip,
            self.sink_rtp_port,
            local_ip=self.local_ip,
            local_port=self.config.source_port,
            bind_iface=self.tx_interface,
        )
        vid_pipeline = (
            f"fdsrc fd={r_fd} do-timestamp=true ! "
            "h264parse config-interval=-1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=sink sync=false max-buffers=1 drop=true"
        )
        aud_pipeline = (
            f"fdsrc fd={ar_fd} do-timestamp=true ! "
            "audio/x-raw,format=S16LE,rate=48000,channels=2,"
            "layout=interleaved ! "
            "audioconvert ! audio/x-raw,format=S16BE ! "
            "audiobuffersplit output-buffer-size=1920 ! "
            "appsink name=sink sync=false max-buffers=32 drop=false"
        )
        try:
            muxer.start(vid_pipeline, aud_pipeline)
        except Exception:
            os.close(r_fd)
            os.close(w_fd)
            os.close(ar_fd)
            os.close(aw_fd)
            muxer.stop()
            raise

        wf_cmd = [
            wf_recorder,
            "-y",
            *self._wf_damage_flag(),
            "-r", str(self.config.fps),
            "-o", monitor.name,
            "-c", "rawvideo",
            "-m", "nut",
            "-x", "nv12",
            "-f", "/dev/stdout",
        ]

        _tqs = (os.environ.get("FLUXCAST_WFD_THREAD_QUEUE_SIZE", "") or "64").strip() or "64"
        _pipe_ll = (os.environ.get("FLUXCAST_WFD_VAAPI_PIPE_LOW_LATENCY", "") or "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if _pipe_ll and _tqs == "64":
            _tqs = (os.environ.get("FLUXCAST_WFD_THREAD_QUEUE_SIZE", "") or "8").strip() or "8"

        # Video-only annex-B into the LPCM muxer (no AAC / rtp_mpegts).
        ffmpeg_cmd = [
            *_ffmpeg_sender_args(self.config.ffmpeg_stats),
            *plan.pre_input,
        ]
        if _pipe_ll:
            ffmpeg_cmd += [
                "-fflags", "nobuffer+genpts+flush_packets",
                "-flags", "low_delay",
                "-probesize", "32",
                "-analyzeduration", "0",
            ]
        else:
            ffmpeg_cmd += ["-fflags", "+genpts"]
        ffmpeg_cmd += [
            "-thread_queue_size", _tqs,
            "-f", "nut",
            "-i", "pipe:0",
            "-map", "0:v:0",
            *plan.vf,
            *plan.video_args,
            "-an",
            "-f", "h264",
            f"/dev/fd/{w_fd}",
        ]

        wf_proc = subprocess.Popen(wf_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if wf_proc.stdout is None:
            muxer.stop()
            os.close(r_fd)
            os.close(w_fd)
            os.close(ar_fd)
            os.close(aw_fd)
            raise WFDNotReady("wf-recorder did not expose stdout.")

        raw_pipe = self._enlarge_pipe_fd(wf_proc.stdout.fileno())
        if raw_pipe:
            print(
                f"[FluxCast WFD Media] raw wf→ffmpeg pipe_sz={raw_pipe}B "
                f"(default was 65536)",
                flush=True,
            )

        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=wf_proc.stdout,
            stderr=subprocess.PIPE,
            pass_fds=(w_fd,),
        )
        try:
            wf_proc.stdout.close()
        except Exception:
            pass
        os.close(w_fd)
        self._watch_sender_stderr(wf_proc, "wf-recorder-pipe-lpcm")
        self._watch_sender_stderr(ffmpeg_proc, "ffmpeg-pipe-lpcm")
        self._start_ffmpeg_io_watchdog(ffmpeg_proc)

        aud_cmd = self._lpcm_audio_capture_cmd(audio_monitor)
        aud_proc = subprocess.Popen(
            aud_cmd,
            stdout=aw_fd,
            stderr=None,
            pass_fds=(aw_fd,),
        )
        os.close(aw_fd)

        time.sleep(2.0)
        if (
            wf_proc.poll() is not None
            or ffmpeg_proc.poll() is not None
            or aud_proc.poll() is not None
            or not muxer._mux_thread
            or not muxer._mux_thread.is_alive()
        ):
            muxer.stop()
            for proc in (wf_proc, ffmpeg_proc, aud_proc):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()
            os.close(r_fd)
            os.close(ar_fd)
            raise WFDNotReady("WFD pipe LPCM muxer failed to stay up")

        self._lpcm_muxer = muxer
        self.processes = [wf_proc, ffmpeg_proc, aud_proc]
        self._lpcm_video_fd = r_fd
        self._lpcm_audio_fd = ar_fd
        print(
            f"[FluxCast WFD Media] LPCM muxer up (pipe) "
            f"(audio_frames={muxer.audio_frames_sent}, video_frames={muxer.frames_sent})"
        )
        return plan.name

    def _start_wf_recorder_raw_pipe(
        self,
        wf_recorder: str,
        monitor,
        encoder_override: str | None = None,
    ) -> str:
        """Raw NV12 pipe → ffmpeg encode (VAAPI hwupload or libx264).

        Prefer ``-x nv12`` so we skip CPU ``format=nv12`` before hwupload.
        Falls back to yuv420p + format=nv12 if needed via encode plan.
        Used for RENDER ENGINE ``vaapi`` / ``cpu`` and as DMA-BUF fallback.

        Returns the resolved encode plan name (``vaapi`` / ``qsv`` / ``libx264``).
        """
        meta = self._desktop_bitrate_plan(monitor)
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        # NV12 from wf-recorder avoids CPU colorspace convert; only hwupload remains.
        input_pix = "nv12"
        plan = build_encode_plan(
            h264_profile=self.config.h264_profile,
            level=meta["level"],
            fps=self.config.fps,
            gop=meta["gop"],
            bitrate=meta["effective_bitrate"],
            bufsize=_vbv_bufsize(meta["effective_bitrate"], self.config),
            vf_scale=meta["vf_scale"],
            output_height=meta["parsed_out"][1],
            input_pix_fmt=input_pix,
            encoder_override=encoder_override,
        )

        wf_cmd = [
            wf_recorder,
            "-y",
            *self._wf_damage_flag(),
            "-r", str(self.config.fps),
            "-o", monitor.name,
            "-c", "rawvideo",
            "-m", "nut",
            "-x", "nv12",
            "-f", "/dev/stdout",
        ]

        _tqs = (os.environ.get("FLUXCAST_WFD_THREAD_QUEUE_SIZE", "") or "64").strip() or "64"
        # VAAPI pipe low-latency input: skip demux probing / extra buffering.
        # Enable with FLUXCAST_WFD_VAAPI_PIPE_LOW_LATENCY=1 (test before defaulting).
        _pipe_ll = (os.environ.get("FLUXCAST_WFD_VAAPI_PIPE_LOW_LATENCY", "") or "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if _pipe_ll and _tqs == "64":
            _tqs = (os.environ.get("FLUXCAST_WFD_THREAD_QUEUE_SIZE", "") or "8").strip() or "8"
        ffmpeg_cmd = [
            *_ffmpeg_sender_args(self.config.ffmpeg_stats),
            *plan.pre_input,
        ]
        if _pipe_ll:
            ffmpeg_cmd += [
                "-fflags", "nobuffer+genpts+flush_packets",
                "-flags", "low_delay",
                "-probesize", "32",
                "-analyzeduration", "0",
            ]
        else:
            ffmpeg_cmd += ["-fflags", "+genpts"]
        ffmpeg_cmd += [
            "-thread_queue_size", _tqs,
            "-f", "nut",
            "-i", "pipe:0",
        ]

        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-thread_queue_size", _tqs,
                "-f", "pulse",
                "-i", audio_monitor,
                "-map", "0:v:0",
                "-map", "1:a:0",
            ]
        else:
            ffmpeg_cmd += ["-map", "0:v:0"]

        ffmpeg_cmd += plan.vf
        ffmpeg_cmd += plan.video_args

        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-af", "aresample=async=1",
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "128k",
                "-ac", "2",
                "-ar", "48000",
                "-streamid", "1:4352",
            ]

        ffmpeg_cmd += self._common_output_args()

        print(f"[FluxCast WFD Media] Capturing screen : {monitor.name} ({meta['src_res']})")
        if not self.config.no_audio:
            print(f"[FluxCast WFD Media] Capturing audio  : {audio_monitor}")
        if meta["out_res"] != meta["src_res"]:
            print(f"[FluxCast WFD Media] Scaling output  : {meta['out_res']}")
        pipe_note = (
            f"pipe {input_pix}+hwupload"
            if plan.name in ("vaapi", "qsv")
            else f"pipe {input_pix}+software"
        )
        print(
            f"[FluxCast WFD Media] Video encoder   : {plan.note} [{pipe_note}]"
        )
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        self._spawn_wf_ffmpeg(wf_cmd, ffmpeg_cmd)
        return plan.name

    def _spawn_wf_ffmpeg(self, wf_cmd: list[str], ffmpeg_cmd: list[str]) -> None:
        """Spawn wf-recorder | ffmpeg with stage timing on the nut pipe."""
        import fcntl

        t_spawn = time.monotonic()
        fps = int(getattr(self.config, "fps", 30) or 30)
        # Rough glass-to-glass budget (ms) for logs — TV display buffer unknown.
        budget = {
            "capture_frame_ms": round(1000.0 / max(fps, 1), 1),
            "encode_budget_ms": round(2 * 1000.0 / max(fps, 1), 1),
            "net_budget_ms": 30.0,
            "tv_display_budget_ms": round(3 * 1000.0 / max(fps, 1), 1),
        }
        budget["sum_excl_queue_ms"] = round(
            budget["capture_frame_ms"]
            + budget["encode_budget_ms"]
            + budget["net_budget_ms"]
            + budget["tv_display_budget_ms"],
            1,
        )
        print(
            "[FluxCast WFD Media] Latency budget (est., excl. demux queue): "
            f"capture≈{budget['capture_frame_ms']}ms + encode≈{budget['encode_budget_ms']}ms "
            f"+ net≈{budget['net_budget_ms']}ms + TV≈{budget['tv_display_budget_ms']}ms "
            f"→ ~{budget['sum_excl_queue_ms']}ms @ {fps}fps",
            flush=True,
        )
        _append_latency_log(
            getattr(self.config, "latency_log_path", None),
            "latency_budget",
            fps=fps,
            **budget,
        )

        wf_proc = subprocess.Popen(wf_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if wf_proc.stdout is None:
            wf_proc.kill()
            raise WFDNotReady("wf-recorder did not expose stdout.")

        # Match LPCM path: enlarge wf stdout so a brief VAAPI pause does not
        # fill the default 64 KiB pipe and storm the buffer pool.
        raw_sz = self._enlarge_pipe_fd(wf_proc.stdout.fileno())
        if raw_sz:
            print(
                f"[FluxCast WFD Media] raw wf→ffmpeg pipe_sz={raw_sz}B "
                "(AAC/RTP relay path)",
                flush=True,
            )

        # Relay wf→ffmpeg so we can timestamp first captured nut bytes without
        # a large ffmpeg demux backlog owning the pipe.
        r_fd, w_fd = os.pipe()
        try:
            # Prefer 8 MiB like LPCM; fall back via _enlarge_pipe_fd helpers.
            self._enlarge_pipe_fd(r_fd)
            self._enlarge_pipe_fd(w_fd)
        except OSError:
            pass

        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd, stdin=r_fd, stderr=subprocess.PIPE, pass_fds=(r_fd,)
        )
        os.close(r_fd)
        self._start_ffmpeg_io_watchdog(ffmpeg_proc)

        first_nut = {"t": None}

        def _relay() -> None:
            try:
                while True:
                    chunk = wf_proc.stdout.read(256 * 1024)
                    if not chunk:
                        break
                    if first_nut["t"] is None:
                        first_nut["t"] = time.monotonic()
                        dt_ms = round((first_nut["t"] - t_spawn) * 1000.0, 1)
                        print(
                            f"[FluxCast WFD Media] Latency probe: first capture bytes "
                            f"(nut) {dt_ms} ms after sender spawn",
                            flush=True,
                        )
                        _append_latency_log(
                            getattr(self.config, "latency_log_path", None),
                            "first_capture_bytes",
                            ms_after_spawn=dt_ms,
                        )
                    try:
                        os.write(w_fd, chunk)
                    except BrokenPipeError:
                        break
            except Exception:
                pass
            finally:
                try:
                    wf_proc.stdout.close()
                except Exception:
                    pass
                try:
                    os.close(w_fd)
                except Exception:
                    pass

        threading.Thread(target=_relay, name="fluxcast-nut-relay", daemon=True).start()
        self._watch_sender_stderr(wf_proc, "wf-recorder")
        self._watch_sender_stderr(ffmpeg_proc, "ffmpeg")
        time.sleep(1.0)

        if wf_proc.poll() is not None:
            ffmpeg_proc.terminate()
            raise WFDNotReady("wf-recorder exited immediately during WFD streaming.")
        if ffmpeg_proc.poll() is not None:
            wf_proc.terminate()
            raise WFDNotReady("ffmpeg exited immediately during WFD streaming.")

        self.processes = [wf_proc, ffmpeg_proc]

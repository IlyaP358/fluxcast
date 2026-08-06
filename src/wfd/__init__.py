import re
import random
import socket
import socketserver
import shutil
import subprocess
import threading
import time
import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from typing import Optional

from diagnostics import print_report, run_diagnostics
from capture.portal_capture import PortalCaptureError, PortalCaptureSession, close_portal_capture, start_portal_capture

 
from .constants import WFD_RTSP_PORT, WFD_UIBC_PORT, _DEVICE_NAME, WFD_CEA_640P60, WFD_CEA_720P30, WFD_CEA_720P60, WFD_CEA_1080P30, WFD_CEA_1080P60, WFD_VESA_1200P30, WFD_VESA_1200P60, WFD_LEVEL_31, WFD_LEVEL_32, WFD_LEVEL_40, WFD_LEVEL_42, WFD_LEVEL_50, WFD_LEVEL_51, WFD_AUDIO_AAC, WFD_AUDIO_LPCM_48K, NM_DEST, NM_PATH


from .ie import _wfd_ie_device_info, _wfd_ie_device_name, WFDPeer, _parse_gdbus_byte_array, _parse_wfd_ies_rtsp_port










from .config import WFDNotReady, WFDMediaConfig, WFDVideoFormat, WFDCEAMode


from .rtsp.message import RTSPMessage, _read_rtsp_message, _parse_parameters, _sink_advertises_uibc, _parse_rtp_ports, _parse_transport_client_ports








from .modes import WFD_CEA_MODES, WFD_VESA_MODES, _parse_sink_video_format, _choose_profile, _encoder_h264_profile, _max_wfd_level, _wfd_level_for_mode, _desired_resolution, _choose_cea_mode, _selected_video_format, _h264_level_for_mode



from .encoding import _parse_resolution, _fit_inside, _letterbox_vf, _vbv_bufsize, _bitrate_to_kbits, _kbits_to_bitrate_text, _quality_floor_kbits, _calculate_gop


from .env import _detect_audio_monitor, _is_hyprland_session, _is_wayland_session, _is_x11_session, _wfd_capture_backend_order










from .gst import _gst_has_element, _wfd_gst_prog_map, _gst_rtp_link, _gst_dump_branch, _gst_wfd_sender_available, _gst_pipewiresrc_properties, _gst_x264enc_props_cache, _gst_x264enc_properties, _pipewiresrc_selector_attempts, _gst_pick_aac_encoder






from .dump import _process_written_bytes, _ts_dump_reported, report_ts_dump, schedule_ts_dump_report





































from .latency import _append_latency_log






















from .net import _safe_source_port, _rtp_url, _interface_for_ip, _netdev_tx_bytes, _ffmpeg_sender_args










from .media.pipeline import WFDMediaPipeline












from .rtsp.handler import _WFDRTSPHandler


from .rtsp.rtsp_server import _ThreadingTCPServer, WFDRTSPServer




from .proc import _run


from .firewall import _firewalld_active, _FIREWALL_AUTH_TIMEOUT, _FIREWALL_QUERY_TIMEOUT, _WFD_FIREWALL_ZONE, _print_firewall_manual_hint, _open_wfd_firewall_port, _close_wfd_firewall_port










from .p2p.dbus import _wfd_source_ie, _object_paths, _variant_string, _variant_uint, _variant_uint_tuple, NM_ACTIVE_STATE_NAMES, NM_DEVICE_STATE_NAMES, NM_DEVICE_REASON_NAMES, _gdbus_call, _nm_get_property, _nm_get_string, _variant_byte_array


















from .p2p.nm import _nm_device_summary, _nm_active_devices, _wait_for_nm_activation, _nm_p2p_device_path, _nm_start_find, _nm_stop_find, _nm_scan, _connection_settings, _connect_peer, _disconnect_device, _deactivate_connection
















from .p2p.peers import _select_peer, _scan_and_select, _default_wifi_interface, _parse_peer_name, active_scan, print_scan






from .p2p.device import _p2p_device_iface_paths, _set_p2p_device_name, _read_p2p_go_intent, _set_p2p_go_intent












from .session import _cleanup_step, start_experimental_backend














from .p2p.addressing import _get_peer_ip_from_arp, _get_peer_ip_from_p2p_iface, _wait_for_peer_ip






from .probe import _active_rtsp_probe





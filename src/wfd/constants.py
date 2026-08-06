import re
import socket


WFD_RTSP_PORT = 7236
WFD_UIBC_PORT = 7239  # local TCP port the sink connects to for input (#37, opt-in)
try:
    _DEVICE_NAME: str = re.sub(r"[^a-zA-Z0-9\-]", "", socket.gethostname().split(".")[0])[:32] or "FluxCast"
except OSError:
    _DEVICE_NAME = "FluxCast"

# WFD CEA resolution bitmask. The current backend intentionally negotiates
# only the !common! HD modes that Samsung TVs usually accept reliably.
WFD_CEA_640P60  = 0x00000001  # bit 0: 640x480p60
WFD_CEA_720P30  = 0x00000020  # bit 5: 1280x720p30, mandatory HD mode
WFD_CEA_720P60  = 0x00000040  # bit 6: 1280x720p60
WFD_CEA_1080P30 = 0x00000080  # bit 7: 1920x1080p30
WFD_CEA_1080P60 = 0x00000100  # bit 8: 1920x1080p60

# VESA resolution bitmasks (Table 5-11 / AOSP VideoFormats.cpp)
WFD_VESA_1200P30 = 0x10000000  # bit 28: 1920x1200p30
WFD_VESA_1200P60 = 0x20000000  # bit 29: 1920x1200p60

WFD_LEVEL_31 = 0x01
WFD_LEVEL_32 = 0x02
WFD_LEVEL_40 = 0x04
WFD_LEVEL_42 = 0x10
WFD_LEVEL_50 = 0x20
WFD_LEVEL_51 = 0x40
WFD_AUDIO_AAC = "AAC 00000001 00"
WFD_AUDIO_LPCM_48K  = "LPCM 00000002 00"
NM_DEST = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"

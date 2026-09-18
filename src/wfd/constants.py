import re
import socket


WFD_RTSP_PORT = 7236
WFD_UIBC_PORT = 7239  # local TCP port the sink connects to for input (#37, opt-in)
try:
    _DEVICE_NAME: str = re.sub(r"[^a-zA-Z0-9\-]", "", socket.gethostname().split(".")[0])[:32] or "FluxCast"
except OSError:
    _DEVICE_NAME = "FluxCast"

# WFD CEA resolution bitmask (Wi-Fi Display Table / AOSP VideoFormats.cpp).
# Bit index == table row. Progressive modes are negotiable; interlaced rows are
# recognized for capability listing but not selected in M4 (progressive capture).
WFD_CEA_640P60   = 0x00000001  # bit 0:  640x480p60
WFD_CEA_480P60   = 0x00000002  # bit 1:  720x480p60
WFD_CEA_480I60   = 0x00000004  # bit 2:  720x480i60
WFD_CEA_576P50   = 0x00000008  # bit 3:  720x576p50
WFD_CEA_576I50   = 0x00000010  # bit 4:  720x576i50
WFD_CEA_720P30   = 0x00000020  # bit 5:  1280x720p30 (mandatory HD)
WFD_CEA_720P60   = 0x00000040  # bit 6:  1280x720p60
WFD_CEA_1080P30  = 0x00000080  # bit 7:  1920x1080p30
WFD_CEA_1080P60  = 0x00000100  # bit 8:  1920x1080p60
WFD_CEA_1080I60  = 0x00000200  # bit 9:  1920x1080i60
WFD_CEA_720P25   = 0x00000400  # bit 10: 1280x720p25
WFD_CEA_720P50   = 0x00000800  # bit 11: 1280x720p50
WFD_CEA_1080P25  = 0x00001000  # bit 12: 1920x1080p25
WFD_CEA_1080P50  = 0x00002000  # bit 13: 1920x1080p50
WFD_CEA_1080I50  = 0x00004000  # bit 14: 1920x1080i50
WFD_CEA_720P24   = 0x00008000  # bit 15: 1280x720p24
WFD_CEA_1080P24  = 0x00010000  # bit 16: 1920x1080p24

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

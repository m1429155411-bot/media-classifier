# -*- coding: utf-8 -*-
"""
媒体分类工具 —— 本地界面服务（图片 / 视频双板块）
零第三方依赖（仅 Python 3.9+ 标准库），仅监听 127.0.0.1。
视频元数据用标准库解析 MP4/MOV/AVI/MKV/WebM/FLV 容器头，
无法识别的格式由 Windows Shell 媒体属性兜底，不需要 ffprobe。

原程序（图片分类.exe）逻辑：
  扫描【当前工作目录】顶层的 .png/.jpg/.jpeg/.bmp/.gif，
  用 Pillow 读取分辨率，复制到「分类/<宽>x<高>/」子目录。
本工具保留该分类语义，并补齐「选文件夹 / 递归 / 预览 / 进度 / 日志 / 更多格式」。
"""
import os
import re
import sys
import json
import time
import base64
import shutil
import struct
import mimetypes
import tempfile
import threading
import subprocess
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
HOST = "127.0.0.1"

EXTS = [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"]
VIDEO_EXTS = [".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".flv", ".wmv",
              ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp", ".vob", ".rmvb"]
MEDIA_EXTS = {"image": EXTS, "video": VIDEO_EXTS}
LEGACY_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif")

STATE = {
    "running": False,
    "scanning": False,
    "total": 0,
    "done": 0,
    "ok": 0,
    "skipped": 0,
    "stayed": 0,          # 未匹配关键词且「没包含时归入」留空 → 保留在原文件夹
    "failed": 0,
    "logs": [],
    "groups": [],
    "scan": {},
    "finished": False,
    "abort": False,
    "output": "",
}
LOCK = threading.Lock()
ABORT = threading.Event()
EXIT_EVENT = threading.Event()

# 规则保存在用户配置目录，打包成 exe 后重启也不会丢失
CFG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "ImageClassifier")
RULES_PATH = os.path.join(CFG_DIR, "rules.json")
NAMEKW_PATH = os.path.join(CFG_DIR, "name_keywords.json")
THEME_PATH = os.path.join(CFG_DIR, "theme.json")

NUMERIC_FIELDS = ("width", "height", "mp", "aspect", "size_mb", "duration")
TEXT_FIELDS = ("ext", "name", "month", "orientation", "codec")
FIELD_LABELS = {
    "width": "宽度 px", "height": "高度 px", "mp": "像素总量 MP",
    "aspect": "宽高比", "size_mb": "文件大小 MB", "ext": "扩展名",
    "name": "文件名", "month": "修改月份", "orientation": "方向",
    "duration": "时长 秒（视频）", "codec": "视频编码（视频）",
}

DEFAULT_IMAGE_RULES = {
    "rules": [
        {"enabled": True, "field": "width", "op": ">=", "value": "1920", "target": "高清大图"},
        {"enabled": True, "field": "ext", "op": "==", "value": "png", "target": "PNG 图片"},
        {"enabled": True, "field": "size_mb", "op": ">=", "value": "5", "target": "大文件"},
    ],
    "fallback": "其他",
}
DEFAULT_VIDEO_RULES = {
    "rules": [
        {"enabled": True, "field": "height", "op": ">=", "value": "1080", "target": "高清视频"},
        {"enabled": True, "field": "duration", "op": ">=", "value": "600", "target": "长视频"},
        {"enabled": True, "field": "size_mb", "op": ">=", "value": "200", "target": "大体积"},
    ],
    "fallback": "其他视频",
}


def _defaults(media):
    src = DEFAULT_VIDEO_RULES if media == "video" else DEFAULT_IMAGE_RULES
    return {"rules": [dict(r) for r in src["rules"]], "fallback": src["fallback"]}


def load_rules(media="image"):
    """图片与视频各有一套独立规则；兼容早期只存一套的旧配置文件。"""
    try:
        with open(RULES_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and ("image" in d or "video" in d):
            sect = d.get(media) or {}
            if isinstance(sect.get("rules"), list):
                return {"rules": sect["rules"], "fallback": sect.get("fallback", "其他")}
        elif media == "image" and isinstance(d, dict) and isinstance(d.get("rules"), list):
            return {"rules": d["rules"], "fallback": d.get("fallback", "其他")}
    except Exception:
        pass
    return _defaults(media)


def save_rules(data):
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        return False
    media = data.get("media") if data.get("media") in ("image", "video") else "image"
    os.makedirs(CFG_DIR, exist_ok=True)
    payload = {"image": load_rules("image"), "video": load_rules("video")}
    payload[media] = {"rules": data["rules"][:200],
                      "fallback": str(data.get("fallback") or "其他")[:80]}
    with open(RULES_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return True


# 按文件名「自定义内容」关键词配置：图片 / 视频各一套
# 「没包含时归入」留空 = 不移动未匹配文件，让它留在原文件夹
def _blank_namekw():
    return {"keywords": "", "fallback": ""}


def load_name_keywords(media="image"):
    def sect_fallback(sect):
        fb = sect.get("fallback") if isinstance(sect, dict) else ""
        return str(fb).strip()[:80] if isinstance(fb, str) else ""

    try:
        with open(NAMEKW_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            if "image" in d or "video" in d:
                sect = d.get(media) or {}
                if isinstance(sect.get("keywords"), str):
                    return {"keywords": sect["keywords"],
                            "fallback": sect_fallback(sect)}
            elif media == "image" and isinstance(d.get("keywords"), str):
                return {"keywords": d["keywords"], "fallback": sect_fallback(d)}
    except Exception:
        pass
    return _blank_namekw()


def save_name_keywords(data):
    if not isinstance(data, dict) or not isinstance(data.get("keywords"), str):
        return False
    media = data.get("media") if data.get("media") in ("image", "video") else "image"
    fb = data.get("fallback")
    fb = str(fb).strip()[:80] if isinstance(fb, str) else ""
    os.makedirs(CFG_DIR, exist_ok=True)
    payload = {"_v": 2, "image": load_name_keywords("image"),
               "video": load_name_keywords("video")}
    payload[media] = {"keywords": data["keywords"][:8000], "fallback": fb}
    with open(NAMEKW_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return True


def _migrate_namekw():
    """旧版会自动把「未匹配」写进配置；新版「留空」代表不移动文件。
    仅对未打版本标记的旧配置迁移一次，之后用户手动填写的「未匹配」会原样保留。
    """
    try:
        with open(NAMEKW_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or d.get("_v") == 2:
            return
        sects = [d[k] for k in ("image", "video") if isinstance(d.get(k), dict)]
        if isinstance(d.get("keywords"), str):
            sects.append(d)
        for s in sects:
            if s.get("fallback") == "未匹配":
                s["fallback"] = ""
        d["_v"] = 2
        with open(NAMEKW_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_migrate_namekw()


# 界面主题：写入本机配置，独立 exe 启动时用于设置窗口背景色，避免浅色主题闪黑
def load_theme():
    try:
        with open(THEME_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        t = d.get("theme") if isinstance(d, dict) else d
        if t in ("light", "dark"):
            return t
    except Exception:
        pass
    return "dark"


def save_theme(t):
    if t not in ("light", "dark"):
        return False
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(THEME_PATH, "w", encoding="utf-8") as f:
            json.dump({"theme": t}, f)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 图片尺寸解析
SOF = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _jpeg_size(data):
    i, n = 2, len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xD8, 0xD9, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xDA:                       # SOS，元数据结束
            break
        if i + 4 > n:
            break
        seglen = struct.unpack_from(">H", data, i + 2)[0]
        if seglen < 2:
            i += 2
            continue
        if m in SOF and i + 9 <= n:
            h, w = struct.unpack_from(">HH", data, i + 5)
            return w, h
        i += 2 + seglen
    return None


def _webp_size(data):
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    kind = data[12:16]
    if kind == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    if kind == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        w = struct.unpack_from("<H", data, 26)[0] & 0x3FFF
        h = struct.unpack_from("<H", data, 28)[0] & 0x3FFF
        return w, h
    if kind == b"VP8L" and data[20] == 0x2F:
        b = data[21:26]
        if len(b) < 5:
            return None
        w = (b[1] | ((b[2] & 0x3F) << 8)) + 1
        h = ((b[2] >> 6) | (b[3] << 2) | ((b[4] & 0x0F) << 10)) + 1
        return w, h
    return None


def _tiff_size(data):
    if data[:2] == b"II":
        end = "<"
    elif data[:2] == b"MM":
        end = ">"
    else:
        return None
    try:
        off = struct.unpack_from(end + "I", data, 4)[0]
        cnt = struct.unpack_from(end + "H", data, off)[0]
        w = h = None
        for k in range(min(cnt, 200)):
            p = off + 2 + k * 12
            tag, typ = struct.unpack_from(end + "HH", data, p)
            if typ == 3:
                val = struct.unpack_from(end + "H", data, p + 8)[0]
            elif typ == 4:
                val = struct.unpack_from(end + "I", data, p + 8)[0]
            else:
                continue
            if tag == 0x0100:
                w = val
            elif tag == 0x0101:
                h = val
            if w and h:
                return w, h
    except Exception:
        return None
    return None


def image_size(path):
    """纯标准库读取常见图片像素尺寸，返回 (w, h) 或 None。"""
    try:
        with open(path, "rb") as f:
            data = f.read(131072)
        if len(data) < 12:
            return None
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return struct.unpack_from(">II", data, 16)
        if data[:2] == b"\xff\xd8":
            return _jpeg_size(data)
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack_from("<HH", data, 6)
        if data[:2] == b"BM":
            w, h = struct.unpack_from("<ii", data, 18)
            return (w, abs(h)) if w > 0 and h != 0 else None
        if data[:4] == b"RIFF":
            return _webp_size(data)
        if data[:4] in (b"II*\x00", b"MM\x00*"):
            return _tiff_size(data)
        return None
    except Exception:
        return None


# ---------------------------------------------------------------- 视频元数据解析
# 本机无 ffprobe、也不引入第三方库：直接解析常见容器头部。
# 覆盖 MP4/MOV/M4V(ISO BMFF)、AVI(RIFF)、MKV/WebM(EBML)、FLV(AMF0)；
# WMV / TS / MPG 等由 Windows Shell 媒体属性接口兜底。
CODEC_MAP = {
    b"avc1": "H.264", b"avc3": "H.264", b"hvc1": "H.265", b"hev1": "H.265",
    b"vp09": "VP9", b"vp08": "VP8", b"av01": "AV1", b"mp4v": "MPEG-4",
    b"jpeg": "MJPEG", b"mjpa": "MJPEG", b"dvh1": "杜比视界", b"dva1": "杜比视界",
    b"H264": "H.264", b"X264": "H.264", b"avc": "H.264", b"HEVC": "H.265",
    b"H265": "H.265", b"DIVX": "MPEG-4", b"XVID": "MPEG-4", b"MP4V": "MPEG-4",
}


def _boxes(data, start=0, end=None):
    """遍历 ISO BMFF box，产出 (类型, 内容起点, box 终点)。"""
    end = len(data) if end is None else min(end, len(data))
    p = start
    while p + 8 <= end:
        n, typ = struct.unpack_from(">I4s", data, p)
        head = 8
        if n == 1:
            if p + 16 > end:
                return
            n = struct.unpack_from(">Q", data, p + 8)[0]
            head = 16
        elif n == 0:
            n = end - p
        if n < head or p + n > end:
            return
        yield typ, p + head, p + n
        p += n


def _mp4_stsd(data, s, e):
    """从 stsd 的 VisualSampleEntry 中取宽高与编码。"""
    if s + 8 > e:
        return (0, 0, "")
    cnt = struct.unpack_from(">I", data, s + 4)[0]
    ep, first = s + 8, ""
    for _ in range(min(cnt, 32)):
        if ep + 8 > e:
            break
        esz = struct.unpack_from(">I", data, ep)[0]
        fmt = data[ep + 4:ep + 8]
        if esz < 16 or ep + esz > e:
            break
        c = CODEC_MAP.get(fmt, fmt.decode("latin1", "replace").strip().upper())
        if not first and c and fmt != b"mp4a":
            first = c
        if ep + 36 <= e:
            ew = struct.unpack_from(">H", data, ep + 32)[0]
            eh = struct.unpack_from(">H", data, ep + 34)[0]
            if ew > 0 and eh > 0:
                return (ew, eh, c or first)
        ep += esz
    return (0, 0, first)


def _mp4_find_stsd(data, s, e):
    for t2, s2, e2 in _boxes(data, s, e):
        if t2 != b"mdia":
            continue
        for t3, s3, e3 in _boxes(data, s2, e2):
            if t3 != b"minf":
                continue
            for t4, s4, e4 in _boxes(data, s3, e3):
                if t4 != b"stbl":
                    continue
                for t5, s5, e5 in _boxes(data, s4, e4):
                    if t5 == b"stsd":
                        return _mp4_stsd(data, s5, e5)
    return (0, 0, "")


def _mp4_meta(f, size):
    w = h = 0
    duration, codec = 0.0, ""
    moov = None
    pos = 0
    for _ in range(4096):                       # 顺序跳过大体积 mdat，moov 在尾部也能找到
        if pos + 8 > size:
            break
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        box_len, typ = struct.unpack(">I4s", hdr)
        head = 8
        if box_len == 1:
            ext = f.read(8)
            if len(ext) < 8:
                break
            box_len = struct.unpack(">Q", ext)[0]
            head = 16
        elif box_len == 0:
            box_len = size - pos
        if box_len < head:
            break
        if typ == b"moov":
            moov = (pos + head, min(box_len - head, 8 * 1024 * 1024))
            break
        pos += box_len
    if not moov:
        return (0, 0, 0.0, "")
    f.seek(moov[0])
    data = f.read(moov[1])
    for typ, s, e in _boxes(data):
        if typ == b"mvhd":
            ver = data[s] if s < len(data) else 0
            ts = dur = 0
            if ver == 1 and s + 32 <= e:
                ts = struct.unpack_from(">I", data, s + 20)[0]
                dur = struct.unpack_from(">Q", data, s + 24)[0]
            elif s + 20 <= e:
                ts = struct.unpack_from(">I", data, s + 12)[0]
                dur = struct.unpack_from(">I", data, s + 16)[0]
            if ts > 0:
                duration = dur / ts
        elif typ == b"trak":
            for t2, s2, e2 in _boxes(data, s, e):
                if t2 == b"tkhd" and not (w and h):
                    ver = data[s2] if s2 < len(data) else 0
                    off = 88 if ver == 1 else 76
                    if s2 + off + 8 <= e2:
                        tw = struct.unpack_from(">I", data, s2 + off)[0] >> 16
                        th = struct.unpack_from(">I", data, s2 + off + 4)[0] >> 16
                        if tw > 0 and th > 0:
                            w, h = tw, th
            # stsd 里的 VisualSampleEntry 同时给出更准确的宽高与编码
            ew, eh, cc = _mp4_find_stsd(data, s, e)
            if cc and not codec:
                codec = cc
            if ew and eh and not (w and h):
                w, h = ew, eh
    return (w, h, duration, codec)


def _avi_meta(f, size):
    f.seek(0)
    head = f.read(min(size, 262144))
    i = head.find(b"avih")
    if i < 0 or i + 48 > len(head):
        return (0, 0, 0.0, "")
    s = i + 8                                  # 跳过 'avih' + 4 字节 chunk 长度
    uspf = struct.unpack_from("<I", head, s)[0]
    frames = struct.unpack_from("<I", head, s + 16)[0]
    w = struct.unpack_from("<I", head, s + 32)[0]
    h = struct.unpack_from("<I", head, s + 36)[0]
    dur = frames * uspf / 1_000_000 if uspf else 0.0
    codec = ""
    j = head.find(b"strf")
    if j > 0 and j + 28 <= len(head):
        cc = head[j + 24:j + 28]
        if cc and all(32 <= c < 127 for c in cc):
            codec = CODEC_MAP.get(cc, cc.decode("latin1", "replace").strip().upper())
    return (w, h, dur, codec)


def _ebml_vint(data, pos, keep_marker):
    if pos >= len(data):
        return None
    b = data[pos]
    if b == 0:
        return None
    mask, ln = 0x80, 1
    while not (b & mask):
        mask >>= 1
        ln += 1
        if ln > 8:
            return None
    if pos + ln > len(data):
        return None
    v = b if keep_marker else (b & (mask - 1))
    for k in range(1, ln):
        v = (v << 8) | data[pos + k]
    return (v, ln)


def _mkv_codec(c):
    c = c.upper()
    for key, label in (("V_MPEG4/ISO/AVC", "H.264"), ("V_MPEGH/ISO/HEVC", "H.265"),
                       ("V_VP9", "VP9"), ("V_AV1", "AV1"), ("V_VP8", "VP8"),
                       ("V_THEORA", "Theora"), ("V_MPEG4/ISO/", "MPEG-4"),
                       ("V_MPEG4/MS/V3", "MPEG-4"), ("V_REAL/", "RealVideo"),
                       ("V_MS/VFW/FOURCC", "FOURCC")):
        if key in c:
            return label
    return c[:24] or "未知"


_MKV_MASTERS = {0x18538067, 0x1549A966, 0x1654AE6B, 0xAE, 0xE0}


def _mkv_meta(f, size):
    f.seek(0)
    data = f.read(min(size, 4 * 1024 * 1024))
    v = {"w": 0, "h": 0, "scale": 1_000_000, "dur_raw": 0.0, "codec": ""}

    def walk(start, end, depth=0):
        p = start
        while p + 2 <= end and depth < 7:
            ident = _ebml_vint(data, p, True)
            if not ident:
                break
            eid, idl = ident
            sz = _ebml_vint(data, p + idl, False)
            if not sz:
                break
            esz, szl = sz
            cs = p + idl + szl
            ce = end if esz == (1 << (7 * szl)) - 1 else min(end, cs + esz)
            if ce <= cs:
                break
            p = ce
            if eid in _MKV_MASTERS:
                walk(cs, ce, depth + 1)
            elif eid == 0x2AD7B1:
                v["scale"] = int.from_bytes(data[cs:ce], "big") or 1_000_000
            elif eid == 0x4489:
                raw = data[cs:ce]
                if len(raw) == 4:
                    v["dur_raw"] = struct.unpack(">f", raw)[0]
                elif len(raw) == 8:
                    v["dur_raw"] = struct.unpack(">d", raw)[0]
            elif eid == 0xB0:
                v["w"] = int.from_bytes(data[cs:ce], "big")
            elif eid == 0xBA:
                v["h"] = int.from_bytes(data[cs:ce], "big")
            elif eid == 0x86:
                v["codec"] = _mkv_codec(data[cs:ce].decode("latin1", "replace"))

    walk(0, len(data))
    return (v["w"], v["h"], v["dur_raw"] * v["scale"] / 1e9, v["codec"])


def _amf0(b, p):
    if p >= len(b):
        return None, p
    t, p = b[p], p + 1
    if t == 0x02:
        if p + 2 > len(b):
            return None, p
        n = int.from_bytes(b[p:p + 2], "big")
        p += 2
        return b[p:p + n].decode("utf-8", "replace"), p + n
    if t == 0x05:
        if p + 8 > len(b):
            return None, p
        return struct.unpack_from(">d", b, p)[0], p + 8
    if t == 0x01:
        return bool(b[p]), p + 1
    if t == 0x08:
        p += 4
        d = {}
        while p + 3 <= len(b):
            n = int.from_bytes(b[p:p + 2], "big")
            if n == 0 and b[p + 2] == 0x09:
                return d, p + 3
            p += 2
            key = b[p:p + n].decode("utf-8", "replace")
            p += n
            val, p = _amf0(b, p)
            if val is None:
                break
            d[key] = val
        return d, p
    return None, p


def _flv_meta(f, size):
    f.seek(0)
    d = f.read(min(size, 1024 * 1024))
    if d[:3] != b"FLV":
        return (0, 0, 0.0, "")
    p = 13
    if p + 11 > len(d) or d[p] != 18:
        return (0, 0, 0.0, "")
    ln = int.from_bytes(d[p + 1:p + 4], "big")
    body = d[p + 11:p + 11 + ln]
    _, q = _amf0(body, 0)
    val, _ = _amf0(body, q)
    if not isinstance(val, dict):
        return (0, 0, 0.0, "")
    try:
        w, h = int(val.get("width") or 0), int(val.get("height") or 0)
        dur = float(val.get("duration") or 0)
    except (TypeError, ValueError):
        w = h = 0
        dur = 0.0
    codec = ""
    try:
        vc = val.get("videocodecid")
        if vc:
            codec = {2: "H.263", 7: "H.264", 12: "H.265", 9: "VP9"}.get(int(vc), str(vc))
    except (TypeError, ValueError):
        pass
    return (w, h, dur, codec)


def _video_meta(path):
    """返回 (宽, 高, 时长秒, 编码)；无法解析时返回零值。"""
    try:
        size = os.path.getsize(path)
        if size < 16:
            return (0, 0, 0.0, "")
        with open(path, "rb") as f:
            head = f.read(16)
            ext = os.path.splitext(path)[1].lower()
            if head[4:8] in (b"ftyp", b"moov", b"free", b"wide", b"mdat", b"pnot") \
                    or ext in (".mp4", ".mov", ".m4v", ".3gp"):
                return _mp4_meta(f, size)
            if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
                return _avi_meta(f, size)
            if head[:4] == b"\x1a\x45\xdf\xa3":
                return _mkv_meta(f, size)
            if head[:3] == b"FLV":
                return _flv_meta(f, size)
    except Exception:
        pass
    return (0, 0, 0.0, "")


# 容器头解析失败时，用 Windows 资源管理器自己的媒体属性处理器读取
SHELL_VIDEO_PS = r'''
$ErrorActionPreference = 'SilentlyContinue'
$lines = Get-Content -LiteralPath $env:IMCLS_VLIST -Encoding UTF8
$shell = New-Object -ComObject Shell.Application
$res = New-Object System.Collections.ArrayList
foreach ($line in $lines) {
  $p = [string]$line
  if (-not $p) { continue }
  $dir = Split-Path -Parent $p
  $nm = Split-Path -Leaf $p
  $f = $shell.NameSpace($dir)
  if ($f) {
    $it = $f.ParseName($nm)
    if ($it) {
      [void]$res.Add(@{ p = $p;
        w = $it.ExtendedProperty('System.Video.FrameWidth');
        h = $it.ExtendedProperty('System.Video.FrameHeight');
        d = $it.ExtendedProperty('System.Media.Duration') })
    }
  }
}
$json = @{ items = @($res.ToArray()) } | ConvertTo-Json -Compress -Depth 4
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
'''


def _as_float(v):
    try:
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        if not s:
            return 0.0
        if ":" in s:                       # PowerShell 有时返回 00:03:12
            t = 0.0
            for part in s.split(":"):
                t = t * 60 + float(part)
            return t
        return float(s)
    except Exception:
        return 0.0


def shell_video_meta(paths):
    """批量读取系统媒体属性，返回 {路径: {w,h,duration}}。"""
    if not paths or os.name != "nt":
        return {}
    fd, listpath = tempfile.mkstemp(prefix="imcls_v_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(paths))
        env = dict(os.environ)
        env["IMCLS_VLIST"] = listpath
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
             "-Command", SHELL_VIDEO_PS],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=90, creationflags=subprocess.CREATE_NO_WINDOW, env=env,
        )
        for line in reversed((r.stdout or "").strip().splitlines()):
            try:
                d = json.loads(base64.b64decode(line.strip()).decode("utf-8"))
            except Exception:
                continue
            items = d.get("items", [])
            if isinstance(items, dict):
                items = [items]
            out = {}
            for it in items:
                if not isinstance(it, dict):
                    continue
                key = it.get("p")
                if isinstance(key, dict):          # 兼容 Get-Content 的扩展属性对象
                    key = key.get("value")
                if not isinstance(key, str) or not key:
                    continue
                raw = _as_float(it.get("d"))
                dur = raw / 10_000_000 if raw > 100000 else raw   # 100ns 单位 → 秒
                out[key] = {"w": int(_as_float(it.get("w"))),
                                    "h": int(_as_float(it.get("h"))),
                                    "duration": dur}
            return out
    except Exception as e:
        log(f"系统媒体属性读取失败：{e}", "warn")
    finally:
        try:
            os.remove(listpath)
        except OSError:
            pass
    return {}


def media_meta_batch(files, media):
    """一次解析全部文件，返回 {路径: {size, duration, codec}}。"""
    if media != "video":
        return {p: {"size": image_size(p), "duration": 0.0, "codec": ""} for p in files}
    out, need = {}, []
    for p in files:
        w, h, dur, codec = _video_meta(p)
        out[p] = {"size": (w, h) if w > 0 and h > 0 else None,
                  "duration": dur, "codec": codec}
        if not (w > 0 and h > 0):
            need.append(p)
    if need:
        log(f"{len(need)} 个视频无法从容器头读取尺寸，尝试系统媒体属性…", "dim")
        for p, v in shell_video_meta(need[:200]).items():
            if p not in out:
                continue
            if v.get("w") and v.get("h"):
                out[p]["size"] = (int(v["w"]), int(v["h"]))
            if v.get("duration") and not out[p]["duration"]:
                out[p]["duration"] = float(v["duration"])
    return out


def duration_str(sec):
    try:
        s = int(float(sec or 0))
    except (TypeError, ValueError):
        return "--:--"
    if s <= 0:
        return "--:--"
    h, r = divmod(s, 3600)
    m, ss = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{ss:02d}" if h else f"{m:02d}:{ss:02d}"


# ---------------------------------------------------------------- 工具函数
def safe_name(s):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(s)).strip().rstrip(".") or "未命名"


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def log(msg, kind="info"):
    with LOCK:
        STATE["logs"].append({"t": time.strftime("%H:%M:%S"), "kind": kind, "msg": str(msg)})
        if len(STATE["logs"]) > 3000:
            del STATE["logs"][:1000]


def rule_context(path, size, file_bytes, extra=None):
    """构造自定义规则可用的字段集合。extra = {media, duration, codec}。"""
    extra = extra or {}
    w, h = size if size else (0, 0)
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    is_video = extra.get("media") == "video"
    if not size:
        orient = "未知"
    elif is_video:
        orient = "方屏" if w == h else ("横屏" if w > h else "竖屏")
    else:
        orient = "方图" if w == h else ("横图" if w > h else "竖图")
    return {
        "width": w, "height": h,
        "mp": round(w * h / 1_000_000, 3),
        "aspect": round(w / h, 4) if h else 0,
        "size_mb": round(file_bytes / 1048576, 4),
        "ext": ext,
        "name": os.path.basename(path).lower(),
        "month": time.strftime("%Y-%m", time.localtime(os.path.getmtime(path))),
        "orientation": orient,
        "duration": round(float(extra.get("duration") or 0), 2),
        "codec": str(extra.get("codec") or "").lower(),
    }


def _compare(a, op, v):
    try:
        if op == ">":
            return a > v
        if op == ">=":
            return a >= v
        if op == "<":
            return a < v
        if op == "<=":
            return a <= v
        if op == "==":
            return a == v
        if op == "!=":
            return a != v
    except TypeError:
        return False
    return False


def eval_custom(ctx, rules, fallback):
    """按规则顺序匹配，首个命中的规则决定目标目录；全部未命中归入 fallback。"""
    for r in rules or []:
        if not isinstance(r, dict) or not r.get("enabled", True):
            continue
        field, op = r.get("field"), r.get("op")
        raw = str(r.get("value", "")).strip()
        if field not in ctx or not raw:
            continue
        if field in NUMERIC_FIELDS:
            try:
                v = float(raw)
            except ValueError:
                continue
            hit = _compare(float(ctx[field]), op, v)
        else:
            a, v = str(ctx[field]).lower(), raw.lower()
            if op in ("==", "!="):
                hit = (a == v) if op == "==" else (a != v)
            elif op == "contains":
                hit = v in a
            elif op == "startswith":
                hit = a.startswith(v)
            elif op == "endswith":
                hit = a.endswith(v)
            else:
                hit = False
        if hit:
            return safe_name(r.get("target") or "未命名规则")
    return safe_name(fallback or "其他")


# ---------------------------------------------------------------- 按文件名称分组
NAME_MODES = ("first", "prefix", "stem", "keyword")
NAME_SPLIT = re.compile(r"[\s_\-.+()\[\]{}【】〔〕·,，、;；#～~]+")
NAME_KW_SEP = re.compile(r"[\n\r,，;；]+")
NAME_KW_ARROW = re.compile(r"\s*(?:=>|->|→|=〉)\s*")


def _is_cjk(ch):
    o = ord(ch)
    return 0x3400 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF


def name_group(path, name_mode="first"):
    """按文件名称归档：首字符 / 名称前缀 / 完整主文件名。"""
    stem = os.path.splitext(os.path.basename(path))[0].strip()
    if not stem:
        return "未命名"
    if name_mode == "stem":
        return stem[:80]
    if name_mode == "prefix":
        parts = [x for x in NAME_SPLIT.split(stem) if x]
        return (parts[0] if parts else stem)[:40]
    ch = stem[0]
    if ch.isascii() and "A" <= ch.upper() <= "Z":
        return ch.upper()
    if ch in "0123456789":
        return "数字 0-9"
    if _is_cjk(ch):
        return "中文 · " + ch
    return "符号及其他"


def parse_name_keywords(raw):
    """把多行文本解析成 [(关键词小写, 目标目录名), ...]。

    每行一个关键词；支持「关键词 => 目录名」自定义目标文件夹；
    # 开头为注释；大小写不敏感；最多 100 条。
    """
    out = []
    if isinstance(raw, list):
        raw = "\n".join(str(x) for x in raw)
    for line in NAME_KW_SEP.split(str(raw or "")):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = NAME_KW_ARROW.split(line, 1)
        kw = parts[0].strip()
        target = parts[1].strip() if len(parts) > 1 and parts[1].strip() else kw
        if kw:
            out.append((kw.lower(), target))
        if len(out) >= 100:
            break
    return out


def keyword_group(path, keywords, fallback=""):
    """自上而下首个命中的关键词决定目录；都不命中时：
    - fallback 有值 → 归入该目录
    - fallback 为空 → 返回 None，调用方跳过该文件（留在原文件夹不动）
    """
    name = os.path.basename(path).lower()
    for kw, target in keywords:
        if kw in name:
            return target
    fb = str(fallback or "").strip()
    return fb or None


def group_key(path, size, file_bytes, mode, rules=None, fallback="其他", extra=None):
    extra = extra or {}
    is_video = extra.get("media") == "video"
    ext = os.path.splitext(path)[1].lower()
    w, h = size if size else (0, 0)
    dur = float(extra.get("duration") or 0)
    codec = str(extra.get("codec") or "")
    if mode == "resolution":
        return f"{w}x{h}" if size else ("无法读取分辨率" if is_video else "无法读取尺寸")
    if mode == "format":
        e = ext.lstrip(".").upper()
        return "JPG" if e in ("JPG", "JPEG") else (e or "未知")
    if mode == "orientation":
        if not size:
            return "未知"
        if is_video:
            return "方屏" if w == h else ("横屏" if w > h else "竖屏")
        return "方图" if w == h else ("横图" if w > h else "竖图")
    if mode == "date":
        return time.strftime("%Y-%m", time.localtime(os.path.getmtime(path)))
    if mode == "bucket":
        if not size:
            return "未知"
        mp = w * h / 1_000_000
        if is_video:
            if mp < 0.5:
                return "低清 (小于50万像素)"
            if mp < 1:
                return "标清 (50-100万像素)"
            if mp < 2.1:
                return "高清 (100-210万像素)"
            if mp < 8.3:
                return "超高清 (210-830万像素)"
            return "超高分辨率 (830万像素以上)"
        if mp < 1:
            return "小图 (小于100万像素)"
        if mp < 4:
            return "中图 (100-400万像素)"
        if mp < 12:
            return "大图 (400-1200万像素)"
        return "超大图 (1200万像素以上)"
    if mode == "filesize":
        kb, mb, gb = 1024, 1048576, 1073741824
        if is_video:
            if file_bytes < 10 * mb:
                return "小于 10MB"
            if file_bytes < 50 * mb:
                return "10 - 50MB"
            if file_bytes < 200 * mb:
                return "50 - 200MB"
            if file_bytes < gb:
                return "200MB - 1GB"
            return "1GB 以上"
        if file_bytes < 100 * kb:
            return "小于 100KB"
        if file_bytes < 1 * mb:
            return "100KB - 1MB"
        if file_bytes < 5 * mb:
            return "1 - 5MB"
        if file_bytes < 20 * mb:
            return "5 - 20MB"
        return "20MB 以上"
    if mode == "vquality":                       # 视频：按短边判定清晰度档位
        if not size:
            return "分辨率未知"
        q = min(w, h)
        if q <= 480:
            return "480P 及以下"
        if q <= 720:
            return "720P 高清"
        if q <= 1080:
            return "1080P 全高清"
        if q <= 1440:
            return "2K / 1440P"
        if q <= 2160:
            return "4K 超高清"
        return "4K 以上"
    if mode == "duration":
        if dur <= 0:
            return "时长未知"
        if dur < 60:
            return "1 分钟以内"
        if dur < 300:
            return "1 - 5 分钟"
        if dur < 900:
            return "5 - 15 分钟"
        if dur < 3600:
            return "15 - 60 分钟"
        return "1 小时以上"
    if mode == "codec":
        return codec or "编码未知"
    if mode == "name":
        if extra.get("name_mode") == "keyword":
            return keyword_group(path, extra.get("name_keywords") or [],
                                 extra.get("name_fallback"))
        return name_group(path, extra.get("name_mode"))
    if mode == "custom":
        return eval_custom(rule_context(path, size, file_bytes, extra), rules, fallback)
    return "未分类"


def collect(folder, exts, recursive, output_dir):
    out = []
    exts = tuple(e.lower() for e in exts)
    out_abs = os.path.abspath(output_dir)
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if os.path.abspath(os.path.join(root, d)) != out_abs
                       and not d.startswith(".")]
            for name in files:
                if name.lower().endswith(exts):
                    p = os.path.join(root, name)
                    if os.path.isfile(p):
                        out.append(p)
    else:
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if os.path.isfile(p) and name.lower().endswith(exts):
                out.append(p)
    return sorted(out)


def suggest_keywords(folder, exts, recursive, media, output):
    """从当前文件夹的文件名中提取「出现在多个文件里」的共同片段，作为关键词建议。"""
    default_out = "视频分类" if media == "video" else "分类"
    out_dir = os.path.join(folder, safe_name(output or default_out))
    files = collect(folder, exts, recursive, out_dir)[:400]
    stems = [os.path.splitext(os.path.basename(p))[0].lower() for p in files]
    strip_chars = " _-.()[]{}【】〔〕·,，、;；#～~+0123456789"
    freq = {}
    for i, s in enumerate(stems):
        seen = set()
        n = len(s)
        for a in range(n):
            for length in range(2, min(14, n - a) + 1):
                sub = s[a:a + length]
                if sub in seen:
                    continue
                seen.add(sub)
                if not sub.strip(strip_chars):   # 纯数字 / 纯符号片段不作为建议
                    continue
                if sub[-1].isdigit():
                    continue                     # 去掉 IMG_00 / g_0 这类带编号尾巴的片段
                freq.setdefault(sub, set()).add(i)
    pairs = [(sub, idxs) for sub, idxs in freq.items() if len(idxs) >= 2]
    pairs.sort(key=lambda x: (-len(x[1]), -len(x[0]), x[0]))
    chosen = []
    for sub, idxs in pairs:
        df = len(idxs)
        # 去掉与已选片段互相包含且覆盖范围相同的冗余项
        if any((sub in c and df <= len(ci)) or (c in sub and df >= len(ci))
               for c, ci in chosen):
            continue
        chosen.append((sub, idxs))
        if len(chosen) >= 16:
            break
    return {"ok": True, "total": len(files),
            "items": [{"kw": sub, "count": len(idxs)} for sub, idxs in chosen]}


def scan_worker(cfg):
    with LOCK:
        STATE["scanning"] = True
    folder = cfg["folder"]
    media = cfg.get("media", "image")
    exts = cfg.get("exts") or MEDIA_EXTS.get(media, EXTS)
    recursive = cfg.get("recursive", False)
    mode = cfg.get("mode", "vquality" if media == "video" else "resolution")
    rules = cfg.get("rules") or load_rules(media)["rules"]
    fallback = cfg.get("fallback", "其他")
    default_out = "视频分类" if media == "video" else "分类"
    name_mode = cfg.get("name_mode") if cfg.get("name_mode") in NAME_MODES else "first"
    name_keywords = parse_name_keywords(cfg.get("name_keywords"))
    name_fallback = str(cfg.get("name_fallback") or "").strip()[:80]  # 留空 = 未匹配文件不移动
    output_dir = os.path.join(folder, safe_name(cfg.get("output", default_out)))
    files = collect(folder, exts, recursive, output_dir)
    metas = media_meta_batch(files, media)
    groups = {}
    total_size = 0
    bad = 0
    stayed = 0
    for p in files:
        try:
            sz = os.path.getsize(p)
        except OSError:
            sz = 0
        total_size += sz
        m = metas[p]
        size = m["size"]
        extra = {"media": media, "duration": m["duration"], "codec": m["codec"],
                 "name_mode": name_mode, "name_keywords": name_keywords,
                 "name_fallback": name_fallback}
        if size is None:
            bad += 1
        key = group_key(p, size, sz, mode, rules, fallback, extra)
        if key is None:                         # 未匹配且「没包含时归入」留空
            stayed += 1
            continue
        g = groups.setdefault(key, {"key": key, "count": 0, "size": 0,
                                    "samples": [], "res": "", "meta": ""})
        g["count"] += 1
        g["size"] += sz
        if size:
            g["res"] = f"{size[0]} × {size[1]}"
            if media == "video":
                g["meta"] = f"{size[0]}×{size[1]} · {duration_str(m['duration'])}"
                if m["codec"]:
                    g["meta"] += f" · {m['codec']}"
        if len(g["samples"]) < 3:
            g["samples"].append(p)
    result = sorted(groups.values(), key=lambda x: -x["count"])
    with LOCK:
        STATE["groups"] = result
        STATE["scanning"] = False
        STATE["scan"] = {
            "total": sum(g["count"] for g in result),
            "size": human(total_size),
            "groups": len(result),
            "bad": bad,
            "stayed": stayed,
            "folder": folder,
            "output": output_dir,
            "media": media,
        }
    log(f"扫描完成：{STATE['scan']['total']} 个{'视频' if media == 'video' else '图片'}，"
        f"{len(result)} 个分组"
        + (f"，{stayed} 个未匹配（留在原文件夹）" if stayed else "")
        + (f"，{bad} 个无法解析元数据" if bad else ""), "ok")


def same_file(src, dst):
    try:
        return (os.path.getsize(src) == os.path.getsize(dst)
                and abs(os.path.getmtime(src) - os.path.getmtime(dst)) < 2)
    except OSError:
        return False


def classify_worker(cfg):
    folder = cfg["folder"]
    media = cfg.get("media", "image")
    exts = cfg.get("exts") or MEDIA_EXTS.get(media, EXTS)
    recursive = cfg.get("recursive", False)
    mode = cfg.get("mode", "vquality" if media == "video" else "resolution")
    rules = cfg.get("rules") or load_rules(media)["rules"]
    fallback = cfg.get("fallback", "其他")
    action = cfg.get("action", "copy")
    conflict = cfg.get("conflict", "rename")
    skip_same = cfg.get("skip_same", True)
    default_out = "视频分类" if media == "video" else "分类"
    name_mode = cfg.get("name_mode") if cfg.get("name_mode") in NAME_MODES else "first"
    name_keywords = parse_name_keywords(cfg.get("name_keywords"))
    name_fallback = str(cfg.get("name_fallback") or "").strip()[:80]  # 留空 = 未匹配文件不移动
    output_root = os.path.join(folder, safe_name(cfg.get("output", default_out)))
    files = collect(folder, exts, recursive, output_root)
    metas = media_meta_batch(files, media)
    with LOCK:
        STATE.update(total=len(files), done=0, ok=0, skipped=0, stayed=0, failed=0,
                     finished=False, output=output_root)
    ABORT.clear()
    log(f"开始分类：{len(files)} 个文件 → {output_root}", "ok")
    created_root = not os.path.isdir(output_root)
    os.makedirs(output_root, exist_ok=True)
    for p in files:
        if ABORT.is_set():
            log("已由用户中止", "warn")
            break
        name = os.path.basename(p)
        try:
            m = metas[p]
            try:
                file_bytes = os.path.getsize(p)
            except OSError:
                file_bytes = 0
            extra = {"media": media, "duration": m["duration"], "codec": m["codec"],
                     "name_mode": name_mode, "name_keywords": name_keywords,
                     "name_fallback": name_fallback}
            key = group_key(p, m["size"], file_bytes, mode, rules, fallback, extra)
            if key is None:                     # 未匹配且未填「没包含时归入」→ 原文件保持不动
                with LOCK:
                    STATE["stayed"] += 1
                log(f"留在原文件夹（未匹配）{name}", "dim")
                continue
            target_dir = os.path.join(output_root, safe_name(key))
            os.makedirs(target_dir, exist_ok=True)
            dst = os.path.join(target_dir, name)
            if os.path.exists(dst):
                if skip_same and same_file(p, dst):
                    with LOCK:
                        STATE["skipped"] += 1
                    log(f"跳过（已存在相同文件）{name}", "dim")
                    continue
                if conflict == "skip":
                    with LOCK:
                        STATE["skipped"] += 1
                    log(f"跳过（目标已存在）{name}", "dim")
                    continue
                if conflict == "rename":
                    stem, ext = os.path.splitext(name)
                    i = 1
                    while os.path.exists(dst):
                        dst = os.path.join(target_dir, f"{stem}_{i}{ext}")
                        i += 1
            if action == "move":
                shutil.move(p, dst)
            else:
                shutil.copy2(p, dst)
            with LOCK:
                STATE["ok"] += 1
            log(f"{'移动' if action == 'move' else '复制'} {name} → {safe_name(key)}", "ok")
        except Exception as e:
            with LOCK:
                STATE["failed"] += 1
            log(f"处理 {name} 时出错：{e}", "err")
        finally:
            with LOCK:
                STATE["done"] += 1
    with LOCK:
        STATE["running"] = False
        STATE["finished"] = True
    log(f"分类结束：成功 {STATE['ok']}，留在原处 {STATE['stayed']}，"
        f"跳过 {STATE['skipped']}，失败 {STATE['failed']}", "ok")
    # 全部未匹配且允许留空时，不让本次运行留下一个空的输出目录
    if created_root and not STATE["ok"] and STATE["stayed"]:
        try:
            if not os.listdir(output_root):
                os.rmdir(output_root)
                log("未处理任何文件，已移除空的输出目录", "dim")
        except OSError:
            pass


def legacy_worker(cfg, exe):
    """兼容模式：以选中文件夹为工作目录调用原始 exe。"""
    folder = cfg["folder"]
    with LOCK:
        STATE.update(total=0, done=0, ok=0, skipped=0, stayed=0, failed=0,
                     finished=False, output=os.path.join(folder, "分类"))
    log(f"调用原程序：{exe}（工作目录 {folder}）", "info")
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        r = subprocess.run([exe], cwd=folder, capture_output=True, timeout=600,
                           startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW)
        # 原程序以控制台 ANSI 代码页输出中文（简体中文 Windows 为 GBK）
        def dec(b):
            if not b:
                return ""
            for enc in ("utf-8", "gbk", "mbcs"):
                try:
                    return b.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
            return b.decode("gbk", errors="replace")
        for line in dec(r.stdout).splitlines():
            if line.strip():
                log(line.strip(), "ok")
                with LOCK:
                    STATE["ok"] += 1
        if r.returncode != 0:
            log(f"原程序退出码 {r.returncode}：{dec(r.stderr).strip()[:400]}", "err")
        else:
            log("原程序执行完毕（输出目录：分类/<宽>x<高>）", "ok")
    except Exception as e:
        log(f"调用原程序失败：{e}", "err")
    with LOCK:
        STATE["running"] = False
        STATE["finished"] = True
        STATE["done"] = STATE["total"] = STATE["ok"]


def find_legacy_exe():
    cands = [
        os.path.join(os.path.expanduser("~"), "Desktop", "图片分类.exe"),
        r"K:\桌面\图片分类.exe",
        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop", "图片分类.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


# ---------------------------------------------------------------- 系统文件夹对话框
PICKER_PS = r'''
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = '选择要分类的图片文件夹'
$d.ShowNewFolderButton = $false
$d.RootFolder = [System.Environment+SpecialFolder]::MyComputer
$owner = New-Object System.Windows.Forms.Form
$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$owner.Size = New-Object System.Drawing.Size(1,1)
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.TopMost = $true
$owner.Show()
$r = $d.ShowDialog($owner)
$owner.Close()
if ($r -eq [System.Windows.Forms.DialogResult]::OK) {
  [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($d.SelectedPath))
}
'''


def _win32_pick_folder():
    """PowerShell 不可用时的兜底：直接调用 shell32.SHBrowseForFolderW。"""
    import ctypes
    from ctypes import wintypes

    class BROWSEINFO(ctypes.Structure):
        _fields_ = [("hwndOwner", wintypes.HWND), ("pidlRoot", ctypes.c_void_p),
                    ("pszDisplayName", wintypes.LPWSTR), ("lpszTitle", wintypes.LPWSTR),
                    ("ulFlags", wintypes.UINT), ("lpfn", ctypes.c_void_p),
                    ("lParam", ctypes.c_void_p), ("iImage", ctypes.c_int)]

    buf = ctypes.create_unicode_buffer(1024)
    bi = BROWSEINFO(None, None, buf, "选择要分类的图片文件夹", 0x40, None, None, 0)
    pidl = ctypes.windll.shell32.SHBrowseForFolderW(ctypes.byref(bi))
    if not pidl:
        return ""
    try:
        ctypes.windll.shell32.SHGetPathFromIDListW(pidl, buf)
        return buf.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(pidl)


def pick_folder():
    ok_cancel = False
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
             "-Command", PICKER_PS],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in reversed((r.stdout or "").strip().splitlines()):
            try:
                p = base64.b64decode(line.strip()).decode("utf-8")
                if p:
                    return p
            except Exception:
                continue
        ok_cancel = (r.returncode == 0)      # 正常返回但无输出 = 用户取消
    except Exception as e:
        log(f"系统对话框调用异常，切换原生接口：{e}", "warn")
    if ok_cancel:
        return ""
    try:
        return _win32_pick_folder()
    except Exception as e:
        log(f"打开文件夹对话框失败：{e}", "err")
        return ""


# ---------------------------------------------------------------- 拖放文件夹识别
# 浏览器 / WebView2 出于安全不会暴露拖入项的完整路径，只给得出名称。
# 因此借助 Windows Shell COM 读取「资源管理器窗口当前目录 + 选中项」，
# 再用名称在常用目录中定位，两条线索足以覆盖桌面 / 资源管理器拖入的绝大多数场景。
DROP_PS = r'''
$ErrorActionPreference = 'SilentlyContinue'
$out = New-Object System.Collections.ArrayList
try {
  $shell = New-Object -ComObject Shell.Application
  foreach ($w in @($shell.Windows())) {
    try {
      $doc = $w.Document
      $cwd = $doc.Folder.Self.Path
      if ($cwd) { [void]$out.Add(@{ kind = 'cwd'; path = $cwd }) }
      foreach ($s in @($doc.SelectedItems())) {
        if ($s.Path) { [void]$out.Add(@{ kind = 'selected'; path = $s.Path }) }
      }
    } catch {}
  }
} catch {}
$json = @{ items = @($out.ToArray()) } | ConvertTo-Json -Compress -Depth 4
[Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($json))
'''

_URI_RE = re.compile(r"file:[^\s\"'<>]+", re.I)
_WIN_RE = re.compile(r"[A-Za-z]:[\\/][^\r\n\"'<>|]+")


def shell_context():
    """返回资源管理器窗口的当前目录(kind=cwd)与选中项(kind=selected)路径。"""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
             "-Command", DROP_PS],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in reversed((r.stdout or "").strip().splitlines()):
            try:
                d = json.loads(base64.b64decode(line.strip()).decode("utf-8"))
            except Exception:
                continue
            items = d.get("items", [])
            if isinstance(items, dict):
                items = [items]
            return [x for x in items if isinstance(x, dict) and x.get("path")]
    except Exception:
        pass
    return []


def _norm_path(p):
    """把 file:/// URI、正斜杠、引号包裹等统一成 Windows 本地路径。"""
    p = (p or "").strip().strip('"').strip("'").strip()
    if not p:
        return ""
    if p.lower().startswith("file:"):
        raw = p[5:]
        if raw.startswith("///"):
            p = unquote(raw[3:])
        elif raw.startswith("//"):
            p = "\\\\" + unquote(raw[2:])
        else:
            p = unquote(raw.lstrip("/"))
    p = p.replace("/", "\\")
    if len(p) > 3 and p.endswith("\\"):
        p = p.rstrip("\\")
    return p


def _text_paths(texts):
    """从 text/uri-list、text/plain、text/html 里抽出可能的绝对路径。"""
    out = []
    for t in texts:
        if not isinstance(t, str) or not t.strip():
            continue
        if "%" in t:
            t = unquote(t)
        out.extend(_URI_RE.findall(t))
        out.extend(_WIN_RE.findall(t))
    return out


def _drop_ok(path, source, name=""):
    p = os.path.normpath(path)
    return {"ok": True, "path": p, "candidates": [p], "source": source, "name": name}


def resolve_drop(data):
    """把前端拖放事件里能拿到的信息还原成源文件夹绝对路径。"""
    if not isinstance(data, dict):
        data = {}
    paths = [x for x in (data.get("paths") or []) if isinstance(x, str) and x.strip()]
    texts = [x for x in (data.get("texts") or []) if isinstance(x, str) and x.strip()]
    names = [x.strip().strip('"') for x in (data.get("names") or [])
             if isinstance(x, str) and x.strip()]
    entries = [x for x in (data.get("entries") or []) if isinstance(x, str) and x.strip()]
    hint = data.get("hint") if isinstance(data.get("hint"), str) else ""

    # 文件夹拖入时 Chrome 往往只给 webkitGetAsEntry，不给 File.name
    for e in entries:
        tail = e.rstrip("/").split("/")[-1]
        if tail and tail not in names:
            names.append(tail)

    # 1) 宿主直接给了完整路径 / URI 列表 / 拖入的是纯文本路径
    for raw in paths + _text_paths(texts):
        p = _norm_path(raw)
        if not p:
            continue
        if os.path.isdir(p):
            return _drop_ok(p, "拖入路径", names[0] if names else "")
        if os.path.isfile(p) and os.path.isdir(os.path.dirname(p)):
            return _drop_ok(os.path.dirname(p), "拖入文件 · 取其所在目录", names[0] if names else "")

    # 2) 资源管理器上下文（拖放时选中的正是被拖项，最可靠）
    ctx = shell_context()
    selected = [_norm_path(c["path"]) for c in ctx if c.get("kind") == "selected"]
    cwds = [_norm_path(c["path"]) for c in ctx if c.get("kind") == "cwd"]

    cand = []

    def add(p, src):
        if not p:
            return
        try:
            p = os.path.normpath(p)
        except Exception:
            return
        if os.path.isdir(p) and p not in [x[0] for x in cand]:
            cand.append((p, src))

    for s in selected:
        base = os.path.basename(s.rstrip("\\/"))
        if os.path.isdir(s):
            if not names or base in names:
                add(s, "资源管理器选中项")
        elif os.path.isfile(s):
            if not names or base in names:
                add(os.path.dirname(s), "拖入文件所在目录")

    # 3) 按名称在「资源管理器当前目录 / 现有源文件夹 / 常用目录 / 各盘根目录」查找
    roots = [(p, "资源管理器当前目录") for p in cwds]
    if hint:
        h = _norm_path(hint)
        roots.append((h, "当前源文件夹"))
        roots.append((os.path.dirname(h.rstrip("\\/")), "当前源文件夹的上级"))
    roots.extend((q["path"], q["label"]) for q in quick_paths())
    roots.append((os.path.expanduser("~"), "用户目录"))
    for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        r = d + ":\\"
        if os.path.isdir(r):
            roots.append((r, d + " 盘根目录"))

    for n in names:
        if re.match(r"^[A-Za-z]:[\\/]", n) or n.startswith("\\\\"):
            p = _norm_path(n)
            if os.path.isdir(p):
                add(p, "路径文本")
            elif os.path.isfile(p):
                add(os.path.dirname(p), "拖入文件所在目录")
        for r, label in roots:
            p = os.path.join(r, n)
            if os.path.isdir(p):
                add(p, label)
            elif os.path.isfile(p):
                add(r, label + " · 文件所在目录")

    if cand:
        return {"ok": True, "path": cand[0][0], "candidates": [x[0] for x in cand[:8]],
                "source": cand[0][1], "name": names[0] if names else ""}
    # 完全无法定位时，把资源管理器当前目录作为可点选的候选交给用户
    return {"ok": False, "msg": "未能定位拖入的文件夹，请点「浏览…」或粘贴完整路径",
            "candidates": [p for p in cwds if os.path.isdir(p)][:5],
            "name": names[0] if names else ""}


def quick_paths():
    home = os.path.expanduser("~")
    out = []
    for label, p in (("桌面", os.path.join(home, "Desktop")),
                     ("图片", os.path.join(home, "Pictures")),
                     ("下载", os.path.join(home, "Downloads")),
                     ("桌面(K:)", r"K:\桌面")):
        if os.path.isdir(p):
            out.append({"label": label, "path": p})
    return out


def find_qqbrowser():
    for p in (r"D:\Program Files\Tencent\QQBrowser\QQBrowser.exe",
              r"C:\Program Files\Tencent\QQBrowser\QQBrowser.exe",
              r"C:\Program Files (x86)\Tencent\QQBrowser\QQBrowser.exe"):
        if os.path.isfile(p):
            return p
    return ""


# ---------------------------------------------------------------- HTTP
class LocalServer(ThreadingHTTPServer):
    # Windows 下 SO_REUSEADDR 会允许多个进程同时监听同一端口，必须关闭
    allow_reuse_address = False
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as e:
                self._send(500, f"index.html 缺失: {e}", "text/plain; charset=utf-8")
        elif u.path == "/api/state":
            with LOCK:
                snap = {k: (v[:] if k == "logs" else v) for k, v in STATE.items()}
            self._json(snap)
        elif u.path == "/api/meta":
            self._json({"quick": quick_paths(), "exe": find_legacy_exe(),
                        "exts": EXTS, "video_exts": VIDEO_EXTS, "cwd": os.getcwd(),
                        "fields": [{"id": k, "label": v,
                                    "type": "number" if k in NUMERIC_FIELDS else "text",
                                    "media": "video" if k in ("duration", "codec") else "both"}
                                   for k, v in FIELD_LABELS.items()],
                        "ops_num": [">", ">=", "<", "<=", "==", "!="],
                        "ops_text": ["==", "!=", "contains", "startswith", "endswith"],
                        "name_modes": list(NAME_MODES),
                        "rules": load_rules("image"), "port": self.server.server_address[1]})
        elif u.path == "/api/rules":
            media = q.get("media", ["image"])[0]
            self._json(load_rules(media if media in MEDIA_EXTS else "image"))
        elif u.path == "/api/namekw":
            media = q.get("media", ["image"])[0]
            self._json(load_name_keywords(media if media in MEDIA_EXTS else "image"))
        elif u.path == "/api/theme":
            self._json({"theme": load_theme()})
        elif u.path == "/api/file":
            p = unquote(q.get("p", [""])[0])
            if not p or not os.path.isfile(p) or not p.lower().endswith(tuple(EXTS)):
                return self._send(404, b"", "text/plain")
            try:
                with open(p, "rb") as f:
                    data = f.read()
                ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
                self._send(200, data, ctype)
            except OSError:
                self._send(404, b"", "text/plain")
        else:
            self._send(404, b"Not Found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        try:
            cfg = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            cfg = {}
        try:
            if u.path == "/api/browse":
                p = pick_folder()
                return self._json({"path": p})
            if u.path == "/api/drop":
                r = resolve_drop(cfg)
                if r.get("ok"):
                    log(f"拖放识别源文件夹：{r['path']}（{r.get('source', '')}）", "ok")
                else:
                    log(f"拖放识别失败：{r.get('name') or '未知项目'}", "warn")
                return self._json(r)
            if u.path == "/api/rules":
                ok = save_rules(cfg)
                log("自定义规则已保存" if ok else "规则保存失败", "ok" if ok else "err")
                return self._json({"ok": ok})
            if u.path == "/api/namekw":
                ok = save_name_keywords(cfg)
                if ok:
                    log("文件名关键词已保存", "ok")
                return self._json({"ok": ok})
            if u.path == "/api/theme":
                return self._json({"ok": save_theme(cfg.get("theme"))})
            if u.path == "/api/name_suggest":
                folder = cfg.get("folder", "")
                if not os.path.isdir(folder):
                    return self._json({"ok": False, "msg": "文件夹不存在"})
                media = cfg.get("media") if cfg.get("media") in MEDIA_EXTS else "image"
                exts = cfg.get("exts") or MEDIA_EXTS.get(media, EXTS)
                return self._json(suggest_keywords(folder, exts,
                                                   cfg.get("recursive", False),
                                                   media, cfg.get("output", "")))
            if u.path == "/api/quit":
                log("正在退出程序…", "warn")
                EXIT_EVENT.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return self._json({"ok": True})
            if u.path == "/api/open":
                p = cfg.get("path", "")
                if p and os.path.isdir(p):
                    os.startfile(p)
                    return self._json({"ok": True})
                return self._json({"ok": False, "msg": "目录不存在"})
            if u.path == "/api/abort":
                ABORT.set()
                log("收到中止请求", "warn")
                return self._json({"ok": True})
            if u.path == "/api/scan":
                folder = cfg.get("folder", "")
                if not os.path.isdir(folder):
                    return self._json({"ok": False, "msg": "文件夹不存在"})
                if STATE["running"] or STATE["scanning"]:
                    return self._json({"ok": False, "msg": "任务进行中"})
                with LOCK:
                    STATE["logs"] = []
                    STATE["groups"] = []
                    STATE["scan"] = {}
                    STATE["finished"] = False
                    STATE["scanning"] = True
                threading.Thread(target=lambda: self._guard(scan_worker, cfg), daemon=True).start()
                return self._json({"ok": True})
            if u.path == "/api/classify":
                folder = cfg.get("folder", "")
                if not os.path.isdir(folder):
                    return self._json({"ok": False, "msg": "文件夹不存在"})
                if STATE["running"] or STATE["scanning"]:
                    return self._json({"ok": False, "msg": "任务进行中"})
                with LOCK:
                    STATE["logs"] = []
                    STATE["finished"] = False
                    STATE["running"] = True
                if cfg.get("media") == "video" and cfg.get("engine") == "legacy":
                    with LOCK:
                        STATE["running"] = False
                    return self._json({"ok": False, "msg": "原程序只处理图片，视频分类请使用内置引擎"})
                if cfg.get("engine") == "legacy":
                    exe = find_legacy_exe()
                    if not exe:
                        with LOCK:
                            STATE["running"] = False
                        return self._json({"ok": False, "msg": "未找到原程序 图片分类.exe"})
                    th = threading.Thread(target=lambda: self._guard(legacy_worker, cfg, exe), daemon=True)
                else:
                    th = threading.Thread(target=lambda: self._guard(classify_worker, cfg), daemon=True)
                th.start()
                return self._json({"ok": True})
            return self._json({"ok": False, "msg": "未知接口"}, 404)
        except Exception:
            traceback.print_exc()
            return self._json({"ok": False, "msg": "服务端异常"}, 500)

    def _guard(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:
            traceback.print_exc()
            log(f"任务异常：{e}", "err")
            with LOCK:
                STATE["running"] = False
                STATE["scanning"] = False
                STATE["finished"] = True


def launch_browser(url, delay=0.8):
    def run():
        time.sleep(delay)
        qq = find_qqbrowser()
        try:
            if qq:
                subprocess.Popen([qq, url], creationflags=subprocess.DETACHED_PROCESS)
            else:
                import webbrowser
                webbrowser.open(url)
        except Exception:
            pass
    if os.environ.get("IMGCLASS_NO_BROWSER") != "1":
        threading.Thread(target=run, daemon=True).start()


def main():
    os.chdir(BASE)
    url = f"http://{HOST}:{PORT}/"
    try:
        srv = LocalServer((HOST, PORT), Handler)
    except OSError as e:
        if getattr(e, "winerror", None) == 10048 or getattr(e, "errno", None) in (98, 48):
            print("检测到服务已在运行，直接打开界面：", url, flush=True)
            launch_browser(url, delay=0)
            time.sleep(2)
            return
        raise
    launch_browser(url)
    print("图片分类工具已启动：", url)
    print("关闭此窗口即可退出服务。", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()

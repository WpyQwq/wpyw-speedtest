#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wpyw 网速测速（命令行版）v1.0
================================
零依赖网速测试工具，针对中国大陆宽带（联通 / 电信 / 移动）优化：
多线程 HTTP 下载 + 流式上传 + ICMP 延迟/抖动/丢包，全部节点在运行时实测优选。

用法：
    python wpyw-speedtest.py            # 标准测速（约 25-40 秒）
    python wpyw-speedtest.py --quick    # 快速测速（约 15 秒）
    python wpyw-speedtest.py --full     # 完整测速（更准，约 60 秒）
    python wpyw-speedtest.py --json out.json
    python wpyw-speedtest.py --help
"""

from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import os
import platform
import random
import re
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

VERSION = "1.0"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# SSL：测速节点常见自签/链不全，这里不做证书校验（只测速度，不传敏感数据）
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# ────────────────────────────────────────────────────────────────────────────
# 终端 UI
# ────────────────────────────────────────────────────────────────────────────
class C:
    """ANSI 颜色。--no-color / 非终端时自动降级为空串。"""

    on = True
    R = B = DIM = ""
    RED = GRN = YEL = BLU = CYA = MAG = GRY = WHT = ""

    @classmethod
    def setup(cls, enabled: bool) -> None:
        cls.on = enabled
        if not enabled:
            cls.R = cls.B = cls.DIM = ""
            cls.RED = cls.GRN = cls.YEL = cls.BLU = cls.CYA = cls.MAG = cls.GRY = cls.WHT = ""
            return
        cls.R = "\x1b[0m"
        cls.B = "\x1b[1m"
        cls.DIM = "\x1b[2m"
        cls.RED = "\x1b[38;5;203m"
        cls.GRN = "\x1b[38;5;114m"
        cls.YEL = "\x1b[38;5;221m"
        cls.BLU = "\x1b[38;5;75m"
        cls.CYA = "\x1b[38;5;80m"
        cls.MAG = "\x1b[38;5;176m"
        cls.GRY = "\x1b[38;5;245m"
        cls.WHT = "\x1b[38;5;231m"


SPARK = "▁▂▃▄▅▆▇█"
FILL, EMPTY = "█", "░"


def enable_vt() -> bool:
    """在 Windows 控制台打开 ANSI 转义支持。"""
    if os.name != "nt":
        return sys.stdout.isatty()
    try:
        k = ctypes.windll.kernel32
        for std in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            h = k.GetStdHandle(std)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
        return bool(sys.stdout.isatty())
    except Exception:
        return False


LIVE_OK = True          # 非终端（重定向/管道）时不刷进度行


def term_width(default: int = 100) -> int:
    if not LIVE_OK:
        return min(default, 96)
    try:
        return max(60, min(160, shutil.get_terminal_size((default, 25)).columns))
    except Exception:
        return default


def bar(pct: float, width: int = 26, color: str = "") -> str:
    n = int(round(width * max(0.0, min(100.0, pct)) / 100.0))
    s = FILL * n + EMPTY * (width - n)
    return f"{color}{s}{C.R}" if color else s


def sparkline(samples, width: int = 40) -> str:
    if not samples:
        return ""
    step = max(1, len(samples) // width)
    pts = samples[::step][-width:]
    hi = max(pts) or 1.0
    return "".join(SPARK[min(7, int(p * 8 / hi))] for p in pts)


def fmt_speed(mbps: float) -> str:
    if mbps >= 1000:
        return f"{mbps/1000:.2f} Gbit/s"
    if mbps >= 1:
        return f"{mbps:.2f} Mbit/s"
    return f"{mbps*1000:.1f} Kbit/s"


def rule(title: str = "", color: str = "") -> None:
    w = term_width()
    if not title:
        print(f"{C.GRY}{'─' * w}{C.R}")
        return
    head = f"── {title} "
    pad = max(0, w - len(head) - 2)
    col = color or C.CYA
    print(f"\n{col}{C.B}{head}{'─' * pad}{C.R}")


def row(label: str, value: str, note: str = "", lw: int = 12) -> None:
    pad = " " * max(1, lw - disp_len(label))
    print(f"  {C.GRY}{label}{C.R}{pad}{value}" + (f"  {C.GRY}{note}{C.R}" if note else ""))


def disp_len(s: str) -> int:
    """粗略的显示宽度（中日韩字符算 2 列）。"""
    n = 0
    for ch in s:
        n += 2 if ("\u1100" <= ch <= "\u115f" or "\u2e80" <= ch <= "\ua4cf"
                   or "\uac00" <= ch <= "\ud7a3" or "\uf900" <= ch <= "\ufaff"
                   or "\ufe30" <= ch <= "\ufe6f" or "\uff00" <= ch <= "\uff60") else 1
    return n


def live(text: str) -> None:
    """原地刷新一行（仅交互终端）。"""
    if not LIVE_OK:
        return
    w = term_width()
    line = text
    # 按显示宽度截断，避免折行
    if disp_len(re.sub(r"\x1b\[[0-9;]*m", "", line)) > w - 1:
        plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
        line = plain[: w - 1]
    sys.stdout.write("\r\x1b[2K" + line)
    sys.stdout.flush()


# ────────────────────────────────────────────────────────────────────────────
# 本机 / 线路信息
# ────────────────────────────────────────────────────────────────────────────
def clear_line() -> None:
    if LIVE_OK:
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "未知"
    finally:
        s.close()


def default_gateway() -> str | None:
    """从 route print 解析默认网关（不需要管理员权限）。"""
    try:
        out = subprocess.run(["route", "print", "-4"], capture_output=True, timeout=8).stdout
        text = out.decode("gbk", "replace") if isinstance(out, bytes) else str(out)
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                return parts[2]
    except Exception:
        pass
    return None


def local_dns() -> str | None:
    """从 ipconfig /all 里取本机实际使用的第一个 IPv4 DNS（通常是运营商的）。"""
    try:
        out = subprocess.run(["ipconfig", "/all"], capture_output=True, timeout=8).stdout
        text = out.decode("gbk", "replace") if isinstance(out, bytes) else str(out)
        hits = []
        for line in text.splitlines():
            if re.search(r"DNS\s*Servers|DNS 服务器", line, re.I):
                for ip in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", line.split(":", 1)[-1].split("：", 1)[-1]):
                    if not ip.startswith(("127.", "0.")):
                        hits.append(ip)
            elif hits and re.match(r"^\s{20,}((?:\d{1,3}\.){3}\d{1,3})\s*$", line):
                hits.append(line.strip())
        return hits[0] if hits else None
    except Exception:
        return None


def public_ip_info(timeout: float = 3.0):
    """尽力而为地取公网 IP 与运营商（国内优先）。"""
    result = {"ip": None, "isp": None, "region": None, "source": None}

    def try_ipip():
        req = urllib.request.Request("https://myip.ipip.net", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            body = r.read(300).decode("utf-8", "replace")
        m = re.search(r"IP[：:]\s*([0-9a-fA-F:.]+)", body)
        loc = re.search(r"来自于[：:]\s*(.+)", body)
        return {"ip": m.group(1) if m else None,
                "region": loc.group(1).strip() if loc else None,
                "isp": (loc.group(1).strip() if loc else None), "source": "ipip.net"}

    def try_ipapi():
        req = urllib.request.Request(
            "http://ip-api.com/json/?lang=zh-CN&fields=query,country,regionName,city,isp,as",
            headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        if d.get("status") == "success" or d.get("query"):
            return {"ip": d.get("query"),
                    "region": " ".join(x for x in [d.get("country"), d.get("regionName"), d.get("city")] if x),
                    "isp": d.get("isp"), "source": "ip-api.com"}

    def try_ipinfo():
        req = urllib.request.Request("https://ipinfo.io/json", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return {"ip": d.get("ip"),
                "region": " ".join(x for x in [d.get("country"), d.get("region"), d.get("city")] if x),
                "isp": d.get("org"), "source": "ipinfo.io"}

    box: list = []
    lock = threading.Lock()

    def run(fn):
        try:
            r = fn()
            if r and r.get("ip"):
                with lock:
                    box.append(r)
        except Exception:
            pass

    threads = [threading.Thread(target=run, args=(f,), daemon=True) for f in (try_ipip, try_ipapi, try_ipinfo)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 2)
    if box:
        # 优先国内来源，其次取到运营商名字的那个
        box.sort(key=lambda r: 0 if r.get("source") == "ipip.net" else 1)
        result = box[0]
    return result


def isp_short(text: str | None) -> str:
    if not text:
        return "未知"
    for key in ("联通", "电信", "移动", "广电", "教育网", "Unicom", "Telecom", "Mobile"):
        if key in text:
            return {"Unicom": "联通", "Telecom": "电信", "Mobile": "移动"}.get(key, key)
    return "未知"


# ────────────────────────────────────────────────────────────────────────────
# 延迟测试（ICMP）
# ────────────────────────────────────────────────────────────────────────────
PING_TIME = re.compile(r"(?:time|时间)\s*[=<]\s*([\d.]+)\s*ms", re.I)
PING_LT = re.compile(r"(?:time|时间)\s*<\s*1\s*ms", re.I)


def ping_once_stats(host: str, count: int = 4, timeout_ms: int = 1500) -> dict:
    """调用系统 ping，返回 min/avg/max/jitter/loss。"""
    t0 = time.perf_counter()
    try:
        p = subprocess.run(["ping", "-n", str(count), "-w", str(timeout_ms), host],
                           capture_output=True, timeout=count * (timeout_ms / 1000.0) + 8)
        raw = p.stdout or b""
    except Exception:
        return {"host": host, "avg": None, "loss": 100.0, "samples": [], "elapsed": time.perf_counter() - t0}

    text = None
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        text = raw.decode("latin-1", "replace")

    times, replies = [], 0
    for line in text.splitlines():
        if re.search(r"TTL\s*=", line, re.I):
            replies += 1
            if PING_LT.search(line):
                times.append(0.5)
                continue
            m = PING_TIME.search(line)
            if m:
                times.append(float(m.group(1)))
    loss = max(0.0, (count - replies) * 100.0 / count)
    if not times:
        return {"host": host, "avg": None, "loss": loss, "samples": [], "elapsed": time.perf_counter() - t0}
    jitter = 0.0
    if len(times) > 1:
        jitter = statistics.mean(abs(b - a) for a, b in zip(times, times[1:]))
    return {"host": host, "avg": statistics.mean(times), "min": min(times), "max": max(times),
            "jitter": jitter, "loss": loss, "samples": times, "elapsed": time.perf_counter() - t0}


def tcp_latency(host: str, port: int, tries: int = 3) -> float | None:
    vals = []
    for _ in range(tries):
        try:
            t0 = time.perf_counter()
            s = socket.create_connection((host, port), timeout=3)
            vals.append((time.perf_counter() - t0) * 1000)
            s.close()
        except Exception:
            pass
    return min(vals) if vals else None


def phase_latency(hosts, gateway, quick: bool, do_tcp: str | None) -> dict:
    rule("① 延迟 / 抖动 / 丢包", C.BLU)
    count = 3 if quick else 5
    results, lock = [], threading.Lock()

    def run(host, label, kind="dom", note=""):
        r = ping_once_stats(host, count)
        r.update({"label": label, "kind": kind, "note": note})
        with lock:
            results.append(r)

    threads = []
    if gateway:
        threads.append(threading.Thread(target=run, args=(gateway, "默认网关", "gw"), daemon=True))
    for item in hosts:
        host, label = item[0], item[1]
        kind = item[2] if len(item) > 2 else "dom"
        note = item[3] if len(item) > 3 else ""
        threads.append(threading.Thread(target=run, args=(host, label, kind, note), daemon=True))
    for t in threads:
        t.start()
    for t in threads:
        t.join(count * 3 + 12)

    labels = ["默认网关"] + [h[1] for h in hosts]
    order = {lbl: i for i, lbl in enumerate(labels)}
    results.sort(key=lambda r: order.get(r["label"], 99))

    print()
    for r in results:
        if r["avg"] is None:
            hint = r.get("note") or ("该目标屏蔽 ICMP，不代表你的网络有问题" if r.get("kind") != "intl"
                                    else "国际方向不通")
            print(f"  {C.GRY}{r['label']:<15}{C.R}{C.YEL}不通 / 全部超时{C.R}  {C.GRY}{hint}{C.R}")
            continue
        ms = r["avg"]
        abroad = r.get("kind") == "intl"
        col = C.GRY if abroad else (C.GRN if ms < 30 else (C.YEL if ms < 80 else C.RED))
        jit = f"  抖动 {r['jitter']:.1f} ms" if r.get("jitter") else ""
        loss = f"  丢包 {r['loss']:.0f}%" if r["loss"] else ""
        lenbar = "▎" * max(1, min(20, int(ms / 5) + 1))
        note = f"  {C.GRY}{r['note']}{C.R}" if r.get("note") else ""
        print(f"  {r['label']:<15}{col}{ms:7.1f} ms{C.R}  {C.GRY}{lenbar}{jit}{loss}{C.R}{note}")

    if do_tcp:
        host, port = do_tcp.split(":")
        ms = tcp_latency(host, int(port))
        if ms is not None:
            print(f"  {'测速节点 TCP':<13}{C.CYA}{ms:7.1f} ms{C.R}  {C.GRY}到测速节点的纯网络往返（最优值）{C.R}")

    # 汇总：只统计国内目标（国际出口常被限速/屏蔽，参考价值不同）
    dom = [r for r in results if r["avg"] is not None and r.get("kind") != "intl" and r["label"] != "默认网关"]
    base = dom or [r for r in results if r["avg"] is not None and r["label"] != "默认网关"]
    avg = statistics.mean(r["avg"] for r in base) if base else None
    loss = max((r["loss"] for r in base), default=0.0)
    jit = statistics.mean([r["jitter"] for r in base if r.get("jitter")]) if base else 0.0
    intl = next((r for r in results if r.get("kind") == "intl" and r["avg"] is not None), None)
    return {"avg": avg, "jitter": jit, "loss": loss, "rows": results,
            "intl_avg": intl["avg"] if intl else None, "intl_loss": intl["loss"] if intl else None}


# ────────────────────────────────────────────────────────────────────────────
# 下载测速
# ────────────────────────────────────────────────────────────────────────────
DL_NODES = [
    ("腾讯云镜像", "深圳", "https://mirrors.cloud.tencent.com/ubuntu/ls-lR.gz"),
    ("华为云镜像", "贵州", "https://mirrors.huaweicloud.com/ubuntu/ls-lR.gz"),
    ("阿里云镜像", "杭州", "https://mirrors.aliyun.com/ubuntu/ls-lR.gz"),
    ("上海交大镜像", "上海", "https://mirror.sjtu.edu.cn/ubuntu/ls-lR.gz"),
    ("北大镜像", "北京", "https://mirrors.pku.edu.cn/ubuntu/ls-lR.gz"),
    ("中科大镜像", "合肥", "https://mirrors.ustc.edu.cn/ubuntu/ls-lR.gz"),
    ("清华 TUNA", "北京", "https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ls-lR.gz"),
    ("微信 CDN", "腾讯", "https://dldir1.qq.com/weixin/Windows/WeChatSetup.exe"),
]


class DownloadTest:
    def __init__(self, url: str, conns: int, seconds: float):
        self.url = url
        self.conns = conns
        self.seconds = seconds
        self.total = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.samples: list[float] = []
        self.errors = 0

    def _worker(self, size: int):
        while not self.stop.is_set():
            try:
                start = random.randint(0, max(0, size - (1 << 20))) if size > (2 << 20) else 0
                req = urllib.request.Request(self.url, headers={
                    "User-Agent": UA, "Range": f"bytes={start}-", "Accept-Encoding": "identity",
                    "Cache-Control": "no-cache",
                })
                with urllib.request.urlopen(req, timeout=8, context=SSL_CTX) as r:
                    while not self.stop.is_set():
                        b = r.read(1 << 16)
                        if not b:
                            break
                        with self.lock:
                            self.total += len(b)
            except Exception:
                with self.lock:
                    self.errors += 1
                if self.stop.is_set():
                    return
                time.sleep(0.05)

    def _size(self) -> int:
        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=6, context=SSL_CTX) as r:
                cr = r.headers.get("Content-Range") or ""
                m = re.search(r"/(\d+)$", cr)
                if m:
                    return int(m.group(1))
                cl = r.headers.get("Content-Length")
                return int(cl) if cl else 0
        except Exception:
            return 0

    def run(self, live_draw=None) -> dict:
        size = self._size()
        workers = [threading.Thread(target=self._worker, args=(size,), daemon=True) for _ in range(self.conns)]
        t0 = time.perf_counter()
        for w in workers:
            w.start()

        last_total, last_t = 0, t0
        ramp_bytes, ramp_t = 0, None
        peak = 0.0
        while True:
            time.sleep(0.1)
            now = time.perf_counter()
            with self.lock:
                cur = self.total
            inst = (cur - last_total) * 8 / max(1e-6, now - last_t) / 1e6
            last_total, last_t = cur, now
            self.samples.append(inst)
            elapsed = now - t0
            if elapsed > 1.2:                      # 跳过慢启动
                if ramp_t is None:
                    ramp_bytes, ramp_t = cur, now
                peak = max(peak, inst)
            if live_draw:
                live_draw(inst, elapsed)
            if elapsed >= self.seconds:
                break

        self.stop.set()
        end = time.perf_counter()
        for w in workers:
            w.join(1.5)
        total = self.total
        overall = total * 8 / max(1e-6, end - t0) / 1e6
        if ramp_t is not None:
            steady = (total - ramp_bytes) * 8 / max(1e-6, end - ramp_t) / 1e6
        else:
            steady = overall
        return {"mbps": max(steady, 0.0), "overall": overall, "peak": peak,
                "mb": total / 1e6, "seconds": end - t0, "samples": list(self.samples),
                "errors": self.errors, "size": size}


def pick_download_node(quick: bool, conns: int, verbose=True):
    """实测优选下载节点：按顺序短测，够快就停。"""
    budget = 2.0 if quick else 2.5
    max_try = 2 if quick else 4
    best = None
    tried = 0
    for name, city, url in DL_NODES:
        if tried >= max_try:
            break
        tried += 1
        if verbose:
            live(f"  {C.GRY}试节点 {name}（{city}）…{C.R}")
        t = DownloadTest(url, max(2, conns // 2), budget)
        r = t.run()
        if verbose:
            clear_line()
        ok = r["mbps"] > 0.5
        if verbose:
            col = C.GRN if r["mbps"] >= 50 else (C.YEL if r["mbps"] >= 5 else C.RED)
            print(f"  {C.GRY}节点试测 {name:<10}{C.R}{col}{r['mbps']:8.1f} Mbit/s{C.R}"
                  f"  {C.GRY}{city} · {conns // 2} 连接 / {budget:.0f}s{C.R}")
        if ok and (best is None or r["mbps"] > best[3]["mbps"]):
            best = (name, city, url, r)
        if ok and r["mbps"] >= 300:            # 已足够快，不再浪费时间
            break
    if best is None:
        raise RuntimeError("所有下载节点均不可用，请检查网络连接")
    return best


def phase_download(quick: bool, dl_seconds: float, conns: int) -> dict:
    rule("② 下载速度", C.GRN)
    name, city, url, probe = pick_download_node(quick, conns)
    if quick:
        seconds, threads = 5.0, max(4, conns)
    else:
        seconds, threads = dl_seconds, conns

    t = DownloadTest(url, threads, seconds)
    w = term_width()

    def draw(inst, elapsed):
        left = f"  {C.GRY}下载{C.R} {C.B}{C.GRN}{fmt_speed(inst):>13}{C.R}"
        right = f"{C.GRY}{elapsed:4.1f}/{seconds:.0f}s  {threads} 连接  {name}{C.R}"
        live(f"{left}  {right}")

    r = t.run(draw)
    clear_line()
    print(f"  节点  {C.WHT}{name}（{city}）{C.R}   {C.GRY}{threads} 连接 · {r['seconds']:.1f}s · "
          f"共 {r['mb']:.0f} MB{C.R}")
    print(f"  实测  {C.B}{C.GRN}{fmt_speed(r['mbps'])}{C.R}"
          f"   =  {C.WHT}{r['mbps']/8:.1f} MB/s{C.R}   {C.GRY}峰值 {fmt_speed(r['peak'])}{C.R}")
    sp = sparkline(r["samples"], min(48, w - 20))
    print(f"  曲线  {C.GRN}{sp}{C.R} {C.GRY}(左→右 {r['seconds']:.0f}s){C.R}")
    if r["errors"]:
        print(f"  {C.YEL}注意：{r['errors']} 次连接重试（轻微丢包/节点限速）{C.R}")
    r.update({"node": f"{name}（{city}）", "url": url})
    return r


# ────────────────────────────────────────────────────────────────────────────
# 上传测速
# ────────────────────────────────────────────────────────────────────────────
def ookla_cn_upload_targets(timeout: float = 6.0):
    """从 speedtest.net 取中国境内节点，构造上传地址（联通节点优先）。"""
    out = []
    try:
        url = "https://www.speedtest.net/api/js/servers?engine=js&limit=20&https=true"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            servers = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return out
    for s in servers:
        if "China" not in (s.get("country") or ""):
            continue
        host = (s.get("host") or "").split(":")[0]
        port = s.get("port") or 8080
        if not host or str(port) == "None":
            continue
        sponsor = s.get("sponsor") or ""
        city = s.get("name") or ""
        prio = 0 if ("nicom" in sponsor or "联通" in sponsor) else (1 if ("obile" in sponsor or "移动" in sponsor) else 2)
        out.append((prio, f"{sponsor}（{city}）", f"http://{host}:{port}/speedtest/upload.php"))
    out.sort(key=lambda x: x[0])
    return [(n, u) for _, n, u in out]


UL_FALLBACK = [
    ("Cloudflare 全球节点", "https://speed.cloudflare.com/__up"),
]


class UploadTest:
    """流式上传测速。

    每轮发送一个固定大小（默认 4MB）的请求，正常收尾再开下一轮 ——
    比「发一半直接掐断」稳得多，也不会在测速节点上留下半开连接拖慢后续测量。
    只有恰好卡在截止时刻的那一轮会被中断。
    """

    BODY = 4 << 20

    def __init__(self, url: str, conns: int, seconds: float, body: int | None = None):
        self.url = url
        self.conns = conns
        self.seconds = seconds
        self.body = body or self.BODY
        self.total = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.errors = 0
        self.samples: list[float] = []
        p = urllib.parse.urlsplit(url)
        self.https = p.scheme == "https"
        self.host = p.hostname
        self.port = p.port or (443 if self.https else 80)
        self.path = p.path or "/"
        self.mb = 0.0

    def _conn(self):
        if self.https:
            return http.client.HTTPSConnection(self.host, self.port, timeout=8, context=SSL_CTX)
        return http.client.HTTPConnection(self.host, self.port, timeout=8)

    def _worker(self):
        chunk = b"\x00" * (1 << 18)
        while not self.stop.is_set():
            c = None
            try:
                c = self._conn()
                c.putrequest("POST", self.path, skip_accept_encoding=True)
                c.putheader("User-Agent", UA)
                c.putheader("Content-Type", "application/x-www-form-urlencoded")
                c.putheader("Content-Length", str(self.body))
                c.endheaders()
                left = self.body
                clean = True
                while left > 0:
                    if self.stop.is_set():
                        clean = False
                        break
                    n = min(left, len(chunk))
                    c.send(chunk[:n])
                    left -= n
                    with self.lock:
                        self.total += n
                if clean:
                    c.getresponse().read()          # 正常收尾，不留半开连接
            except Exception:
                with self.lock:
                    self.errors += 1
                if self.stop.is_set():
                    return
                time.sleep(0.05)
            finally:
                try:
                    if c:
                        c.close()
                except Exception:
                    pass

    def run(self, live_draw=None) -> dict:
        workers = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.conns)]
        t0 = time.perf_counter()
        for w in workers:
            w.start()
        last_total, last_t = 0, t0
        peak = 0.0
        ramp_bytes, ramp_t = 0, None
        while True:
            time.sleep(0.1)
            now = time.perf_counter()
            with self.lock:
                cur = self.total
            inst = (cur - last_total) * 8 / max(1e-6, now - last_t) / 1e6
            last_total, last_t = cur, now
            self.samples.append(inst)
            elapsed = now - t0
            if elapsed > 1.2:
                if ramp_t is None:
                    ramp_bytes, ramp_t = cur, now
                peak = max(peak, inst)
            if live_draw:
                live_draw(inst, elapsed)
            if elapsed >= self.seconds:
                break
        self.stop.set()
        end = time.perf_counter()
        for w in workers:
            w.join(1.5)
        total = self.total
        overall = total * 8 / max(1e-6, end - t0) / 1e6
        steady = (total - ramp_bytes) * 8 / max(1e-6, end - ramp_t) / 1e6 if ramp_t else overall
        return {"mbps": max(steady, 0.0), "overall": overall, "peak": peak,
                "mb": total / 1e6, "seconds": end - t0, "samples": list(self.samples),
                "errors": self.errors}


def phase_upload(quick: bool, ul_seconds: float) -> dict | None:
    rule("③ 上传速度", C.MAG)
    live(f"  {C.GRY}寻找可用上传节点…{C.R}")
    targets = []
    try:
        targets = ookla_cn_upload_targets()
    except Exception:
        pass
    targets = targets[:2] + UL_FALLBACK
    budget = 2.5 if quick else 3.0
    best = None
    for name, url in targets[: (2 if quick else 3)]:
        t = UploadTest(url, 2, budget, body=2 << 20)
        try:
            r = t.run()
        except Exception:
            continue
        clear_line()
        col = C.GRN if r["mbps"] >= 20 else (C.YEL if r["mbps"] >= 3 else C.RED)
        print(f"  {C.GRY}节点试测 {name:<24}{C.R}{col}{r['mbps']:8.1f} Mbit/s{C.R}")
        if r["mbps"] > 0.3 and (best is None or r["mbps"] > best[2]["mbps"]):
            best = (name, url, r)
        if best and best[2]["mbps"] >= 40:
            break
    if best is None:
        print(f"  {C.YEL}未找到可用上传测速节点，跳过上传测试{C.R}")
        return None

    name, url, _ = best
    seconds = 4.0 if quick else ul_seconds
    conns = 4
    t = UploadTest(url, conns, seconds)

    def draw(inst, elapsed):
        live(f"  {C.GRY}上传{C.R} {C.B}{C.MAG}{fmt_speed(inst):>13}{C.R}"
             f"  {C.GRY}{elapsed:4.1f}/{seconds:.0f}s  {conns} 连接  {name}{C.R}")

    r = t.run(draw)
    clear_line()
    print(f"  节点  {C.WHT}{name}{C.R}   {C.GRY}{conns} 连接 · {r['seconds']:.1f}s · 共 {r['mb']:.0f} MB{C.R}")
    print(f"  实测  {C.B}{C.MAG}{fmt_speed(r['mbps'])}{C.R}"
          f"   =  {C.WHT}{r['mbps']/8:.1f} MB/s{C.R}   {C.GRY}峰值 {fmt_speed(r['peak'])}{C.R}")
    sp = sparkline(r["samples"], min(48, term_width() - 20))
    print(f"  曲线  {C.MAG}{sp}{C.R}")
    if r["peak"] > max(1.0, r["mbps"]) * 2.2:
        print(f"  {C.YEL}说明：上行波动较大（峰值 {fmt_speed(r['peak'])}），公共测速节点常有排队限速，"
              f"结果偏保守{C.R}")
    r.update({"node": name, "url": url})
    return r


# ────────────────────────────────────────────────────────────────────────────
# 评级与结论
# ────────────────────────────────────────────────────────────────────────────
TIERS = [2000, 1000, 500, 300, 200, 100, 50, 30, 20, 10, 4]


def tier_of(mbps: float) -> str:
    for t in TIERS:
        if mbps >= t * 0.8:
            return f"{t}M 档" if t < 1000 else f"{t//1000}G 档"
    return f"{mbps:.1f}M（低于 4M）"


def grade_latency(ms: float | None, loss: float, jitter: float) -> tuple[str, str, int]:
    if ms is None:
        return "不可用", C.RED, 0
    if loss >= 20:
        return "差（丢包严重）", C.RED, 1
    if ms < 15 and jitter < 5:
        return "极佳", C.GRN, 5
    if ms < 35 and jitter < 12:
        return "优秀", C.GRN, 4
    if ms < 70:
        return "良好", C.YEL, 3
    if ms < 120:
        return "一般", C.YEL, 2
    return "较差", C.RED, 1


def grade_speed(down: float, up: float | None) -> tuple[str, str]:
    if up is None:                                  # 只测了下行，评级不虚高
        if down >= 300:
            return "A（上行未测）", C.GRN
        if down >= 100:
            return "B（上行未测）", C.YEL
        if down >= 30:
            return "C（上行未测）", C.YEL
        return "D（上行未测）", C.RED
    score = 0
    if down >= 300:
        score += 3
    elif down >= 100:
        score += 1
    if up >= 30:
        score += 3
    elif up >= 10:
        score += 1
    if score >= 6:
        return "A+", C.GRN
    if score >= 4:
        return "A", C.GRN
    if score >= 2:
        return "B", C.YEL
    if score >= 1:
        return "C", C.YEL
    return "D", C.RED


def stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n)


def advice(down: float, up: float | None, lat: float | None, loss: float) -> list[str]:
    tips = []
    if down >= 300:
        tips.append(f"下载 {down/8:.0f} MB/s：4K 在线视频可同时跑 {max(1, int(down/25))} 路，游戏更新/大文件秒级")
    elif down >= 100:
        tips.append(f"下载 {down/8:.0f} MB/s：日常 4K 流媒体、多人同时在线都没问题")
    elif down >= 30:
        tips.append(f"下载 {down/8:.0f} MB/s：1080P/部分 4K 够用，大型下载偏慢")
    else:
        tips.append(f"下载仅 {down/8:.1f} MB/s：偏低，建议检查是否被限速/路由器瓶颈或 WiFi 信号")
    if up is not None:
        if up >= 30:
            tips.append(f"上传 {up/8:.1f} MB/s：可稳定 4K 直播推流、大文件快速上传")
        elif up >= 10:
            tips.append(f"上传 {up/8:.1f} MB/s：1080P 直播/视频会议够用，网盘上传偏慢")
        else:
            tips.append(f"上传 {up/8:.1f} MB/s：偏慢，直播推流会卡，建议确认宽带套餐的上行档位")
    if lat is not None:
        if lat < 20:
            tips.append(f"延迟 {lat:.0f} ms：电竞级，网游/远程桌面手感好")
        elif lat < 60:
            tips.append(f"延迟 {lat:.0f} ms：网游正常水平")
        else:
            tips.append(f"延迟 {lat:.0f} ms：偏高，检查是否走了 WiFi 或路由器开了 QoS/限速")
    if loss > 1:
        tips.append(f"丢包 {loss:.1f}%：有丢包，检查网线/水晶头、路由器负载或联系运营商")
    if down > 300 and (up or 0) < 10:
        tips.append("下行很快但上行很慢 —— 这是家用宽带的典型不对称，属正常现象")
    return tips


# ────────────────────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────────────────────
def banner() -> None:
    w = min(term_width() - 2, 74)
    title = f" Wpyw 网速测速 · 命令行版 v{VERSION} "
    print(f"{C.CYA}╭{'─' * (w - 2)}╮{C.R}")
    pad = " " * max(0, w - 4 - disp_len(title))
    print(f"{C.CYA}│{C.R} {C.B}{C.WHT}{title}{C.R}{pad} {C.CYA}│{C.R}")
    sub = "联通 / 电信 / 移动宽带 · 多线程测速 · 节点实测优选"
    pad2 = " " * max(0, w - 4 - disp_len(sub))
    print(f"{C.CYA}│{C.R} {C.GRY}{sub}{C.R}{pad2} {C.CYA}│{C.R}")
    print(f"{C.CYA}╰{'─' * (w - 2)}╯{C.R}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wpyw-speedtest",
        description="Wpyw 网速测速（命令行版）· 零依赖，针对中国大陆宽带优化",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  python wpyw-speedtest.py --quick\n  python wpyw-speedtest.py --json result.json\n"
               "  python wpyw-speedtest.py --no-upload --duration 12\n")
    p.add_argument("--quick", action="store_true", help="快速模式（约 15 秒，精度略低）")
    p.add_argument("--full", action="store_true", help="完整模式（约 60 秒，更接近峰值）")
    p.add_argument("--duration", type=float, default=8.0, help="下载测试时长秒数（默认 8）")
    p.add_argument("--upload-duration", type=float, default=6.0, help="上传测试时长秒数（默认 6）")
    p.add_argument("--conns", type=int, default=8, help="下载并发连接数（默认 8，跑满千兆可设 12-16）")
    p.add_argument("--no-upload", action="store_true", help="跳过上传测试")
    p.add_argument("--no-ping", action="store_true", help="跳过延迟测试")
    p.add_argument("--json", metavar="文件", help="把结果写入 JSON 文件（- 表示打印到标准输出）")
    p.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    p.add_argument("--ascii", action="store_true", help="用纯 ASCII 符号（老终端/重定向时更安全）")
    p.add_argument("--version", action="version", version=f"Wpyw 网速测速 v{VERSION}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    global FILL, EMPTY, SPARK, LIVE_OK
    if args.ascii:
        FILL, EMPTY, SPARK = "#", ".", "_.-~=+*#"

    try:
        LIVE_OK = bool(sys.stdout.isatty())
    except Exception:
        LIVE_OK = False
    try:
        sys.stdout.reconfigure(errors="replace")     # 老代码页下也不因生僻字崩掉
    except Exception:
        pass

    color_ok = (not args.no_color) and (enable_vt() or os.environ.get("FORCE_COLOR") == "1")
    if os.environ.get("FORCE_COLOR") == "1":
        color_ok = True
    C.setup(color_ok)

    quick = args.quick and not args.full
    banner()

    # ── 基本信息（并发取 IP/运营商，最多 3 秒）────────────────────────────
    ip_box: dict = {}
    gw_box: dict = {}

    def _ip():
        ip_box.update(public_ip_info())

    def _gw():
        gw_box["gw"] = default_gateway()

    t1 = threading.Thread(target=_ip, daemon=True)
    t2 = threading.Thread(target=_gw, daemon=True)
    t1.start()
    t2.start()

    mode = "快速" if quick else ("完整" if args.full else "标准")
    t_start = time.time()
    print()
    row("开始时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    row("测试模式", f"{mode}    {C.GRY}下载 {args.duration if not quick else 5:.0f}s / "
                   f"{args.conns} 连接    上传 {0 if args.no_upload else (args.upload_duration if not quick else 4):.0f}s{C.R}")
    row("本机地址", local_ip())
    t1.join(4)
    t2.join(4)
    gw = gw_box.get("gw")
    pub = ip_box
    if pub.get("ip"):
        row("公网地址", f"{C.WHT}{pub['ip']}{C.R}"
                       + (f"    {C.GRY}{pub.get('region') or ''}{C.R}" if pub.get("region") else ""))
        isp = isp_short(pub.get("isp") or pub.get("region"))
        col = C.GRN if isp == "联通" else (C.CYA if isp in ("电信", "移动") else C.YEL)
        row("运营商", f"{col}{C.B}{isp}{C.R}"
                     + (f"    {C.GRY}{(pub.get('isp') or '').strip()}{C.R}" if pub.get("isp") else ""))
    else:
        row("公网地址", f"{C.GRY}查询失败（不影响测速）{C.R}")
    if gw:
        row("默认网关", gw)

    result: dict = {
        "version": VERSION, "time": datetime.now().isoformat(timespec="seconds"),
        "mode": mode, "host": platform.node(), "local_ip": local_ip(),
        "public_ip": pub.get("ip"), "isp": isp_short(pub.get("isp") or pub.get("region")),
        "region": pub.get("region"), "gateway": gw,
    }

    # ── ① 延迟 ──────────────────────────────────────────────────────────
    lat = None
    if not args.no_ping:
        dns = local_dns()
        hosts = []
        if dns:
            hosts.append((dns, "本机 DNS", "dom", "运营商分配的解析服务器"))
        hosts += [
            ("223.5.5.5", "阿里云 DNS", "dom", ""),
            ("119.29.29.29", "腾讯 DNS", "dom", ""),
            ("180.76.76.76", "百度 DNS", "dom", ""),
            ("8.8.8.8", "国际出口", "intl", "国际线路，仅供参考（常被限速）"),
        ]
        if quick:
            keep = ("本机 DNS", "阿里云 DNS", "国际出口")
            hosts = [h for h in hosts if h[1] in keep]
            if not any(h[1] == "阿里云 DNS" for h in hosts):
                hosts.insert(0, ("223.5.5.5", "阿里云 DNS", "dom", ""))
        lat = phase_latency(hosts, gw, quick, "mirrors.cloud.tencent.com:443")
        col = C.GRN if (lat["avg"] or 999) < 35 else (C.YEL if (lat["avg"] or 999) < 80 else C.RED)
        g, gc, gs = grade_latency(lat["avg"], lat["loss"], lat["jitter"])
        avg_txt = f"{lat['avg']:.1f} ms" if lat["avg"] is not None else "N/A"
        print()
        print(f"  国内平均延迟  {col}{C.B}{avg_txt}{C.R}"
              f"     抖动 {C.WHT}{lat['jitter']:.1f} ms{C.R}"
              f"     丢包 {C.WHT}{lat['loss']:.1f}%{C.R}"
              f"     {gc}{stars(gs)} {g}{C.R}")
        if lat.get("intl_avg") is not None:
            print(f"  {C.GRY}国际出口 8.8.8.8  {lat['intl_avg']:.0f} ms"
                  f"（丢包 {lat['intl_loss']:.0f}%）—— 国际方向，不计入上面的均值{C.R}")
        result["latency"] = {k: v for k, v in lat.items() if k != "rows"}
        result["latency"]["rows"] = [{k: r.get(k) for k in ("label", "avg", "jitter", "loss", "kind")}
                                     for r in lat["rows"]]

    # ── ② 下载 ──────────────────────────────────────────────────────────
    try:
        dl = phase_download(quick, args.duration, max(2, args.conns))
    except Exception as e:
        print(f"\n  {C.RED}下载测速失败：{e}{C.R}")
        return 2
    result["download"] = {k: v for k, v in dl.items() if k != "samples"}
    result["download"]["samples"] = [round(x, 2) for x in dl["samples"]]

    # ── ③ 上传 ──────────────────────────────────────────────────────────
    ul = None
    if not args.no_upload:
        try:
            ul = phase_upload(quick, args.upload_duration)
        except Exception as e:
            print(f"  {C.YEL}上传测速失败：{e}{C.R}")
    if ul:
        result["upload"] = {k: v for k, v in ul.items() if k != "samples"}
        result["upload"]["samples"] = [round(x, 2) for x in ul["samples"]]

    # ── ④ 结论 ──────────────────────────────────────────────────────────
    rule("④ 结论", C.YEL)
    down = dl["mbps"]
    up = ul["mbps"] if ul else None
    lat_avg = lat["avg"] if lat else None
    loss = lat["loss"] if lat else 0.0
    t_down, t_up = tier_of(down), (tier_of(up) if up else "未测")
    line = (f"  下载 {C.B}{C.GRN}{fmt_speed(down)}{C.R}  {C.GRY}(≈{t_down}){C.R}"
            f"    上传 {C.B}{C.MAG}{(fmt_speed(up) if up else '未测')}{C.R}  {C.GRY}(≈{t_up}){C.R}")
    print("\n" + line)
    if lat_avg is not None:
        print(f"  延迟 {C.B}{C.WHT}{lat_avg:.1f} ms{C.R}     抖动 {C.WHT}{lat['jitter']:.1f} ms{C.R}"
              f"     丢包 {C.WHT}{loss:.1f}%{C.R}")
    print(f"  下载等效  {C.WHT}{down/8:.1f} MB/s{C.R}" + (f"     上传等效  {C.WHT}{up/8:.1f} MB/s{C.R}" if up else ""))

    g, gc = grade_speed(down, up)
    print(f"\n  综合评级  {gc}{C.B}{g}{C.R}      "
          f"{C.GRY}（依据：下行 {down:.0f} Mbps / 上行 {(up or 0):.0f} Mbps / 延迟 "
          f"{(f'{lat_avg:.0f} ms' if lat_avg is not None else '未测')}）{C.R}")
    print()
    for tip in advice(down, up, lat_avg, loss):
        print(f"  {C.CYA}·{C.R} {tip}")

    result["tier"] = {"download": t_down, "upload": t_up}
    result["grade"] = g
    result["advice"] = advice(down, up, lat_avg, loss)
    result["elapsed"] = round(time.time() - t_start, 1)

    print()
    rule()
    print(f"  {C.GRY}总耗时 {result['elapsed']:.0f} 秒 · 测速节点与结果已记录"
          f"{'  ·  结果已写入 ' + args.json if args.json and args.json != '-' else ''}{C.R}")
    print()

    if args.json:
        payload = json.dumps(result, ensure_ascii=False, indent=2)
        if args.json == "-":
            # 机器可读输出固定用 UTF-8，不受控制台代码页影响
            try:
                sys.stdout.flush()
                sys.stdout.buffer.write(payload.encode("utf-8") + b"\n")
                sys.stdout.buffer.flush()
            except Exception:
                print(payload)
        else:
            try:
                with open(args.json, "w", encoding="utf-8") as f:
                    f.write(payload)
                print(f"  {C.GRN}JSON 已保存：{os.path.abspath(args.json)}{C.R}\n")
            except Exception as e:
                print(f"  {C.RED}JSON 写入失败：{e}{C.R}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        clear_line()
        print(f"\n{C.YEL}已取消{C.R}")
        sys.exit(130)

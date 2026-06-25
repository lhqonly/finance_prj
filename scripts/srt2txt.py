#!/usr/bin/env python3
"""把 .srt / .vtt 字幕清洗成纯文本（去时间轴、去序号、去重复行）。
用法: srt2txt.py input.srt > output.txt
"""
import re
import sys


def clean(path: str) -> str:
    with open(path, encoding="utf-8", errors="ignore") as f:
        raw = f.read()

    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.isdigit():                       # srt 序号
            continue
        if "-->" in s:                        # 时间轴
            continue
        if s.upper().startswith("WEBVTT"):    # vtt 头
            continue
        if s.startswith(("Kind:", "Language:")):
            continue
        # 去掉 vtt 内联标签 <00:00:01.000> 和 <c> 之类
        s = re.sub(r"<[^>]+>", "", s).strip()
        if s:
            lines.append(s)

    # 相邻去重（AI 字幕常把同一句重复刷出来）
    out = []
    for s in lines:
        if not out or out[-1] != s:
            out.append(s)
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("用法: srt2txt.py <字幕文件.srt|.vtt>")
    sys.stdout.write(clean(sys.argv[1]) + "\n")

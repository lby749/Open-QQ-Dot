"""Render the Open QQ Dot promo and tutorial videos from local screenshots.

No external editor is required: Pillow draws the frames, ffmpeg receives raw
frames through a pipe and adds a generated ambient soundtrack.  The output is
1920x1080 H.264 MP4 in ``exports/videos``.
"""
from __future__ import annotations

import math
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "exports" / "media"
OUT = ROOT / "exports" / "videos"
OUT.mkdir(parents=True, exist_ok=True)
W, H, FPS = 1920, 1080, 24
FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
BG = (7, 13, 24)
PANEL = (15, 27, 45)
TEXT = (232, 240, 255)
MUTED = (148, 163, 191)
BLUE = (59, 130, 246)
MINT = (52, 211, 153)
ORANGE = (251, 146, 60)


def font(size, bold=False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def base_background():
    y = np.linspace(0, 1, H)[:, None]
    x = np.linspace(0, 1, W)[None, :]
    arr = np.zeros((H, W, 3), dtype=np.float32)
    arr[:, :, 0] = 7 + 5 * y
    arr[:, :, 1] = 13 + 12 * y
    arr[:, :, 2] = 24 + 30 * y
    for cx, cy, radius, color, strength in (
        (0.18, 0.22, 0.55, (37, 99, 235), 0.26),
        (0.82, 0.28, 0.50, (14, 165, 233), 0.20),
        (0.72, 0.84, 0.55, (16, 185, 129), 0.10),
    ):
        d = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        glow = np.clip(1 - d / radius, 0, 1) ** 2 * strength
        for i in range(3):
            arr[:, :, i] += glow * color[i]
    # fine grid
    for gx in range(0, W, 80):
        arr[:, gx:gx + 1, :] += 5
    for gy in range(0, H, 80):
        arr[gy:gy + 1, :, :] += 5
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


BASE = base_background()


def panel(draw, box, radius=28, fill=PANEL, outline=(38, 57, 84), alpha=238):
    if isinstance(draw, Image.Image):
        draw = ImageDraw.Draw(draw)
    x1, y1, x2, y2 = box
    draw.rounded_rectangle([x1 + 6, y1 + 10, x2 + 6, y2 + 10], radius=radius,
                           fill=(0, 0, 0, 80))
    draw.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=(*fill, alpha),
                           outline=outline, width=2)


def text(draw, xy, value, size, color=TEXT, bold=False, anchor="la"):
    draw.text(xy, value, font=font(size, bold), fill=color, anchor=anchor)


def wrap(draw, value, size, width, bold=False):
    f = font(size, bold)
    lines, current = [], ""
    for char in value:
        if draw.textlength(current + char, font=f) <= width:
            current += char
        else:
            lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


def paragraph(draw, xy, value, size, width, color=MUTED, bold=False, leading=1.5):
    x, y = xy
    for line in wrap(draw, value, size, width, bold):
        text(draw, (x, y), line, size, color, bold)
        y += int(size * leading)
    return y


_ROUND_CACHE = {}


def rounded_image(img, image, box, radius=24, border=(51, 77, 111)):
    x1, y1, x2, y2 = box
    w, h = max(1, x2 - x1), max(1, y2 - y1)
    key = (id(image), tuple(box), radius, tuple(border))
    patch = _ROUND_CACHE.get(key)
    if patch is None:
        copy = image.copy().convert("RGB")
        ratio = max(w / copy.width, h / copy.height)
        copy = copy.resize((max(1, int(copy.width * ratio)), max(1, int(copy.height * ratio))), Image.LANCZOS)
        left = (copy.width - w) // 2
        top = (copy.height - h) // 2
        copy = copy.crop((left, top, left + w, top + h)).convert("RGBA")
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
        patch = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        patch.paste(copy, (0, 0), mask)
        ImageDraw.Draw(patch).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius,
                                                outline=border, width=2)
        _ROUND_CACHE[key] = patch
    img.alpha_composite(patch, (x1, y1))


def phone(img, screenshot, box, caption=""):
    x1, y1, x2, y2 = box
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([x1 - 12, y1 - 12, x2 + 12, y2 + 12], radius=54, fill=(3, 7, 13, 255),
                        outline=(61, 84, 113), width=3)
    rounded_image(img, screenshot, (x1, y1, x2, y2), radius=42, border=(20, 30, 46))
    d.rounded_rectangle([x1 + (x2 - x1) * .36, y1 - 2, x2 - (x2 - x1) * .36, y1 + 13],
                        radius=7, fill=(3, 7, 13, 255))
    if caption:
        text(d, ((x1 + x2) // 2, y2 + 34), caption, 24, MUTED, anchor="ma")


def ease(t):
    return 0.5 - 0.5 * math.cos(math.pi * max(0, min(1, t)))


def chip(draw, x, y, label, color=BLUE):
    width = int(draw.textlength(label, font=font(28, True))) + 58
    draw.rounded_rectangle([x, y, x + width, y + 56], radius=28, fill=(*color, 36), outline=(*color, 150), width=2)
    text(draw, (x + 29, y + 14), label, 28, TEXT, True)
    return width


def title_scene(img, p):
    d = ImageDraw.Draw(img)
    icon = Image.open(ROOT / "assets" / "app-icon.png").convert("RGBA")
    size = 220
    icon = icon.resize((size, size), Image.LANCZOS)
    img.alpha_composite(icon, (W // 2 - size // 2, 205))
    text(d, (W // 2, 480), "Open QQ Dot", 92, TEXT, True, "ma")
    text(d, (W // 2, 595), "把 QQ 变成你的桌面 AI 入口", 40, MUTED, anchor="ma")
    line_w = int(360 * ease(p * 1.4))
    d.rounded_rectangle([W // 2 - line_w // 2, 680, W // 2 + line_w // 2, 686], radius=3, fill=BLUE)
    text(d, (W // 2, 738), "桌面直连 · 独立登录 · 本地数据", 28, (138, 180, 255), anchor="ma")


def concept_scene(img, p):
    d = ImageDraw.Draw(img)
    text(d, (140, 220), "不再打开网页扫码", 66, TEXT, True)
    paragraph(d, (144, 320), "DotLink 在你的桌面里完成启动、快速登录、窗口内二维码与密码登录；"
                              "登录成功后自动连接消息桥接。", 31, 720)
    y = 500
    for label, color in (("桌面原生登录", BLUE), ("快速登录 / 扫码 / 密码", MINT), ("断线自动恢复", ORANGE)):
        chip(d, 144, y, label, color)
        y += 82
    panel(d, (1000, 165, 1780, 900))
    x = 1060
    for i, (num, label, detail) in enumerate((
            ("01", "登录", "三条路径，任选一条"),
            ("02", "连接", "自动启动桥接与 OneBot"),
            ("03", "使用", "好友、任务、胶囊都在桌面"))):
        cy = 240 + i * 210
        d.ellipse([x, cy, x + 68, cy + 68], fill=(*BLUE, 42), outline=(*BLUE, 160), width=2)
        text(d, (x + 34, cy + 34), num, 25, (147, 197, 253), True, "mm")
        text(d, (x + 92, cy + 2), label, 34, TEXT, True)
        text(d, (x + 92, cy + 48), detail, 23, MUTED)


def desktop_scene(img, p, screenshot):
    d = ImageDraw.Draw(img)
    text(d, (110, 92), "桌面原生，一眼可控", 50, TEXT, True)
    text(d, (112, 154), "登录、好友、诊断与导出全部在同一个窗口", 25, MUTED)
    panel(d, (430, 220, 1830, 930))
    rounded_image(img, screenshot, (455, 250, 1775, 880), radius=18)
    for y, label, color in ((315, "快速登录 / 扫码 / 密码", BLUE), (520, "登录后自动连接", MINT),
                            (735, "高级操作按需展开", ORANGE)):
        d.rounded_rectangle([55, y, 57, y + 145], radius=2, fill=color)
        text(d, (82, y + 4), label, 26, TEXT, True)
        text(d, (82, y + 45), "无需网页，无需另开控制台", 20, MUTED)
    text(d, (W // 2, 1002), "从安装到可聊天，只差一次登录", 30, (147, 197, 253), anchor="ma")


def phone_scene(img, p, status, chat):
    d = ImageDraw.Draw(img)
    text(d, (120, 100), "手机，是同一间好友小屋", 54, TEXT, True)
    paragraph(d, (124, 190), "扫码配对后，手机直接打开当前好友的状态、控制台、聊天与记忆；"
                              "模型和数据仍然留在电脑。", 28, 700)
    for i, label in enumerate(("状态总览", "智能体控制台", "聊天与记忆")):
        text(d, (128, 340 + i * 66), "—  " + label, 29, MUTED)
    phone(img, status, (960, 135, 1380, 920), "状态")
    phone(img, chat, (1420, 250, 1800, 965), "聊天")
    text(d, (W // 2, 1018), "配对一次，随时打开", 26, (147, 197, 253), anchor="ma")


def feature_scene(img, p):
    d = ImageDraw.Draw(img)
    text(d, (W // 2, 92), "一个入口，覆盖整条工作流", 48, TEXT, True, "ma")
    items = (
        ("聊天记录导出", "统一 JSON，迁移/备份都可用", BLUE),
        ("跨平台迁移", "ChatGPT / Claude / DeepSeek", MINT),
        ("任务胶囊", "工作流、技能、样例、示教", ORANGE),
        ("一键回溯", "任务文件、迁移、胶囊", (167, 139, 250)),
        ("数据备份", "好友、任务、设置一个文件", (34, 211, 238)),
        ("双身份发送", "好友身份 / 模型身份", (244, 114, 182)),
    )
    for i, (title_, detail, color) in enumerate(items):
        col, row = i % 3, i // 3
        x, y = 180 + col * 540, 230 + row * 320
        panel(d, (x, y, x + 480, y + 250))
        d.rounded_rectangle([x + 38, y + 42, x + 96, y + 100], radius=18, fill=(*color, 45), outline=(*color, 160), width=2)
        text(d, (x + 46, y + 52), "✦", 34, color, True)
        text(d, (x + 38, y + 126), title_, 31, TEXT, True)
        paragraph(d, (x + 38, y + 174), detail, 22, 390)


def flow_scene(img, p, installer):
    d = ImageDraw.Draw(img)
    text(d, (110, 82), "一键部署，保留你的数据", 48, TEXT, True)
    text(d, (112, 144), "安装器只替换程序与运行时；好友、任务、登录状态不动", 24, MUTED)
    panel(d, (90, 205, 960, 900))
    rounded_image(img, installer, (115, 230, 935, 875), radius=16)
    steps = (("1", "检查环境", "磁盘、目录、运行时"), ("2", "安装", "程序 + 运行时，可取消"),
             ("3", "配置", "独立 Python 环境"), ("4", "启动", "自动打开 QQ 连接页"))
    for i, (num, title_, detail) in enumerate(steps):
        y = 245 + i * 150
        d.ellipse([1030, y, 1092, y + 62], fill=(*BLUE, 40), outline=(*BLUE, 150), width=2)
        text(d, (1061, y + 31), num, 25, (147, 197, 253), True, "mm")
        text(d, (1118, y - 2), title_, 30, TEXT, True)
        text(d, (1118, y + 38), detail, 22, MUTED)
    text(d, (110, 1002), "失败可重试；更新会保留 friends/、data/ 和登录状态", 25, (147, 197, 253))


def outro(img, p):
    d = ImageDraw.Draw(img)
    icon = Image.open(ROOT / "assets" / "app-icon.png").convert("RGBA").resize((180, 180), Image.LANCZOS)
    img.alpha_composite(icon, (W // 2 - 90, 270))
    text(d, (W // 2, 505), "Open QQ Dot", 82, TEXT, True, "ma")
    text(d, (W // 2, 610), "让 AI 住进你的桌面 QQ", 36, MUTED, anchor="ma")
    text(d, (W // 2, 720), "本地运行 · 数据留在自己电脑", 26, (147, 197, 253), anchor="ma")


def tutorial_header(img, step, title_, subtitle):
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([76, 58, 190, 116], radius=29, fill=(*BLUE, 40), outline=(*BLUE, 150), width=2)
    text(d, (133, 87), f"STEP {step}", 22, (191, 219, 254), True, "mm")
    text(d, (220, 62), title_, 42, TEXT, True)
    text(d, (222, 122), subtitle, 23, MUTED)


def tutorial_install(img, p):
    tutorial_header(img, 1, "安装：双击，下一步", "安装器会预检磁盘、运行时与依赖，并保留已有数据")
    panel(img, (100, 210, 1060, 900))
    rounded_image(img, INSTALLER, (125, 235, 1035, 875), radius=14)
    for i, (label, detail) in enumerate((("勾选需要的选项", "快捷方式 / 开机启动 / 文件关联"),
                                         ("确认安装目录", "已有数据会保留在原处"),
                                         ("等待四个阶段", "可随时取消，失败可重试"))):
        y = 280 + i * 190
        text(img and ImageDraw.Draw(img), (1120, y), label, 31, TEXT, True)
        paragraph(ImageDraw.Draw(img), (1122, y + 48), detail, 23, 650)


def tutorial_login(img, p, shot):
    tutorial_header(img, 2, "登录：三种方式，任选其一", "窗口内快速登录 / 扫码 / QQ 号 + 密码；二维码可刷新")
    panel(img, (100, 205, 1340, 900))
    rounded_image(img, shot, (125, 230, 1315, 875), radius=16)
    d = ImageDraw.Draw(img)
    for y, text_, color in ((320, "快速登录", MINT), (500, "扫码登录", BLUE), (680, "密码登录", ORANGE)):
        d.rounded_rectangle([1390, y, 1392, y + 120], radius=2, fill=color)
        text(d, (1415, y - 4), text_, 31, TEXT, True)
        text(d, (1415, y + 44), "登录成功后自动连接", 21, MUTED)
    text(d, (W // 2, 1000), "如果二维码过期，点「刷新二维码」或重新扫码", 25, (147, 197, 253), anchor="ma")


def tutorial_friend(img, p, shot):
    tutorial_header(img, 3, "好友：从 QQ 一键导入", "勾选当前账号好友，已有好友不会重复添加")
    panel(img, (100, 205, 1500, 900))
    rounded_image(img, shot, (125, 230, 1475, 875), radius=16)
    d = ImageDraw.Draw(img)
    text(d, (1540, 320), "登录后导入", 34, TEXT, True)
    paragraph(d, (1542, 375), "「从 QQ 导入好友」会读取当前账号好友列表；"
                                "勾选后逐个添加，完成即可聊天。", 23, 280)
    text(d, (1540, 560), "也可以手动输入 QQ 号", 24, MUTED)


def tutorial_mobile(img, p, status, console):
    tutorial_header(img, 4, "手机：扫码配对同一个好友", "手机应用直接打开好友小屋；配对一次即可")
    phone(img, status, (250, 215, 650, 940), "状态")
    phone(img, console, (760, 235, 1160, 960), "控制台")
    d = ImageDraw.Draw(img)
    text(d, (1260, 330), "配对方式", 34, TEXT, True)
    paragraph(d, (1262, 390), "电脑端打开好友「小屋状态」，用手机应用扫配对二维码；"
                                "也可以使用 friendhouse://pair 深链。", 24, 520)
    text(d, (1262, 610), "模型和数据仍在电脑", 26, (147, 197, 253), True)


def tutorial_identity(img, p, chat, model_shot):
    tutorial_header(img, 5, "聊天：好友身份 / 模型身份", "切换后输入框颜色立即变化；发出的气泡按身份分左右和颜色")
    phone(img, chat, (180, 215, 580, 940), "聊天：模型身份")
    panel(img, (690, 250, 1480, 860))
    rounded_image(img, model_shot, (715, 275, 1455, 835), radius=14)
    d = ImageDraw.Draw(img)
    text(d, (1530, 300), "切换即生效", 34, TEXT, True)
    paragraph(d, (1532, 355), "好友身份：直接以当前 QQ 账号发送，右侧蓝色气泡。", 23, 310)
    paragraph(d, (1532, 485), "模型身份：由当前模型生成回复后发出，左侧绿色气泡。", 23, 310)
    text(d, (1532, 680), "两个身份同步电脑端配置", 22, (147, 197, 253))


def tutorial_features(img, p):
    tutorial_header(img, 5, "进阶：迁移、胶囊、回溯、备份", "都在桌面端完成，文件可拖进来")
    cards = (
        ("迁移助手", "ChatGPT / Claude / DeepSeek 导出 → 项目和会话", BLUE),
        ("任务胶囊", ".oqtc 导入后作为工具运行，支持学习复用", MINT),
        ("回溯中心", "任务文件重做、迁移/胶囊撤销、记忆快照", ORANGE),
        ("备份恢复", ".oqdbackup 一键搬家，旧数据不会直接删除", (34, 211, 238)),
    )
    for i, (title_, detail, color) in enumerate(cards):
        x, y = 150 + (i % 2) * 850, 270 + (i // 2) * 320
        panel(img, (x, y, x + 770, y + 250))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([x + 36, y + 40, x + 100, y + 104], radius=18, fill=(*color, 45), outline=(*color, 160), width=2)
        text(d, (x + 44, y + 50), "✦", 34, color, True)
        text(d, (x + 36, y + 130), title_, 32, TEXT, True)
        paragraph(d, (x + 36, y + 180), detail, 22, 690)


def tutorial_finish(img, p):
    d = ImageDraw.Draw(img)
    text(d, (W // 2, 300), "现在，去连接你的 QQ", 66, TEXT, True, "ma")
    text(d, (W // 2, 410), "安装 → 登录 → 导入好友 → 开始使用", 34, MUTED, anchor="ma")
    for i, line in enumerate(("桌面直连，不再打开网页", "登录数据独立保存", "手机端只作查看与控制")):
        text(d, (W // 2, 540 + i * 64), "✓  " + line, 28, (147, 197, 253), anchor="ma")
    icon = Image.open(ROOT / "assets" / "app-icon.png").convert("RGBA").resize((130, 130), Image.LANCZOS)
    img.alpha_composite(icon, (W // 2 - 65, 760))


def make_audio(path, duration, promo=True):
    sr = 44100
    n = int(sr * duration)
    t = np.arange(n) / sr
    rng = np.random.default_rng(7)
    audio = np.zeros(n, dtype=np.float32)
    if promo:
        chords = [(130.8, 164.8, 196.0), (110.0, 130.8, 164.8), (87.3, 110.0, 130.8), (98.0, 123.5, 146.8)]
        bpm = 92
    else:
        chords = [(110.0, 130.8, 164.8), (87.3, 110.0, 130.8), (98.0, 123.5, 146.8), (82.4, 98.0, 123.5)]
        bpm = 74
    bar = 60 / bpm * 4
    for start in np.arange(0, duration, bar):
        chord = chords[int(start / bar) % len(chords)]
        for f in chord:
            env = np.clip((t - start) / 2.5, 0, 1) * np.clip((start + bar + 2 - t) / 2.5, 0, 1)
            audio += 0.045 * np.sin(2 * np.pi * f * t) * env
            audio += 0.018 * np.sin(2 * np.pi * f * 2.003 * t) * env
    # soft arpeggio
    step = 60 / bpm / 2
    for k, start in enumerate(np.arange(0, duration, step)):
        f = chords[int(start / bar) % len(chords)][k % 3] * 4
        env = np.exp(-(t - start).clip(0) * 3.2) * ((t >= start) & (t < start + step * 1.8))
        audio += 0.035 * np.sin(2 * np.pi * f * t) * env
    # gentle noise wash and fades
    audio += 0.003 * rng.standard_normal(n)
    fade = np.minimum(1, np.minimum(t / 3, (duration - t) / 4))
    audio *= np.clip(fade, 0, 1) * 0.8
    stereo = np.stack([audio, np.roll(audio, 220)], axis=1)
    pcm = (np.clip(stereo, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())


def render(name, duration, scenes):
    audio = OUT / f"{name}.wav"
    video = OUT / f"{name}.mp4"
    make_audio(audio, duration, promo=(name == "promo"))
    command = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(audio),
               "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(video)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    total = int(duration * FPS)
    for index in range(total):
        t = index / FPS
        frame = BASE.copy()
        for start, end, fn in scenes:
            if start <= t < end:
                local = (t - start) / max(0.001, end - start)
                fn(frame, local)
                # soft scene fade
                edge = min(t - start, end - t)
                if edge < 0.45:
                    overlay = Image.new("RGBA", (W, H), (5, 9, 16, int(255 * (1 - edge / 0.45))))
                    frame.alpha_composite(overlay)
                break
        # progress bar + watermark
        d = ImageDraw.Draw(frame)
        d.rectangle([0, H - 6, int(W * t / duration), H], fill=(*BLUE, 220))
        text(d, (86, H - 54), "OPEN QQ DOT", 18, (90, 115, 145), True)
        process.stdin.write(frame.convert("RGB").tobytes())
    process.stdin.close()
    process.wait()
    if process.returncode:
        raise SystemExit("ffmpeg failed")
    return video


def main():
    desktop = Image.open(MEDIA / "desktop-dotlink.png").convert("RGB")
    installer = Image.open(MEDIA / "installer-light.png").convert("RGB")
    status = Image.open(MEDIA / "mobile-status-new.png").convert("RGB")
    console = Image.open(MEDIA / "mobile-console-final.png").convert("RGB")
    chat = Image.open(MEDIA / "mobile-chat-app.png").convert("RGB")
    chat_view = Image.open(MEDIA / "chat-model-mode2.png").convert("RGB")
    model_view = Image.open(MEDIA / "model-page2.png").convert("RGB")
    promo = [
        (0, 7, title_scene),
        (7, 17, concept_scene),
        (17, 36, lambda img, p: desktop_scene(img, p, desktop)),
        (36, 52, lambda img, p: phone_scene(img, p, status, chat)),
        (52, 66, feature_scene),
        (66, 77, lambda img, p: flow_scene(img, p, installer)),
        (77, 84, outro),
    ]
    tutorial = [
        (0, 7, lambda img, p: (text(ImageDraw.Draw(img), (W // 2, 360), "Open QQ Dot 使用指南", 72, TEXT, True, "ma"),
                               text(ImageDraw.Draw(img), (W // 2, 470), "从安装到第一条消息", 36, MUTED, anchor="ma"))),
        (7, 28, tutorial_install),
        (28, 50, lambda img, p: tutorial_login(img, p, desktop)),
        (50, 68, lambda img, p: tutorial_friend(img, p, desktop)),
        (68, 84, lambda img, p: tutorial_mobile(img, p, status, console)),
        (84, 100, lambda img, p: tutorial_identity(img, p, chat_view, model_view)),
        (100, 120, tutorial_features),
        (120, 130, tutorial_finish),
    ]
    # tutorial_install needs globals
    globals()["INSTALLER"] = installer
    globals()["DESKTOP"] = desktop
    only = __import__("os").environ.get("VIDEO_ONLY", "")
    if only != "tutorial":
        print("promo", render("OpenQQDot-Promo", 84, promo))
    if only != "promo":
        print("tutorial", render("OpenQQDot-Tutorial", 130, tutorial))


if __name__ == "__main__":
    main()





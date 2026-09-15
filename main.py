"""Ebook 工作台 —— tkinter 界面。

三个模块共用一个窗口、一条日志和一个后台线程：
  1. 小说跨文化改编：中文小说 → 英文小说 + Amazon KDP 全套出版物料
  2. KDP 上架：物料预检 → 字段复制 → Chrome 自动填表
  3. 助眠视频生成：txt 文案 → 慢速朗读 → 背景视频循环 → BGM → ffmpeg 成片 → 上传 YouTube
运行：python main.py
"""
import queue
import re
import subprocess
import threading
import time
import traceback
import webbrowser
from dataclasses import fields
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import notify
import novel_adapter
import kdp_uploader
import pipeline
import uploader
from config import Settings, VOICES, APP_DIR
from ffmpeg_utils import Cancelled
from tts import synth_segment

PREVIEW_TEXT = "闭上眼睛，把今天的一切都放下，让呼吸慢慢沉下去。"

# 自动识别出来的设定字段 → 界面输入框
DETECT_TO_VAR = {
    "orig_era_region": "novel_orig_era",
    "target_country": "novel_target_country",
    "target_era": "novel_target_era",
    "genre": "novel_genre",
    "target_audience": "novel_target_audience",
    "pen_name": "novel_author",     # 界面上手填了作者时，引擎不会回传这一项
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Ebook 工作台")
        # 日志挪到右栏之后要更宽才放得下两列；高度按屏幕可用空间来，别被 Dock 挤没
        w = min(1400, self.winfo_screenwidth() - 80)
        self.geometry(f"{w}x{min(1000, self.winfo_screenheight() - 110)}")
        self.minsize(1080, 620)

        self.settings = Settings.load()
        self.vars = {}
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.last_video = None
        self.kdp_meta = None
        self.t_start = 0.0       # 本次任务开始时间，用来算已用/剩余
        self.stage = ""          # 当前阶段文字，后台线程通过队列更新
        self.frac = 0.0
        self.ch_base = None      # (起算章号, 时刻)，用来估每章耗时
        self.ch_now = None       # (当前章号, 总章数)
        self.actions = []        # 所有会启动后台任务的按钮，忙的时候统一灰掉

        Path(self.settings.output_dir).mkdir(parents=True, exist_ok=True)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain)

    # ---------- 界面骨架 ----------
    def _build(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # 左右两栏用可拖的分隔条：左边页签，右边日志。
        # 原来是上下排的，页签一高就把下面的进度条和日志挤没了；改成左右之后
        # 日志有完整的一列高度，页签再高也不影响。嫌哪边窄就拖分隔条。
        split = ttk.PanedWindow(root, orient="horizontal")
        split.pack(fill="both", expand=True)

        nb = ttk.Notebook(split)

        tab_novel = ttk.Frame(nb, padding=8)
        tab_batch = ttk.Frame(nb, padding=8)
        tab_kdp = ttk.Frame(nb, padding=8)
        tab_video = ttk.Frame(nb, padding=8)
        nb.add(tab_novel, text="  小说改编  ")
        nb.add(tab_batch, text="  批量队列  ")
        nb.add(tab_kdp, text="  KDP 上架  ")
        nb.add(tab_video, text="  助眠视频  ")

        self._build_novel(tab_novel)
        self._build_batch(tab_batch)
        self._build_kdp(tab_kdp)
        self._build_video(tab_video)

        # 右栏：三个页签共用的进度条 + 日志 + 停止
        side = ttk.Frame(split, padding=(8, 0, 0, 0))
        split.add(nb, weight=3)
        split.add(side, weight=2)
        self.nb, self.split = nb, split
        nb.bind("<<NotebookTabChanged>>", lambda e: self.after(20, self._fit_sash))
        self.after(80, self._fit_sash)

        bar = ttk.Frame(side)
        bar.pack(fill="x")
        self.btn_stop = ttk.Button(bar, text="停止", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left")
        ttk.Button(bar, text="打开输出目录",
                   command=self.on_open_output).pack(side="right")

        # 状态文字单独一行：右栏比原来的整行窄，和按钮挤一行会被截断
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(side, textvariable=self.status_var,
                  anchor="w").pack(fill="x", pady=(4, 0))

        self.progress = ttk.Progressbar(side, maximum=1.0)
        self.progress.pack(fill="x", pady=(4, 6))

        log_wrap = ttk.Frame(side)
        log_wrap.pack(fill="both", expand=True)
        # width/height 只是最小请求值，实际尺寸由分隔条决定
        self.log_box = tk.Text(log_wrap, width=44, height=6, wrap="word", state="disabled")
        self.log_box.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(log_wrap, command=self.log_box.yview)
        sb.pack(side="right", fill="y")
        self.log_box.configure(yscrollcommand=sb.set)

        self._sync_engine()
        self._sync_llm()

    # ---------- 页签三：助眠视频 ----------
    def _build_video(self, root):
        pad = dict(padx=6, pady=2)

        # 路径
        paths = ttk.LabelFrame(root, text="素材与输出", padding=6)
        paths.pack(fill="x")
        paths.columnconfigure(1, weight=1)
        self._path_row(paths, 0, "文案 txt", "script_path", "file")
        self._path_row(paths, 1, "背景视频", "video_path", "video")
        self._path_row(paths, 2, "BGM 音频", "bgm_path", "audio")
        self._path_row(paths, 3, "输出目录", "output_dir", "dir")

        mid = ttk.Frame(root)
        mid.pack(fill="x", pady=(8, 0))
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)

        # 语音
        voice = ttk.LabelFrame(mid, text="语音（慢速朗读）", padding=6)
        voice.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        voice.columnconfigure(1, weight=1)

        self.vars["engine"] = tk.StringVar(value=self.settings.engine)
        eng = ttk.Frame(voice)
        eng.grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(eng, text="引擎").pack(side="left", padx=(0, 10))
        for label, val in (("内置音色", "edge"), ("克隆声音", "f5")):
            ttk.Radiobutton(eng, text=label, value=val, variable=self.vars["engine"],
                            command=self._sync_engine).pack(side="left", padx=(0, 12))

        self.vars["voice"] = tk.StringVar(value=self.settings.voice)
        ttk.Label(voice, text="音色").grid(row=1, column=0, sticky="w", **pad)
        self.voice_box = ttk.Combobox(voice, textvariable=self.vars["voice"], values=VOICES)
        self.voice_box.grid(row=1, column=1, sticky="ew", **pad)
        self._spin(voice, 2, "语速 %（负=慢）", "rate", -50, 20, 5)
        self.pitch_spin = self._spin(voice, 3, "音调 Hz（负=低）", "pitch", -20, 10, 1)
        self._spin(voice, 4, "段落停顿 秒", "pause", 0, 10, 0.5)
        self._spin(voice, 5, "开头留白 秒", "lead_in", 0, 30, 1)
        self._spin(voice, 6, "结尾留白 秒", "tail", 0, 60, 1)

        # 画面 + 配乐
        right = ttk.Frame(mid)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        right.columnconfigure(0, weight=1)

        pic = ttk.LabelFrame(right, text="画面（背景视频循环播放）", padding=6)
        pic.pack(fill="x")
        pic.columnconfigure(1, weight=1)
        self._spin(pic, 0, "接缝淡化 秒", "xfade", 0, 10, 0.5)
        self._spin(pic, 1, "帧率（0=跟随源片）", "fps", 0, 60, 1)
        ttk.Label(pic, text="（改帧率会让平移镜头发顿，建议留 0）",
                  foreground="#777").grid(row=2, column=0, columnspan=2,
                                          sticky="w", padx=6)

        music = ttk.LabelFrame(right, text="配乐", padding=6)
        music.pack(fill="x", pady=(8, 0))
        music.columnconfigure(1, weight=1)
        self._spin(music, 0, "BGM 音量 0-1", "bgm_volume", 0, 1, 0.02)
        self._spin(music, 1, "淡入淡出 秒", "bgm_fade", 0, 30, 1)

        # 声音克隆
        self.clone_frame = ttk.LabelFrame(
            root, text="声音克隆（引擎选「克隆声音」时生效；参考文本留空会自动转写）",
            padding=6)
        self.clone_frame.pack(fill="x", pady=(6, 0))
        self.clone_frame.columnconfigure(1, weight=1)
        self._path_row(self.clone_frame, 0, "参考音频", "ref_audio", "audio")
        self.vars["ref_text"] = tk.StringVar(value=self.settings.ref_text)
        ttk.Label(self.clone_frame, text="参考文本").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(self.clone_frame, textvariable=self.vars["ref_text"]).grid(
            row=1, column=1, sticky="ew", **pad)
        start = ttk.Frame(self.clone_frame)
        start.grid(row=1, column=2, sticky="w", **pad)
        ttk.Label(start, text="起点秒").pack(side="left", padx=(0, 4))
        self.vars["ref_start"] = tk.DoubleVar(value=self.settings.ref_start)
        ttk.Spinbox(start, from_=0, to=600, increment=1, width=6,
                    textvariable=self.vars["ref_start"]).pack(side="left")

        self.vars["slow_mode"] = tk.StringVar(value=self.settings.slow_mode)
        slow = ttk.Frame(self.clone_frame)
        slow.grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(slow, text="慢放方式").pack(side="left", padx=(0, 10))
        for label, val in (("引擎生成（更自然）", "engine"),
                           ("后期拉伸（更可控）", "atempo")):
            ttk.Radiobutton(slow, text=label, value=val,
                            variable=self.vars["slow_mode"]).pack(side="left", padx=(0, 12))

        # 标题 / 简介
        meta = ttk.LabelFrame(root, text="视频信息", padding=6)
        meta.pack(fill="x", pady=(8, 0))
        meta.columnconfigure(1, weight=1)
        self.vars["title"] = tk.StringVar(value=self.settings.title)
        ttk.Label(meta, text="标题").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(meta, textvariable=self.vars["title"]).grid(
            row=0, column=1, sticky="ew", **pad)
        ttk.Label(meta, text="简介").grid(row=1, column=0, sticky="nw", **pad)
        self.desc = tk.Text(meta, height=2, wrap="word")
        self.desc.grid(row=1, column=1, sticky="ew", **pad)

        # 按钮
        bar = ttk.Frame(root)
        bar.pack(fill="x", pady=(10, 0))
        self._action(bar, "试听语音", self.on_preview)
        self._action(bar, "生成音频", self.on_generate_audio)
        self._action(bar, "生成视频", self.on_generate)
        self._action(bar, "上传 YouTube", self.on_upload)

    # ---------- 页签一：小说改编 ----------
    def _build_novel(self, root):
        pad = dict(padx=6, pady=3)

        src = ttk.LabelFrame(root, text="源文件与输出", padding=6)
        src.pack(fill="x")
        src.columnconfigure(1, weight=1)
        self._path_row(src, 0, "中文小说 txt（单本模式）", "novel_source_file", "file")
        self._path_row(src, 1, "输出目录（成品放在 输出目录/书名/ 下，共 8 个交付文件）",
                       "output_dir", "dir")

        mid = ttk.Frame(root)
        mid.pack(fill="x", pady=(8, 0))
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)

        # 改编设定
        setting = ttk.LabelFrame(mid, text="改编设定（可自动识别）", padding=6)
        setting.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        setting.columnconfigure(1, weight=1)
        self._entry(setting, 0, "原作时代地区", "novel_orig_era")
        self._entry(setting, 1, "目标国家", "novel_target_country")
        self._entry(setting, 2, "目标年代", "novel_target_era")
        self._entry(setting, 3, "小说类型", "novel_genre")
        self._entry(setting, 4, "目标读者", "novel_target_audience")

        self.vars["novel_auto_detect"] = tk.BooleanVar(
            value=self.settings.novel_auto_detect)
        ttk.Checkbutton(setting, text="开始改编时自动识别（会覆盖上面 5 项）",
                        variable=self.vars["novel_auto_detect"]).grid(
                            row=5, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(setting, text="（按 Amazon 英文市场销量挑目标设定，理由写在日志里）",
                  foreground="#777").grid(row=6, column=0, columnspan=2,
                                          sticky="w", padx=6)

        # 书籍信息
        book = ttk.LabelFrame(mid, text="书籍信息", padding=6)
        book.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        book.columnconfigure(1, weight=1)
        self._entry(book, 0, "作者 / 笔名（留空自动取）", "novel_author")
        self._entry(book, 1, "英文书名", "novel_title")
        ttk.Label(book, text="（留空则由模型拟 3 个书名并自动选用推荐项）",
                  foreground="#777").grid(row=2, column=0, columnspan=2,
                                          sticky="w", padx=6)
        self._entry(book, 3, "副标题", "novel_subtitle")
        self._entry(book, 4, "系列名与卷号", "novel_series")

        # 大模型
        llm = ttk.LabelFrame(root, text="大模型（改编正文和文案全靠它）", padding=6)
        llm.pack(fill="x", pady=(8, 0))
        llm.columnconfigure(1, weight=1)

        self.vars["llm_provider"] = tk.StringVar(value=self.settings.llm_provider)
        prov = ttk.Frame(llm)
        prov.grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(prov, text="接入方式").pack(side="left", padx=(0, 10))
        for label, val in (("API Key（OpenAI / DeepSeek 兼容，按量计费）", "api"),
                           ("本机 CLI（走已登录的订阅额度）", "cli")):
            ttk.Radiobutton(prov, text=label, value=val,
                            variable=self.vars["llm_provider"],
                            command=self._sync_llm).pack(side="left", padx=(0, 12))

        # 命令和模型并排放一行，六行压成四行，把高度让给日志区
        self._pair_row(llm, 1, "① 主 CLI", "llm_cli_command", "模型", "llm_cli_model")
        self._entry(llm, 2, "　 参数格式", "llm_cli_args")
        self._pair_row(llm, 3, "② 备用 CLI", "llm_fallback_cli", "模型", "llm_fallback_cli_model")
        self._entry(llm, 4, "　 参数格式", "llm_fallback_cli_args")

        self.vars["llm_api_key"] = tk.StringVar(value=self.settings.llm_api_key)
        self.api_key_label = ttk.Label(llm, text="API Key")
        self.api_key_label.grid(row=5, column=0, sticky="w", **pad)
        ttk.Entry(llm, textvariable=self.vars["llm_api_key"], show="•").grid(
            row=5, column=1, sticky="ew", **pad)
        self._pair_row(llm, 6, "　 Base URL", "llm_base_url", "模型名", "llm_model")

        # 章节并发。每章调用互相独立，并发只影响速度不影响质量；
        # 但并发过高会撞限流，重试退避反而更慢，所以放在界面上方便按实测调。
        self._spin(llm, 7, "章节并发数", "chapter_workers", 1, 64, 1)
        ttk.Label(llm, text="（每章只依赖「全书总结 + 本章原文」，并发不影响质量。"
                            "日志里频繁出现「第 N 次失败…秒后重试」就说明撞限流了，调小。"
                            "跑 test_concurrency.py 可实测最优值）",
                  foreground="#777", wraplength=520, justify="left").grid(
                      row=8, column=0, columnspan=2, sticky="w", padx=6)

        # CLI 四行按接入方式收放；API 那两行两种模式都留着——它是级联的最后一级
        self.llm_frame, self.cli_rows = llm, (1, 2, 3, 4)
        ttk.Label(llm, text="（CLI 走本机已登录账号的订阅额度，不花钱；一级连续失败 3 次才降到下一级。\n"
                            "参数格式里 {model} {prompt} 会替换成实际值 —— 各家 CLI 参数不同，"
                            "codex 的 -p 是 --profile 不是 --print）",
                  foreground="#777").grid(row=5, column=0, columnspan=2,
                                          sticky="w", padx=6)

        self.vars["use_mock_adaptation"] = tk.BooleanVar(
            value=self.settings.use_mock_adaptation)
        ttk.Checkbutton(prov, text="离线演示（不调模型，只看排版）",
                        variable=self.vars["use_mock_adaptation"]).pack(side="left")

        bar = ttk.Frame(root)
        bar.pack(fill="x", pady=(10, 0))
        self._action(bar, "测试模型连通", self.on_llm_test)
        self._action(bar, "智能识别设定", self.on_detect_settings)
        self._action(bar, "开始改编", self.on_adapt)
        self._action(bar, "开始改编 + 自动发布",
                     lambda: self.on_adapt(auto_publish=True))
        ttk.Button(bar, text="打开项目目录",
                   command=self.on_open_project).pack(side="left", padx=6)

    # ---------- 页签二：KDP 上架 ----------
    def _build_kdp(self, root):
        pad = dict(padx=6, pady=3)

        proj = ttk.LabelFrame(root, text="待上架的项目目录", padding=6)
        proj.pack(fill="x")
        proj.columnconfigure(1, weight=1)
        self._path_row(proj, 0, "项目目录", "kdp_project_dir", "dir")
        ttk.Label(proj, text="（选改编产出的那个书名目录，里面应有 01_English_Manuscript.docx 等文件；"
                             "改编跑完会自动填在这里）",
                  foreground="#777").grid(row=1, column=0, columnspan=3,
                                          sticky="w", padx=6)

        listing = ttk.LabelFrame(root, text="上架参数", padding=6)
        listing.pack(fill="x", pady=(8, 0))
        listing.columnconfigure(1, weight=1)

        self.vars["kdp_price"] = tk.DoubleVar(value=self.settings.kdp_price)
        ttk.Label(listing, text="定价 USD").grid(row=0, column=0, sticky="w", **pad)
        ttk.Spinbox(listing, from_=0.99, to=200, increment=0.5, width=10,
                    textvariable=self.vars["kdp_price"]).grid(
                        row=0, column=1, sticky="w", **pad)

        self.vars["kdp_royalty"] = tk.StringVar(value=self.settings.kdp_royalty)
        ttk.Label(listing, text="版税方案").grid(row=1, column=0, sticky="w", **pad)
        ttk.Combobox(listing, textvariable=self.vars["kdp_royalty"],
                     values=["35%", "70%"], width=8, state="readonly").grid(
                         row=1, column=1, sticky="w", **pad)

        self.vars["kdp_marketplace"] = tk.StringVar(value=self.settings.kdp_marketplace)
        ttk.Label(listing, text="主市场").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(listing, textvariable=self.vars["kdp_marketplace"],
                     values=["amazon.com", "amazon.co.uk", "amazon.de",
                             "amazon.ca", "amazon.com.au"], width=16).grid(
                                 row=2, column=1, sticky="w", **pad)

        self._path_row(listing, 3, "Chrome 用户目录", "kdp_chrome_profile", "dir")
        ttk.Label(listing, text="（留空会用 ~/.kdp_chrome_profile；第一次手动登录 + 2FA，之后自动复用）",
                  foreground="#777").grid(row=4, column=0, columnspan=3,
                                          sticky="w", padx=6)

        bar1 = ttk.Frame(root)
        bar1.pack(fill="x", pady=(10, 0))
        ttk.Button(bar1, text="读取并预检", command=self.on_kdp_check).pack(side="left")
        ttk.Button(bar1, text="复制简介 HTML",
                   command=lambda: self._copy_meta("description_html", "简介 HTML")
                   ).pack(side="left", padx=6)
        ttk.Button(bar1, text="复制 7 个关键词",
                   command=lambda: self._copy_meta("keywords_7", "7 个搜索关键词")
                   ).pack(side="left")
        ttk.Button(bar1, text="打开 KDP 书架",
                   command=lambda: webbrowser.open("https://kdp.amazon.com/en_US/bookshelf")
                   ).pack(side="left", padx=6)

        bar2 = ttk.Frame(root)
        bar2.pack(fill="x", pady=(6, 0))
        for key, text in (("kdp_auto_upload", "改编跑完自动建 KDP 草稿"),
                          ("kdp_auto_publish", "并自动点 Publish（不可逆，慎开）")):
            self.vars[key] = tk.BooleanVar(value=getattr(self.settings, key))
            ttk.Checkbutton(listing, text=text, variable=self.vars[key]).grid(
                row=5 if key == "kdp_auto_upload" else 6,
                column=0, columnspan=3, sticky="w", **pad)

        self._action(bar2, "自动建 KDP 草稿", self.on_kdp_upload)
        ttk.Label(bar2, text="三步全自动：字段+分类 → 上传 EPUB 和封面 → AI 申报+定价；"
                             "停在 Publish 前，发布那一下要你自己按。",
                  foreground="#777").pack(side="left", padx=8)

    LOG_MIN_W = 340      # 右栏日志至少留这么宽，再窄就没法看了

    def _fit_sash(self):
        """三个页签宽窄不一，切过去时把分隔条挪到刚好放得下的位置，别把控件裁掉。"""
        total = self.split.winfo_width()
        if total < 400:
            return
        tab = self.nb.nametowidget(self.nb.select())
        # 页签边框吃掉的宽度得实测，写死容易把右边一列按钮切掉
        chrome = max(self.nb.winfo_width() - tab.winfo_width(), 16)
        want = tab.winfo_reqwidth() + chrome
        # 上限让日志保住 LOG_MIN_W；下限别让页签窄到换行
        self.split.sashpos(0, max(min(want, total - self.LOG_MIN_W), int(total * 0.45)))

    # ---------- 小控件 ----------
    def _action(self, parent, text, command):
        """会启动后台任务的按钮，登记进 self.actions 以便忙时统一禁用。"""
        btn = ttk.Button(parent, text=text, command=command)
        btn.pack(side="left", padx=(0, 6))
        self.actions.append(btn)
        return btn

    def _build_batch(self, root):
        """Drive 批量队列：文件夹里每个 txt 一本书，一本一本串行跑。

        列表只读，重活在 batch.py 里，CLI 也调同一套 —— 长跑建议走
        `cli.py batch --bg`，远程桌面断开也不影响。
        """
        pad = dict(padx=6, pady=3)
        ttk.Label(root, foreground="#777", justify="left",
                  text="Drive 文件夹里每个 txt 是一本待改编的书，一本一本串行跑。\n"
                       "中断后重进会跳过已完成的；单本跑一半中断，会从缺口那章接着补。\n"
                       "长跑建议用 cli.py batch --bg，远程桌面断开也不受影响。"
                  ).pack(anchor="w", pady=(0, 6))

        box = ttk.LabelFrame(root, text="Google Drive 批量队列", padding=6)
        box.pack(fill="both", expand=True)
        box.columnconfigure(1, weight=1)
        box.rowconfigure(2, weight=1)

        ttk.Label(box, text="Drive 文件夹").grid(row=0, column=0, sticky="w", **pad)
        self.vars["gdrive_folder"] = tk.StringVar(value=self.settings.gdrive_folder)
        ttk.Entry(box, textvariable=self.vars["gdrive_folder"]).grid(
            row=0, column=1, sticky="ew", **pad)
        ttk.Button(box, text="刷新列表", command=self.on_batch_refresh).grid(
            row=0, column=2, **pad)

        bar = ttk.Frame(box)
        bar.grid(row=1, column=0, columnspan=3, sticky="ew", padx=6)
        ttk.Label(bar, text="每本间隔（分钟）").pack(side="left")
        self.vars["batch_gap_minutes"] = tk.StringVar(
            value=str(self.settings.batch_gap_minutes))
        ttk.Entry(bar, textvariable=self.vars["batch_gap_minutes"], width=7).pack(
            side="left", padx=(4, 10))
        ttk.Label(bar, text="（0=改完立刻跑下一本。一本书本身要几小时，通常不需要额外间隔）",
                  foreground="#777").pack(side="left")

        cols = ("status", "folder", "name", "size", "note")
        self.batch_tree = ttk.Treeview(box, columns=cols, show="headings", height=8)
        for c, txt, w in (("status", "状态", 62), ("folder", "题材", 66),
                          ("name", "文件名", 230), ("size", "大小", 74),
                          ("note", "备注", 300)):
            self.batch_tree.heading(c, text=txt)
            self.batch_tree.column(c, width=w, anchor="w")
        self.batch_tree.grid(row=2, column=0, columnspan=3, sticky="nsew", padx=6, pady=4)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.batch_tree.yview)
        sb.grid(row=2, column=3, sticky="ns")
        self.batch_tree.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(box)
        btns.grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 4))
        ttk.Button(btns, text="开始批量", command=self.on_batch_start).pack(side="left")
        ttk.Button(btns, text="失败的重试", command=self.on_batch_retry).pack(
            side="left", padx=6)

    def _batch_folder(self):
        f = self.vars["gdrive_folder"].get().strip()
        if not f:
            messagebox.showinfo("提示", "先填 Drive 文件夹名（或文件夹 ID）。")
        return f

    def on_batch_refresh(self):
        folder = self._batch_folder()
        if not folder:
            return

        def job():
            import batch
            # 密钥路径一般不用填：gdrive.py 会在项目根目录自动认 service_account 的 json
            jobs = batch.list_jobs(folder, sa_path=getattr(self.settings, "gdrive_sa_json", ""))
            self.q.put(("batch_jobs", jobs))
            self.log(f"队列已刷新：{len(jobs)} 本")

        self._start(job)

    def _render_jobs(self, jobs):
        self.batch_tree.delete(*self.batch_tree.get_children())
        for j in jobs:
            note = j.project_dir or ""
            if j.error:
                note = f"失败：{j.error[:70]}"
            self.batch_tree.insert("", "end", values=(
                j.status, j.folder, j.name, f"{j.size_mb:.1f} MB", note))

    def on_batch_start(self):
        folder = self._batch_folder()
        if not folder:
            return
        try:
            gap = float(self.vars["batch_gap_minutes"].get() or 0)
        except ValueError:
            messagebox.showerror("参数错误", "「每本间隔」得是个数字。")
            return
        if not messagebox.askyesno(
                "确认", f"将把 Drive 文件夹「{folder}」里的 txt 一本一本改编。\n\n"
                        f"每本要跑几百章，耗时以小时计；中断后重进会接着跑。\n"
                        f"每本间隔 {gap:g} 分钟。\n\n"
                        f"提示：长跑更建议用 `cli.py batch --bg`，\n"
                        f"远程桌面断开也不受影响。确认继续？"):
            return

        s = self._collect()      # 用界面上的最新设置，不是启动时那份

        def job():
            import batch
            batch.run_batch(folder, s, log=self.log, cancel=self.cancel,
                            sa_path=getattr(s, "gdrive_sa_json", ""),
                            gap_minutes=gap)
            self.q.put(("batch_jobs", batch.list_jobs(folder,
                                                      sa_path=getattr(s, "gdrive_sa_json", ""))))

        self._start(job)

    def on_batch_retry(self):
        import batch
        st = batch.load_state()
        hit = [k for k, v in st.items() if v.get("status") == batch.FAILED]
        for k in hit:
            batch.reset(k)
        self.log(f"已把 {len(hit)} 本失败的打回待处理。")
        self.on_batch_refresh()

    def _path_row(self, parent, row, label, key, kind):
        pad = dict(padx=6, pady=2)
        # 同一个 key 可能在多个页签出现（比如输出目录），共用一个变量保持同步
        if key not in self.vars:
            self.vars[key] = tk.StringVar(value=getattr(self.settings, key))
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(parent, textvariable=self.vars[key]).grid(
            row=row, column=1, sticky="ew", **pad)
        ttk.Button(parent, text="浏览…",
                   command=lambda: self._browse(key, kind)).grid(row=row, column=2, **pad)

    def _entry(self, parent, row, label, key):
        pad = dict(padx=6, pady=2)
        self.vars[key] = tk.StringVar(value=getattr(self.settings, key))
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **pad)
        entry = ttk.Entry(parent, textvariable=self.vars[key])
        entry.grid(row=row, column=1, sticky="ew", **pad)
        return entry

    def _pair_row(self, parent, row, label, key, label2, key2):
        """一行放两个输入框：左边宽（命令/URL），右边窄（模型名）。"""
        pad = dict(padx=6, pady=2)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **pad)
        box = ttk.Frame(parent)
        box.grid(row=row, column=1, sticky="ew", **pad)
        box.columnconfigure(0, weight=3)
        box.columnconfigure(2, weight=2)
        for col, (lbl, k) in enumerate(((None, key), (label2, key2))):
            self.vars[k] = tk.StringVar(value=getattr(self.settings, k))
            if lbl:
                ttk.Label(box, text=lbl).grid(row=0, column=1, padx=(10, 4))
            ttk.Entry(box, textvariable=self.vars[k]).grid(row=0, column=col * 2, sticky="ew")

    def _spin(self, parent, row, label, key, lo, hi, step):
        pad = dict(padx=6, pady=2)
        cur = getattr(self.settings, key)
        var = tk.DoubleVar(value=cur) if isinstance(cur, float) else tk.IntVar(value=cur)
        self.vars[key] = var
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **pad)
        spin = ttk.Spinbox(parent, from_=lo, to=hi, increment=step, textvariable=var,
                           width=10)
        spin.grid(row=row, column=1, sticky="w", **pad)
        return spin

    def _browse(self, key, kind):
        cur = self.vars[key].get()
        if kind == "file":
            p = filedialog.askopenfilename(title="选择 txt 文件",
                                           filetypes=[("文本", "*.txt"), ("全部", "*.*")])
        elif kind == "video":
            p = filedialog.askopenfilename(
                title="选择背景视频",
                filetypes=[("视频", "*.mp4 *.mov *.m4v *.mkv *.webm"), ("全部", "*.*")])
        elif kind == "audio":
            p = filedialog.askopenfilename(
                title="选择参考音频",
                filetypes=[("音频", "*.wav *.mp3 *.m4a *.flac *.ogg"), ("全部", "*.*")])
        else:
            p = filedialog.askdirectory(title="选择目录",
                                        initialdir=cur if Path(cur).is_dir() else str(APP_DIR))
        if p:
            self.vars[key].set(p)

    def _sync_engine(self):
        """内置音色和克隆声音用的是不同的参数，把用不上的那些灰掉。"""
        cloning = self.vars["engine"].get() == "f5"
        self.voice_box.configure(state="disabled" if cloning else "normal")
        self.pitch_spin.configure(state="disabled" if cloning else "normal")

        def walk(parent):
            for child in parent.winfo_children():
                if isinstance(child, (ttk.Entry, ttk.Button, ttk.Spinbox,
                                      ttk.Radiobutton)):
                    child.configure(state="normal" if cloning else "disabled")
                walk(child)      # 起点秒那组控件嵌在子 Frame 里

        walk(self.clone_frame)

    def _sync_llm(self):
        """选 API 时把 CLI 那几行收起来；选 CLI 时全都留着——API 是级联的最后一级。"""
        cli = self.vars["llm_provider"].get() == "cli"
        for r in self.cli_rows:
            for w in self.llm_frame.grid_slaves(row=r):
                w.grid() if cli else w.grid_remove()
        self.api_key_label.configure(text="③ API Key（前两级都挂了才用）" if cli else "API Key")

    # ---------- 与后台线程通信 ----------
    def log(self, msg):
        self.q.put(("log", str(msg)))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", payload + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "progress":
                    self.progress["value"] = payload
                    self.frac = payload
                elif kind == "stage":
                    self.stage = payload
                    m = re.match(r"改编第 (\d+)/(\d+) 章", payload)
                    if m:
                        # 逐章改编占了整个流程 95% 以上的时间，剩余时间按章速算才准
                        self.ch_now = (int(m.group(1)), int(m.group(2)))
                        if self.ch_base is None:
                            self.ch_base = (int(m.group(1)), time.time())
                elif kind == "busy":
                    self._set_busy(payload)
                elif kind == "done":
                    self.last_video = payload
                elif kind == "settings":
                    # 自动识别出来的改编设定，回填到界面上方便手动微调
                    for key, var_name in DETECT_TO_VAR.items():
                        if payload.get(key):
                            self.vars[var_name].set(str(payload[key]).strip())
                elif kind == "title":
                    self.vars["novel_title"].set(payload)
                    self.log(f"书名已填回界面：{payload}（续跑要靠它对上项目目录，别改）")
                elif kind == "project":
                    # 改编完直接把项目目录填进 KDP 页签，省一次手动选目录
                    self.vars["kdp_project_dir"].set(payload)
                elif kind == "batch_jobs":
                    self._render_jobs(payload)
                elif kind == "error":
                    messagebox.showerror("出错了", payload)
        except queue.Empty:
            pass
        self._tick_status()
        self.after(100, self._drain)

    @staticmethod
    def _hms(sec):
        sec = max(0, int(sec))
        return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"

    def _tick_status(self):
        """每秒刷一次状态行：光看进度条动不动，长任务里根本不知道它是不是还活着。"""
        if not (self.worker and self.worker.is_alive()):
            return
        if time.time() - getattr(self, "_last_tick", 0) < 1:
            return
        self._last_tick = time.time()
        elapsed = time.time() - self.t_start
        parts = [self.stage or "运行中", f"已用 {self._hms(elapsed)}"]

        eta = None
        if self.ch_base and self.ch_now and self.ch_now[0] > self.ch_base[0]:
            idx, total = self.ch_now
            per = (time.time() - self.ch_base[1]) / (idx - self.ch_base[0])
            eta = per * (total - idx + 1)
            parts.insert(1, f"{idx / total * 100:.1f}%  ·  每章 {per:.0f} 秒")
        elif self.frac > 0.02:
            parts.insert(1, f"{self.frac * 100:.1f}%")
            eta = elapsed * (1 - self.frac) / self.frac
        if eta:
            parts.append(f"预计剩余 {self._hms(eta)}")
        self.status_var.set("  ·  ".join(parts))

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in self.actions:
            b.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")
        if not busy:
            self.status_var.set(f"就绪（上一次耗时 {self._hms(time.time() - self.t_start)}）"
                                if self.t_start else "就绪")

    def _collect(self) -> Settings:
        s = Settings()
        for f in fields(Settings):
            if f.name not in self.vars:
                continue
            v = self.vars[f.name].get()
            setattr(s, f.name, f.type(v) if f.type in (int, float, str, bool) else v)
        s.save()
        return s

    def _start(self, fn):
        if self.worker and self.worker.is_alive():
            return
        self.cancel.clear()
        self.t_start = time.time()
        self.stage = "启动中"
        self.frac = 0.0
        self.ch_base = self.ch_now = None
        self.q.put(("busy", True))
        self.q.put(("progress", 0.0))

        def wrapper():
            try:
                fn()
            except Cancelled:
                self.log("已停止。")
            except Exception as exc:
                self.log("失败：" + str(exc))
                self.log(traceback.format_exc(limit=3))
                self.q.put(("error", str(exc)))
            finally:
                self.q.put(("busy", False))

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    # ---------- 视频动作 ----------
    def on_preview(self):
        s = self._collect()

        def job():
            out = Path(s.output_dir) / "_preview.wav"
            out.parent.mkdir(parents=True, exist_ok=True)
            if s.engine == "f5":
                self.log(f"试听（克隆声音）  语速 {s.rate:+d}%")
            else:
                self.log(f"试听：{s.voice}  语速 {s.rate:+d}%  音调 {s.pitch:+d}Hz")
            synth_segment(PREVIEW_TEXT, out, s, log=self.log)
            subprocess.run(["afplay", str(out)])

        self._start(job)

    def on_generate_audio(self):
        """只出音频，生成完直接用系统播放器打开——长文案渲一遍画面太贵，先听声音。"""
        s = self._collect()

        def job():
            path = pipeline.generate_audio(
                s, log=self.log,
                progress=lambda p: self.q.put(("progress", p)),
                cancel=self.cancel,
            )
            subprocess.run(["open", str(path)])

        self._start(job)

    def on_generate(self):
        s = self._collect()

        def job():
            path = pipeline.generate(
                s, log=self.log,
                progress=lambda p: self.q.put(("progress", p)),
                cancel=self.cancel,
            )
            self.q.put(("done", path))

        self._start(job)

    def on_upload(self):
        if not self.last_video or not Path(self.last_video).exists():
            path = filedialog.askopenfilename(title="选择要上传的 mp4",
                                              initialdir=self.vars["output_dir"].get(),
                                              filetypes=[("视频", "*.mp4")])
            if not path:
                return
            self.last_video = path
        title = self.vars["title"].get()
        desc = self.desc.get("1.0", "end").strip()
        video = self.last_video

        def job():
            uploader.upload(video, title, desc, privacy="private", log=self.log)

        self._start(job)

    # ---------- 小说改编动作 ----------
    def _novel_inputs_ok(self, s) -> bool:
        if not Path(s.novel_source_file or "").is_file():
            messagebox.showerror("出错了", "请先选择中文小说 txt 文件")
            return False
        if (not s.use_mock_adaptation and s.llm_provider == "api"
                and not s.llm_api_key.strip()):
            messagebox.showerror(
                "出错了", "没填 API Key，改编正文没法生成。\n"
                          "要么填上 Key，要么改用「本机 CLI」，"
                          "要么勾选「离线演示模式」先看排版。")
            return False
        return True

    def on_llm_test(self):
        """发一句最短的 prompt，确认当前接入方式真的通，别等跑到一半才炸。"""
        s = self._collect()
        if s.use_mock_adaptation:
            messagebox.showinfo("提示", "当前是离线演示模式，不会调用任何模型。")
            return

        def job():
            cfg = novel_adapter.config_from_settings(s)
            engine = novel_adapter.NovelAdaptationEngine(cfg, log_func=self.log)
            way = (f"本机 CLI：{cfg.cli_command} {cfg.cli_model}".strip()
                   if cfg.provider == "cli" else f"API：{cfg.model_name} @ {cfg.base_url}")
            self.q.put(("stage", "测试模型连通"))
            self.log(f"正在测试 {way} ...")
            reply = engine._call_llm("Reply with exactly one word: PONG")
            self.log(f"模型回了：{reply[:200] or '（空）'}")
            self.log("连通正常。" if "PONG" in reply.upper() else
                     "有回应但内容不对劲，先看看上面这行是不是登录提示。")
            self.q.put(("progress", 1.0))

        self._start(job)

    def on_detect_settings(self):
        """只跑识别这一步：读原文样本，把 5 项设定填回界面，不改编正文。"""
        s = self._collect()
        if not self._novel_inputs_ok(s):
            return

        def job():
            cfg = novel_adapter.config_from_settings(s)
            engine = novel_adapter.NovelAdaptationEngine(cfg, log_func=self.log)
            self.q.put(("stage", "识别改编设定"))
            self.q.put(("settings", engine.analyze_source_settings()))
            self.q.put(("progress", 1.0))
            self.log("设定已填回界面，可以手动微调后再点「开始改编」。")

        self._start(job)

    def on_adapt(self, auto_publish: bool = False):
        s = self._collect()
        if not self._novel_inputs_ok(s):
            return

        if auto_publish:
            # 只对这一次生效，不写回 settings.json——发布不可逆，不该被记成默认行为
            if not messagebox.askyesno(
                    "确认：改编完直接发布",
                    "全书改编完成后，会自动：\n\n"
                    "  1. 导出 EPUB / 封面 / 出版文案\n"
                    "  2. 开 Chrome 建 KDP 草稿并上传正文封面\n"
                    "  3. 填 AI 申报、版税、定价\n"
                    f"  4. 点下 Publish（${s.kdp_price:.2f} / {s.kdp_royalty}）\n\n"
                    "第 4 步不可逆：书会进 Amazon 审核，通过后公开售卖。\n"
                    "全程无人值守，中途没人替你看质量。确认继续？"):
                return
            s.kdp_auto_upload = True
            s.kdp_auto_publish = True
            self.log("⚠️ 本次为「改编 + 自动发布」模式，跑完会直接提交上架。")

        def job():
            cfg = novel_adapter.config_from_settings(s)
            engine = novel_adapter.NovelAdaptationEngine(cfg, log_func=self.log)
            # 续跑时不能重新识别：模型两次给的目标设定未必一样，
            # 前 50 章在好莱坞、后 749 章跑到摄政英国就完蛋了
            done = list((novel_adapter.project_dir_for(cfg) / "_chapters").glob("*.json"))
            if s.novel_auto_detect and done:
                self.log(f"已有 {len(done)} 章进度，跳过自动识别，沿用上次的改编设定。")
            elif s.novel_auto_detect:
                self.q.put(("stage", "识别改编设定"))
                self.q.put(("settings", engine.analyze_source_settings()))
            t0 = time.time()
            try:
                proj = engine.run_full_pipeline(
                    cancel_event=self.cancel,
                    progress_cb=lambda p: self.q.put(("progress", p)),
                    stage_cb=lambda t: self.q.put(("stage", t)),
                )
                self.q.put(("project", str(proj)))
                self._notify_book(cfg, proj, (time.time() - t0) / 60, s)
            except Exception as exc:
                done, total = self._chapter_counts(cfg)
                notify.send(notify.book_failed(cfg.book_title, exc, done, total),
                            log=self.log)
                raise
            finally:
                # 中途炸了也要把定下来的书名回填，否则下次续跑会另起一个目录
                if cfg.book_title and not s.novel_title.strip():
                    self.q.put(("title", cfg.book_title))

        self._start(job)

    @staticmethod
    def _chapter_counts(cfg):
        """(已完成章数, 总章数)。给通知文案用。"""
        ch_dir = novel_adapter.project_dir_for(cfg) / "_chapters"
        done = len(list(ch_dir.glob("*.json"))) if ch_dir.is_dir() else 0
        total = 0
        try:
            text = Path(cfg.source_file).read_text("utf-8", errors="replace")
            total = len(novel_adapter.ChapterSplitter.split_text(text))
        except Exception:
            pass
        return done, total

    def _notify_book(self, cfg, proj, minutes, s):
        """改编跑完推 Telegram；开了自动上架就顺手把上架结果一起报。"""
        done, total = self._chapter_counts(cfg)
        kdp = ""
        if getattr(s, "kdp_auto_upload", False) and done >= total > 0:
            self.log("改编完成，开始自动建 KDP 草稿…")
            import cli
            kdp = cli.run_kdp_autopost(s, proj)
            self.log(kdp)
        notify.send(notify.book_done(cfg.book_title, done, total, minutes, proj, kdp),
                    log=self.log)

    def on_open_project(self):
        d = self.vars["kdp_project_dir"].get() or self.vars["output_dir"].get()
        if Path(d).is_dir():
            subprocess.run(["open", d])
        else:
            messagebox.showinfo("提示", "还没有项目目录，先跑一次改编。")

    # ---------- KDP 动作 ----------
    def _load_kdp_meta(self):
        """读项目目录里的元数据；读不到就弹窗提示并返回 None。"""
        d = self.vars["kdp_project_dir"].get()
        if not d or not Path(d).is_dir():
            messagebox.showerror("出错了", "请先选择项目目录")
            return None
        s = self._collect()
        meta = kdp_uploader.KDPMetadata.load_from_project_dir(Path(d))
        meta.price = s.kdp_price
        meta.royalty = s.kdp_royalty
        meta.marketplace = s.kdp_marketplace
        self.kdp_meta = meta
        return meta

    def on_kdp_check(self):
        meta = self._load_kdp_meta()
        if not meta:
            return
        self.log("—— KDP 元数据 ——")
        self.log(f"书名：{meta.title}")
        self.log(f"副标题：{meta.subtitle or '（无）'}")
        self.log(f"作者：{meta.author_first} {meta.author_last}".strip())
        self.log(f"简介：HTML {len(meta.description_html)} 字符 / "
                 f"纯文本 {len(meta.description_text)} 字符")
        self.log(f"关键词：{len(meta.keywords_7)} 个 — {'; '.join(meta.keywords_7)}")
        self.log(f"正文：{meta.manuscript_path or '缺失'}")
        self.log(f"封面：{meta.cover_path or '缺失'}")
        self.log(f"定价：${meta.price:.2f}  版税 {meta.royalty}  市场 {meta.marketplace}")

        issues = kdp_uploader.KDPPreflightChecker.check(meta)
        if issues:
            self.log(f"—— 预检发现 {len(issues)} 项 ——")
            for i in issues:
                self.log("  " + i)
        else:
            self.log("—— 预检通过，没发现问题 ——")

    def _copy_meta(self, field, label):
        meta = self.kdp_meta or self._load_kdp_meta()
        if not meta:
            return
        value = getattr(meta, field)
        text = "\n".join(value) if isinstance(value, list) else value
        if not text:
            messagebox.showinfo("提示", f"{label} 是空的，先跑一次「读取并预检」看看缺什么。")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.log(f"已复制{label}到剪贴板（{len(text)} 字符）")

    def on_kdp_upload(self):
        meta = self._load_kdp_meta()
        if not meta:
            return
        issues = [i for i in kdp_uploader.KDPPreflightChecker.check(meta)
                  if i.startswith("【严重】")]
        if issues:
            if not messagebox.askyesno(
                    "预检没过", "预检有严重问题：\n\n" + "\n".join(issues) + "\n\n仍要继续吗？"):
                return
        if not messagebox.askyesno(
                "确认", f"将打开 Chrome 登录 Amazon KDP，为《{meta.title}》建一本新书草稿：\n\n"
                        f"  · 填书名、作者、简介、7 个关键词、3 个分类\n"
                        f"  · 上传正文（{Path(meta.manuscript_path).name if meta.manuscript_path else '缺失'}）和封面\n"
                        f"  · 填 AI 生成内容申报、版税 {meta.royalty}、定价 ${meta.price:.2f}\n\n"
                        f"不会点发布，浏览器会一直开着让你自己核对。确认继续？"):
            return
        profile = self.vars["kdp_chrome_profile"].get().strip() or None

        def job():
            up = kdp_uploader.KDPBrowserUploader(user_data_dir=profile, log_func=self.log)
            up.upload_ebook(meta, cancel_event=self.cancel)
            # 故意不 close()：最后的 Publish 要你在这个浏览器里自己按

        self._start(job)

    # ---------- 通用 ----------
    def on_stop(self):
        self.cancel.set()
        self.log("正在停止…")

    def on_open_output(self):
        subprocess.run(["open", self.vars["output_dir"].get()])

    def _on_close(self):
        try:
            self._collect()
        except Exception:
            pass
        self.cancel.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()

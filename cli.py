"""Ebook 自动化命令行控制脚本 (Headless CLI Runner - 异步后台与 Telegram 通知版)
专为 Hermes Agent、远程控制与无人值守自动化设计。

支持命令：
  python3 cli.py video                        # 生成助眠视频 (自动读取 settings.json)
  python3 cli.py video --bg                   # 后台生成助眠视频，完成后 Telegram 自动推送
  python3 cli.py adapt                        # 从第一个缺口补到最后一章，再导出全套 KDP 物料
  python3 cli.py adapt --bg                   # 后台执行全书改编（耗时数小时），完成后 Telegram 推送
  python3 cli.py adapt --from 351 --to 353    # 只补第 351~353 章（不导出交付文件）
  python3 cli.py adapt --progress             # 只看改编进度：已完成多少章、缺口在第几章
  python3 cli.py hollywood --from 1 --to 20   # 执行好莱坞快餐爽文改写
  python3 cli.py hollywood --from 1 --to 20 --bg # 后台批量改写，完成后 Telegram 自动推送
  python3 cli.py status                       # 检查当前所有后台任务的实时运行状态与进度
  python3 cli.py kdp-check                    # 检查最近一次改编的 KDP 物料完备性
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "settings.json"
PID_FILE = APP_DIR / "running_task.pid"
LOG_FILE = APP_DIR / "running_task.log"

sys.path.insert(0, str(APP_DIR))
from config import Settings
# pipeline 不在这里导：它会拉起助眠视频那条链路（tts -> edge_tts、f5-tts 等），
# 而小说改编和 KDP 上架完全用不到。顶部无条件导入的话，只想跑 adapt 的机器
# 也得装一堆 TTS 依赖，缺一个就整个 cli 起不来。用到时再导。
import novel_adapter
import kdp_uploader
import notify


# ---------- Telegram 通知 ----------
# 具体实现挪到 notify.py，界面那边也要用；凭据只从 ~/.hermes/.env 读
notify_telegram = notify.send


# ---------- 后台化辅助函数 ----------

def daemonize_or_run(target_func, args, task_name):
    """如果指定了 --bg，则 fork/popen 转入后台脱机运行，避免 Hermes 终端 180 秒超时"""
    if getattr(args, "bg", False):
        python_bin = sys.executable
        cli_script = str(Path(__file__).resolve())
        # 重构命令行参数，去掉 --bg
        forward_args = [a for a in sys.argv[1:] if a != "--bg"]
        cmd = [python_bin, cli_script] + forward_args

        log_f = open(LOG_FILE, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

        task_info = {
            "pid": proc.pid,
            "task_name": task_name,
            "start_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cmd": " ".join(cmd),
        }
        PID_FILE.write_text(json.dumps(task_info, ensure_ascii=False, indent=2), encoding="utf-8")

        print("=" * 60)
        print(f"🚀 【{task_name}】已成功转入后台脱机运行！")
        print(f"📌 任务进程 PID: {proc.pid}")
        print(f"📜 实时日志文件: {LOG_FILE}")
        print(f"💡 本次调用已安全返回，不会卡住你的聊天窗口。")
        print(f"📲 当长任务全部完成时，系统会自动向你的 Telegram 发送通知！")
        print("=" * 60)
        sys.exit(0)
    else:
        target_func(args)


# ---------- 核心任务逻辑 ----------

def run_video(args):
    print("🎬 [1/3] 正在加载助眠视频配置...")
    s = Settings.load() if hasattr(Settings, "load") else Settings()
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except Exception as e:
            print(f"⚠️ 读取 settings.json 异常: {e}，使用默认设置")

    if args.script:
        s.script_path = args.script
    if args.video:
        s.video_path = args.video
    if args.output:
        s.output_dir = args.output

    print(f"📝 文案文件: {s.script_path}")
    print(f"🎥 背景视频: {s.video_path}")
    print(f"🎵 语音引擎: {s.engine} ({s.voice})")
    print("⏳ 开始生成助眠视频，请稍候...")

    t0 = time.time()
    try:
        import pipeline          # 只有真要生成视频时才需要 TTS 那套依赖
        out_path = pipeline.generate(s, log=print)
        elapsed = time.time() - t0
        success_msg = f"🎉 *助眠视频生成成功！*\n\n📁 路径: `{out_path}`\n⏱️ 耗时: {elapsed:.1f} 秒"
        print("\n" + success_msg)
        notify_telegram(success_msg)
    except Exception as e:
        err_msg = f"❌ *助眠视频生成失败*: {e}"
        print("\n" + err_msg)
        notify_telegram(err_msg)
        raise


def chapter_progress(cfg):
    """数一下改编到哪儿了：(已完成, 总章数, 第一个缺口章号, 项目目录)。不调用任何模型。"""
    proj_dir = novel_adapter.project_dir_for(cfg)
    ch_dir = proj_dir / "_chapters"
    done = {int(p.stem) for p in ch_dir.glob("*.json")} if ch_dir.exists() else set()
    total = 0
    if Path(cfg.source_file or "").is_file():
        text = Path(cfg.source_file).read_text("utf-8", errors="replace")
        total = len(novel_adapter.ChapterSplitter.split_text(text))
    gap = next((i for i in range(1, total + 1) if i not in done), None)
    return len(done), total, gap, proj_dir


def load_cfg(args):
    s = Settings.load() if hasattr(Settings, "load") else Settings()
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except Exception:
            pass
    cfg = novel_adapter.config_from_settings(s)
    if getattr(args, "source", None):
        cfg.source_file = args.source
        # 命令行显式换了源文件，界面上那个书名就不是这本书的了。
        # 不清空的话项目目录还是按旧书名走，新书会写进旧书的目录里，
        # 把人家的章节缓存覆盖掉。留空让引擎从改编档案里取这本书自己的名字。
        if str(args.source) != str(s.novel_source_file):
            if cfg.book_title:
                print(f"（源文件换成了 {Path(args.source).name}，"
                      f"忽略设置里的书名「{cfg.book_title}」，改用自动识别）")
            cfg.book_title = ""
            cfg.subtitle = ""
    # --title 放在清空之后：显式指定的书名优先级最高，不该被上面那段抹掉
    if getattr(args, "title", ""):
        cfg.book_title = args.title.strip()
    # 占位目录按源文件区分。不给的话每次重跑都用同一个
    # output/Novel_Adaptation_Project，而上次跑完它已经改名成书名目录了 ——
    # 于是这次找不到进度，重新生成改编档案、模型给出不同的书名、再建一个新目录，
    # 几百章的活白跑一遍。实测已经因此跑出了两个同书异名的目录。
    if cfg.source_file:
        import hashlib
        cfg.work_id = hashlib.md5(
            str(Path(cfg.source_file).resolve()).encode()).hexdigest()[:10]
    return cfg


# 传过的书在自己目录里留个标记。adapt 每跑完一次结尾都会调上架，
# 不记的话重跑一次就在 KDP 上多出一整套重复草稿，只能上网页一本本删。
KDP_UPLOADED_MARK = ".kdp_uploaded"


def _vol_no(d: Path) -> int:
    """目录名里的卷号，Vol7_Xxx -> 7。不是分卷目录就返回 0。"""
    m = re.match(r"Vol(\d+)", d.name)
    return int(m.group(1)) if m else 0


def _book_dirs(proj_dir: Path, only_vols=None):
    """这个项目要上架几本书。分卷了就是各卷目录，没分卷就是项目本身。

    按卷号数字排序，不能用字典序 —— Vol10 会排到 Vol2 前面去。
    only_vols 给一组卷号时只留这几本（`kdp-upload --vol 3` 走这条）。
    """
    vols = [d for d in proj_dir.glob("Vol*")
            if d.is_dir() and (d / "03_Publishing_Copy.txt").exists()]
    vols.sort(key=_vol_no)
    if only_vols:
        vols = [d for d in vols if _vol_no(d) in set(only_vols)]
    return vols or ([proj_dir] if not only_vols else [])


def _kdp_post_one(settings, book_dir: Path, upload_one, do_pub: bool):
    """上架一本。返回 (成不成, 给 Telegram 的一行)。"""
    meta = kdp_uploader.KDPMetadata.load_from_project_dir(book_dir)
    meta.price = settings.kdp_price
    meta.royalty = settings.kdp_royalty
    bad = [i for i in kdp_uploader.KDPPreflightChecker.check(meta)
           if i.startswith("【严重】")]
    if bad:
        return False, f"⚠️ {book_dir.name}：预检没过 —— " + "；".join(bad)
    upload_one(meta, None, do_pub)
    (book_dir / KDP_UPLOADED_MARK).write_text(
        f"{meta.title}\n{time.strftime('%F %T')}\n", encoding="utf-8")
    return True, f"✅ {book_dir.name}：{meta.title}"


def run_kdp_autopost(settings, proj_dir, limit: int = 0,
                     do_publish=None, redo: bool = False, only_vols=None) -> str:
    """改编跑完直接建 KDP 草稿。分卷了就一卷一本，逐本上架。

    发布与否看 kdp_auto_publish：点了 Publish 就撤不回来了，所以默认只到草稿。

    limit / do_publish / redo 是给 `cli.py kdp-upload` 试跑用的：
    只传前几本、强制只建草稿、忽略已传标记重来。
    """
    try:
        proj_dir = Path(proj_dir)
        books = _book_dirs(proj_dir, only_vols)
        if not books:
            return f"⚠️ *没有上架*：`{proj_dir.name}` 里没有第 {only_vols} 卷"
        todo = books if redo else [b for b in books
                                   if not (b / KDP_UPLOADED_MARK).exists()]
        done = len(books) - len(todo)
        if not todo:
            return (f"📚 这 {len(books)} 本之前都传过了，跳过。"
                    f"要重传就删掉各自目录里的 `{KDP_UPLOADED_MARK}`，"
                    f"或者用 `cli.py kdp-upload --redo`")
        if limit:
            todo = todo[:limit]

        do_pub = (bool(getattr(settings, "kdp_auto_publish", False))
                  if do_publish is None else bool(do_publish))
        print(f"\n📤 开始自动上架：{len(todo)} 本"
              + (f"（另有 {done} 本传过了，跳过）" if done else "")
              + f"，自动发布={do_pub}")
        up = kdp_uploader.KDPBrowserUploader(
            user_data_dir=settings.kdp_chrome_profile or None, log_func=print)

        lines, ok = [], 0
        # 整套书一把锁：锁放开的空当别的终端会接管同一个 Chrome 去传它的书，
        # 一套系列就被切散了。代价是别人要排队等这一整套传完。
        with up.upload_session() as upload_one:
            for i, b in enumerate(todo, 1):
                if len(todo) > 1:
                    print(f"\n—— 第 {i}/{len(todo)} 本：{b.name} ——")
                try:
                    good, line = _kdp_post_one(settings, b, upload_one, do_pub)
                    ok += good
                    lines.append(line)
                except Exception as exc:
                    # 一本挂了不能拖垮剩下的：成功的已打标记，重跑只补没传的
                    print(f"   ❌ 这本失败了，继续下一本：{exc}")
                    lines.append(f"❌ {b.name}：{exc}")

        head = (f"🚀 *{ok}/{len(todo)} 本已提交发布*，等 Amazon 审核（约 72 小时）"
                if do_pub else
                f"📝 *{ok}/{len(todo)} 本草稿已建好*，正文封面定价都填完了，就差你点 Publish")
        if ok < len(todo):
            head += "（有失败的，重跑会只补没传的那几本）"
        return head + "\n" + "\n".join(lines)
    except Exception as exc:
        return f"⚠️ *自动上架失败*：{exc}"


def run_kdp_upload(args):
    """拿一本已经生成好的书去试上架，不用重跑改编。

    默认只建草稿，哪怕 settings 里 kdp_auto_publish 是开的 —— 这条命令就是用来
    试跑的，草稿在 KDP 网页上能直接删，发布出去撤不回来。真要发得显式 --publish。
    """
    s = Settings.load() if hasattr(Settings, "load") else Settings()
    if SETTINGS_FILE.exists():
        try:
            for k, v in json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except Exception:
            pass

    # 路径必须显式给。原来是「没给就取 output 下最近改动的那个」—— 多本书时那就是
    # 在猜，猜错就是把 B 书的卷传到 Amazon 上，而且每本里都有 Vol1、光看卷号分不出谁的。
    proj = Path(args.proj).expanduser().resolve()
    if not proj.exists():
        print(f"❌ 目录不存在：{proj}")
        return

    # --vol 3 / --vol 1,3,5：只挑这几卷
    only = [int(x) for x in re.split(r"[,\s]+", args.vol or "") if x.strip().isdigit()]
    books = _book_dirs(proj, only)
    if not books:
        print(f"❌ {proj.name} 里没有第 {only} 卷。有的是："
              f"{[d.name for d in _book_dirs(proj)]}")
        return
    print(f"🔍 项目：{proj}")
    print(f"   共 {len(books)} 本" + ("（分卷）" if books != [proj] else "（单本）"))
    if only:
        print(f"   只传第 {only} 卷")
    if args.limit:
        print(f"   本次只传前 {args.limit} 本")
    print(f"   模式：{'⚠️ 真发布（撤不回来）' if args.publish else '只建草稿（可删）'}")
    if args.dry_run:
        for b in (books[:args.limit] if args.limit else books):
            meta = kdp_uploader.KDPMetadata.load_from_project_dir(b)
            bad = [i for i in kdp_uploader.KDPPreflightChecker.check(meta)
                   if i.startswith("【严重】")]
            mark = "✅" if not bad else "❌"
            done = "（传过了）" if (b / KDP_UPLOADED_MARK).exists() else ""
            print(f"   {mark} {b.name}{done} → {meta.title or '(书名为空)'}")
            for i in bad:
                print(f"        {i}")
        print("\n（--dry-run：只看不传，没碰浏览器）")
        return

    print(run_kdp_autopost(s, proj, limit=args.limit, do_publish=args.publish,
                           redo=args.redo, only_vols=only))


def run_chrome(args):
    """把 Chrome 带调试端口重起，用你自己的 profile，这样上架时沿用已有登录态。

    Chrome 的调试端口只能在启动时用 --remote-debugging-port 打开，没有任何办法
    对一个已经在跑的 Chrome 事后补上。所以只能关掉重起 —— 走 AppleScript 优雅退出，
    会话会保存，重开后标签页能恢复。
    """
    profile = args.profile or kdp_uploader.default_chrome_profile()
    print(f"profile: {profile}")
    print(f"profile-directory: {args.profile_dir}")
    kdp_uploader.launch_debug_chrome(
        profile=profile, profile_dir=args.profile_dir,
        port=args.port, log=print)
    print("\n好了。这就是你平时那个 Chrome，cookie、书签、登录态都在，照常用。")
    print("上架时脚本会自动接管它，不再另开窗口。")
    print("注意：上传跑着的时候别翻页/切标签/关窗口，会打断它。")


def run_batch(args):
    """把 Drive 文件夹里的 txt 一本一本跑完。"""
    import batch

    s = Settings.load()
    if SETTINGS_FILE.exists():
        try:
            for k, v in json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except Exception:
            pass

    folder = args.folder or getattr(s, "gdrive_folder", "")
    if not folder:
        print("❌ 没指定 Drive 文件夹。用 --folder，或在界面设置里填 gdrive_folder。")
        return
    sa = getattr(s, "gdrive_sa_json", "")

    if args.retry_failed:
        st = batch.load_state()
        hit = [k for k, v in st.items() if v.get("status") == batch.FAILED]
        for k in hit:
            batch.reset(k)
        print(f"已把 {len(hit)} 本失败的打回待处理。")

    try:
        jobs = batch.list_jobs(folder, sa_path=sa)
    except Exception as exc:
        print(f"❌ 读 Drive 失败：{exc}")
        return

    try:
        fname = batch.folder_info(folder, sa_path=sa)["name"]
    except Exception:
        fname = folder
    print(f"\n📚 队列（{fname}）共 {len(jobs)} 本：")
    for j in jobs:
        note = f"  -> {j.project_dir}" if j.project_dir else ""
        note += f"  [{j.error[:60]}]" if j.error else ""
        print(f"  {j.status:4} {j.folder[:6]:<7} {j.name[:34]:36} "
              f"{j.size_mb:6.1f} MB{note}")
    if args.list:
        return

    gap = args.gap if args.gap is not None else getattr(s, "batch_gap_minutes", 0.0)
    t0 = time.time()
    tally = batch.run_batch(folder, s, log=print, sa_path=sa,
                            gap_minutes=gap, limit=args.limit)
    msg = (f"📚 *Drive 批量结束*\n\n"
           f"完成 {tally['done']} 本，跳过 {tally['skip']} 本，失败 {tally['fail']} 本\n"
           f"⏱️ 总耗时 {(time.time() - t0) / 60:.0f} 分钟")
    print("\n" + msg)
    notify_telegram(msg)


def run_kdp_categories(args):
    """扒一次 KDP 分类表。浏览器会开着等你登录，扒完自己关。"""
    s = Settings.load()
    up = kdp_uploader.KDPBrowserUploader(
        user_data_dir=s.kdp_chrome_profile or None, log_func=print)
    try:
        path = up.dump_categories(args.out or None)
        print(f"\n✅ 分类表已更新：{path}")
    finally:
        up.close()


def run_adapt(args):
    print("📖 [1/3] 正在加载小说跨文化改编与 KDP 出版配置...")
    cfg = load_cfg(args)
    s = Settings.load()
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except Exception:
            pass

    done, total, gap, proj_dir = chapter_progress(cfg)
    print(f"📄 原著小说: {cfg.source_file}")
    print(f"📚 书名: {cfg.book_title or '(未定，会从改编档案里取)'}")
    print(f"🌍 目标国家/时代: {cfg.target_country} / {cfg.target_era}")
    print(f"🤖 通道: {cfg.cli_command} → {cfg.fallback_cli_command} → "
          f"{cfg.model_name if cfg.api_key else 'API(未配置)'}")
    print(f"📁 项目目录: {proj_dir}")
    print(f"📊 进度: {done}/{total} 章已完成" + (f"，第一个缺口在第 {gap} 章" if gap else "，全部完成"))

    if getattr(args, "progress", False):
        return

    rng = None
    if args.first or args.last:
        rng = (args.first or 1, args.last or total or 10 ** 9)
        print(f"⏳ 本次只补第 {rng[0]}~{rng[1]} 章（不导出交付文件）...")
    else:
        print("⏳ 开始全套小说改编与排版流水线（长篇任务可能需要几小时）...")

    t0 = time.time()
    try:
        engine = novel_adapter.NovelAdaptationEngine(cfg, log_func=print)
        proj_dir = engine.run_full_pipeline(chapter_range=rng)
        mins = (time.time() - t0) / 60
        if rng:
            now_done, total, gap, _ = chapter_progress(cfg)
            success_msg = (
                f"✅ *第 {rng[0]}~{rng[1]} 章改编完成*\n\n"
                f"📁 项目目录: `{proj_dir}`\n"
                f"📊 当前进度: {now_done}/{total} 章"
                + (f"，下一个缺口第 {gap} 章" if gap else "，全书已齐，可跑 `adapt` 导出物料")
                + f"\n⏱️ 耗时: {mins:.1f} 分钟"
            )
        else:
            success_msg = (
                f"🎉 *全套小说改编与 KDP 物料制作完成！*\n\n"
                f"📁 项目目录: `{proj_dir}`\n"
                f"📄 正文: `07_Manuscript.epub` / `01_English_Manuscript.docx`\n"
                f"⏱️ 总耗时: {mins:.1f} 分钟"
            )
            if getattr(s, "kdp_auto_upload", False):
                success_msg += "\n\n" + run_kdp_autopost(s, proj_dir)
        print("\n" + success_msg)
        notify_telegram(success_msg)
    except Exception as e:
        err_msg = f"❌ *小说改编失败*: {e}"
        print("\n" + err_msg)
        notify_telegram(err_msg)
        raise


def run_hollywood(args):
    hollywood_script = Path("/Users/yuhaomao/Documents/zhenhuan_rewrite/run_hollywood.py")
    if not hollywood_script.exists():
        print(f"❌ 找不到脚本: {hollywood_script}")
        return

    cmd = [
        sys.executable,
        str(hollywood_script),
        "--from", str(args.start_idx),
        "--to", str(args.end_idx),
        "--chunk-size", str(args.chunk_size),
        "--limit", str(args.limit),
    ]
    print(f"🚀 正在启动好莱坞快餐爽文改写 (第 {args.start_idx} 章 ~ 第 {args.end_idx} 章)...")

    t0 = time.time()
    res = subprocess.run(cmd)
    elapsed = time.time() - t0

    if res.returncode == 0:
        success_msg = (
            f"🎉 *好莱坞爽文改写完成！*\n\n"
            f"📖 章节范围: 第 {args.start_idx} 章 ~ 第 {args.end_idx} 章\n"
            f"📁 产物目录: `/Users/yuhaomao/Documents/zhenhuan_rewrite/out/hollywood/`\n"
            f"⏱️ 耗时: {elapsed/60:.1f} 分钟"
        )
        print("\n" + success_msg)
        notify_telegram(success_msg)
    else:
        err_msg = f"❌ *好莱坞改写中断*，退出码: {res.returncode}"
        print("\n" + err_msg)
        notify_telegram(err_msg)


def check_status(args):
    """查看当前后台任务是否仍在运行，并展示最新日志"""
    if not PID_FILE.exists():
        print("ℹ️ 当前没有正在运行的后台任务。")
        return

    try:
        info = json.loads(PID_FILE.read_text(encoding="utf-8"))
        pid = info.get("pid")
        task_name = info.get("task_name")
        start_time = info.get("start_time")

        # 检查进程是否存活
        is_running = False
        if pid:
            try:
                os.kill(pid, 0)
                is_running = True
            except OSError:
                is_running = False

        status_text = "🟢 正在运行中" if is_running else "⚪ 已结束或退出"
        print("=" * 50)
        print(f"📋 任务名称: {task_name}")
        print(f"📊 状态: {status_text} (PID: {pid})")
        print(f"⏰ 启动时间: {start_time}")

        try:
            done, total, gap, _ = chapter_progress(load_cfg(args))
            if total:
                print(f"📊 改编进度: {done}/{total} 章"
                      + (f"，缺口在第 {gap} 章" if gap else "，全部完成"))
        except Exception:
            pass

        if LOG_FILE.exists():
            lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
            last_lines = lines[-8:] if len(lines) >= 8 else lines
            print("\n📜 最新进度日志 (最后几行):")
            for l in last_lines:
                print(f"  > {l}")
        print("=" * 50)

        if not is_running:
            PID_FILE.unlink(missing_ok=True)
    except Exception as e:
        print(f"⚠️ 解析任务状态失败: {e}")


def run_kdp_check(args):
    s = Settings.load() if hasattr(Settings, "load") else Settings()
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except Exception:
            pass

    proj_dir = Path(s.kdp_project_dir) if s.kdp_project_dir else None
    if not proj_dir or not proj_dir.exists():
        out_root = Path(s.output_dir)
        subdirs = [d for d in out_root.iterdir() if d.is_dir() and not d.name.startswith("_")]
        if subdirs:
            proj_dir = sorted(subdirs, key=lambda p: p.stat().st_mtime)[-1]

    if not proj_dir or not proj_dir.exists():
        print("❌ 未找到有效的小说改编项目目录，请先运行 adapt")
        return

    print(f"🔍 检查项目目录: {proj_dir}")
    meta = kdp_uploader.KDPMetadata.load_from_project_dir(proj_dir)
    print("=" * 50)
    print(f"书名: {meta.title or '未命名'}")
    print(f"副标题: {meta.subtitle or '无'}")
    print(f"作者: {meta.author_first} {meta.author_last}")
    print(f"价格/版税: ${meta.price} ({meta.royalty})")
    print(f"7个关键词: {', '.join(meta.keywords_7) if meta.keywords_7 else '未提取'}")
    print(f"正文母稿: {'✅ 已就绪' if Path(meta.manuscript_path).exists() else '❌ 缺失'}")
    print(f"封面文件: {'✅ 已就绪' if Path(meta.cover_path).exists() else '⚠️ 暂无'}")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="Ebook 自动化命令行工具 (Hermes 异步后台与通知入口)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. 助眠视频
    p_video = subparsers.add_parser("video", help="生成助眠视频")
    p_video.add_argument("--script", type=str, help="文案 txt 路径")
    p_video.add_argument("--video", type=str, help="背景 mp4 路径")
    p_video.add_argument("--output", type=str, help="输出目录")
    p_video.add_argument("--bg", action="store_true", help="后台脱机运行，并在完成后 Telegram 通知")

    # 2. 小说跨文化改编
    p_adapt = subparsers.add_parser("adapt", help="执行小说跨文化改编与 KDP 母稿生成")
    p_adapt.add_argument("--source", type=str, help="中文原著 txt 路径")
    p_adapt.add_argument("--title", type=str, default="",
                         help="英文书名。不给就从改编档案里取推荐书名；"
                              "档案解析不出来时会落到占位名，这时用它手动指定")
    p_adapt.add_argument("--from", dest="first", type=int, default=0, help="只改编这一章起（含）")
    p_adapt.add_argument("--to", dest="last", type=int, default=0, help="只改编到这一章为止（含）")
    p_adapt.add_argument("--progress", action="store_true", help="只看进度，不调用模型")
    p_adapt.add_argument("--bg", action="store_true", help="后台脱机运行，并在完成后 Telegram 通知")

    # 3. 好莱坞爽文改写
    p_hw = subparsers.add_parser("hollywood", help="执行好莱坞快餐爽文批量改写")
    p_hw.add_argument("--from", dest="start_idx", type=int, default=1, help="起始章节")
    p_hw.add_argument("--to", dest="end_idx", type=int, default=10, help="截止章节")
    p_hw.add_argument("--chunk-size", type=int, default=2, help="每包章节数")
    p_hw.add_argument("--limit", type=int, default=5, help="最多处理包数")
    p_hw.add_argument("--bg", action="store_true", help="后台脱机运行，并在完成后 Telegram 通知")

    # 4. 任务状态查询
    p_status = subparsers.add_parser("status", help="查询当前后台长任务的实时进度与状态")

    # 5. KDP 检查
    p_kdp = subparsers.add_parser("kdp-check", help="检查最近一次小说项目的 KDP 上架物料")

    # 6. Drive 批量
    p_batch = subparsers.add_parser(
        "batch", help="把 Google Drive 文件夹里的 txt 一本一本改编并上架")
    p_batch.add_argument("--folder", type=str, default="", help="Drive 文件夹名或 ID")
    p_batch.add_argument("--list", action="store_true", help="只看队列，不跑")
    p_batch.add_argument("--limit", type=int, default=0, help="本次最多跑几本")
    p_batch.add_argument("--gap", type=float, default=None, help="每本之间隔几分钟")
    p_batch.add_argument("--retry-failed", action="store_true", help="把失败的打回待处理")
    p_batch.add_argument("--bg", action="store_true", help="后台脱机运行，完成后 Telegram 通知")

    # 6. 扒 KDP 分类表
    p_cat = subparsers.add_parser(
        "kdp-categories", help="从真实 KDP 分类弹层扒下整棵分类树，写入 kdp_categories.json")
    p_cat.add_argument("--out", type=str, default="", help="输出路径，默认 kdp_categories.json")

    # 7. 拿已经生成好的书去试上架（默认只建草稿）
    p_up = subparsers.add_parser(
        "kdp-upload", help="拿已经生成好的书去上架，不用重跑改编；默认只建草稿")
    p_up.add_argument("--proj", type=str, required=True,
                      help="要传的目录，必填。给书的目录就传它下面所有卷，"
                           "给某个卷目录就只传那一卷。例："
                           "output/Book1 或 output/Book1/Vol1_Xxx")
    p_up.add_argument("--vol", type=str, default="",
                      help="只传指定卷，比如 --vol 3 或 --vol 1,3,5")
    p_up.add_argument("--limit", type=int, default=0, help="只传前几本，试跑时填 1")
    p_up.add_argument("--redo", action="store_true",
                      help="忽略 .kdp_uploaded 标记，已传过的也重传")
    p_up.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="只列出要传哪几本并跑预检，不碰浏览器")
    p_up.add_argument("--publish", action="store_true",
                      help="真的点发布（撤不回来）。不给就只建草稿")

    # 8. 把 Chrome 带调试端口重起，好让上架沿用你自己的登录态
    p_chrome = subparsers.add_parser(
        "chrome", help="用你自己的 Chrome profile 带调试端口重起，上架时直接复用登录态")
    p_chrome.add_argument("--profile", type=str, default="",
                          help="user-data-dir，默认你真实的 Chrome 目录")
    p_chrome.add_argument("--profile-dir", dest="profile_dir", type=str, default="Default",
                          help="profile 子目录，多账号时可能是 Profile 1、Profile 2")
    p_chrome.add_argument("--port", type=int, default=kdp_uploader.CHROME_DEBUG_PORT,
                          help="调试端口，默认 9333")

    args = parser.parse_args()

    if args.command == "video":
        daemonize_or_run(run_video, args, "助眠视频生成")
    elif args.command == "adapt":
        daemonize_or_run(run_adapt, args, "全套小说改编与KDP物料制作")
    elif args.command == "hollywood":
        daemonize_or_run(run_hollywood, args, "好莱坞爽文批量改写")
    elif args.command == "status":
        check_status(args)
    elif args.command == "kdp-check":
        run_kdp_check(args)
    elif args.command == "batch":
        daemonize_or_run(run_batch, args, "Drive 批量改编与上架")
    elif args.command == "kdp-categories":
        run_kdp_categories(args)
    elif args.command == "chrome":
        run_chrome(args)
    elif args.command == "kdp-upload":
        run_kdp_upload(args)


if __name__ == "__main__":
    main()

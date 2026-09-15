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
import pipeline
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
    return cfg


def run_kdp_autopost(settings, proj_dir) -> str:
    """改编跑完直接建 KDP 草稿。返回一段给 Telegram 用的结果文字。

    发布与否看 kdp_auto_publish：点了 Publish 就撤不回来了，所以默认只到草稿。
    """
    try:
        meta = kdp_uploader.KDPMetadata.load_from_project_dir(Path(proj_dir))
        meta.price = settings.kdp_price
        meta.royalty = settings.kdp_royalty
        issues = [i for i in kdp_uploader.KDPPreflightChecker.check(meta)
                  if i.startswith("【严重】")]
        if issues:
            return "⚠️ *预检没过，没有上架*：\n" + "\n".join(issues)

        do_pub = bool(getattr(settings, "kdp_auto_publish", False))
        print(f"\n📤 开始自动上架（自动发布={do_pub}）...")
        up = kdp_uploader.KDPBrowserUploader(
            user_data_dir=settings.kdp_chrome_profile or None, log_func=print)
        up.upload_ebook(meta, do_publish=do_pub)
        return ("🚀 *已自动提交发布*，等 Amazon 审核（约 72 小时）"
                if do_pub else
                "📝 *KDP 草稿已建好*，正文封面定价都填完了，就差你点 Publish")
    except Exception as exc:
        return f"⚠️ *自动上架失败*：{exc}"


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


if __name__ == "__main__":
    main()

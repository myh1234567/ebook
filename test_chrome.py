"""本地自检：Chrome 能不能接管、登录态在不在、各卷物料齐不齐。

全程只读，不建草稿、不发布、不改 Chrome 的任何状态。跑真上架之前先跑这个，
把「能不能连上」和「东西对不对」这两类问题在碰 Amazon 之前就排掉。

    python3 test_chrome.py                 # 自动找最近一次的项目
    python3 test_chrome.py --proj <目录>   # 指定项目目录
"""
import argparse
import json
import sys
from pathlib import Path

import kdp_uploader

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "settings.json"
OK, NO, WARN = "  ✅", "  ❌", "  ⚠️ "
KDP_SHELF = "https://kdp.amazon.com/en_US/bookshelf"


def load_settings():
    s = {}
    if SETTINGS_FILE.exists():
        try:
            s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"{WARN}settings.json 读不了：{exc}")
    return s


def step1_chrome():
    """Chrome 在不在、端口开没开。"""
    print("\n【1】Chrome 与调试端口")
    binp = Path(kdp_uploader.CHROME_BIN)
    print(f"{OK if binp.exists() else NO} Chrome 程序：{binp}")
    if not binp.exists():
        print("     装的不是标准路径的 Chrome，改 kdp_uploader.CHROME_BIN")
        return False

    pids = kdp_uploader.chrome_pids()
    print(f"{OK if pids else WARN} 正在运行的 Chrome 主进程：{pids or '没有'}")

    alive = kdp_uploader.debug_port_alive()
    print(f"{OK if alive else NO} 调试端口 {kdp_uploader.CHROME_DEBUG_PORT}："
          f"{'开着' if alive else '没开'}")
    if not alive:
        print("     调试端口只能在 Chrome 启动时开，没法事后补。先跑：")
        print("       python3 cli.py chrome")
        return False
    return True


def step2_attach():
    """真的用 Selenium 连上去，读一下版本和当前页面。"""
    print("\n【2】Selenium 接管")
    try:
        up = kdp_uploader.KDPBrowserUploader(log_func=lambda m: print("     " + m))
        up.start_browser()
        drv = up.driver
        ver = drv.capabilities.get("browserVersion", "?")
        print(f"{OK} 接上了，Chrome {ver}")
        print(f"     当前标签页：{drv.title[:60] or '(空)'}")
        print(f"     共 {len(drv.window_handles)} 个标签页")
        return up
    except Exception as exc:
        print(f"{NO} 接不上：{exc}")
        return None


def step3_login(up):
    """看 KDP 登录态在不在。只打开书架页读一眼，不动任何东西。"""
    print("\n【3】KDP 登录态")
    drv = up.driver
    before = drv.current_url
    try:
        drv.get(KDP_SHELF)
        import time
        time.sleep(4)
        url = drv.current_url.lower()
        if "signin" in url or "/ap/" in url:
            print(f"{NO} 没登录（被弹到登录页）")
            print("     在这个 Chrome 窗口里手动登录一次 KDP，之后就一直有了")
            return False
        print(f"{OK} 已登录，书架页打开正常")
        print(f"     {drv.current_url[:70]}")
        return True
    except Exception as exc:
        print(f"{NO} 打不开书架页：{exc}")
        return False
    finally:
        if before and before.startswith("http"):
            try:
                drv.get(before)      # 把你原来那页还回去
            except Exception:
                pass


def find_project(settings, explicit=""):
    if explicit:
        return Path(explicit).expanduser().resolve()
    out = Path(settings.get("output_dir") or (APP_DIR / "output"))
    if not out.exists():
        return None
    dirs = [d for d in out.iterdir() if d.is_dir() and not d.name.startswith("_")]
    return sorted(dirs, key=lambda p: p.stat().st_mtime)[-1] if dirs else None


def step4_materials(settings, proj):
    """逐本预检。这一步完全不碰浏览器，最该先跑。"""
    print("\n【4】各卷上架物料")
    if not proj or not proj.exists():
        print(f"{NO} 找不到项目目录，用 --proj 指定")
        return False
    print(f"     项目：{proj}")

    import cli
    books = cli._book_dirs(proj)
    print(f"     要上架 {len(books)} 本"
          + ("（分卷）" if books != [proj] else "（未分卷，单本）"))

    all_ok = True
    for b in books:
        meta = kdp_uploader.KDPMetadata.load_from_project_dir(b)
        meta.price = settings.get("kdp_price", 2.99)
        bad = [i for i in kdp_uploader.KDPPreflightChecker.check(meta)
               if i.startswith("【严重】")]
        mark = OK if not bad else NO
        all_ok &= not bad
        done = "（传过了）" if (b / cli.KDP_UPLOADED_MARK).exists() else ""
        print(f"{mark} {b.name}{done}")
        print(f"       书名：{meta.title or '(空)'}")
        print(f"       分类：{'、'.join(meta.categories) or '(空，会用默认)'}")
        print(f"       关键词 {len(meta.keywords_7)} 个 / 简介 "
              f"{len(meta.description_html or meta.description_text)} 字符")
        for i in bad:
            print(f"       {i}")
        import novel_adapter
        if novel_adapter.is_placeholder_title(meta.title.split(":")[0]):
            print(f"{WARN}      书名还是占位名，会原样传到 Amazon，先定书名")
            all_ok = False
    return all_ok


def main():
    ap = argparse.ArgumentParser(description="上架链路本地自检（只读，不建草稿）")
    ap.add_argument("--proj", default="", help="项目目录，默认取最近改动的那个")
    ap.add_argument("--skip-browser", action="store_true", help="只查物料，不碰浏览器")
    args = ap.parse_args()

    settings = load_settings()
    proj = find_project(settings, args.proj)

    # 物料检查放前面：不需要浏览器，最便宜，问题也最常出在这儿
    mat_ok = step4_materials(settings, proj)

    br_ok = None
    if not args.skip_browser:
        if step1_chrome():
            up = step2_attach()
            br_ok = bool(up) and step3_login(up)
        else:
            br_ok = False

    print("\n" + "=" * 54)
    print(f"物料  {'通过' if mat_ok else '有问题，见上'}")
    if br_ok is not None:
        print(f"浏览器 {'通过' if br_ok else '有问题，见上'}")
    if mat_ok and br_ok:
        print("\n都通过了。下一步建议先把 kdp_auto_publish 关掉，跑一次只建草稿：")
        print("  草稿在 KDP 网页上可以直接删，发布出去就撤不回来了。")
    print("=" * 54)
    return 0 if (mat_ok and br_ok is not False) else 1


if __name__ == "__main__":
    sys.exit(main())

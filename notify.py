"""Telegram 通知。界面和命令行共用，长任务跑完往手机推一条。

凭据只从 ~/.hermes/.env 读，不写在代码里。读不到就静默跳过，
不能因为发不出通知把跑了几小时的任务带崩。
"""
import json
import urllib.request
from pathlib import Path

ENV_FILE = Path.home() / ".hermes" / ".env"
KEYS = {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_HOME_CHANNEL": "chat_id"}


def _creds():
    out = {"token": "", "chat_id": ""}
    if ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text("utf-8", errors="ignore").splitlines():
                key, _, val = line.strip().partition("=")
                if key in KEYS and val.strip():
                    out[KEYS[key]] = val.strip()
        except Exception:
            pass
    return out["token"], out["chat_id"]


def send(text: str, log=None) -> bool:
    """推一条 Markdown 消息。成功返回 True。"""
    say = log or (lambda _: None)
    token, chat_id = _creds()
    if not token or not chat_id:
        say("没配 Telegram 凭据（~/.hermes/.env 里的 TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_HOME_CHANNEL），跳过推送。")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _post(body: dict) -> None:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass

    # 先按 Markdown 发。失败原因里常带 `_foo_bar()` 这种下划线，Telegram 会把它
    # 当成没闭合的斜体标记直接 400 —— 结果就是最该发出去的那条失败通知发不出去。
    # 所以 400 一律退回纯文本重发一次，宁可没有格式也不能丢消息。
    try:
        _post({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
        say("已推送 Telegram 通知。")
        return True
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            say(f"Telegram 推送失败：{exc}")
            return False
    except Exception as exc:
        say(f"Telegram 推送失败：{exc}")
        return False

    try:
        _post({"chat_id": chat_id, "text": text})
        say("已推送 Telegram 通知（Markdown 解析失败，改用纯文本）。")
        return True
    except Exception as exc:
        say(f"Telegram 推送失败：{exc}")
        return False


def book_done(title: str, chapters: int, total: int, minutes: float,
              proj_dir, kdp_status: str = "") -> str:
    """改编完成的通知文案。"""
    head = (f"✅ *《{title}》改编完成*" if chapters >= total
            else f"⏸️ *《{title}》改编中断*")
    lines = [head, "",
             f"📖 章节：{chapters}/{total}",
             f"⏱️ 本次耗时：{minutes:.0f} 分钟",
             f"📁 目录：`{proj_dir}`"]
    if chapters < total:
        lines.append(f"⚠️ 还差 {total - chapters} 章，再跑一次 `adapt` 会接着补")
    if kdp_status:
        lines += ["", kdp_status]
    return "\n".join(lines)


def book_failed(title: str, err: str, chapters: int = 0, total: int = 0) -> str:
    msg = [f"❌ *《{title or '未命名'}》改编失败*", "", f"原因：{str(err)[:300]}"]
    if total:
        msg.append(f"已完成 {chapters}/{total} 章，进度没丢，重跑会接着补")
    return "\n".join(msg)

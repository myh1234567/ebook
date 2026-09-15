"""文案切分 + 慢速朗读，输出一条带段落停顿的旁白 wav。

两个引擎，界面上可切：
- edge：微软内置音色，免费、快，语速直接传给 TTS；
- f5  ：F5-TTS 声音克隆，用参考音频复刻音色，语速靠 atempo 后期慢放（保音高）。
不管哪个引擎，每段最终都归一成 48k 单声道 wav，后面的流程完全一样。
"""
import asyncio
import re
from pathlib import Path

import edge_tts

from ffmpeg_utils import run, silence, duration, Cancelled

CJK = re.compile(r"[㐀-鿿]")
# 中英文的句末标点，用来把过长的段落再切碎
SENT_SPLIT = re.compile(r"(?<=[。！？!?；;…])\s*|(?<=[.!?])\s+")

# ---- 文本规范化 ----
# F5-TTS 是字符级词表，全角冒号/分号/括号这些不在词表里，模型会把它们念成怪声。
# 所以念之前统一成一小套安全标点：表停顿的转逗号句号，不表音的直接删掉。
# 先把所有标点统一成 ASCII 的 , . ! ? 四种，最后再按语言渲染回去。
# 这样中英文只需要维护一套规则。
_ELLIPSIS = re.compile(r"[…‥]+|\.{3,}")          # 省略号当长停顿，转句号
_TO_COMMA = re.compile(r"[，、：；:;—–―~〜]|--+")  # 这些都只是"停顿"
_DROP = str.maketrans({c: "" for c in "《》〈〉「」『』【】〔〕（）()[]{}｛｝“”‘’\"·•*#＊　"})
_TO_ASCII = str.maketrans({"。": ".", "！": "!", "？": "?"})
_TO_FULL = str.maketrans({",": "，", ".": "。", "!": "！", "?": "？"})
# 连着一串标点（含中间空格）只留最强的：句末 > 停顿
_PUNCT_RUN = re.compile(r"[,.!?][\s,.!?]*")
# 中文保留汉字，英文保留连字符和撇号（well-known / don't 不能被拆坏）
_ALLOWED = re.compile(r"[0-9A-Za-z一-鿿'\-,.!?\s]")


def _collapse(m) -> str:
    run = m.group(0)
    for p in ".!?":               # 句末标点优先保留
        if p in run:
            return p
    return ","


def normalize_for_speech(text: str) -> str:
    """把文案清成 TTS 念得出来的样子。

    不做这一步，F5-TTS 会把词表外的标点（：；（）等）念成杂音——它是字符级词表，
    词表里没有的符号就成了未知 token。

    中英文分别渲染：中文用全角标点且不留空格，英文用半角标点且标点后补空格。
    """
    cjk = bool(CJK.search(text))
    text = _ELLIPSIS.sub(".", text)
    text = text.translate(_DROP).translate(_TO_ASCII)
    text = _TO_COMMA.sub(",", text)
    text = "".join(ch for ch in text if _ALLOWED.match(ch))
    text = _PUNCT_RUN.sub(_collapse, text)

    if cjk:
        text = re.sub(r"\s*([,.!?])\s*", lambda m: m.group(1), text)
        text = text.translate(_TO_FULL)
    else:
        text = re.sub(r"\s*([,.!?])", r"\1", text)      # 标点前不留空格
        text = re.sub(r"([,.!?])(?=[^\s])", r"\1 ", text)  # 标点后补一个空格
    return re.sub(r"[ \t]+", " ", text).strip(" ,，、")


def read_script(path) -> str:
    return Path(path).read_text("utf-8")


def split_paragraphs(text: str, max_chars: int = 600) -> list:
    """按空行切段落；段落太长再按句末标点切，避免单次 TTS 请求过大。"""
    blocks = re.split(r"\n\s*\n", text.strip())
    out = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        sep = "" if CJK.search(block) else " "
        para = sep.join(lines)
        if len(para) <= max_chars:
            out.append(para)
            continue
        chunk = ""
        for sent in SENT_SPLIT.split(para):
            if not sent:
                continue
            if len(chunk) + len(sent) > max_chars and chunk:
                out.append(chunk)
                chunk = ""
            chunk += sent
        if chunk:
            out.append(chunk)
    # 切完再规范化：切句子要用到 ；… 这些标点，规范化会把它们换掉
    return [t for t in (normalize_for_speech(p) for p in out) if t]


async def _speak(text: str, out_path: Path, voice: str, rate: int, pitch: int) -> None:
    comm = edge_tts.Communicate(
        text, voice,
        rate=f"{rate:+d}%",
        pitch=f"{pitch:+d}Hz",
    )
    await comm.save(str(out_path))


def speak_to_mp3(text: str, out_path: Path, voice: str, rate: int, pitch: int,
                 retries: int = 3) -> Path:
    """edge-tts 偶尔会断连，这里做几次重试。"""
    last = None
    for attempt in range(retries):
        try:
            asyncio.run(_speak(text, out_path, voice, rate, pitch))
            if out_path.exists() and out_path.stat().st_size > 0:
                return out_path
            last = RuntimeError("edge-tts 返回了空音频")
        except Exception as exc:  # 网络/服务端抖动
            last = exc
    raise RuntimeError(f"语音合成失败（重试 {retries} 次）：{last}")


# 只裁掉首尾静音，中间的自然停顿要留着（助眠的节奏就靠它）
_TRIM = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05"


def _normalize(src: Path, dst: Path, atempo: float = 1.0, trim: bool = False,
               cancel=None) -> Path:
    """统一成 48k 单声道 wav，顺带按需慢放和裁静音。"""
    filters = []
    if trim:
        filters += [_TRIM, "areverse", _TRIM, "areverse"]
    if abs(atempo - 1.0) > 1e-3:
        filters.append(f"atempo={atempo:.3f}")   # atempo 保持音高不变
    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += ["-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)]
    run(cmd, log=lambda _m: None, cancel=cancel)
    return dst


def synth_segment(text: str, out_wav: Path, s, log=print, cancel=None) -> Path:
    """按所选引擎合成一段，产出 48k 单声道 wav。"""
    if s.engine == "f5":
        import clone_tts          # 懒加载：不用克隆就不该等 torch 起来
        raw = out_wav.with_name(out_wav.stem + "_raw.wav")
        factor = 1 + s.rate / 100
        # 两条路都能放慢，但来源不同：让模型慢着说，还是把正常语速的音频拉长。
        # 拉伸比例大了（比如 0.75）声音会发硬，所以默认走引擎。
        engine_speed = factor if s.slow_mode == "engine" else 1.0
        atempo = 1.0 if s.slow_mode == "engine" else factor
        clone_tts.speak(text, raw, s, speed=engine_speed, log=log)
        _normalize(raw, out_wav, atempo=atempo, trim=True, cancel=cancel)
    else:
        mp3 = out_wav.with_suffix(".mp3")
        speak_to_mp3(text, mp3, s.voice, s.rate, s.pitch)
        _normalize(mp3, out_wav, cancel=cancel)
    return out_wav


def synth_narration(paragraphs: list, work_dir: Path, s, log=print,
                    progress=None, cancel=None) -> Path:
    """逐段合成，段间插静音，拼成 narration.wav（48k 单声道）。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    gap = silence(work_dir / "gap.wav", s.pause, log=log) if s.pause > 0 else None

    pieces = []
    for i, para in enumerate(paragraphs):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        wav = work_dir / f"seg_{i:04d}.wav"
        log(f"[{i + 1}/{len(paragraphs)}] 合成：{para[:30]}…")
        synth_segment(para, wav, s, log=log, cancel=cancel)
        if pieces and gap is not None:
            pieces.append(gap)
        pieces.append(wav)
        if progress:
            progress((i + 1) / len(paragraphs))

    listfile = work_dir / "narration_list.txt"
    listfile.write_text(
        "".join(f"file '{p.name}'\n" for p in pieces), "utf-8"
    )
    narration = work_dir / "narration.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c:a", "pcm_s16le", str(narration)], log=log, cancel=cancel)

    dur = duration(narration)
    chars = sum(len(p) for p in paragraphs)
    # 段落停顿不算进朗读时间，否则语速会被低估
    speech = max(dur - (len(paragraphs) - 1) * float(s.pause), 1e-3)
    rate = chars / speech
    log(f"旁白时长 {dur:.1f} 秒（{chars} 字，朗读 {rate:.1f} 字/秒）")
    if rate < 3.0:
        tip = "提示：正常中文朗读约 4~5 字/秒，现在明显偏慢。"
        if s.engine == "f5":
            tip += "除了「语速 %」，参考音频本身的语速也会被克隆一起学走。"
        else:
            tip += "把「语速 %」往 0 调一些会更自然。"
        log(tip)
    return narration

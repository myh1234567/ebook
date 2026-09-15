"""把各步串起来。

音频和视频分成两条入口：generate_audio() 只做到混音（用来先听听效果，
长文案生成一次要几十分钟，不该为了听一句话把画面也渲一遍），
generate() 在它之上接画面和成片。
"""
import re
import shutil
import time
from pathlib import Path

from ffmpeg_utils import ensure_ffmpeg
from tts import read_script, split_paragraphs, synth_narration
from video import prepare_background, mix_audio, compose


def _slug(name: str) -> str:
    s = re.sub(r"[^\w一-鿿-]+", "_", name).strip("_")
    return s or "sleep_video"


def _work_dir(s) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = Path(s.output_dir) / "_work" / f"{_slug(s.title)}_{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    return work


def _narrate_and_mix(s, work: Path, log, progress, cancel):
    """校验 → 切文案 → 合成旁白 → 混音。返回 (音轨路径, 成片总时长)。

    progress 在这里报 0~1；调用方要按自己的进度权重缩放。
    """
    ensure_ffmpeg()
    if not s.script_path or not Path(s.script_path).is_file():
        raise RuntimeError("请先选择文案 txt 文件")
    if s.engine == "f5" and not Path(s.ref_audio or "").is_file():
        raise RuntimeError("引擎选了「克隆声音」，请先选一个参考音频")

    paragraphs = split_paragraphs(read_script(s.script_path))
    if not paragraphs:
        raise RuntimeError("文案是空的")
    bgm = s.bgm_path if Path(s.bgm_path or "").is_file() else None
    log(f"文案 {len(paragraphs)} 段")

    log("① 合成旁白…")
    narration = synth_narration(
        paragraphs, work, s, log=log,
        progress=lambda p: progress(p * 0.85), cancel=cancel,
    )

    log("② 混音…")
    audio = work / "audio.m4a"
    total = mix_audio(narration, bgm, audio, s, log=log, cancel=cancel)
    progress(1.0)
    return audio, total


def generate_audio(s, log=print, progress=None, cancel=None) -> Path:
    """只出音频，用来先试听。"""
    progress = progress or (lambda _p: None)
    work = _work_dir(s)
    audio, total = _narrate_and_mix(s, work, log, progress, cancel)

    out = Path(s.output_dir) / f"{work.name}.m4a"
    shutil.copy(audio, out)
    log(f"音频完成：{out}（{total / 60:.1f} 分钟）")
    return out


def generate(s, log=print, progress=None, cancel=None) -> Path:
    """完整成片。"""
    progress = progress or (lambda _p: None)
    if not Path(s.video_path or "").is_file():
        raise RuntimeError("请先选择背景视频 mp4")

    work = _work_dir(s)
    # 先混音：拿到成片总时长，背景视频才知道要准备多长，不会白编码用不上的部分
    audio, total = _narrate_and_mix(
        s, work, log, lambda p: progress(p * 0.72), cancel)

    log("③ 准备背景视频…")
    loop_clip = prepare_background(s.video_path, work / "loop.mp4", total, s,
                                   log=log, cancel=cancel)
    progress(0.92)

    log("④ 合成成片…")
    final = Path(s.output_dir) / f"{work.name}.mp4"
    compose(loop_clip, audio, total, final, log=log, cancel=cancel)
    progress(1.0)

    log(f"完成：{final}（{total / 60:.1f} 分钟）")
    log(f"中间文件保留在：{work}")
    return final

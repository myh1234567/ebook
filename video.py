"""画面与音频合成。

核心性能决策：不对整段时长逐帧渲染。背景视频只规范化编码一次，再用 -stream_loop
无损循环到音频长度，成片阶段 -c:v copy。所以做一小时的成片，编码量只有背景视频本身那么长。
"""
from pathlib import Path

import math

from ffmpeg_utils import run, duration, measure, video_fps

# 旁白统一拉到这个平均电平，峰值不越过这个上限（都是 dBFS）。
# 不做归一化的话，旁白响度完全由 TTS 参考音频的录音电平决定——参考录得轻，成片就听不清。
TARGET_MEAN = -23.0
PEAK_CEILING = -1.5

# 统一成 1080p / 固定帧率 / yuv420p，源片是竖屏、4K、HEVC、VFR 都能吃
_FIT = ("scale={w}:{h}:force_original_aspect_ratio=increase,"
        "crop={w}:{h},fps={fps},setsar=1,format=yuv420p")


def prepare_background(src, out_path: Path, total: float, s,
                       log=print, cancel=None) -> Path:
    """把背景视频规范化成可循环的片段。

    只编码真正用得上的长度：源片比成片长就直接截断（不会循环，也就不需要接缝处理）；
    比成片短才会循环，这时把尾部和头部交叉淡化，接缝两侧正好是同一帧。
    """
    src = Path(src)
    if not src.is_file():
        raise RuntimeError(f"背景视频不存在：{src}")
    src_dur = duration(src)
    xf = float(s.xfade)
    # 帧率默认跟随源片：30fps 的素材硬转 24fps 会每 5 帧丢 1 帧，平移镜头明显发顿
    fps = float(s.fps) if int(s.fps) else (video_fps(src) or 24.0)
    fps = min(max(fps, 1.0), 120.0)
    if not int(s.fps):
        log(f"帧率跟随源视频：{fps:g} fps")
    fit = _FIT.format(w=int(s.width), h=int(s.height), fps=f"{fps:g}")
    enc = ["-r", f"{fps:g}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-g", str(int(fps * 2)), "-an"]  # -an 丢掉源片音轨

    will_loop = src_dur < total
    if will_loop and xf > 0 and src_dur > 2 * xf:
        # 尾 xf 秒和头交叉淡化。两个输入各自 seek，不用把整段缓存在内存里。
        log(f"背景视频 {src.name}（{src_dur:.0f}s）会循环 {total / src_dur:.1f} 次，"
            f"接缝做 {xf:.0f}s 交叉淡化")
        cmd = ["ffmpeg", "-y",
               "-ss", f"{src_dur - xf:.3f}", "-t", f"{xf:.3f}", "-i", str(src),
               "-t", f"{src_dur - xf:.3f}", "-i", str(src),
               "-filter_complex",
               f"[0:v]{fit},setpts=PTS-STARTPTS[tail];"
               f"[1:v]{fit},setpts=PTS-STARTPTS[body];"
               f"[tail][body]xfade=transition=fade:duration={xf:.3f}:offset=0[v]",
               "-map", "[v]"] + enc + [str(out_path)]
    else:
        keep = min(src_dur, total)
        if will_loop:
            log(f"背景视频 {src.name}（{src_dur:.0f}s）会循环播放")
        else:
            log(f"背景视频 {src.name}（{src_dur:.0f}s）截取前 {keep:.0f}s")
        cmd = ["ffmpeg", "-y", "-t", f"{keep:.3f}", "-i", str(src),
               "-vf", fit, "-map", "0:v"] + enc + [str(out_path)]

    run(cmd, log=log, cancel=cancel)
    return out_path


def mix_audio(narration: Path, bgm, out_path: Path, s, log=print, cancel=None) -> float:
    """旁白归一化到统一响度，BGM 按相对人声的比例垫底。返回成片总时长。"""
    narr = duration(narration)
    total = narr + float(s.lead_in) + float(s.tail)
    fade = min(float(s.bgm_fade), total / 3)
    lead_ms = int(float(s.lead_in) * 1000)
    stereo = "aformat=channel_layouts=stereo:sample_rates=48000"

    # 静态增益，不用动态压缩：段落间的静音不会被抬起来（助眠视频里那会变成恼人的底噪起伏）
    v_mean, v_peak = measure(narration)
    voice_gain = min(TARGET_MEAN - v_mean, PEAK_CEILING - v_peak)
    log(f"旁白响度 {v_mean:.1f}dB / 峰值 {v_peak:.1f}dB → 增益 {voice_gain:+.1f}dB")

    cmd = ["ffmpeg", "-y", "-i", str(narration)]
    voice = (f"[0:a]volume={voice_gain:.2f}dB,adelay={lead_ms}:all=1,apad,"
             f"atrim=0:{total:.3f},asetpts=N/SR/TB,{stereo}[voice]")

    if bgm:
        # BGM 的目标电平是「比归一化后的人声低多少」，而不是乘它自己的原始音量。
        # 否则换一首响度不同的 BGM，人声和背景的比例就全变了。
        vol = max(float(s.bgm_volume), 1e-4)
        b_mean, _ = measure(bgm)
        bgm_gain = (TARGET_MEAN + 20 * math.log10(vol)) - b_mean
        log(f"BGM {Path(bgm).name} 循环铺底：{b_mean:.1f}dB → 增益 {bgm_gain:+.1f}dB"
            f"（比人声低 {-20 * math.log10(vol):.0f}dB）")
        cmd += ["-stream_loop", "-1", "-i", str(bgm)]
        parts = [
            voice,
            f"[1:a]volume={bgm_gain:.2f}dB,"
            f"afade=t=in:st=0:d={fade:.2f},"
            f"afade=t=out:st={total - fade:.3f}:d={fade:.2f},"
            f"atrim=0:{total:.3f},asetpts=N/SR/TB,{stereo}[bg]",
            "[voice][bg]amix=inputs=2:duration=longest:normalize=0[a]",
        ]
    else:
        parts = [voice.replace("[voice]", "[a]")]
        log(f"只有旁白 {narr:.0f}s（没选 BGM）")

    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "[a]",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        str(out_path),
    ]
    run(cmd, log=log, cancel=cancel)
    return total


def compose(loop_clip: Path, audio: Path, total: float, out_path: Path,
            log=print, cancel=None) -> Path:
    """循环画面 + 混好的音轨 → 成片。视频流直接 copy，所以这一步很快。"""
    log(f"合成成片：{total / 60:.1f} 分钟")
    run([
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(loop_clip),
        "-i", str(audio),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy", "-c:a", "copy",
        "-t", f"{total:.3f}",
        "-movflags", "+faststart",
        str(out_path),
    ], log=log, cancel=cancel)
    return out_path

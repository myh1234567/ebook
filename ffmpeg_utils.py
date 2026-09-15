"""ffmpeg / ffprobe 的最小封装：跑命令、看时长、生成静音。"""
import subprocess
import shutil
from pathlib import Path


class Cancelled(Exception):
    pass


def ensure_ffmpeg() -> None:
    for exe in ("ffmpeg", "ffprobe"):
        if not shutil.which(exe):
            raise RuntimeError(f"找不到 {exe}，请先安装：brew install ffmpeg")


def run(cmd: list, log=print, cancel=None) -> None:
    """跑一条 ffmpeg 命令。失败时把 stderr 的尾部抛出来，方便定位。"""
    log(f"$ ffmpeg {' '.join(str(c) for c in cmd[1:])[:300]}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    err_tail = []
    for line in proc.stderr:
        err_tail.append(line)
        if len(err_tail) > 40:
            err_tail.pop(0)
        if cancel is not None and cancel.is_set():
            proc.kill()
            proc.wait()
            raise Cancelled()
    code = proc.wait()
    if code != 0:
        raise RuntimeError("ffmpeg 失败：\n" + "".join(err_tail))


def duration(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"ffprobe 读不出时长：{path}")
    return float(out.stdout.strip())


def video_fps(path) -> float:
    """读源视频的帧率。强行改帧率会让平移镜头发顿，所以默认跟随源片。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "default=nk=1:nw=1",
         str(path)],
        capture_output=True, text=True,
    )
    txt = out.stdout.strip()
    try:
        if "/" in txt:
            n, d = txt.split("/")
            return float(n) / float(d) if float(d) else 0.0
        return float(txt)
    except ValueError:
        return 0.0


def measure(path) -> tuple:
    """量一段音频的平均电平和峰值（dB）。用来把响度拉到统一水平。"""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    mean = peak = None
    for line in out.stderr.splitlines():
        if "mean_volume:" in line:
            mean = float(line.split("mean_volume:")[1].split("dB")[0])
        elif "max_volume:" in line:
            peak = float(line.split("max_volume:")[1].split("dB")[0])
    if mean is None or peak is None:
        raise RuntimeError(f"量不出音量：{path}")
    return mean, peak


def silence(path: Path, seconds: float, log=print) -> Path:
    """生成一段 48k 单声道静音 wav（和 TTS 片段格式一致，方便直接 concat）。"""
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
         "-t", f"{seconds:.3f}", "-c:a", "pcm_s16le", str(path)], log=log)
    return path

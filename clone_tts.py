"""F5-TTS 声音克隆：给一段参考音频，用同一个人的声音念文案。

两个缓存都是必要的，不然每段文案都要重载一次模型（十几秒）、重切一次参考音频：
- 模型加载一次常驻；
- 参考音频按 (文件, 起点) 缓存切好的片段。

torch / f5_tts 都是懒加载，否则 GUI 启动要多等十几秒。
"""
import random
from pathlib import Path

from ffmpeg_utils import run

REF_SECONDS = 12.0        # F5-TTS 的参考片段不宜过长，10~15 秒效果最好
_model = None
_ref_cache = {}


def _force_serial_batches(log=print):
    """让 F5-TTS 串行处理长文本的多个 batch。

    文本长到被切成 2 个以上 batch 时，F5-TTS 会用 ThreadPoolExecutor 并发推理
    （utils_infer.py 里那句 `with ThreadPoolExecutor() as executor`）。
    PyTorch 的 MPS 后端不支持多线程并发提交，会直接触发 Metal 断言
    "A command encoder is already encoding to this command buffer" 并杀掉整个进程。

    它本来就是按提交顺序收集结果的，所以限成单 worker 只是串行执行，输出不变。
    """
    try:
        import f5_tts.infer.utils_infer as ui
        if getattr(ui.ThreadPoolExecutor, "_serial", False):
            return
        base = ui.ThreadPoolExecutor

        class SerialExecutor(base):
            _serial = True

            def __init__(self, *_a, **kw):
                kw.pop("max_workers", None)
                super().__init__(max_workers=1, **kw)

        ui.ThreadPoolExecutor = SerialExecutor
    except Exception as exc:
        # 升级 f5-tts 后内部结构可能变了。不该因此起不来，但要说清楚风险。
        log(f"警告：没能给 F5-TTS 打串行补丁（{exc}）。"
            "长段文案可能触发 MPS 多线程崩溃，把段落改短些可以规避。")


def _get_model(log=print):
    global _model
    if _model is not None:
        return _model
    log("加载 F5-TTS 模型（首次会下载约 1.5GB，之后走缓存）…")
    from f5_tts.api import F5TTS
    _force_serial_batches(log=log)
    try:
        _model = F5TTS()
    except Exception as exc:      # MPS 上偶尔有算子不支持，退回 CPU
        log(f"默认设备加载失败（{exc}），改用 CPU…")
        _model = F5TTS(device="cpu")
    log("模型就绪。")
    return _model


def prepare_ref(s, log=print) -> Path:
    """把参考音频切成 24k 单声道的短片段。"""
    src = Path(s.ref_audio)
    if not src.is_file():
        raise RuntimeError(f"参考音频不存在：{src}")
    key = (str(src), float(s.ref_start))
    if key in _ref_cache:
        return _ref_cache[key]

    out = src.parent / f".ref_{src.stem}_{int(float(s.ref_start))}s.wav"
    run(["ffmpeg", "-y", "-ss", f"{float(s.ref_start):.2f}", "-i", str(src),
         "-t", f"{REF_SECONDS:.2f}", "-ar", "24000", "-ac", "1",
         "-c:a", "pcm_s16le", str(out)], log=lambda _m: None)
    log(f"参考音频：{src.name} 第 {s.ref_start:.0f}~{s.ref_start + REF_SECONDS:.0f} 秒")
    if not s.ref_text.strip():
        log("提示：参考文本留空，会自动转写参考音频。转写有偏差会让时长估计失准，"
            "念出来拖沓含混——手动填上那句话能明显改善。")
    _ref_cache[key] = out
    return out


def speak(text: str, out_wav: Path, s, speed: float = 1.0, log=print) -> Path:
    """用克隆的声音念一段文字，输出 24k wav。

    speed < 1 让模型直接用慢语速生成——比事后拉伸自然，因为声音本来就是慢着说出来的。
    """
    model = _get_model(log=log)
    ref = prepare_ref(s, log=log)
    model.infer(
        ref_file=str(ref),
        ref_text=s.ref_text.strip(),   # 留空则 F5-TTS 自动转写参考音频
        gen_text=text,
        file_wave=str(out_wav),
        speed=speed,
        remove_silence=False,          # 首尾静音由我们自己用 ffmpeg 裁，更可控
        show_info=lambda *_a, **_k: None,
        # 必须显式给种子：F5-TTS 不给种子时会取 random.randint(0, sys.maxsize)，
        # 而它的 seed_everything() 把种子塞进 os.environ["PYTHONHASHSEED"]，
        # 那个变量的上限是 2**32-1，超了之后本进程再也起不了 Python 子进程。
        seed=random.randint(0, 2 ** 32 - 1),
    )
    if not out_wav.exists() or out_wav.stat().st_size == 0:
        raise RuntimeError("F5-TTS 没有生成音频")
    return out_wav

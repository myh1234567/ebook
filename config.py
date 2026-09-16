"""配置：全部可调参数集中在这里，GUI 读写，settings.json 持久化。"""
from dataclasses import dataclass, asdict, fields
from pathlib import Path
import json

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "settings.json"

# edge-tts 里适合助眠的几个音色（可以在界面直接手填别的）
VOICES = [
    "zh-CN-XiaoxiaoNeural",   # 中文女声，温柔
    "zh-CN-XiaoyiNeural",     # 中文女声，偏轻
    "zh-CN-YunxiaNeural",     # 中文男声，偏少年
    "zh-CN-YunjianNeural",    # 中文男声，低沉
    "en-US-AriaNeural",       # 英文女声
    "en-US-JennyNeural",      # 英文女声，柔和
    "en-GB-SoniaNeural",      # 英音女声，很适合 ASMR
    "en-US-GuyNeural",        # 英文男声
]


@dataclass
class Settings:
    # ---------- 视频生成模块参数 ----------
    script_path: str = ""
    video_path: str = str(APP_DIR / "assets" / "video" / "test_loop.mp4")
    bgm_path: str = str(APP_DIR / "assets" / "bgm" / "test_pad.mp3")
    output_dir: str = str(APP_DIR / "output")
    title: str = "sleep_story"

    # 语音
    engine: str = "edge"     # edge = 内置音色；f5 = 用参考音频克隆声音
    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: int = -25          # 语速百分比，负数=慢
    pitch: int = -2          # 音调 Hz，负数=低沉（仅 edge 引擎）

    # 声音克隆（engine = f5 时生效）
    ref_audio: str = ""      # 参考音频
    ref_text: str = ""       # 参考文本
    ref_start: float = 0.0   # 起点秒数
    slow_mode: str = "engine"
    pause: float = 2.5       # 段落静音秒
    lead_in: float = 4.0     # 开头留白秒
    tail: float = 10.0       # 结尾留白秒

    # 画面
    xfade: float = 3.0       # 接缝淡化秒
    fps: int = 0             # 0 = 跟随源
    width: int = 1920
    height: int = 1080

    # 配乐
    bgm_volume: float = 0.12
    bgm_fade: float = 8.0

    # ---------- 小说跨文化改编模块参数 ----------
    novel_source_file: str = ""      # 单本模式用；批量模式走下面的 Drive 设置
    # 批量：Drive 文件夹里每个 txt 是一本待改编的书，一本一本串行跑
    gdrive_folder: str = ""          # 文件夹名或文件夹 ID
    gdrive_sa_json: str = ""         # 服务账号密钥路径，留空则在项目根目录自动找
    batch_gap_minutes: float = 0.0   # 每本之间隔多久，防止短时间大量上架触发 KDP 风控
    novel_auto_detect: bool = True   # 开始改编前先让模型读原文，自动判定下面这 5 项设定
    novel_orig_era: str = "中国古代/近代"
    novel_target_country: str = "United States"
    novel_target_era: str = "Late 19th Century (1890s)"
    novel_genre: str = "Historical Fiction / Romance"
    novel_target_audience: str = "Adult English Fiction Readers"
    novel_author: str = ""   # 留空则按题材自动取一个英文笔名，每本书各取各的
    novel_title: str = ""
    novel_subtitle: str = ""
    novel_series: str = ""

    # ---------- 大模型设置 ----------
    llm_provider: str = "api"        # api = 走 API Key 计费；cli = 走本机 CLI 的订阅额度
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_cli_command: str = "agy"     # 本机 CLI 可执行文件，用 <cmd> --model X -p "..." 调用
    llm_cli_model: str = "gemini-3.1-pro-high"
    # 主 CLI 的参数格式。{model} / {prompt} 会替换成实际值；模型留空时，
    # 含 {model} 的那项连同它前面的开关会被整段去掉。
    # 各家 CLI 对同一个短参数的含义完全不同（codex 的 -p 是 --profile 不是 --print），
    # 所以这里必须可配，不能写死。
    #   agy / claude / gemini :  --model {model} -p {prompt}
    #   codex                 :  exec --skip-git-repo-check --model {model} {prompt}
    # codex 那个 --skip-git-repo-check 是必须的：_call_cli 故意在系统临时目录里跑
    # （不让这类自带文件工具的 CLI 看见项目目录），而临时目录永远不是受信任目录，
    # 不加这个 flag 每次都会被 codex 拒掉。
    llm_cli_args: str = "--model {model} -p {prompt}"
    llm_fallback_cli: str = "claude"        # 主 CLI 连续失败后顶上的第二个 CLI
    llm_fallback_cli_model: str = ""        # 留空用它自己的默认模型
    llm_fallback_cli_args: str = "--model {model} -p {prompt}"   # 备用 CLI 的参数格式
    use_mock_adaptation: bool = False
    # 章节并发数。每章调用互相独立，并发不影响质量、只影响速度。
    # 实测单次调用约 16 秒且与推理档位无关，所以并发是唯一有效的提速手段。
    # 太高会集中触发限流。日志里频繁出现「第 N 次失败…秒后重试」就说明该调小。
    chapter_workers: int = 20
    # 分卷：把一本长篇中文小说切成 3-8 本英文书当系列发（第一本免费引流）。
    # 切点按剧情走，不是按字数平均分。关掉就只出全书一本。
    split_volumes: bool = True

    # ---------- Amazon KDP 上传与上架参数 ----------
    kdp_project_dir: str = ""
    kdp_price: float = 2.99
    kdp_royalty: str = "70%"
    kdp_marketplace: str = "amazon.com"
    kdp_chrome_profile: str = ""
    kdp_auto_upload: bool = False    # 改编跑完自动开 Chrome 建草稿、传正文封面、填定价
    kdp_auto_publish: bool = False   # 连 Publish 也自动点——不可逆，默认关

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text("utf-8"))
            except Exception:
                return s
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(s, k, v)
        return s

    def save(self) -> None:
        SETTINGS_FILE.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), "utf-8"
        )

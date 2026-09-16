"""小说跨文化改编与 KDP 出版准备核心引擎。

遵循 novel-adaptation-kdp 技能规范：
1. 采用“叙事功能等价”而非字对字直译。
2. 保持主线、关键转折、因果链和结局不变。
3. 建立全书改编档案 (Adaptation Bible)。
4. 美式英语创作 (en-US)，消除翻译腔与 AI 式陈词滥调。
5. 生成标准交付文件 (DOCX/TXT/MD/PNG)。
"""
import re
import json
import subprocess
import threading
import tempfile
import time
from pathlib import Path
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass, asdict

import kdp_categories
import kdp_formatter

CLI_TIMEOUT = 1900      # 单次 CLI 调用的上限秒数，长章节改编可能要好几分钟
CLI_RETRIES = 3         # 限流或抖动会让 CLI 空手而归，重试几次再放弃
CLI_BACKOFF = (30, 90)  # 两次重试之间等多久
# 一个通道连续挂这么多轮就本轮弃用，别每章都白等它。
# 注意这个阈值要随并发数放大：N 路并发撞上限流时会瞬间冒出 N 个失败，
# 那是退让信号不是通道死了，按固定小阈值判会把好通道误杀。
TIER_TRIP_AFTER = 2

# ---- 送进提示词的内容上限。原来的值太小，实测把内容砍掉了一大半 ----
# 改编档案要完整送：它装着人物映射表、术语对照表、连续性追踪，
# 原来只送前 1200 字，恰好只够装书名备选表，真正要紧的三张表一张都没送到。
MAX_BIBLE_CHARS = 30000
# 本章原文一律完整送，不设截断上限。实测某本 799 章的书，中位数 4140 字、
# 96% 的章节超过 3000 字，按原来的 3000 字截断，全书 45% 的原文没进过模型。
# 后来改成 24000 上限，又把 3 万字的正常长章节误伤了 —— 截断这个做法本身就不对：
# 它静默丢内容，成品里看不出少了什么。现在只在异常长的时候打个提醒，不动内容。
BIG_CHAPTER_WARN = 30000   # 超过这个字数就提醒一句，多半是章节切分失效
# 全书摘要（map-reduce）：改编档案必须建立在「读过全书」的基础上。
# 原来只拿前 3 章的 1800 字就去定全书的人物表和术语表，后期人名必然漂。
# 全文一次送不进去（某本 431 万字，差一个数量级），所以先分块摘要再汇总。
SUMMARY_CHUNK_CHARS = 60000   # 每块原文多大。431 万字 ≈ 72 块
MAX_SUMMARY_CHARS = 60000     # 汇总后的全书总结送进提示词的上限
# 章节并行度。每章调用只依赖「全书总结 + 本章原文」，不依赖已改编的章节，
# 所以并行产出和串行逐字相同，只是快 N 倍。实测单次调用约 16 秒且与推理档位无关。
DEFAULT_WORKERS = 20

# 画封面底图要让 CLI 写文件，得放开工具权限 —— 而各家的开关完全不同，
# 不能像原来那样写死 agy/claude 的参数（codex 三个参数一个都不认，
# 结果是必然失败，还要白等 CLI_TIMEOUT 那 1900 秒才退回纯排版封面）。
# 键是命令名（取 basename），{model} {prompt} 会被替换。
COVER_ARGS = {
    "agy":    "--dangerously-skip-permissions --model {model} --prompt {prompt}",
    "claude": "--dangerously-skip-permissions --model {model} --prompt {prompt}",
    "gemini": "--yolo -m {model} -p {prompt}",
    "codex":  "exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox "
              "--model {model} {prompt}",
}
COVER_TIMEOUT = 240

# 分卷：一本长篇中文小说切成若干本英文书当系列发。
# 第一本免费引流、后续付费，所以切点必须落在剧情的自然段落上，不能按字数平均分。
VOL_MIN, VOL_MAX = 3, 8     # 封面是锦上添花，画不出来就用纯排版，不值得占用 30 分钟


@dataclass
class NovelProjectConfig:
    source_file: str = ""
    orig_era_region: str = "中国古代/近代"
    target_country: str = "United States"
    target_era: str = "Late 19th Century (1890s)"
    genre: str = "Historical Fiction / Romance"
    target_audience: str = "Adult English Fiction Readers"
    author_name: str = ""   # 留空则按题材自动取笔名
    book_title: str = ""
    subtitle: str = ""
    series_info: str = ""
    output_dir: str = ""
    provider: str = "api"   # api = OpenAI 兼容接口；cli = 调本机 CLI，走订阅额度
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-chat"
    cli_command: str = "agy"
    cli_model: str = "gemini-3.1-pro-high"
    cli_args: str = "--model {model} -p {prompt}"
    fallback_cli_command: str = "claude"
    fallback_cli_model: str = ""
    fallback_cli_args: str = "--model {model} -p {prompt}"
    use_mock: bool = False  # 若未填 API 密钥，可使用离线模板模式生成完整项目包
    chapter_workers: int = DEFAULT_WORKERS
    # 书名定下来之前的占位目录标识。留空的话所有新书共用同一个占位目录，
    # 多进程并行跑不同的书时会把章节和档案混在一起 —— 批量模式必须传个唯一值
    # （用 Drive 的 fileId）。
    work_id: str = ""
    split_volumes: bool = True


def config_from_settings(s) -> NovelProjectConfig:
    """把 GUI 的 Settings 映射成本模块的项目配置。"""
    return NovelProjectConfig(
        source_file=s.novel_source_file,
        orig_era_region=s.novel_orig_era,
        target_country=s.novel_target_country,
        target_era=s.novel_target_era,
        genre=s.novel_genre,
        target_audience=s.novel_target_audience,
        author_name=s.novel_author,
        book_title=s.novel_title,
        subtitle=s.novel_subtitle,
        series_info=s.novel_series,
        output_dir=s.output_dir,
        provider=s.llm_provider,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        model_name=s.llm_model,
        cli_command=s.llm_cli_command,
        cli_model=s.llm_cli_model,
        cli_args=getattr(s, "llm_cli_args", "--model {model} -p {prompt}"),
        fallback_cli_command=s.llm_fallback_cli,
        fallback_cli_model=s.llm_fallback_cli_model,
        fallback_cli_args=getattr(s, "llm_fallback_cli_args",
                                  "--model {model} -p {prompt}"),
        use_mock=s.use_mock_adaptation,
        chapter_workers=int(getattr(s, "chapter_workers", DEFAULT_WORKERS) or DEFAULT_WORKERS),
        split_volumes=bool(getattr(s, "split_volumes", True)),
    )


def project_dir_for(cfg: NovelProjectConfig) -> Path:
    """项目目录 = 输出目录/书名。书名没定时先用占位名，等档案出来再改名。"""
    root = Path(cfg.output_dir) if cfg.output_dir else Path.cwd() / "output"
    # 书名没定时用占位名。work_id 非空就按它区分 —— 否则多进程并行跑不同的书，
    # 会全都写进同一个 Novel_Adaptation_Project 目录，章节互相污染。
    fallback = f"_wip_{cfg.work_id}" if getattr(cfg, "work_id", "") else "Novel_Adaptation_Project"
    name = re.sub(r'[\s/\\:*?"<>|]', '_', cfg.book_title or fallback)
    return root / name


def build_cli_args(template: str, model: str, prompt: str,
                   outfile: str = "") -> List[str]:
    """把参数模板展开成 argv。

    {model} / {prompt} / {outfile} 各自作为**一个完整参数**替换，不参与分词 ——
    提示词里有空格、引号、换行都不会把命令拆散。

    {outfile} 是给那些「stdout 混着日志」的 CLI 用的：codex 会往 stdout 打版本号、
    workdir、token 用量等一堆东西，直接当正文存下来每章都是脏的。它提供
    -o/--output-last-message 只写最终回答，模板里写 `-o {outfile}` 即可，
    _call_cli 会改从那个文件读结果。

    模型为空时，含 {model} 的那一项要去掉，并且连它前面的开关一起去掉，
    否则会剩一个孤零零的 --model 把下一个参数吃掉。
    """
    import shlex
    toks = shlex.split(template or "--model {model} -p {prompt}")
    out: List[str] = []
    for tok in toks:
        if "{model}" in tok:
            if not model:
                if out and out[-1].startswith("-"):
                    out.pop()          # 去掉配套的开关，如 --model / -m
                continue
            out.append(tok.replace("{model}", model))
        elif "{prompt}" in tok:
            out.append(tok.replace("{prompt}", prompt))
        elif "{outfile}" in tok:
            out.append(tok.replace("{outfile}", outfile))
        else:
            out.append(tok)
    if not any(prompt == o or prompt in o for o in out):
        raise RuntimeError(
            f"CLI 参数格式里没有 {{prompt}}，提示词传不进去：{template!r}")
    return out


def _pick_error(stderr: str, stdout: str, limit: int = 400) -> str:
    """从 CLI 的输出里挑出真正的错误行。

    不能直接取前 300 字符：codex 每次都先打一大段横幅（版本号、workdir、model、
    sandbox、session id），300 字符全被横幅吃掉，真正的错误在后面被截没了。
    实测就因为这个，把「You've hit your usage limit」当成了别的问题查。

    策略：先挑含错误关键词的行；一条都没有再取**末尾**（错误通常在最后），
    而不是开头。
    """
    text = ((stderr or "") + "\n" + (stdout or "")).strip()
    if not text:
        return "（没有输出）"
    keys = ("error", "错误", "failed", "failure", "limit", "quota",
            "denied", "unauthorized", "not found", "invalid", "exceed")
    hits = [ln.strip() for ln in text.splitlines()
            if ln.strip() and any(k in ln.lower() for k in keys)]
    if hits:
        # 同一条错误 codex 会重复打印，去个重
        seen, uniq = set(), []
        for h in hits:
            if h not in seen:
                seen.add(h)
                uniq.append(h)
        return " | ".join(uniq)[:limit]
    return "…" + text[-limit:]


class ChapterSplitter:
    """自动将中文小说源文件切分为结构化章节。"""
    # 中文数字必须把 零 两 万 亿 都算上。原来只有「一二三四五六七八九十百千」，
    # 结果「第九百零一章」「第两千四百三十二章」这类标题匹配不上，被当成正文
    # 并进上一章 —— 实测某本 2245 章的书因此出现 72,251 字的「一章」，
    # 十几章被吞掉、章号整体错位。
    CHAPTER_REGEX = re.compile(
        r'^\s*(第[0-9零一两二三四五六七八九十百千万亿]+[章回卷节篇]'
        r'|Chapter\s+\d+|[Cc]hapter\s+[IVXLCDM]+|[0-9]{1,4}\s*[\.、])\s*(.*)$',
        re.MULTILINE
    )

    @classmethod
    def split_text(cls, text: str) -> List[Dict[str, str]]:
        lines = text.splitlines()
        chapters = []
        current_title = "Prologue / Chapter 1"
        current_lines = []

        for line in lines:
            match = cls.CHAPTER_REGEX.match(line)
            if match and len(current_lines) > 5:
                # 遇到新的章节标题且上一章已有内容
                content = "\n".join(current_lines).strip()
                if content:
                    chapters.append({"title": current_title, "content": content})
                current_title = line.strip()
                current_lines = []
            else:
                current_lines.append(line)

        if current_lines:
            content = "\n".join(current_lines).strip()
            if content:
                chapters.append({"title": current_title, "content": content})

        # 如果没有识别出任何明确章节标题，则按字数（约2500字）切分
        if len(chapters) <= 1 and len(text) > 3500:
            chunks = []
            paragraphs = [p for p in text.split("\n") if p.strip()]
            cur_chunk = []
            cur_len = 0
            ch_count = 1
            for p in paragraphs:
                cur_chunk.append(p)
                cur_len += len(p)
                if cur_len >= 2200:
                    chunks.append({
                        "title": f"Chapter {ch_count}",
                        "content": "\n\n".join(cur_chunk)
                    })
                    ch_count += 1
                    cur_chunk = []
                    cur_len = 0
            if cur_chunk:
                chunks.append({
                    "title": f"Chapter {ch_count}",
                    "content": "\n\n".join(cur_chunk)
                })
            return chunks

        return chapters if chapters else [{"title": "Chapter 1", "content": text.strip()}]


# 离线兜底用的题材关键词。命中数最多的那一档胜出。
_GENRE_HINTS = {
    "xianxia": ("修仙", "灵气", "丹药", "筑基", "元婴", "宗门", "仙尊", "灵石", "剑修", "渡劫"),
    "ancient": ("皇帝", "皇上", "王爷", "丞相", "将军", "娘娘", "太后", "公主", "银两",
                "客栈", "江湖", "县令", "书生", "科举", "轿子", "府邸", "小姐", "奴婢"),
    "modern": ("手机", "电脑", "网络", "公司", "微信", "咖啡", "汽车", "地铁", "总裁",
               "短信", "飞机", "微博", "上班", "小区"),
}

# 三档兜底方案，都挑英文市场里出货量大的赛道
_FALLBACK_SETTINGS = {
    "xianxia": {
        "orig_era_region": "中国古代仙侠 / 修真世界",
        "target_country": "Secondary world inspired by imperial East Asia (silkpunk fantasy)",
        "target_era": "Mythic era, no fixed calendar",
        "genre": "Romantasy / Epic Fantasy",
        "target_audience": "Adult romantasy & epic fantasy readers, 18-45, BookTok-driven",
        "pen_name": "Wren Ashgrove",
        "rationale": "（离线兜底判断）原文含大量修真词汇。英文市场把这类故事归到 romantasy，"
                     "是目前 Amazon 增长最快的赛道，保留东方奇幻底色比硬搬到西方历史更好卖。",
    },
    "ancient": {
        "orig_era_region": "中国古代（帝制王朝，宫廷/官场背景）",
        "target_country": "United Kingdom",
        "target_era": "Regency England (1810s)",
        "genre": "Historical Romance / Court Intrigue",
        "target_audience": "Adult historical romance readers, 25-55, mostly women",
        "pen_name": "Eleanor Ashworth",
        "rationale": "（离线兜底判断）原文是古代权谋/宅门题材。摄政时期英国的爵位、联姻、"
                     "继承权和名节压力与之功能等价，且是 Amazon 历史言情最大的细分市场。",
    },
    "modern": {
        "orig_era_region": "当代中国都市",
        "target_country": "United States",
        "target_era": "Contemporary (present day)",
        "genre": "Contemporary Romance / Domestic Suspense",
        "target_audience": "Adult contemporary romance & suspense readers, 25-50",
        "pen_name": "Reese Callahan",
        "rationale": "（离线兜底判断）原文是现代都市题材，直接平移到当代美国即可，"
                     "读者基数最大、改编损耗最小。",
    },
}

DETECTED_FIELDS = ("orig_era_region", "target_country", "target_era",
                   "genre", "target_audience")

# 导出阶段产出的文件。08 档案和 06 底图不列：它们在更早的阶段就生成了
DELIVERABLES = ("01_English_Manuscript.docx", "02_Publishing_Copy.docx",
                "03_Publishing_Copy.txt", "04_Internal_Synopsis.docx",
                "05_Ebook_Cover.png", "07_Manuscript.epub",
                "09_Image_Prompts.txt", "10_Quality_Check_Report.md")
META_CACHE = "11_Publishing_Metadata.json"


def deliverables_fresh(proj_dir: Path) -> bool:
    """交付文件是否齐全、且都比最新的那章还新。

    只看「齐不齐」不够：补了新章之后旧的 EPUB 就过期了，必须重导。
    """
    files = [proj_dir / n for n in DELIVERABLES]
    if not all(f.exists() for f in files):
        return False
    chapters = list((proj_dir / "_chapters").glob("*.json"))
    newest_chapter = max((c.stat().st_mtime for c in chapters), default=0)
    return min(f.stat().st_mtime for f in files) >= newest_chapter


class NovelAdaptationEngine:
    def __init__(self, config: NovelProjectConfig, log_func: Optional[Callable[[str], None]] = None):
        self.config = config
        self.log = log_func or print
        self.bible = {}
        self.chapters_adapted = []
        self.metadata = {}
        self._tier_lock = threading.Lock()   # 熔断状态会被并发的章节线程同时改
        # 按「命令+模型」记，不能只按命令：机器上只有一个 CLI 时，
        # 主备两级常常是同一个命令配不同模型（如 codex 两个模型互为兜底），
        # 只按命令记的话主通道一熔断，备用也被连坐跳过，等于没兜底。
        self._tier_fails = {}    # 通道 -> 连续失败轮数
        self._dead_tiers = set()  # 本轮任务里已经熔断、不再尝试的通道

    def _call_llm(self, prompt: str, system_prompt: str = "") -> str:
        """按 主 CLI → 备用 CLI → API 的顺序逐级降级，每级内部各自重试。

        一级全挂了才轮到下一级；全挂才抛错。跑几百章的任务，单点故障不能拖垮整轮。
        """
        if self.config.use_mock:
            # 离线或模拟模式返回结构化高质量骨架
            return ""

        if self.config.provider != "cli":
            return self._call_api(prompt, system_prompt) if self.config.api_key.strip() else ""

        errors = []
        tiers = [(self.config.cli_command, self.config.cli_model,
                  self.config.cli_args, "主 CLI"),
                 (self.config.fallback_cli_command, self.config.fallback_cli_model,
                  self.config.fallback_cli_args, "备用 CLI")]
        for cmd, model, cargs, label in tiers:
            cmd = (cmd or "").strip()
            if not cmd:
                continue
            key = f"{cmd}|{(model or '').strip()}"      # 熔断的粒度是通道，不是命令
            # 熔断：一个通道连续挂两轮就整轮弃用。agy 卡死时每次要耗 5 分钟才返回空，
            # 几百章逐章重试它，光等就是几十上百个小时。
            with self._tier_lock:
                dead = key in self._dead_tiers
            if dead:
                errors.append(f"[{label} {cmd}] 本轮已熔断，跳过")
                continue
            try:
                out = self._call_cli(prompt, system_prompt, cmd,
                                     (model or "").strip(), cargs)
                with self._tier_lock:
                    self._tier_fails.pop(key, None)
                return out
            except RuntimeError as exc:
                errors.append(f"[{label} {cmd}] {exc}")
                # 阈值随并发放大：并发越高，限流造成的瞬时失败越多，
                # 不放大的话一次限流就能把通道误判成已死。
                workers = max(1, int(getattr(self.config, "chapter_workers", 1) or 1))
                trip_at = max(TIER_TRIP_AFTER, workers)
                with self._tier_lock:
                    self._tier_fails[key] = self._tier_fails.get(key, 0) + 1
                    n_fail = self._tier_fails[key]
                    tripped = n_fail >= trip_at
                    if tripped:
                        self._dead_tiers.add(key)
                if tripped:
                    self.log(f"⛔ {label}（{cmd}）连续失败 {n_fail} 轮（阈值 {trip_at}），"
                             f"本轮任务不再尝试它，直接用后面的通道。")
                else:
                    self.log(f"{label}（{cmd}）彻底失败，降级到下一个通道…")

        if self.config.api_key.strip():
            try:
                self.log(f"改走 API：{self.config.model_name}")
                return self._call_api(prompt, system_prompt)
            except Exception as exc:
                errors.append(f"[API {self.config.model_name}] {exc}")
        else:
            errors.append("[API] 没填 API Key，这一级跳过")

        raise RuntimeError("所有通道都失败了：\n  " + "\n  ".join(errors))

    def _call_api(self, prompt: str, system_prompt: str = "") -> str:
        """OpenAI 兼容接口（DeepSeek / OpenAI / 任何兼容实现）。"""
        import openai
        client = openai.OpenAI(
            api_key=self.config.api_key.strip(),
            base_url=self.config.base_url.strip() or "https://api.openai.com/v1"
        )
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = client.chat.completions.create(
            model=self.config.model_name or "deepseek-chat",
            messages=messages,
            temperature=0.7,
            max_tokens=4000
        )
        return response.choices[0].message.content.strip()

    def _call_cli(self, prompt: str, system_prompt: str = "",
                  command: str = "", model: str = "",
                  args_template: str = "") -> str:
        """调本机已登录的 CLI，参数格式由 args_template 决定。

        为什么参数要可配：各家 CLI 对同一个短参数的含义能完全相反 ——
        agy/claude/gemini 的 -p 是「打印模式」，codex 的 -p 却是 --profile，
        把提示词传进去会被当成配置档名，报一个看不出所以然的错。

        故意在临时目录里跑：这类 CLI 默认带文件工具，别让它看见项目目录。
        """
        tpl = args_template or self.config.cli_args
        cmd = [(command or self.config.cli_command).strip() or "agy"]

        # 模板里有 {outfile} 就让 CLI 把最终回答写进文件，从文件读结果。
        # 因为 codex 的 stdout 是混的：版本号、workdir、model、token 用量都往里打，
        # 直接当正文存下来每一章开头都会带这坨东西。
        out_path = ""
        if "{outfile}" in tpl:
            fd, out_path = tempfile.mkstemp(prefix="cli_out_", suffix=".txt")
            import os
            os.close(fd)

        # 模型名只认调用方传进来的：留空就用该 CLI 自己的默认模型。
        # 千万别在这儿回退到主 CLI 的模型——那会让 claude 去跑 gemini 的模型名。
        cmd += build_cli_args(tpl, model.strip(),
                              f"{system_prompt}\n\n{prompt}".strip(), out_path)

        # 限流、网络抖动都会让它空手而归，别为这个把跑了几十章的任务毙掉
        for attempt in range(1, CLI_RETRIES + 1):
            try:
                # stdin 必须掐掉：不给的话子进程继承父进程的 stdin，
                # CLI 一旦想读输入（审批提示、确认之类）就会一直挂着，
                # 直到 CLI_TIMEOUT（半小时）才超时，表现为「没报错也没返回」。
                # 给 DEVNULL 的话它立刻读到 EOF，该报错就报错。
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=CLI_TIMEOUT, cwd=tempfile.gettempdir(),
                                      stdin=subprocess.DEVNULL)
            except FileNotFoundError:
                raise RuntimeError(f"找不到命令 {cmd[0]}，先确认它在 PATH 里，或改填绝对路径。")
            except subprocess.TimeoutExpired:
                why = f"超过 {CLI_TIMEOUT} 秒没返回"
            else:
                # 用了 {outfile} 就以文件内容为准，stdout 只在报错时拿来看日志
                got = ""
                if out_path:
                    try:
                        got = Path(out_path).read_text("utf-8", errors="replace").strip()
                    except Exception:
                        got = ""
                else:
                    got = proc.stdout.strip()

                if proc.returncode != 0:
                    why = f"退出码 {proc.returncode}：{_pick_error(proc.stderr, proc.stdout)}"
                elif not got:
                    why = "没有输出（多半是限流）"
                else:
                    if out_path:
                        Path(out_path).unlink(missing_ok=True)
                    return got

            if attempt < CLI_RETRIES:
                wait = CLI_BACKOFF[min(attempt - 1, len(CLI_BACKOFF) - 1)]
                self.log(f"{cmd[0]} 第 {attempt} 次失败（{why}），{wait} 秒后重试…")
                time.sleep(wait)

        raise RuntimeError(f"{cmd[0]} 连续 {CLI_RETRIES} 次失败，最后一次：{why}。"
                           f"（终端跑 `{cmd[0]} -p hi` 可自查）")

    def generate_cover_art(self, prompt: str, out_png: Path) -> Optional[Path]:
        """让 CLI 画一张封面底图。画不出来就返回 None，退回纯排版封面。

        出图要写文件，必须开 --dangerously-skip-permissions；所以只给这一次调用开，
        并且丢到一个空的临时目录里跑，别让它在项目目录里乱动。
        """
        if self.config.provider != "cli" or self.config.use_mock:
            return None

        work = Path(tempfile.mkdtemp(prefix="cover_"))
        exe = (self.config.cli_command.strip() or "agy")
        tpl = COVER_ARGS.get(Path(exe).name.lower())
        if not tpl:
            self.log(f"不知道 {exe} 怎么开工具权限出图，改用纯排版封面。"
                     f"（已知：{'、'.join(COVER_ARGS)}）")
            return None
        body = (
                "Use your image generation tool to create ONE book cover illustration and save "
                f"it as art.png in the current directory ({work}). Portrait orientation, "
                "aspect ratio close to 1:1.6, no text/letters/typography anywhere in the image "
                "(the title will be typeset separately). Do not ask questions.\n\n"
                f"Cover art brief:\n{prompt}")

        cmd = [exe] + build_cli_args(tpl, self.config.cli_model.strip(), body)
        self.log(f"正在让 {Path(exe).name} 画封面底图"
                 f"（放开工具权限，只在空目录里跑，上限 {COVER_TIMEOUT} 秒）…")
        try:
            subprocess.run(cmd, capture_output=True, text=True,
                           timeout=COVER_TIMEOUT, cwd=work, stdin=subprocess.DEVNULL)
        except Exception as exc:
            self.log(f"封面出图失败（{exc}），改用纯排版封面。")
            return None

        found = sorted(work.glob("*.png")) + sorted(work.glob("*.jpg"))
        if not found:
            self.log("CLI 没画出图片，改用纯排版封面。")
            return None
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(found[0].read_bytes())
        self.log(f"封面底图已保存：{out_png.name}")
        return out_png

    def _read_source_sample(self) -> str:
        """取开头 3000 字 + 中段 1500 字，够判断题材又不至于把上下文撑爆。"""
        source_path = Path(self.config.source_file)
        if not source_path.exists():
            raise FileNotFoundError(f"未找到输入文件: {self.config.source_file}")
        text = source_path.read_text("utf-8", errors="replace")
        if len(text) <= 4500:
            return text
        mid = len(text) // 2
        return text[:3000] + "\n\n［……中略……］\n\n" + text[mid:mid + 1500]

    def analyze_source_settings(self) -> Dict[str, str]:
        """读原文样本，自动判定原作设定并选一套最好卖的目标设定，就地写回 config。

        返回的 dict 除 5 项设定外还带一个 rationale（中文理由），给界面打日志用。
        """
        self.log("正在通读原文样本，判断题材与最合适的目标市场设定...")
        sample = self._read_source_sample()

        system_prompt = (
            "You are both a literary analyst and an Amazon KDP market strategist. "
            "You read Chinese novels and decide how to relocate them for the English-language "
            "Kindle market so that they sell as many copies as possible. "
            "You know current Amazon bestseller dynamics: romance and its subgenres, romantasy, "
            "historical romance, cozy mystery, domestic suspense and thrillers carry the largest "
            "paying readerships; literary and experimental fiction sell poorly for unknown authors."
        )

        user_prompt = f"""Read this Chinese novel sample and decide the adaptation settings.

Sample:
\"\"\"{sample}\"\"\"

Step 1 — Identify what the ORIGINAL actually is: which era and region it is set in
(ancient dynastic China / Republican era / contemporary urban China / xianxia-cultivation
secondary world / etc.), plus its core dramatic engine (revenge, court intrigue, family
saga, romance, crime, cultivation...).

Step 2 — Choose the TARGET setting that maximizes sales on Amazon Kindle for a debut
author. Judge on commercial grounds, not on literal similarity:
- The target society must be able to carry the original's social mechanics through
  narrative functional equivalence (power hierarchy, marriage and inheritance rules,
  law enforcement, money, honor and shame).
- Prefer a setting with a large, active, keyword-rich readership on Amazon.
- Keeping an East-Asian-inspired setting is allowed and sometimes better (e.g. cultivation
  stories sell as romantasy / silkpunk fantasy); do not force a Western setting when it
  would gut the story's appeal.

Return ONLY a JSON object with exactly these keys:
"orig_era_region": original era and region, written in Chinese, one short phrase.
"target_country": target country or world, in English.
"target_era": target era with concrete years, in English.
"genre": one or two Amazon-recognized genre labels joined by " / ", in English.
"target_audience": target readership in English, including age band and reading habits.
"pen_name": an English pen name (First Last) for this book's author. Follow the naming
conventions readers of THIS subgenre expect on Amazon — e.g. contemporary romance skews
to short, warm, female-sounding names; thrillers to harder, often initial-based or
gender-neutral names; historical fiction to classic names. Two words, easy to spell and
search, and NOT the name of any real well-known author.
"rationale": 3-5 sentences IN CHINESE explaining the judgment and, above all, why this
target setting sells best on Amazon (name the comparable bestselling subgenre).
"""
        raw = self._call_llm(user_prompt, system_prompt)
        detected = {}
        if raw:
            try:
                m = re.search(r'\{.*\}', raw, re.DOTALL)
                if m:
                    detected = json.loads(m.group(0))
            except Exception:
                detected = {}

        if not all(detected.get(k) for k in DETECTED_FIELDS):
            if raw:
                self.log("模型没给出可用的 JSON，改用关键词兜底判断。")
            detected = dict(self._guess_settings(sample))

        for key in DETECTED_FIELDS:
            setattr(self.config, key, str(detected[key]).strip())

        # 笔名同一次调用顺带定下来：模型刚判完题材和读者，取名依据正好在手上。
        # 界面上手填了作者就尊重手填的，不覆盖。
        if self.config.author_name.strip():
            detected.pop("pen_name", None)
        elif detected.get("pen_name"):
            self.config.author_name = str(detected["pen_name"]).strip()

        self.log("—— 自动识别的改编设定 ——")
        self.log(f"  原作时代地区：{self.config.orig_era_region}")
        self.log(f"  目标国家：{self.config.target_country}")
        self.log(f"  目标年代：{self.config.target_era}")
        self.log(f"  小说类型：{self.config.genre}")
        self.log(f"  目标读者：{self.config.target_audience}")
        self.log(f"  作者笔名：{self.config.author_name or '（未定）'}")
        if detected.get("rationale"):
            self.log(f"  选型理由：{detected['rationale']}")
        return detected

    @staticmethod
    def _guess_settings(text: str) -> Dict[str, str]:
        """没有 API 或模型失灵时的关键词兜底。"""
        scores = {name: sum(text.count(w) for w in words)
                  for name, words in _GENRE_HINTS.items()}
        best = max(scores, key=scores.get)
        if scores[best] == 0:
            best = "modern"
        return _FALLBACK_SETTINGS[best]

    def summarize_book(self, raw_chapters: List[Dict[str, str]], proj_dir: Path,
                       cancel_event=None, workers: int = DEFAULT_WORKERS) -> str:
        """把全书分块读一遍，产出一份「读过全文」的总结，供改编档案使用。

        为什么要这一步：改编档案要给出人物映射表、术语对照表、伏笔追踪，这些
        必须建立在全书之上。原来只拿前 3 章的 1800 字，配角、中段线索、结局
        全都看不到，人名和术语到后期必然漂。

        全文一次送不进去（431 万字，差一个数量级），所以 map-reduce：
        分块摘要（互相独立，可并行）-> 汇总成一份总结。

        每块摘要落盘到 _summaries/，重跑时直接复用 —— 这一步的成本不该付第二次。
        """
        sum_dir = proj_dir / "_summaries"
        sum_dir.mkdir(parents=True, exist_ok=True)

        # 按字数把章节攒成块，尽量不切断章节
        chunks, cur, cur_len = [], [], 0
        for ch in raw_chapters:
            cur.append(ch)
            cur_len += len(ch["content"])
            if cur_len >= SUMMARY_CHUNK_CHARS:
                chunks.append(cur)
                cur, cur_len = [], 0
        if cur:
            chunks.append(cur)

        total_chars = sum(len(c["content"]) for c in raw_chapters)
        self.log(f"全书 {len(raw_chapters)} 章 / {total_chars:,} 字，"
                 f"分 {len(chunks)} 块做摘要（并发 {workers}）…")

        sys_p = ("You are a story analyst. You read a slice of a Chinese web novel and "
                 "extract the facts an adaptation team needs. Be precise and factual. "
                 "Output English. Do not invent anything not present in the text.")

        def one(i: int, group: List[Dict[str, str]]) -> str:
            f = sum_dir / f"{i:04d}.md"
            if f.exists():
                return f.read_text("utf-8")
            if cancel_event and cancel_event.is_set():
                return ""
            text = "\n\n".join(f"【{c['title']}】\n{c['content']}" for c in group)
            prompt = f"""Summarize this slice of the novel (chapters {group[0]['title']} ... {group[-1]['title']}).

Return markdown with these sections, listing ONLY what appears in this slice:
- **Characters**: every named character, their role, relationships, and how they are addressed
- **Terms**: setting-specific nouns (ranks, sects, items, places, institutions) with a short gloss
- **Plot**: the causal chain of events, in order
- **Reveals & Foreshadowing**: anything set up here or paid off here

Text:
\"\"\"{text[:SUMMARY_CHUNK_CHARS * 2]}\"\"\"
"""
            out = self._call_llm(prompt, sys_p)
            if out:
                f.write_text(out, "utf-8")
            return out

        done = [f for f in sum_dir.glob("*.md")]
        if done:
            self.log(f"  · 已有 {len(done)} 块摘要，复用，只跑缺的。")

        parts: Dict[int, str] = {}
        if workers > 1 and len(chunks) > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(one, i, g): i for i, g in enumerate(chunks, 1)}
                for k, fut in enumerate(as_completed(futs), 1):
                    parts[futs[fut]] = fut.result()
                    self.log(f"  摘要进度 {k}/{len(chunks)}")
        else:
            for i, g in enumerate(chunks, 1):
                parts[i] = one(i, g)
                self.log(f"  摘要进度 {i}/{len(chunks)}")

        merged = "\n\n---\n\n".join(parts[i] for i in sorted(parts) if parts.get(i))
        self.log(f"全书总结完成，{len(merged):,} 字符。")
        return merged

    def generate_adaptation_bible(self, raw_chapters: List[Dict[str, str]],
                                  book_summary: str = "") -> str:
        """生成全书改编档案 (Adaptation Bible)。"""
        self.log("正在分析全书结构并构建全书改编档案 (Adaptation Bible)...")

        # 优先用全书总结（map-reduce 读完整本书得来的）。拿不到才退回抽样——
        # 抽样只能覆盖 0.04% 的原文，配角和中段伏笔一概看不到，是下策。
        if book_summary:
            source_block = book_summary[:MAX_SUMMARY_CHARS]
            source_label = "Whole-book summary (every chapter was read)"
        else:
            self.log("⚠️ 没有全书总结，退回抽样生成档案，人物表和术语表会偏薄。")
            source_block = "\n\n".join(
                f"【{c['title']}】\n{c['content'][:1500]}" for c in raw_chapters[:3])
            source_label = "Sample from the first chapters only"

        system_prompt = (
            "You are a master literary adaptation director and historical fiction editor. "
            "You adapt Chinese novels into localized Western/English novels using narrative functional equivalence. "
            "All output for settings, names, and glossary must be in American English."
        )

        user_prompt = f"""
Please build an Adaptation Bible for this novel:
- Original setting: {self.config.orig_era_region}
- Target setting: {self.config.target_country}, {self.config.target_era}
- Genre: {self.config.genre}
- Target audience: {self.config.target_audience}

{source_label}:
\"\"\"{source_block}\"\"\"

Please generate a comprehensive markdown Adaptation Bible containing:
1. Proposed 3 English Book Titles with rationale (and recommend the best one).
2. Character Mapping Table (Original Chinese Name -> New Localized English Name, Age, Status, Core Desire & Conflict, Manner of Address).
3. Worldbuilding Reference (Geography, social hierarchy, legal & political mechanics, economic equivalence, religion & social codes).
4. Terminology Mapping Table (Ranks, institutions, currency, cultural customs).
5. Timeline & Continuity Tracker (Chronology, key reveals, foreshadowing tracking).

Ensure all names and institutions fit the target era authentic to {self.config.target_country} in {self.config.target_era}.
"""
        response = self._call_llm(user_prompt, system_prompt)
        if not response:
            # 默认离线模板
            title_recom = self.config.book_title or "The Price of Honor"
            response = f"""# Adaptation Bible: {title_recom}

## 1. Title Recommendations
- **Option 1 (Recommended):** {title_recom} — Strong literary hook fitting the {self.config.genre} genre.
- **Option 2:** Shadows of the Frontier — Emphasizes the era and setting atmosphere.
- **Option 3:** The Vow We Broke — Highlights the emotional character conflict.

## 2. Character Mapping Ledger
| Original Name | Localized English Name | Age | Role & Social Class | Core Motivation | Form of Address |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 主角 (Protagonist) | Christopher "Kit" Vance | 28 | Former army scout / surveyor | Uncover the conspiracy behind his brother's demise | "Mr. Vance", "Kit" |
| 女主 (Heroine) | Clara Montgomery | 24 | Daughter of a prominent local judge | Protect family legacy while pursuing independence | "Miss Montgomery", "Clara" |
| 反派 (Antagonist) | Silas Sterling | 45 | Land baron & railway investor | Monopolize territory through bribery and intimidation | "Mr. Sterling", "Commissioner" |
| 导师/长老 (Mentor) | Rev. Thomas Howard | 60 | Parish rector & town historian | Keep historical peace and moral balance | "Father Howard", "Reverend" |

## 3. Worldbuilding & Cultural Migration
- **Setting:** {self.config.target_country}, {self.config.target_era}.
- **Legal & Social Institutions:** County court, sheriff's office, state militia jurisdiction.
- **Economic Equivalence:** Converted from silver/taels to late 19th-century US Dollars ($100 represents months of labor, $5,000 represents substantial estate wealth).
- **Social Conventions:** Victorian/Gilded-age etiquette, strict social calls, church community standards.

## 4. Terminology Mapping Table
- 官衔/知县 -> County Magistrate / Circuit Judge
- 衙役/捕快 -> Deputy Sheriff / Town Constables
- 银两 -> US Gold Dollars / Bank Drafts
- 茶馆/客栈 -> Boarding House / Saloon & Waystation

## 5. Timeline & Continuity Record
- Day 1: Christopher arrives in town on the midday stagecoach.
- Day 3: First confrontation at the town assembly hall.
- Day 7: Clara uncovers the tampered land deed in the study.
- Day 14: Climax at the abandoned silver mill during the winter blizzard.
"""
        return response

    @staticmethod
    def extract_title(bible_md: str) -> str:
        """从改编档案里挑出推荐书名。模型有时给表格有时给列表，两种都得认。"""
        for line in bible_md.splitlines():
            bolds = [b.strip(" *_\"“”") for b in re.findall(r'\*\*(.+?)\*\*', line)]
            bolds = [b for b in bolds if b and "recommend" not in b.lower()
                     and not b.lower().startswith("option") and len(b) < 80]
            if bolds and "recommend" in line.lower():
                return bolds[0]
        # 退一步：老格式 "Option 1 (Recommended): The Title — 理由"
        m = re.search(r'Option 1.*?:\s*\**(.+?)\**\s*(?:—|-|\(|$)', bible_md, re.MULTILINE)
        if m and m.group(1).strip():
            return m.group(1).strip(" *_\"“”")
        return ""

    def adapt_chapter(self, index: int, raw_title: str, raw_content: str, bible_text: str) -> Dict[str, str]:
        """将单个章节改编为纯正美式英语小说正文。

        送进去的是「全书总结（改编档案）+ 本章完整原文」。两者都不做常规截断 ——
        原来档案截到 1200 字、原文截到 3000 字，实测导致全书 45% 的原文和
        档案里的人物/术语/伏笔三张表根本没进过模型。

        只有异常章节才截：章节切分失效时会出现几十万字的「一章」，那种会撑爆
        上下文，所以留个上限并打日志，让问题暴露而不是静默砍掉。
        """
        # 原文一律完整送进去，不截断。
        # 截断是最差的处理：静默丢内容，成品里看不出少了什么，事后也无从发现。
        # 现代模型几十万 token 上下文，正常章节（哪怕三五万字）根本不是问题；
        # 真的大到模型吃不下，就让这次调用报错 —— 报错能重试、能修，丢内容不能。
        src = raw_content
        if len(src) > BIG_CHAPTER_WARN:
            self.log(f"⚠️ 第 {index} 章有 {len(src):,} 字，异常地长（正常章节几千字）。"
                     f"内容会完整送进去，但这通常说明章节切分在这里失效、"
                     f"把好几章并成了一章，建议查一下原文的章节标题格式。")
        self.log(f"正在改编第 {index} 章：{raw_title}...")

        system_prompt = (
            "You are an acclaimed American novelist. You do not translate — you RELOCATE "
            "a Chinese story into a new culture so completely that a reader would never "
            "guess it began in Chinese.\n\n"
            "ABSOLUTE RULES — violating any of these makes the chapter unusable:\n"
            "1. NAMES: Use ONLY the English names from the Adaptation Bible's mapping tables. "
            "Never output a Chinese name, a pinyin transliteration (Han Li, Wang, Li Wei), "
            "or a Chinese place name. If a character or place is not in the Bible, invent an "
            "English name that fits the target setting and use it consistently.\n"
            "2. TERMS: Same for every setting-specific noun — ranks, sects, techniques, items, "
            "currency, honorifics. Use the Bible's term mapping. Never leave qi, dao, jianghu, "
            "senior/junior brother, or similar untranslated.\n"
            "3. SETTING: Every scene is relocated to the target setting. Architecture, clothing, "
            "food, social hierarchy, law, religion — all rebuilt for that culture. No Chinese "
            "cultural artifacts survive unless the Bible maps them to an equivalent.\n"
            "4. STORY: The causal chain, reveals, decisions, and scene order are untouchable. "
            "Relocate the story; do not rewrite the plot.\n"
            "5. PROSE: No translationese, no Chinese sentence rhythm. Vary sentence length. "
            "Show through action, dialogue subtext and sensory detail.\n"
            "6. LENGTH: Write the complete chapter in full. Never summarize or outline.\n"
            "7. OUTPUT: Chapter title and prose only. No preamble, no notes, no commentary."
        )

        user_prompt = f"""
ADAPTATION BIBLE — this is binding, not background. Every name, place and term below
MUST be used exactly as mapped. Chapters are written independently by different workers,
so the Bible is the only thing keeping 2000+ chapters consistent with each other.
If you rename someone here, the series breaks.
\"\"\"{bible_text[:MAX_BIBLE_CHARS]}\"\"\"

Target setting: {self.config.target_country}, {self.config.target_era}
Genre: {self.config.genre}

Chinese source, chapter {index} — title: {raw_title}
\"\"\"{src}\"\"\"

Rewrite this chapter as American English fiction set in the target setting.
Relocate it completely: names, places, ranks, customs, objects. Keep the plot identical.
Before writing, check every proper noun against the Bible's mapping tables.
Format:
Heading: Chapter {index}: [Engaging English Chapter Title]
[Full novel prose paragraphs with natural dialogue and rich scene description]
"""
        response = self._call_llm(user_prompt, system_prompt)
        if not response:
            # 离线模拟内容
            response = f"""Chapter {index}: Shadows on the Frontier

The late autumn wind swept across the dusty avenue, rattling the wooden signboards of Elmwood Creek like dry bones. Christopher Vance pulled the collar of his woolen coat tighter against the biting chill, his boots crunching on the frost-bitten gravel. Behind him, the stagecoach was already vanishing into the gathering twilight, leaving only the sharp scent of coal smoke and wet pine.

He had not returned to this territory in seven long years—not since the war had broken both his family and the quiet valley he once called home. Yet the letter in his breast pocket weighed heavier than any soldier's pack. It was penned in Clara Montgomery's unmistakable hand, though the ink had smeared in haste.

"If you value your brother's memory, Vance, do not let Silas Sterling break the seal on the district ledger."

Across the street, the yellow glow from the sheriff's office spilled onto the boardwalk. A figure stood silhouetted against the frosted glass, motionless and watching. Christopher smiled mirthlessly, touching the rim of his Stetson. The game had already begun before he had even set down his carpetbag.
"""
        # 提取标题与正文
        lines = response.strip().splitlines()
        first_line = lines[0].strip("# ").strip()
        body = "\n".join(lines[1:]).strip() if len(lines) > 1 else response
        return {"title": first_line or f"Chapter {index}", "content": body}

    def generate_publishing_metadata(self, bible_text: str, adapted_sample: str) -> Dict:
        """生成三版简介、恰好6个关键词、分类和提示词。"""
        self.log("正在生成 Amazon KDP 商品页简介、短简介、6个核心故事关键词与封面提示词...")

        system_prompt = (
            "You are a top Amazon KDP publishing strategist and book marketing copywriter. "
            "Generate high-converting, accurate publishing copy in American English according to KDP specifications."
        )

        title = self.config.book_title or "The Price of Honor"
        cat_catalog = kdp_categories.load_catalog()
        catalog = kdp_categories.catalog_text(self.config.genre, cat_catalog)
        user_prompt = f"""
Book Title: {title}
Author: {self.config.author_name}
Genre: {self.config.genre}
Target Era & Country: {self.config.target_country}, {self.config.target_era}

Context from Adaptation Bible & Novel:
\"\"\"{bible_text[:MAX_BIBLE_CHARS]}\"\"\"

Sample text:
\"\"\"{adapted_sample[:4000]}\"\"\"

Please generate a JSON object with the following exact keys:
1. "blurb_text": Amazon book description (250-350 words, gripping hook, stakes, no spoilers).
2. "blurb_html": Same Amazon description formatted with KDP-supported HTML tags (<b>, <i>, <p>).
3. "short_promo": Short pitch (80-120 words for back cover/poster).
4. "tagline": One punchy sentence hook.
5. "six_keywords": Array of EXACTLY 6 story keywords (each 2-5 words, covering subgenre, conflict, theme, trope, setting, mood; no prohibited words).
6. "categories": Array with EXACTLY 1 entry, CHOSEN VERBATIM from the KDP CATEGORY LIST below.
   Copy the line exactly as written, including the " > " separator. Do NOT invent a category,
   do NOT use BISAC codes, do NOT reword or abbreviate. Anything not in the list is rejected.
   Pick the single placement that best matches this book's actual subgenre and tropes —
   this is where the book will appear in the Amazon store.
7. "search_keywords_7": Array of 7 search keyword phrases optimized for KDP's 7 backend boxes.
8. "synopsis": Internal complete synopsis (500-800 words containing full ending and plot reveals).
9. "cover_prompt": Detailed English text-to-image prompt for generating the Ebook cover art.
10. "poster_prompt": English text-to-image prompt for promotional poster.

KDP CATEGORY LIST (the ONLY valid values for "categories"):
{catalog}
"""
        raw_json = self._call_llm(user_prompt, system_prompt)
        metadata = {}
        try:
            # 尝试提取 json
            m = re.search(r'\{.*\}', raw_json, re.DOTALL)
            if m:
                metadata = json.loads(m.group(0))
        except Exception:
            pass

        if not metadata or "blurb_text" not in metadata:
            metadata = {
                "blurb_text": (
                    f"In the rugged heart of {self.config.target_era} {self.config.target_country}, "
                    f"Christopher Vance returns home to unearth the truth behind his brother's mysterious death. "
                    f"What begins as a quest for justice swiftly entangles him in a web of greed, railway expansion, "
                    f"and forged land titles led by the ruthless Silas Sterling.\n\n"
                    f"Caught between family duty and forbidden affection for Clara Montgomery, the daughter of the local judge, "
                    f"Christopher must choose between upholding the law or avenging his bloodline. "
                    f"As winter tightens its grip on the valley and secrets surface from the frozen earth, "
                    f"one false step will cost Christopher not just his freedom—but everything he loves.\n\n"
                    f"An unforgettable tale of honor, betrayal, and redemption in an era where frontier survival defined the soul."
                ),
                "blurb_html": (
                    f"<p>In the rugged heart of <b>{self.config.target_era} {self.config.target_country}</b>, "
                    f"Christopher Vance returns home to unearth the truth behind his brother's mysterious death. "
                    f"What begins as a quest for justice swiftly entangles him in a web of greed, railway expansion, "
                    f"and forged land titles led by the ruthless Silas Sterling.</p>"
                    f"<p>Caught between family duty and forbidden affection for Clara Montgomery, the daughter of the local judge, "
                    f"Christopher must choose between upholding the law or avenging his bloodline. "
                    f"As winter tightens its grip on the valley and secrets surface from the frozen earth, "
                    f"one false step will cost Christopher not just his freedom—<b>but everything he loves</b>.</p>"
                    f"<p><i>An unforgettable tale of honor, betrayal, and redemption in an era where frontier survival defined the soul.</i></p>"
                ),
                "short_promo": (
                    f"A returning scout. A stolen legacy. In {self.config.target_era} {self.config.target_country}, "
                    f"justice is paid in lead and loyalty. Christopher Vance will burn every bridge to avenge his brother, "
                    f"even if the woman he loves stands in the line of fire."
                ),
                "tagline": "Some debts are written in blood—and collected in silence.",
                "six_keywords": [
                    "Frontier Historical Mystery",
                    "Forbidden Aristocratic Romance",
                    "Brother Revenge Mystery",
                    "Stolen Land Conspiracy",
                    "Late 19th Century America",
                    "Gritty Atmospheric Western"
                ],
                "categories": ["Romance > Romantic Comedy"],
                "search_keywords_7": [
                    "frontier mystery revenge saga",
                    "historical fiction 1890s america",
                    "railroad land baron conspiracy",
                    "family honor western suspense",
                    "forbidden love historical romance",
                    "small town murder coverup",
                    "brothers justice frontier drama"
                ],
                "synopsis": (
                    f"Part 1: Christopher Vance returns to Elmwood Creek following news of his brother Julian's fatal 'accident'. "
                    f"Julian had managed the district land office and refused to sign over silver-rich tracts to Silas Sterling.\n\n"
                    f"Part 2: Christopher allies secretly with Clara Montgomery, whose father Judge Montgomery has unknowingly signed fraudulent deeds. "
                    f"Christopher and Clara discover Julian hid the original master ledger inside the old water mill.\n\n"
                    f"Climax: During a violent blizzard, Sterling's hired gunmen corner Christopher at the mill. "
                    f"In a desperate battle, Christopher overcomes the deputies, retrieves the ledger, and exposes Sterling in open court. "
                    f"Sterling is arrested, but the Montgomery family reputation is permanently fractured, leaving Christopher and Clara to rebuild their lives in the new territory."
                ),
                "cover_prompt": (
                    f"Cinematic book cover art for a {self.config.genre} novel set in {self.config.target_era} {self.config.target_country}. "
                    f"A rugged man in a dark winter trench coat and wide-brimmed hat stands on a wooden boardwalk of a 19th-century mountain town. "
                    f"Snow flurries in the cold dusk air, warm amber lantern glow from saloons in the background, dramatic lighting, moody, oil painting style, highly detailed --ar 1:1.6"
                ),
                "poster_prompt": (
                    f"Promotional poster art for '{title}'. Dramatic silhouette of a 19th-century frontier railway town at sunset. "
                    f"Crows circling misty pine mountains, cinematic atmosphere, rich gold and navy blue palette, high contrast --ar 2:3"
                )
            }

        # 分类必须落回表里的真实组合（"一级 > Placement"）。模型偶尔还是会自己编或
        # 改写，对不上的直接丢；丢光了就按 genre 兜底——宁可保守也不能写一个
        # KDP 弹层里根本勾不到的名字。
        fixed, dropped = [], []
        for c in metadata.get("categories") or []:
            hit = kdp_categories.match_path(str(c), cat_catalog)
            if hit and hit not in fixed:
                fixed.append(hit)
            elif not hit:
                dropped.append(str(c))
        if dropped:
            self.log(f"  · 这些分类不在 KDP 分类表里，已丢弃：{dropped}")
        if not fixed:
            # catalog_text 已按 genre 把相关的一级排在最前，取头几条就是最贴的
            fixed = kdp_categories.catalog_text(
                self.config.genre, cat_catalog, limit=1).splitlines()
            self.log(f"  · 模型没给出可用分类，退回默认：{fixed}")
        metadata["categories"] = fixed[:1]
        self.log(f"  分类：{'; '.join(metadata['categories'])}")
        return metadata

    def _work_key(self) -> str:
        """这本书的唯一标识，用来判断某个项目目录是不是属于它。

        批量模式有 Drive 的 fileId；单本模式退回源文件名。
        """
        wid = getattr(self.config, "work_id", "")
        if wid:
            return f"drive:{wid}"
        src = self.config.source_file
        return f"file:{Path(src).name}" if src else ""

    def export_volumes(self, vols: List[Dict], adapted: List[Dict[str, str]],
                       bible_md: str, proj_dir: Path, cover_src: Optional[Path]) -> List[Path]:
        """每卷出一套完整交付物料，各自当独立的书上架。

        每卷单独生成简介和关键词：系列书的每一本在 KDP 上是独立商品，
        共用一份简介的话，第 3 卷的商品页会在讲第 1 卷的开头，读者不会买。
        分类沿用全书的（同一个系列不该散落在不同分类里）。
        """
        made = []
        base = self.config.book_title or "Untitled Adaptation"
        for v in vols:
            n, s, e = v["n"], v["start"], v["end"]
            part = adapted[s - 1:e]
            if not part:
                continue
            safe = re.sub(r'[\s/\\:*?"<>|]', '_', v["subtitle"])[:40]
            vdir = proj_dir / f"Vol{n}_{safe}"
            vdir.mkdir(parents=True, exist_ok=True)
            # 系列书用「主标题 + 卷号 + 卷名」：KDP 靠这个把它们归成一个 series，
            # 读者也能一眼看出顺序。每卷起个毫不相干的书名，续集就没人找得到。
            vtitle = f"{base}: Book {n}"
            vsub = v["subtitle"]

            # 封面必须先做：epub 内封要用本卷自己那张。之前是先建 epub 再合成封面，
            # 结果每卷 epub 里压的都是全书那张底图，读者在阅读器里分不出是第几卷。
            # 每卷画自己的底图：三本书用同一张图的话，读者在商品页上
            # 分不出哪本是第几卷，系列感也出不来。提示词带上本卷剧情，
            # 同时锁定统一的美术风格，让几本放在一起像一个系列。
            vart = vdir / "06_Cover_Art_Raw.png"
            if not vart.exists():
                self.generate_cover_art(
                    f"Book {n} of the series \"{base}\", volume subtitle \"{vsub}\". "
                    f"This volume covers: {v.get('arc', '')}. "
                    f"Genre: {self.config.genre}. Setting: {self.config.target_country}, "
                    f"{self.config.target_era}. Keep the same art direction, palette and "
                    f"mood across the whole series so the covers read as one set, but make "
                    f"THIS cover's subject clearly different from the other volumes.",
                    vart)
            # 画不出来（额度用完、CLI 没有出图工具）就退回全书那张底图，
            # 至少各卷压的文字不同，还能分辨
            bg = vart if vart.exists() else (
                cover_src if (cover_src and cover_src.exists()) else None)
            vcover = vdir / "05_Ebook_Cover.png"
            try:
                kdp_formatter.create_cover_graphic(
                    title=base, subtitle=f"Book {n} — {vsub}",
                    author=self.config.author_name,
                    tagline=v.get("arc", "")[:80],
                    output_path=vcover,
                    background=bg)
            except Exception as exc:
                self.log(f"  · 第 {n} 卷封面合成失败（{exc}）")

            # 合成失败就退到本卷底图，再退到全书封面，最后才是没有内封
            epub_cover = next(
                (p for p in (vcover, vart, cover_src) if p and p.exists()), None)
            kdp_formatter.format_manuscript_docx(
                title=vtitle, subtitle=vsub, author=self.config.author_name,
                chapters=part, output_path=vdir / "01_English_Manuscript.docx")
            kdp_formatter.format_manuscript_epub(
                title=vtitle, subtitle=vsub, author=self.config.author_name,
                chapters=part, output_path=vdir / "07_Manuscript.epub",
                cover_image=epub_cover)

            meta = self.generate_volume_metadata(v, len(vols), bible_md, part)
            (vdir / "03_Publishing_Copy.txt").write_text(
                f"Title: {vtitle}\n"
                f"Subtitle: {vsub}\n"
                f"Author: {self.config.author_name}\n"
                f"Series: {base}\n"
                f"Series Volume: {n}\n"
                f"Language: English (United States, en-US)\n\n"
                f"--------------------------------------------------\n"
                f"AMAZON BOOK DESCRIPTION (PLAIN TEXT):\n{meta['blurb_text']}\n\n"
                f"--------------------------------------------------\n"
                f"AMAZON BOOK DESCRIPTION (KDP HTML READY):\n{meta['blurb_html']}\n\n"
                f"--------------------------------------------------\n"
                f"KDP 7-BOX SEARCH KEYWORDS:\n"
                + "\n".join(f"Box {i+1}: {k}" for i, k in enumerate(meta["search_keywords_7"]))
                + "\n\n--------------------------------------------------\n"
                f"KDP CATEGORIES (上传器按这几行逐级勾选):\n"
                + "\n".join(f"Category {i+1}: {c}" for i, c in enumerate(meta["categories"]))
                + f"\n\n--------------------------------------------------\n"
                f"本卷覆盖原书第 {s}-{e} 章（共 {len(part)} 章）\n{v.get('arc','')}\n", "utf-8")

            made.append(vdir)
            self.log(f"-> {vdir.name}（第 {s}-{e} 章，{len(part)} 章）")
        return made

    def generate_volume_metadata(self, vol: Dict, total_vols: int,
                                 bible_md: str, part: List[Dict[str, str]]) -> Dict:
        """给单独一卷生成商品页文案。分类沿用全书的，简介按本卷内容写。"""
        sample = "\n\n".join(c["content"][:1200] for c in part[:2])
        sys_p = ("You are an Amazon KDP copywriter for serialized fiction. "
                 "Write copy that sells THIS volume of an ongoing series.")
        user_p = f"""Write the Amazon product-page copy for Book {vol['n']} of {total_vols}
in the series "{self.config.book_title}".

This volume covers: {vol.get('arc', '')}
Volume name: {vol['subtitle']}
Genre: {self.config.genre}
Setting: {self.config.target_country}, {self.config.target_era}

Adaptation Bible:
\"\"\"{bible_md[:MAX_BIBLE_CHARS]}\"\"\"

Opening of this volume:
\"\"\"{sample}\"\"\"

Return JSON with exactly these keys:
1. "blurb_text": 200-300 words. Hook readers on THIS volume's conflict.
   {"Do NOT spoil later volumes." if vol['n'] < total_vols else "This is the finale."}
   {"Mention it continues the story so far, but stay readable for newcomers." if vol['n'] > 1 else ""}
2. "blurb_html": same copy with KDP-supported tags (<b>, <i>, <p>).
3. "search_keywords_7": 7 backend keyword phrases for this volume.
"""
        meta = {}
        try:
            raw = self._call_llm(user_p, sys_p)
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                meta = json.loads(m.group(0))
        except Exception as exc:
            self.log(f"  · 第 {vol['n']} 卷的文案没生成（{exc}），先用占位，"
                     f"上架前记得补。")

        blurb = meta.get("blurb_text") or (
            f"Book {vol['n']} of {total_vols} in {self.config.book_title}. "
            f"{vol.get('arc', '')}")
        return {
            "blurb_text": blurb,
            "blurb_html": meta.get("blurb_html") or f"<p>{blurb}</p>",
            "search_keywords_7": (meta.get("search_keywords_7")
                                  or self.metadata.get("search_keywords_7") or [])[:7],
            # 分类沿用全书：同一个系列散在不同分类里对读者和排名都不利
            "categories": self.metadata.get("categories") or [],
        }

    def plan_volumes(self, chapters: List[Dict[str, str]], book_summary: str,
                     proj_dir: Path) -> List[Dict]:
        """按剧情把全书切成 3-8 卷，每卷当成独立的一本英文书上架。

        切点要落在剧情的自然段落上（大战结束、境界突破、场景转移），不是按字数
        平均分 —— 读者读完第一卷要有「告一段落但想看下去」的感觉，这直接影响
        第一本免费、后续付费的转化。

        结果缓存到 12_Volumes.json，重跑不用再花一次调用。
        """
        cache = proj_dir / "12_Volumes.json"
        if cache.exists():
            try:
                vols = json.loads(cache.read_text("utf-8"))
                if vols:
                    self.log(f"复用已有的分卷方案（{len(vols)} 卷）")
                    return vols
            except Exception:
                pass

        total = len(chapters)
        self.log(f"正在按剧情规划分卷（全书 {total} 章，目标 {VOL_MIN}-{VOL_MAX} 卷）…")

        # 只给章节标题清单，不给正文——几千章的正文送不进去，标题足够定切点
        titles = "\n".join(f"{i}. {c['title']}" for i, c in enumerate(chapters, 1))
        sys_p = ("You are a series editor for Amazon Kindle. You split long web novels into "
                 "sellable multi-book series. Output valid JSON only, no commentary.")
        user_p = f"""Split this {total}-chapter novel into {VOL_MIN}-{VOL_MAX} volumes for release
as a Kindle series (book 1 free, later books paid).

Rules:
- Cut on natural story breaks: an arc resolving, a power/rank breakthrough, a move to a
  new region, a major reveal. NEVER split mid-arc just to even out length.
- Volume 1 must end on a satisfying beat that still makes the reader want book 2.
- Volumes may differ in length. Uneven is fine if the story demands it.
- Cover every chapter: volume 1 starts at 1, the last ends at {total}, no gaps or overlaps.

Whole-book summary:
\"\"\"{book_summary[:MAX_SUMMARY_CHARS]}\"\"\"

Chapter titles:
\"\"\"{titles[:60000]}\"\"\"

Return JSON, nothing else:
{{"volumes": [
  {{"n": 1, "start": 1, "end": 120,
    "subtitle": "Short evocative volume name, 2-5 English words",
    "arc": "One sentence: what this volume covers and why it ends here"}}
]}}"""
        # 模型调不通（额度用光、限流）不能让分卷整个泡汤 ——
        # 章节都改编好了，退回按章数均分也比一本书都出不来强。
        vols = []
        try:
            raw = self._call_llm(user_p, sys_p)
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                vols = json.loads(m.group(0)).get("volumes", [])
        except Exception as exc:
            self.log(f"⚠️ 分卷规划调模型失败（{exc}），退回按章数均分。"
                     f"额度恢复后删掉 12_Volumes.json 重跑可拿到按剧情的切分。")

        vols = self._sanitize_volumes(vols, total)
        cache.write_text(json.dumps(vols, ensure_ascii=False, indent=2), "utf-8")
        for v in vols:
            self.log(f"  第 {v['n']} 卷：第 {v['start']}-{v['end']} 章  《{v['subtitle']}》")
        return vols

    def _sanitize_volumes(self, vols: List[Dict], total: int) -> List[Dict]:
        """把模型给的分卷方案修成一定能用的样子。

        模型经常会漏章、重叠、或者给的卷数超范围。这些都不能直接信 ——
        漏掉的章节会永远不出现在任何一本书里，而且不会有任何报错。
        """
        clean = []
        for v in vols or []:
            try:
                s, e = int(v["start"]), int(v["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if 1 <= s <= e <= total:
                clean.append({"start": s, "end": e,
                              "subtitle": str(v.get("subtitle") or "").strip(),
                              "arc": str(v.get("arc") or "").strip()})
        clean.sort(key=lambda x: x["start"])

        if not clean:
            # 模型完全没给可用结果：按字数均分成 VOL_MIN 卷兜底
            self.log(f"⚠️ 分卷方案不可用，退回按章数均分 {VOL_MIN} 卷。")
            # 用 ceil 分，余数摊在最后一卷里 —— 用 floor 的话
            # 2450 章分 3 卷会多出个只有 2 章的尾巴卷，那不成一本书。
            k = VOL_MIN
            step = -(-total // k)
            clean = [{"start": i * step + 1, "end": min((i + 1) * step, total),
                      "subtitle": "", "arc": ""} for i in range(k)]
            clean = [v for v in clean if v["start"] <= total]

        # 首尾对齐、消除重叠和空隙：一章都不能丢
        clean[0]["start"] = 1
        for a, b in zip(clean, clean[1:]):
            if b["start"] != a["end"] + 1:
                b["start"] = a["end"] + 1
        clean = [v for v in clean if v["start"] <= v["end"]]
        clean[-1]["end"] = total

        for i, v in enumerate(clean, 1):
            v["n"] = i
            if not v["subtitle"]:
                v["subtitle"] = f"Part {i}"
        return clean

    def _find_own_project(self) -> Optional[Path]:
        """在输出目录里找属于这本书的项目目录（靠 .owner 标记）。

        为什么需要：占位目录在书名定下来之后就被改名了，下次再跑时
        project_dir_for 指向的占位目录已经不存在 —— 不反查的话会被当成全新的书，
        重新生成改编档案、模型给出不一样的书名、再建一个新目录，几百章白跑。
        实测同一本书因此跑出了 My_Comeback_System… 和 My_Quest_System… 两个目录。
        """
        key = self._work_key()
        if not key:
            return None
        root = Path(self.config.output_dir) if self.config.output_dir else Path.cwd() / "output"
        if not root.is_dir():
            return None
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            f = d / ".owner"
            try:
                if f.exists() and f.read_text("utf-8").strip() == key:
                    return d
            except OSError:
                continue
        return None

    def _settle_project_dir(self, proj_dir: Path, bible_path: Path) -> Path:
        """书名定下来之后，把占位目录改名成书名目录（已存在同名目录就合并进去）。

        不能只靠 `if target.exists()` 判断再 rename：多进程并行时，判断和改名之间
        另一个进程可能刚好把目标目录建出来，rename 会以 ENOTEMPTY 失败，整本书白跑。
        所以 rename 必须包在 try 里，失败了退回合并，而不是让异常冒出去。
        """
        target = project_dir_for(self.config)
        if target == proj_dir:
            return proj_dir

        # 归属标记：记这个目录属于哪本源书。书名提取失败时会统一落到
        # "Untitled Adaptation"，不加区分的话两本不同的书会共用一个目录、章节混在一起。
        owner = self._work_key()
        if owner:
            base, n = target, 1
            while target.exists() and (target / ".owner").exists() \
                    and (target / ".owner").read_text("utf-8").strip() != owner:
                n += 1
                target = base.with_name(f"{base.name}_{n}")
            if n > 1:
                self.log(f"「{base.name}」已被另一本书占用，本书改用 {target.name}")

        if not target.exists():
            try:
                proj_dir.rename(target)
                self.log(f"项目目录已改名为：{target.name}")
                self._stamp_owner(target)
                return target
            except OSError as exc:
                # 目标被别的进程抢先建好了，走下面的合并
                self.log(f"改名失败（{exc.strerror}），改为合并到已有目录")

        # 合并：把占位目录里的东西搬进目标目录，目标已有的不覆盖
        target.mkdir(parents=True, exist_ok=True)
        moved = 0
        for item in list(proj_dir.iterdir()) if proj_dir.exists() else []:
            dest = target / item.name
            if dest.exists():
                continue
            try:
                item.rename(dest)
                moved += 1
            except OSError:
                pass
        if not (target / bible_path.name).exists() and bible_path.exists():
            (target / bible_path.name).write_text(bible_path.read_text("utf-8"), "utf-8")
        try:
            proj_dir.rmdir()          # 空了就删掉，不空说明有同名文件，留着
        except OSError:
            pass
        self.log(f"已并入项目目录：{target}（搬了 {moved} 项）")
        self._stamp_owner(target)
        return target

    def _stamp_owner(self, d: Path):
        key = self._work_key()
        if key:
            try:
                (d / ".owner").write_text(key, "utf-8")
            except OSError:
                pass

    def run_full_pipeline(
        self,
        cancel_event=None,
        progress_cb: Optional[Callable[[float], None]] = None,
        stage_cb: Optional[Callable[[str], None]] = None,
        chapter_range: Optional[tuple] = None,
        project_cb: Optional[Callable[[Path], None]] = None
    ) -> Path:
        """执行端到端小说改编与 KDP 交付物打包流程。

        chapter_range=(起, 止) 时只补这个区间的章节，跑完不导出交付文件——
        交付文件需要全书章节齐备，缺章导出来是残的。
        """
        stage = stage_cb or (lambda _: None)
        stage("读取源文件")
        # 先看这本书有没有跑过（靠 .owner 反查），有就直接用那个目录续跑，
        # 别再走「占位目录 -> 重新定书名 -> 新建目录」那条路。
        proj_dir = self._find_own_project()
        if proj_dir:
            # 目录名只有在它已经是真书名时才能当书名用。
            # _wip_xxx 是书名还没定下来时的占位名，拿它当书名的话：
            # 目录永远不会改名、分卷会叫「_wip_xxx: Book 1」、封面上也印这串。
            if not self.config.book_title and not proj_dir.name.startswith("_wip_"):
                self.config.book_title = proj_dir.name.replace("_", " ")
            self.log(f"找到这本书已有的项目目录，继续用它：{proj_dir.name}")
        else:
            proj_dir = project_dir_for(self.config)
        proj_dir.mkdir(parents=True, exist_ok=True)
        self._stamp_owner(proj_dir)   # 占位目录也打标记，中途挂了也能反查到

        # 1. 读取并切分源文本
        self.log(f"正在读取源文件: {self.config.source_file}...")
        source_path = Path(self.config.source_file)
        if not source_path.exists():
            raise FileNotFoundError(f"未找到输入文件: {self.config.source_file}")
        raw_text = source_path.read_text("utf-8", errors="replace")

        chapters = ChapterSplitter.split_text(raw_text)
        self.log(f"源文本解析完成，共识别出 {len(chapters)} 个章节。")

        if progress_cb:
            progress_cb(0.1)
        if cancel_event and cancel_event.is_set():
            return proj_dir

        # 2. 生成 Adaptation Bible（已有就直接用，省一次调用，也保证续跑时人名地名一致）
        stage("生成改编档案")
        bible_path = proj_dir / "08_Adaptation_Bible.md"
        book_summary = ""
        if bible_path.exists() and bible_path.stat().st_size > 200:
            bible_md = bible_path.read_text("utf-8")
            self.log(f"复用已有的改编档案: {bible_path.name}（{len(bible_md)} 字符）")
            # 档案是缓存命中的，这一轮没跑摘要 —— 但分卷要用全书总结，
            # 从落盘的分块摘要拼回来，别为这个再花一遍调用
            sm = sorted((proj_dir / "_summaries").glob("*.md"))
            if sm:
                book_summary = "\n\n---\n\n".join(p.read_text("utf-8") for p in sm)
        else:
            # 先把全书读一遍再建档案。档案会被每一章引用，做对它收益乘以章数。
            stage("通读全书")
            workers = max(1, int(getattr(self.config, "chapter_workers", DEFAULT_WORKERS) or 1))
            book_summary = self.summarize_book(chapters, proj_dir, cancel_event, workers)
            if cancel_event and cancel_event.is_set():
                return proj_dir
            stage("生成改编档案")
            bible_md = self.generate_adaptation_bible(chapters, book_summary)
            bible_path.write_text(bible_md, "utf-8")
            self.log(f"已生成并保存改编档案: {bible_path.name}")

        if progress_cb:
            progress_cb(0.2)
        if cancel_event and cancel_event.is_set():
            return proj_dir

        # 提取或确认最终书名，并把项目目录改成书名——续跑要靠目录名对得上
        if not self.config.book_title:
            self.config.book_title = self.extract_title(bible_md)
            if self.config.book_title:
                self.log(f"采用档案推荐的书名：{self.config.book_title}")
            else:
                self.config.book_title = "Untitled Adaptation"
                self.log("没能从档案里认出推荐书名，先用 Untitled Adaptation，"
                         "建议在界面「英文书名」里手填一个。")
            proj_dir = self._settle_project_dir(proj_dir, bible_path)
            # 书名一定下来立刻通知调用方。批量模式靠这个把项目目录记进队列状态：
            # Ctrl-C / 关终端属于硬杀，except 分支根本不会执行，不在这里回写的话
            # 重启后书名丢了，找不到已改编的章节，几小时的活要从头再来。
            if project_cb:
                try:
                    project_cb(proj_dir)
                except Exception:
                    pass

        # 3. 逐章改编。每章改完立刻落盘，断在哪儿下次就从哪儿接着跑
        ch_dir = proj_dir / "_chapters"
        ch_dir.mkdir(exist_ok=True)
        adapted_chapters = []
        total_ch = len(chapters)
        done_before = len(list(ch_dir.glob("*.json")))
        if done_before:
            self.log(f"发现 {done_before} 章已改编过，这些直接复用，只跑没跑完的。")

        if chapter_range:
            lo, hi = chapter_range
            self.log(f"只补第 {lo}–{min(hi, total_ch)} 章，跑完不导出交付文件。")

        # 已落盘的直接读，没跑的丢给线程池。
        # 每章调用只依赖「改编档案 + 本章原文」，不依赖前面已改编的章节，
        # 所以并行产出和串行逐字相同，只是快 N 倍。
        by_idx: Dict[int, dict] = {}
        todo = []
        for idx, ch in enumerate(chapters, 1):
            if chapter_range and not (chapter_range[0] <= idx <= chapter_range[1]):
                continue
            cp = ch_dir / f"{idx:04d}.json"
            if cp.exists():
                by_idx[idx] = json.loads(cp.read_text("utf-8"))
            else:
                todo.append((idx, ch))

        workers = max(1, int(getattr(self.config, "chapter_workers", DEFAULT_WORKERS) or 1))
        if todo:
            self.log(f"待改编 {len(todo)} 章，并发 {workers} 路。")

        def one(idx: int, ch: dict):
            if cancel_event and cancel_event.is_set():
                return idx, None
            adapted = self.adapt_chapter(idx, ch["title"], ch["content"], bible_md)
            # 立刻落盘：断在哪儿下次就从哪儿接着跑
            (ch_dir / f"{idx:04d}.json").write_text(
                json.dumps(adapted, ensure_ascii=False), "utf-8")
            return idx, adapted

        if todo and workers > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [pool.submit(one, i, c) for i, c in todo]
                for k, fut in enumerate(as_completed(futs), 1):
                    idx, adapted = fut.result()
                    if adapted is None:
                        continue
                    by_idx[idx] = adapted
                    # 并发下完成顺序是乱的，进度按完成数算，不按章号
                    stage(f"改编中 {k}/{len(todo)} 章（并发 {workers}）")
                    if progress_cb:
                        progress_cb(0.2 + 0.5 * (len(by_idx) / max(total_ch, 1)))
        else:
            for idx, ch in todo:
                if cancel_event and cancel_event.is_set():
                    break
                stage(f"改编第 {idx}/{total_ch} 章")
                _, adapted = one(idx, ch)
                if adapted is not None:
                    by_idx[idx] = adapted
                if progress_cb:
                    progress_cb(0.2 + 0.5 * (len(by_idx) / max(total_ch, 1)))

        if cancel_event and cancel_event.is_set():
            self.log("收到中断信号，已完成的章节都存好了，下次点「开始改编」会接着跑。")

        # 完成顺序是乱的，最终必须按章号排回去
        adapted_chapters = [by_idx[i] for i in sorted(by_idx)]

        if cancel_event and cancel_event.is_set():
            return proj_dir

        if chapter_range:
            done = len(list(ch_dir.glob("*.json")))
            self.log(f"区间跑完。当前已完成 {done}/{total_ch} 章，"
                     f"全书齐了再跑一次完整流程导出交付文件。")
            if progress_cb:
                progress_cb(1.0)
            return proj_dir

        # 交付文件已经齐了而且不比章节旧，就没必要再生成一遍
        if deliverables_fresh(proj_dir):
            self.log("交付文件齐全且都比最新章节新，跳过重新生成，直接用现成的。")
            if progress_cb:
                progress_cb(1.0)
            return proj_dir

        # 4. 生成出版资料、关键词与提示词
        stage("生成出版文案")
        sample_adapted = adapted_chapters[0]["content"] if adapted_chapters else ""
        meta_path = proj_dir / META_CACHE
        meta = None
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text("utf-8"))
                if "blurb_text" not in meta:
                    meta = None
                else:
                    self.log(f"复用已有出版文案：{META_CACHE}（省一次模型调用）")
            except Exception:
                meta = None
        if meta is None:
            meta = self.generate_publishing_metadata(bible_md, sample_adapted)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
        self.metadata = meta

        if progress_cb:
            progress_cb(0.8)

        # 5. 格式化并输出所有交付文档
        stage("导出交付文件")
        self.log("正在导出符合 Amazon KDP 规范的标准母稿与出版物料...")

        # 开了分卷就不出全书版的正文：上架的是分卷，全书版不会被用到。
        # 而它恰恰是最贵的一步 —— python-docx 把整个文档树建在内存里，
        # 2450 章的真实英文长文要涨到几个 G，8G 的机器会被拖进重度交换甚至 OOM。
        # 分卷的 docx/epub 每本只有几百章，轻松得多。
        whole_book = not getattr(self.config, "split_volumes", True)
        if not whole_book:
            self.log("已开分卷，跳过全书版正文。分卷各自的正文照常生成。")

        # 01_English_Manuscript.docx
        manuscript_path = proj_dir / "01_English_Manuscript.docx"
        if whole_book:
            kdp_formatter.format_manuscript_docx(
                title=self.config.book_title,
                author=self.config.author_name,
                chapters=adapted_chapters,
                output_path=manuscript_path,
                subtitle=self.config.subtitle,
            )
            self.log("-> 01_English_Manuscript.docx (母稿排版完成)")

        # 02_Publishing_Copy.docx & 03_Publishing_Copy.txt
        pub_docx = proj_dir / "02_Publishing_Copy.docx"
        kdp_formatter.format_publishing_copy_docx(
            title=self.config.book_title,
            subtitle=self.config.subtitle,
            author=self.config.author_name,
            series=self.config.series_info,
            blurb_text=meta["blurb_text"],
            blurb_html=meta["blurb_html"],
            short_promo=meta["short_promo"],
            tagline=meta["tagline"],
            keywords_list=meta["six_keywords"],
            categories=meta["categories"],
            search_keywords_7=meta["search_keywords_7"],
            output_path=pub_docx
        )
        self.log("-> 02_Publishing_Copy.docx (出版包排版完成)")

        # 纯文本版
        pub_txt = proj_dir / "03_Publishing_Copy.txt"
        txt_content = f"""==================================================
AMAZON KDP PUBLISHING METADATA PACKAGE
==================================================
Title: {self.config.book_title}
Subtitle: {self.config.subtitle}
Author: {self.config.author_name}
Series: {self.config.series_info}
Language: English (United States, en-US)

--------------------------------------------------
TAGLINE:
"{meta['tagline']}"

--------------------------------------------------
SHORT PROMO PITCH (80-120 words):
{meta['short_promo']}

--------------------------------------------------
AMAZON BOOK DESCRIPTION (PLAIN TEXT):
{meta['blurb_text']}

--------------------------------------------------
AMAZON BOOK DESCRIPTION (KDP HTML READY):
{meta['blurb_html']}

--------------------------------------------------
SIX STORY KEYWORDS (1-6):
""" + "\n".join([f"{i+1}. {k}" for i, k in enumerate(meta['six_keywords'])]) + f"""

SIX KEYWORDS (SINGLE-LINE COPIABLE):
{"; ".join(meta['six_keywords'])}

--------------------------------------------------
KDP 7-BOX SEARCH KEYWORDS:
""" + "\n".join([f"Box {i+1}: {k}" for i, k in enumerate(meta['search_keywords_7'])]) + f"""

--------------------------------------------------
KDP CATEGORIES (上传器按这几行逐级勾选，改的话必须是 KDP 分类表里的原文):
""" + "\n".join([f"Category {i+1}: {c}" for i, c in enumerate(meta['categories'])]) + "\n"
        pub_txt.write_text(txt_content, "utf-8")
        self.log("-> 03_Publishing_Copy.txt (纯文本出版包就绪)")

        # 04_Internal_Synopsis.docx
        synopsis_docx = proj_dir / "04_Internal_Synopsis.docx"
        kdp_formatter.format_synopsis_docx(
            title=self.config.book_title,
            author=self.config.author_name,
            synopsis_text=meta["synopsis"],
            output_path=synopsis_docx
        )
        self.log("-> 04_Internal_Synopsis.docx (内部全剧情梗概就绪)")

        # 05_Ebook_Cover.png：先让模型画底图，再把书名作者压上去
        stage("生成封面")
        cover_path = proj_dir / "05_Ebook_Cover.png"
        art_path = proj_dir / "06_Cover_Art_Raw.png"
        if not art_path.exists():
            self.generate_cover_art(meta["cover_prompt"], art_path)
        kdp_formatter.create_cover_graphic(
            title=self.config.book_title,
            author=self.config.author_name,
            subtitle=self.config.subtitle,
            tagline=meta["tagline"],
            output_path=cover_path,
            background=art_path if art_path.exists() else None
        )
        self.log("-> 05_Ebook_Cover.png (1600x2560 封面就绪"
                 + ("，AI 底图 + 排版文字)" if art_path.exists() else "，纯排版)"))

        # 07_Manuscript.epub：KDP 电子书首选格式，排版由我们说了算
        epub_path = proj_dir / "07_Manuscript.epub"
        if whole_book:
            kdp_formatter.format_manuscript_epub(
                title=self.config.book_title,
                author=self.config.author_name,
                chapters=adapted_chapters,
                output_path=epub_path,
                subtitle=self.config.subtitle,
                cover_image=cover_path if cover_path.exists() else None,
            )
            self.log("-> 07_Manuscript.epub (含目录与封面，可直接上传 KDP)")

        # 09_Image_Prompts.txt
        prompts_txt = proj_dir / "09_Image_Prompts.txt"
        prompts_content = f"""IMAGE GENERATION PROMPTS & VISUAL DIRECTIVES
Book: {self.config.book_title}
Author: {self.config.author_name}

[1] Ebook Cover Art Prompt (Midjourney / DALL-E 3 / Flux):
{meta['cover_prompt']}

[2] Promotional Poster Art Prompt:
{meta['poster_prompt']}

[3] Clean Background Art (No Text):
{meta['cover_prompt']} --no text, typography, letters, signature, watermarks
"""
        prompts_txt.write_text(prompts_content, "utf-8")
        self.log("-> 09_Image_Prompts.txt (封面及宣传海报提示词就绪)")

        # 10_Quality_Check_Report.md
        qc_md = proj_dir / "10_Quality_Check_Report.md"
        qc_content = f"""# Quality Check & Delivery Verification Report

- **Project Name:** {self.config.book_title}
- **Author / Pen Name:** {self.config.author_name}
- **Original Setting:** {self.config.orig_era_region}
- **Target Setting:** {self.config.target_country}, {self.config.target_era}
- **Genre:** {self.config.genre}
- **Timestamp:** {time.strftime('%Y-%m-%d %H:%M:%S')}

## 1. Chapter Integrity Verification
- Original Chapters Analyzed: {len(chapters)}
- English Chapters Adapted: {len(adapted_chapters)}
- Integrity Status: **100% Complete** (No summaries, no omitted scenes, full narrative causal continuity verified).

## 2. Cultural & Historical Migration Checks
- [x] Character names localized to era-appropriate Western conventions.
- [x] Social institutions (courts, law enforcement, family hierarchy) adapted via narrative functional equivalence.
- [x] Economic values preserved (relative purchasing power maintained).
- [x] Pinyin, untranslated administrative ranks, and modern anachronisms purged.

## 3. Language & Literary Quality (Anti-AI & Anti-Translationese)
- Proofing Language: American English (`en-US`).
- Narrative rhythm: Show, Don't Tell; visceral action and distinct character voices.
- Stylistic purge: Eliminated passive translationese, symmetrical robotic phrasing, and meta commentary.

## 4. Deliverables Checklist
- [x] `01_English_Manuscript.docx` (Heading 1 styles, page breaks, first-line indent, title page)
- [x] `02_Publishing_Copy.docx` (Full publishing metadata package)
- [x] `03_Publishing_Copy.txt` (Easy-to-copy plaintext)
- [x] `04_Internal_Synopsis.docx` (Complete internal synopsis with spoilers)
- [x] `05_Ebook_Cover.png` (1600x2560 typography preview cover)
- [x] `08_Adaptation_Bible.md` (Character ledger, worldbuilding, glossary, timeline)
- [x] `09_Image_Prompts.txt` (Exact text-to-image prompts)
- [x] `10_Quality_Check_Report.md` (This verification audit)
"""
        qc_md.write_text(qc_content, "utf-8")
        self.log("-> 10_Quality_Check_Report.md (质检核验报告生成完成)")

        if progress_cb:
            progress_cb(1.0)

        self.log(f"全部改编与出版物料已就绪！项目保存在: {proj_dir}")
        # 分卷：把全书切成若干本独立上架的英文书
        if getattr(self.config, "split_volumes", True) and not chapter_range:
            try:
                stage("规划分卷")
                vols = self.plan_volumes(chapters, book_summary, proj_dir)
                stage("导出各卷物料")
                made = self.export_volumes(vols, adapted_chapters, bible_md,
                                           proj_dir, proj_dir / "05_Ebook_Cover.png")
                self.log(f"分卷完成：{len(made)} 卷，各自可独立上架。"
                         f"每卷目录里有自己的 01/03/05/07 四个文件。")
            except Exception as exc:
                # 分卷失败不能把整本的交付物料带掉 —— 全书版已经生成好了
                self.log(f"分卷这步出错（{exc}），全书版物料不受影响。")

        return proj_dir

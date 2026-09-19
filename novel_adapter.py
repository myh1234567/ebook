"""小说跨文化改编与 KDP 出版准备核心引擎。

遵循 novel-adaptation-kdp 技能规范：
1. 采用“叙事功能等价”而非字对字直译。
2. 保持主线、关键转折、因果链和结局不变。
3. 建立全书改编档案 (Adaptation Bible)。
4. 美式英语创作 (en-US)，消除翻译腔与 AI 式陈词滥调。
5. 生成标准交付文件 (DOCX/TXT/MD/PNG)。
"""
import re
import sys
import json
import subprocess
import threading
import tempfile
import time
from pathlib import Path
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass

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
# 通读全书分两遍。第一遍分段读原文、逐章记事实；第二遍一次读完这些摘要，
# 做所有需要全局视野的判断（分卷点、伏笔、时间跳跃、全书定名）。
# 为什么必须分两遍：读第 1 段时模型不知道第 500 章会发生什么，这时候判断
# 「这个冲突结束了没有」只能靠猜。事实可以边读边记，判断必须等读完。
PASS1_SEGMENT_CHARS = 200000  # 第一遍每段原文多大。171 万字 ≈ 9 段，每段占窗口两成
MAX_SUMMARY_CHARS = 60000     # 单卷梗概送进每章提示词的上限
PREV_SUMMARY_CHAPTERS = 2     # 每章带前几章的摘要，用来接住上一章的结尾
# 改编全程串行，代码里已经没有线程池了：第 N 章要带第 N-1 章的摘要，还要能把
# 这一章新命名的实体立刻补进注册表给第 N+1 章用 —— 两件事都有顺序依赖，并行
# 就是在制造名字分裂。并行只在「不同的书之间」做（多开终端跑 batch --only）。
# chapter_workers 现在只剩一个用途：CLI 通道的熔断阈值随它放大（见 _call_llm）。
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
VOL_MIN, VOL_MAX = 3, 8

# 书名还没定下来时的占位名。这些永远不能当真书名用：
# 一旦当真，目录名会被回读成书名，下次重跑就不再去档案里取，坏书名就焊死了，
# 而且会印到分卷标题（「Untitled Adaptation: Book 1」）和封面上。
PLACEHOLDER_TITLE = "Untitled Adaptation"

# 模型有时不写正文，而是回过头来跟操作者说话：「Please provide the complete Chinese
# text of Chapter 1...」「I cannot proceed without...」。这种回复以前会被原样当成
# 章节正文存进缓存、进 epub、最后印在卖出去的书里。下面用来把它挡下来。
# 只查开头一段：这些词组完全可能出现在正常章节的对白里，整篇搜会误伤。
CHATTER_MARKERS = (
    "please provide", "please supply", "please share", "please paste",
    "could you provide", "the supplied excerpt", "the provided excerpt",
    "the excerpt contains", "only metadata", "no chapter text",
    "i need the", "i cannot", "i can't", "i'm unable", "i am unable",
    "as an ai", "unable to proceed", "does not contain the chapter",
)
CHATTER_SCAN_CHARS = 400
MIN_CHAPTER_CHARS = 400      # 正常一章几千字，几百字以下必有问题


def looks_like_chatter(text: str) -> str:
    """判断一段「章节正文」其实是不是模型在跟你说话。

    正常返回空串；有问题返回一句人话描述，直接拿去报错。
    """
    t = (text or "").strip()
    if not t:
        return "正文是空的"
    if len(t) < MIN_CHAPTER_CHARS:
        return f"正文只有 {len(t)} 字符，正常一章几千字"
    head = t[:CHATTER_SCAN_CHARS].lower()
    for m in CHATTER_MARKERS:
        if m in head:
            return f"开头出现「{m}」，是模型在跟你要原文，不是在写正文"
    return ""


# 书名可能被加粗、斜体或引号包着，模型每次挑的不一样，所以几种都要认
_EMPH_RE = re.compile(
    r'\*\*(.+?)\*\*|\*(.+?)\*|__(.+?)__|_(.+?)_|“(.+?)”|"(.+?)"|《(.+?)》')


# ---- 人名一致性 ----
# 老做法：档案里没有的名字让模型「自己编一个并保持一致」。这条指令不可能被满足 ——
# 第 50 章和第 180 章各写各的，互相看不见对方编了什么，于是同一个配角
# 会有好几个英文名。实测一本书里顾潇变出了四五个名字。
# 新做法：开跑之前先让模型逐块读全书认人 —— 一个人带着他的所有称呼算一条记录，
# 合并成人之后一人发一个英文名，落盘定死；每章只注入它用得到的那几条，禁止自创。
# 这里不用正则扫人名：正则只认「姓+1~2 字」，没有姓的称呼一个都扫不到，而且它
# 产出的是字符串不是人 ——「周皇」和「姬止」是不是同一个人，读过原文才知道。

# 兜底名字池。两个实体撞上同一个英文名时，其中一个改用这里的名字。
# 靠哈希定位，同一个中文名任何时候都算出同一个英文名，结果可复现。
FALLBACK_FIRST = ("Adrian", "Beatrice", "Callum", "Delia", "Edmund", "Fiona",
                  "Gideon", "Harriet", "Isaac", "Jocelyn", "Killian", "Lorna",
                  "Marcus", "Nadia", "Oscar", "Petra", "Quentin", "Rosalind",
                  "Silas", "Tessa", "Ulric", "Vera", "Wendell", "Yvette")
FALLBACK_LAST = ("Ashcroft", "Blackwood", "Carrow", "Danforth", "Ellsworth",
                 "Fairbairn", "Grantham", "Hollis", "Ingram", "Jarvis",
                 "Kingsley", "Lockhart", "Merrick", "Norwood", "Ophell",
                 "Prescott", "Quill", "Ransome", "Sterling", "Thorne")



def fallback_english_name(zh: str) -> str:
    """抽取漏掉的名字走这里。确定性：同一个中文名到哪都算出同一个英文名。"""
    import hashlib
    h = int(hashlib.md5(zh.encode("utf-8")).hexdigest(), 16)
    return (f"{FALLBACK_FIRST[h % len(FALLBACK_FIRST)]} "
            f"{FALLBACK_LAST[(h // len(FALLBACK_FIRST)) % len(FALLBACK_LAST)]}")


def names_in_chapter(raw: str, registry: Dict[str, str]) -> Dict[str, str]:
    """这一章的原文里出现了哪些已映射的名字。只注入这几条，提示词就不会被截断。"""
    return {zh: en for zh, en in registry.items() if zh in raw}


def name_was_used(en: str, body: str) -> bool:
    """这一章到底有没有按映射表用这个名字。

    不能要求全名原样出现。英文小说介绍一次全名之后，通篇都只叫名或只叫姓 ——
    实测已完成的那本书里，506 章出现主角，只有 3 章写过全名 "Luke Yarrow"，
    其余 503 章全是 "Luke"。按全名比对的话，正确的译稿会被整章判死、整章重跑。

    所以只比对名和姓：任一个作为独立单词出现，就算这一章守了表。加词边界是
    防止 "Lee" 命中 "Leeds"；姓也认，是因为有些角色通篇被人直呼其姓。
    """
    tokens = [t for t in re.findall(r"[A-Za-z']+", en) if len(t) >= 3]
    if not tokens:
        return en in body
    return any(re.search(rf"\b{re.escape(t)}\b", body)
               for t in (tokens[0], tokens[-1]))


def is_placeholder_title(name: str) -> bool:
    """判断一个名字是不是占位名（目录名的下划线形态也算）。"""
    n = (name or "").replace("_", " ").strip().lower()
    return (not n) or n == PLACEHOLDER_TITLE.lower() or n.startswith("wip ")


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
    # 番外/外传/序章/尾声这些也必须认。实测某本 422 章正文 + 8 篇番外的书，
    # 番外标题匹配不上，被当成正文并进上一章 —— 成书里 8 篇番外全丢了，
    # 而章数看起来还"对得上"，靠数字根本发现不了。
    CHAPTER_REGEX = re.compile(
        r'^\s*(第[0-9零一两二三四五六七八九十百千万亿]+[章回卷节篇]'
        r'|番外[0-9零一两二三四五六七八九十百千万]*'
        r'|外传[0-9零一两二三四五六七八九十百千万]*'
        r'|[楔契]子|序[章言幕]?|尾声|终章|后记|完结感言'
        r'|Chapter\s+\d+|[Cc]hapter\s+[IVXLCDM]+|[Ee]pilogue|[Pp]rologue'
        r'|[0-9]{1,4}\s*[\.、])\s*(.*)$',
        re.MULTILINE
    )

    # 下载站在正文前面加的信息头，实测长这样：
    #     婴语满级后，丰腴保姆成九零团宠
    #     分类：科幻
    #     总章节：161
    #     来源：笔尖中文(xbiquwk.com)
    #     ==================================================
    # 结尾那条分隔线是明确的界标，比按长度猜可靠得多。
    FRONT_SEP = re.compile(r'^\s*[=\-—_*~#]{10,}\s*$')
    FRONT_SCAN_LINES = 30        # 只在开头找，免得把正文里的分隔线当界标

    @classmethod
    def strip_front_matter(cls, text: str) -> str:
        """切掉正文前的信息头（书名/分类/总章节/来源 + 分隔线）。

        不切的话这段会被当成第 1 章送去改编 —— 模型拿到一堆元信息写不出正文，
        回一句「Please provide the complete Chinese text of Chapter 1」，
        那句话被当成章节正文存进缓存，一路印进了卖出去的书。每本书都中。
        """
        lines = text.splitlines()
        for i, line in enumerate(lines[:cls.FRONT_SCAN_LINES]):
            if not cls.FRONT_SEP.match(line):
                continue
            head = "\n".join(lines[:i])
            # 分隔线之前要是已经有真章节标题，说明这条线是正文的一部分，不能切
            if cls.CHAPTER_REGEX.search(head):
                break
            print(f"[切分] 去掉正文前的信息头 {i + 1} 行：{head.strip()[:36]!r}")
            return "\n".join(lines[i + 1:])
        return text

    @classmethod
    def split_text(cls, text: str) -> List[Dict[str, str]]:
        text = cls.strip_front_matter(text)
        lines = text.splitlines()
        chapters = []
        current_title = None          # None 表示还没遇到过任何章节标题
        current_lines = []

        def close():
            """收尾当前这一章。任何情况下都不丢内容。"""
            content = "\n".join(current_lines).strip()
            if current_title is not None:
                chapters.append({"title": current_title, "content": content})
            elif content:
                # 第一个标题之前还有正文（楔子/序章）。信息头已经在
                # strip_front_matter 里精确切掉了，所以这里剩下的一定是真内容，保留。
                chapters.append({"title": "Prologue", "content": content})

        for line in lines:
            # 原来这里有个 len(current_lines) > 5 的守卫，本意是防止正文里的
            # 「123、」被误当成章节标题。但它会把短章节的标题吞进上一章，
            # 信息头切掉之后第一章正好在首行，于是第一、二章直接被吃掉。
            # 改成看标题行本身的长度：章节标题都很短，正文里的编号行通常很长。
            if cls.CHAPTER_REGEX.match(line) and len(line.strip()) <= 40:
                close()
                current_title = line.strip()
                current_lines = []
            else:
                current_lines.append(line)
        close()

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

# 导出阶段产出的文件。08 档案和 06/12 底图不列：档案在更早的阶段就生成了，
# 底图是出图的中间产物，画不出来时会退回纯排版，不该因为缺它就判定交付不全
DELIVERABLES = ("01_English_Manuscript.docx", "02_Publishing_Copy.docx",
                "03_Publishing_Copy.txt", "04_Internal_Synopsis.docx",
                "05_Ebook_Cover.png", "07_Manuscript.epub",
                "09_Image_Prompts.txt", "10_Quality_Check_Report.md",
                "12_Promotional_Poster.png")
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
        self._tier_lock = threading.Lock()   # 熔断状态可能被 GUI 线程同时读改
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
                # 书名从高度 9.5% 往下排，作者名在 90.5%，人头顶在画面上半部就会被字盖住。
                # 所以构图要求写死在这里，不靠每次的 brief 去提。
                "COMPOSITION (hard requirement): the top 45% of the frame must stay visually "
                "quiet and open — sky, mist, distant landscape, empty wall. Title type is laid "
                "over that band. Place the main figure LOW in the frame: the head sits roughly "
                "60-70% of the way down from the top, well inside the lower half. Never put a "
                "head, face, or any key detail in the upper third. Keep the bottom 10% clear "
                "too — the author name goes there.\n\n"
                "STYLE (hard requirement): photorealistic cinematic photography. Real human "
                "anatomy, real skin and fabric texture, natural or practical lighting, shallow "
                "depth of field, colour-graded like a still frame from a live-action film. "
                "NOT anime, NOT manga, NOT comic book or cel-shaded, NOT cartoon, NOT a "
                "stylised digital painting.\n\n"
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
- The target setting MUST be Western (Anglophone, European, or a secondary world built
  from Western material — Regency Britain, Gilded Age America, Norse or Celtic fantasy,
  1920s New York, a Ruritanian court...). Do NOT choose China, Japan, Korea, or any
  East-Asian-inspired world, and do not keep wuxia/xianxia trappings under new labels.
  The whole cultural frame is being replaced, not relabelled: people, place, institutions,
  religion, food, dress and era all become Western.

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

    def _segment_chapters(self, raw_chapters: List[Dict[str, str]]) -> List[Dict]:
        """把全书按字符数切成连续段，段边界一律落在章边界上。

        切段只为「一次调用装得下」，不为理解剧情 —— 所以纯按长度切，不猜剧情节点。
        剧情节点是第二遍读摘要时才判断的，那时候才看得见全书。
        """
        segs, cur, n = [], [], 0
        for i, c in enumerate(raw_chapters, 1):
            cur.append((i, c))
            n += len(c["content"])
            if n >= PASS1_SEGMENT_CHARS:
                segs.append({"first": cur[0][0], "last": cur[-1][0],
                             "items": cur, "chars": n})
                cur, n = [], 0
        if cur:
            segs.append({"first": cur[0][0], "last": cur[-1][0],
                         "items": cur, "chars": n})
        return segs

    def read_source_pass1(self, raw_chapters: List[Dict[str, str]], proj_dir: Path,
                          cancel_event=None) -> Dict:
        """第一遍：把全文分段读完，逐章记事实。返回 {章号: 摘要} 和实体清单。

        全书一次送不进去，所以分段 —— 但「分段读」和「没读全」是两回事：
        每段都完整进过模型，产出落盘，九段跑完全书每个字都被读过一次。
        跑完之后第二遍读的是这一遍的摘要（体量小一个数量级），那一次才是
        真正的全局视野。

        这一遍只许记事实（谁做了什么、谁和谁什么关系、出现了什么东西），
        不许下判断。读第 1 段时模型不知道第 500 章会发生什么，这时候判断
        「这个冲突结束了没有」只能靠猜，而猜错了后面看不出来。

        串行，不并行：后一段带着前面累计的实体表去读，所以「周皇」出现在第 3 段、
        「姬止」出现在第 1 段时，模型当场就能认出是同一个人。并行的话两段互相
        看不见，只能等最后合并时靠描述去猜。
        """
        out_dir = proj_dir / "_pass1"
        out_dir.mkdir(parents=True, exist_ok=True)
        segs = self._segment_chapters(raw_chapters)
        total_ch = len(raw_chapters)
        self.log(f"第一遍通读：{total_ch} 章 / {sum(s['chars'] for s in segs):,} 字符，"
                 f"分 {len(segs)} 段串行读（每段约 {PASS1_SEGMENT_CHARS // 1000}K 字符）")

        sys_p = ("You read a Chinese web novel and record facts for an adaptation team. "
                 "You record what happens; you do not judge what it means. "
                 "Output valid JSON only, no commentary.")

        summaries: Dict[int, str] = {}
        entities: List[Dict] = []

        for k, seg in enumerate(segs, 1):
            if cancel_event and cancel_event.is_set():
                break
            f = out_dir / f"{k:04d}.json"
            if f.exists():
                try:
                    got = json.loads(f.read_text("utf-8"))
                    summaries.update({int(n): s for n, s in (got.get("chapters") or {}).items()})
                    entities.extend(got.get("entities") or [])
                    self.log(f"  · 第 {k}/{len(segs)} 段（第 {seg['first']}-{seg['last']} 章）已有，复用")
                    continue
                except Exception:
                    pass

            text = "\n\n".join(f"【第 {i} 章 {c['title']}】\n{c['content']}"
                               for i, c in seg["items"])
            # 已经认出来的实体一起带过去，让模型把新出现的称呼挂到已有的人身上，
            # 而不是当成新人再记一遍
            known = ""
            if entities:
                known = "Entities already recorded earlier in this book (attach new "
                known += "aliases to these instead of creating duplicates):\n"
                known += "\n".join(f"  {'、'.join(e['aliases'][:6])} — {e.get('what','')}"
                                   for e in entities[:400])
            prompt = f"""Record the facts in chapters {seg['first']}-{seg['last']} of this novel.

{known}

Return JSON only:
{{"chapters": {{"{seg['first']}": "这一章发生了什么：事件、谁做的、结果", "...": "..."}},
  "entities": [{{"aliases": ["叫法1","叫法2"], "what": "他/它是什么，与谁什么关系",
                "first_ch": {seg['first']}}}]}}

Rules:
- "chapters" must contain one entry for EVERY chapter number from {seg['first']} to
  {seg['last']}, no gaps. 2-4 sentences each: what happened, who did it, what changed.
  Name the concrete objects, documents, injuries and promises that show up — a later
  step needs them to find what was planted here and paid off hundreds of chapters later.
- "entities" is one entry per real thing: people, but also places, households, sects,
  dynasties, named objects. Put EVERY way this text refers to it into "aliases".
  The same person is called by full name, by surname plus a title, by a nickname.
  The same house is called 李府, 李小姐的家, 李姥爷的家 — ONE place, three aliases.
  A dynasty built out of a person's title is a SEPARATE entity, not that person.
- Record facts only. Do NOT judge whether a conflict has ended, whether something is
  foreshadowing, or where the story arcs break — you have not read the rest of the book
  yet, and a later step decides all of that with the whole book in view.

Text:
\"\"\"{text}\"\"\"
"""
            self.log(f"  · 正在读第 {k}/{len(segs)} 段：第 {seg['first']}-{seg['last']} 章"
                     f"（{seg['chars']:,} 字符）…")
            raw = self._call_llm(prompt, sys_p) or ""
            got = {}
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    got = json.loads(m.group(0))
                except Exception as exc:
                    self.log(f"    ⚠️ 第 {k} 段解析失败（{exc}）")
            chs = {int(n): str(s).strip()
                   for n, s in (got.get("chapters") or {}).items()
                   if str(n).isdigit() and str(s).strip()}
            ents = [{"aliases": [str(a).strip() for a in (e.get("aliases") or [])
                                 if str(a).strip()],
                     "what": str(e.get("what", "")).strip()}
                    for e in (got.get("entities") or []) if isinstance(e, dict)]
            ents = [e for e in ents if e["aliases"]]
            want = set(range(seg["first"], seg["last"] + 1))
            missing = sorted(want - set(chs))
            if missing:
                self.log(f"    ⚠️ 这一段有 {len(missing)} 章没记到："
                         f"{missing[:10]}{' …' if len(missing) > 10 else ''}")
            if chs:
                f.write_text(json.dumps({"chapters": {str(n): s for n, s in chs.items()},
                                         "entities": ents},
                                        ensure_ascii=False, indent=2), "utf-8")
            summaries.update(chs)
            entities.extend(ents)
            self.log(f"    ✓ 记下 {len(chs)} 章、{len(ents)} 个实体"
                     f"（累计 {len(summaries)}/{total_ch} 章）")

        gaps = [i for i in range(1, total_ch + 1) if i not in summaries]
        if gaps:
            self.log(f"⚠️ 全书还有 {len(gaps)} 章没有摘要，章号："
                     f"{gaps[:20]}{' …' if len(gaps) > 20 else ''}")
        else:
            self.log(f"第一遍完成：{len(summaries)}/{total_ch} 章全部记下，"
                     f"{len(entities)} 条实体记录。")
        return {"summaries": summaries, "entities": entities}

    def plan_story(self, pass1: Dict, total_ch: int, proj_dir: Path) -> Dict:
        """第二遍：一次读完全部逐章摘要，做所有需要全局视野的判断。

        输入是第一遍压缩出来的东西（823 章摘要约 165K token），一次装得下 ——
        所以这一次模型手里是全书完整骨架，挑分卷点、判断哪个冲突真结束、
        哪条线是伏笔，都有依据，不是拿局部猜。

        产出落盘 13_Story_Plan.json；里面的实体表另存一份 12_Name_Registry.json，
        因为改编、导出、上架都按那个文件名读。
        """
        cache = proj_dir / "13_Story_Plan.json"
        if cache.exists():
            try:
                plan = json.loads(cache.read_text("utf-8"))
                if plan.get("volumes"):
                    self.log(f"复用已有的全书规划（{len(plan['volumes'])} 卷）")
                    return plan
            except Exception:
                pass

        summaries = (pass1 or {}).get("summaries") or {}
        if not summaries:
            # 缓存文件在但内容坏了，而这一轮又没跑第一遍 —— 硬报出来，
            # 不能拿空骨架去规划全书：分卷和定名全错，而且会一路带到成书
            self.log("⚠️ 没有逐章摘要可用（13_Story_Plan.json 坏了？删掉它重跑）")
            return {}
        listing = "\n".join(f"[{n}] {summaries[n]}" for n in sorted(summaries))
        ents = "\n".join(f"{'、'.join(e['aliases'][:8])} — {e.get('what','')}"
                         for e in pass1["entities"][:800])
        self.log(f"第二遍：一次读完 {len(summaries)} 章摘要（{len(listing):,} 字符），"
                 f"规划分卷、定名、找伏笔…")

        sys_p = ("You plan the English adaptation of a Chinese novel. You have the whole "
                 "book in front of you. Output valid JSON only, no commentary.")
        user_p = f"""Below is a per-chapter record of an entire {total_ch}-chapter Chinese
novel, followed by every named entity recorded while reading it. You can see the whole
book at once — every judgement below must use that.

Return JSON:
{{"synopsis": "the whole story in 400-600 words, English",
  "volumes": [{{"from": 1, "to": 94, "title": "English volume title",
                "arc": "what this volume covers and how it ends"}}],
  "people": [{{"name": "English Name", "zh": ["称呼", "别称"], "kind": "person|place|org|thing"}}],
  "foreshadow": [{{"setup_ch": 12, "payoff_ch": 480, "what": "戒指藏进抽屉 -> 成为关键证据"}}],
  "time_jumps": [{{"at_ch": 301, "gap": "十年", "note": "主角从少年变成成年"}}]}}

Rules for "volumes" — split into {VOL_MIN}-{VOL_MAX} volumes, each sold as its own book:
- Cut where the STORY breaks, never at an even chapter count. Strongest signals first:
  a time jump, a main goal finally won or lost, a move to a new region, the cast turning
  over. A fight that ends but whose winner turns out to be someone's agent is NOT a break.
- Every volume ends on "this chapter of their life is closed, but I want the next book".
- Volumes must tile chapters 1..{total_ch} exactly: no gap, no overlap, in order.
- Aim for 80,000-120,000 English words per volume (roughly 0.65 English words per Chinese
  character), but a real story break beats hitting the word count.

Rules for "people" — this table is the whole book's naming law:
- ONE entry per real thing. Merge every alias of the same person, place or household into
  one entry. Records above may list the same thing several times under different aliases —
  the descriptions tell you which are the same; merge them.
- Sharing a surname does not make two entries one; relatives share surnames, and a
  household is not its owner.
- Names fully native to {self.config.target_country}, {self.config.target_era}. Never
  romanize, no pinyin, no Chinese surname left as an English-looking word (Shen, Lin,
  Wang), no "sounds similar" carryover. A reader must not be able to tell this story
  began in Chinese.
- Distinct entries never share a name. Same Chinese family -> same Western surname.

Rules for "foreshadow" and "time_jumps": list only what the records actually show. These
drive what the writing step is told about each chapter, so a wrong entry does real damage.

Entities recorded while reading:
{ents}

Per-chapter record:
{listing}
"""
        raw = self._call_llm(user_p, sys_p) or ""
        plan = {}
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                plan = json.loads(m.group(0))
            except Exception as exc:
                self.log(f"⚠️ 全书规划解析失败（{exc}）")
        if not plan.get("volumes") or not plan.get("people"):
            # 这一步失败会把整本书带偏（全书顶着空名册跑，各章自己编名），
            # 但以前只留下一行「失败了」，模型原样回的内容当场丢掉 ——
            # 事后完全分不清是没回 JSON、回了但缺字段、还是输出被截断。
            # 落盘留证，并说清缺的是哪半边。
            bad = proj_dir / "13_Story_Plan.failed.txt"
            try:
                bad.write_text(raw, "utf-8")
            except Exception:
                pass
            miss = "、".join(k for k in ("volumes", "people") if not plan.get(k))
            self.log(f"⚠️ 模型没给出可用的分卷或名册（缺 {miss}），这一步失败了。"
                     f"模型回了 {len(raw)} 字符，已原样存进 {bad.name} 供排查。"
                     f"开头 200 字符：{raw[:200]!r}")
            return {}

        # 分卷必须严丝合缝盖满全书。缺一段就是整段章节没人认领，
        # 到导出时才发现的话，前面几小时的改编已经按错的卷跑完了。
        vols = sorted([v for v in plan["volumes"]
                       if isinstance(v, dict) and v.get("from") and v.get("to")],
                      key=lambda v: int(v["from"]))
        fixed, nxt = [], 1
        for v in vols:
            a, b = int(v["from"]), int(v["to"])
            a = max(a, nxt)
            if b < a:
                continue
            fixed.append({**v, "from": a, "to": min(b, total_ch)})
            nxt = min(b, total_ch) + 1
        if fixed and nxt <= total_ch:
            self.log(f"  · 分卷没盖到第 {nxt}-{total_ch} 章，并进最后一卷")
            fixed[-1]["to"] = total_ch
        plan["volumes"] = fixed

        people = []
        for p in plan["people"]:
            en = str((p or {}).get("name", "")).strip()
            zhs = [str(z).strip() for z in (p.get("zh") or []) if str(z).strip()]
            if zhs and en and en.isascii() and re.match(r'^[A-Za-z][\w\s\'.-]*$', en):
                people.append({"name": en, "zh": zhs, "kind": p.get("kind", "person")})
        # 撞名按「实体」查，不按称呼查：同一个人的几种称呼共用一个英文名是对的，
        # 按称呼查会把它当成撞名拆开，正好制造要防的问题。
        seen = {}
        for p in people:
            if p["name"] in seen:
                new = fallback_english_name(p["zh"][0])
                self.log(f"  · {p['zh'][0]} 与 {seen[p['name']]} 撞名（{p['name']}），改用 {new}")
                p["name"] = new
            seen[p["name"]] = p["zh"][0]
        plan["people"] = people

        cache.write_text(json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")
        (proj_dir / "12_Name_Registry.json").write_text(
            json.dumps({"people": people}, ensure_ascii=False, indent=2), "utf-8")
        self.log(f"-> 13_Story_Plan.json：{len(plan['volumes'])} 卷、{len(people)} 个实体、"
                 f"{len(plan.get('foreshadow') or [])} 条伏笔、"
                 f"{len(plan.get('time_jumps') or [])} 处时间跳跃")
        for v in plan["volumes"]:
            self.log(f"     第 {v['from']:>3}-{v['to']:>3} 章  {v.get('title', '')}")
        return plan
    def _make_kpf(self, docx: Path, proj_dir: Path):
        """用 Kindle Create 把母稿转成 KPF，落在 proj_dir/KPF/ 下。

        为什么值得做：KDP 也收 DOCX，但转换在它服务器上做，排版我们看不见也管不着。
        KPF 是本地转好的成品，上传后不再二次转换。

        失败只记一笔，不抛：上传那边没有 KPF 会自动退回 DOCX，为了一个可选的
        排版升级把整本书的交付卡住不值得。

        注意这一步会真的操作 Kindle Create 的界面，几分钟，期间别抢鼠标键盘。
        """
        if sys.platform != "darwin":
            return
        if not Path("/Applications/Kindle Create.app").exists():
            self.log("  · 没装 Kindle Create，跳过 KPF；上传会用 DOCX")
            return
        have = sorted(proj_dir.glob("KPF/*.kpf"))
        if have and have[0].stat().st_mtime >= docx.stat().st_mtime:
            self.log(f"  · 已有 {have[0].name} 且不比母稿旧，跳过转换")
            return
        try:
            import kindle_create
            kindle_create.KindleCreate(log=self.log).convert(
                docx=docx, out_dir=proj_dir,
                title=self.config.book_title, author=self.config.author_name)
        except Exception as exc:
            self.log(f"  ⚠️ KPF 转换失败（{exc}），上传会退回 DOCX")

    @staticmethod
    def _persist_registry_additions(proj_dir: Path, registry: Dict[str, str]):
        """把改编途中新增的称呼写回注册表，保持按人分组的存法。

        只增不改：已有实体的英文名一个字不动，新称呼挂到同名实体下，
        剩下的才另起一条。改已有的名字会让前面写完的章节全部对不上。
        """
        path = proj_dir / "12_Name_Registry.json"
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception:
            data = {}
        people = data.get("people") if isinstance(data, dict) else None
        if not isinstance(people, list):
            people = [{"name": v, "zh": [k]} for k, v in (data or {}).items()
                      if isinstance(v, str)]
        by_name = {}
        for p in people:
            by_name.setdefault(p.get("name"), p)
        for zh, en in registry.items():
            p = by_name.get(en)
            if p is None:
                p = {"name": en, "zh": []}
                people.append(p)
                by_name[en] = p
            if zh not in p["zh"]:
                p["zh"].append(zh)
        path.write_text(json.dumps({"people": people}, ensure_ascii=False, indent=2),
                        "utf-8")

    @staticmethod
    def _flatten_registry(data) -> Dict[str, str]:
        """把落盘的注册表摊成各章要用的 {称呼: 英文名}。

        新格式是按人存的 {"people": [{"name", "zh": [...]}]}；老项目里存的是
        扁平的 {称呼: 英文名}，一并认 —— 已经跑到一半的书不该因为换了存法就
        重新定名字。
        """
        if isinstance(data, dict) and isinstance(data.get("people"), list):
            return {zh: p["name"] for p in data["people"]
                    for zh in (p.get("zh") or []) if p.get("name")}
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, str)}
        return {}

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
6. Historical Fact-Check Log — ONLY the facts the plot actually leans on: laws and
   institutions that existed then, what a person of each character's standing was legally
   and practically allowed to do, weapons/ranks/unit structure, how fast news and travel
   moved, what medicine could and could not fix, and whether key objects and clothing
   existed yet. One row each: claim -> verdict (verified / uncertain) -> what the story
   does about it. Mark anything you are not sure of as UNCERTAIN instead of asserting it;
   an invented town or regiment inside a real period is fine, silently bending a real
   event to fit the plot is not.

HARD REQUIREMENTS:
- Every name must be fully native to {self.config.target_country}, {self.config.target_era}.
  No pinyin, no Chinese surname kept as an English-looking word (Shen, Lin, Wang, Chen...),
  no East-Asian-flavoured invented names, no "sounds similar to the original" carryover.
  A reader must not be able to tell this story began in Chinese.
- Institutions must be REBUILT, not renamed. If the original turns on a procedure
  (a succession, an election, an appointment, an inheritance, a trial), work out how that
  procedure actually operates in {self.config.target_country} in {self.config.target_era} —
  real offices, real timelines, real eligibility rules — and rewrite the mechanics to match.
  Section 3 must contain a "Mechanics Translation" table: original procedure -> the target
  society's real procedure -> what changes in the plot because of it. Renaming an emperor
  a "president" while keeping the original's timing and powers is a failure.
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

## 6. Historical Fact-Check Log
| Claim the plot leans on | Verdict | What the story does |
| --- | --- | --- |
| A county sheriff could hold a suspect without charge for days | Verified | Used as-is for the jailhouse scene |
| Telegraph reached frontier towns by the 1880s | Verified | News of the land sale arrives in a day, not a week |
| A woman could hold a land deed in her own name | UNCERTAIN — varies by state | Clara's deed is held in trust; plot function unchanged |
"""
        return response

    @staticmethod
    def extract_title(bible_md: str) -> str:
        """从改编档案里挑出推荐书名。

        模型给的形态很杂，实测见过这几种：
            **Recommended title:** *书名*     加粗的是标签，书名在后面用斜体
            ### 1. *书名* — Recommended       书名斜体，Recommended 是普通词
            **书名** — Recommended            书名加粗
            Option 1 (Recommended): 书名      纯文本

        原来只认加粗，碰上第一种时唯一的加粗段是「Recommended title:」这个标签，
        被那条「滤掉含 recommend 的」规则干掉，剩下空列表 —— 整本书就落到占位名，
        分卷标题和封面上全印着 Untitled Adaptation。
        """
        def clean(s: str) -> str:
            return (s or "").strip(" *_\"“”‘’:：—–-").strip()

        def emphasized(line: str) -> List[str]:
            out = []
            for m in _EMPH_RE.finditer(line):
                v = clean(next((g for g in m.groups() if g), ""))
                if v and len(v) < 80:
                    out.append(v)
            return out

        # 1) 带 recommend 的行：书名是那个「本身不含 recommend」的强调段
        for line in bible_md.splitlines():
            if "recommend" not in line.lower():
                continue
            cands = [s for s in emphasized(line)
                     if "recommend" not in s.lower()
                     and not s.lower().startswith("option")]
            if cands:
                return cands[0]
            # 整行没有强调标记，退回冒号后面那截："Recommended title: 书名"
            if ":" in line or "：" in line:
                tail = re.sub(r'[*_#]+', '', re.split(r'[:：]', line, maxsplit=1)[1])
                # 书名后面常跟一段理由，用空格围起来的破折号/连字符隔开。
                # 只切「带空格的」分隔符，免得把 Twenty-One 这种带连字符的书名切断。
                tail = clean(re.split(r'\s+[—–-]\s+', tail, maxsplit=1)[0])
                if tail and len(tail) < 80 and "recommend" not in tail.lower():
                    return tail

        # 2) 档案的标题行：# Adaptation Bible: *书名*
        m = re.search(r'^#+\s*Adaptation Bible\s*[:：]\s*(.+)$', bible_md, re.MULTILINE)
        if m:
            t = clean(re.sub(r'[*_]+', '', m.group(1)))
            if t and len(t) < 80:
                return t

        # 3) 老格式 "Option 1 (Recommended): The Title — 理由"
        m = re.search(r'Option 1.*?:\s*\**(.+?)\**\s*(?:—|-|\(|$)', bible_md, re.MULTILINE)
        if m and m.group(1).strip():
            return clean(m.group(1))
        return ""

    def adapt_chapter(self, index: int, raw_title: str, raw_content: str,
                      bible_text: str, registry: Optional[Dict[str, str]] = None,
                      prev_summaries: Optional[List[str]] = None,
                      vol_brief: str = "",
                      notes: Optional[List[str]] = None) -> Dict[str, str]:
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
            "1. NAMES: The NAME LOCK table below is the law. Every person in this chapter "
            "appears there — use that exact English name, spelled exactly that way. "
            "Do NOT invent a name for anyone in the table, do NOT shorten or vary it, and "
            "never output a Chinese name or a pinyin transliteration (Han Li, Wang, Li Wei). "
            "If someone or somewhere in this chapter is NOT in the table, name them and "
            "report them in the JSON block at the end — the next chapter gets your name "
            "from there, so it is used consistently for the rest of the book.\n"
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

        # 只注入本章出现的那几个名字。以前是把整份档案塞进来再截到 3 万字符 ——
        # 映射表一长，后半截被静默切掉，那些角色的名字模型根本看不到，只能现编。
        # 定向词表通常几百字符，既不会被截，也不给模型自由发挥的余地。
        chapter_names = names_in_chapter(src, registry or {})
        if chapter_names:
            lock = "\n".join(f"  {zh}  ->  {en}" for zh, en in chapter_names.items())
            lock_block = (
                f"NAME LOCK — every person appearing in this chapter, and the exact English\n"
                f"name to use for them. Every chapter of this book is written against this same\n"
                f"table; it is the only thing keeping one character from having five\n"
                f"different names across the book.\n"
                f"{lock}\n")
        else:
            lock_block = ""

        # 本卷梗概：让这一章知道自己在整本书的哪个位置、这一卷要走到哪儿
        vol_block = f"\nTHIS VOLUME:\n{vol_brief[:MAX_SUMMARY_CHARS]}\n" if vol_brief else ""
        # 前几章的摘要：衔接靠它。章节是串行跑的，所以这里拿到的一定是
        # 刚写完那几章的实际内容，不是原文摘要 —— 接的是英文稿，不是中文。
        if prev_summaries:
            prev_block = ("\nWHAT JUST HAPPENED (the chapters immediately before this one,"
                          " as you wrote them):\n"
                          + "\n".join(f"  [{index - len(prev_summaries) + i}] {s}"
                                      for i, s in enumerate(prev_summaries)) + "\n")
        else:
            prev_block = ""
        # 本章的特殊指示：伏笔回收、时间跳跃、卷首卷末 —— 都是第二遍通读时判断出来的
        notes_block = ("\nSPECIAL TO THIS CHAPTER:\n"
                       + "\n".join(f"  - {n}" for n in notes) + "\n") if notes else ""

        user_prompt = f"""
{lock_block}
ADAPTATION BIBLE — this is binding, not background. Every place and term below
MUST be used exactly as mapped.
\"\"\"{bible_text[:MAX_BIBLE_CHARS]}\"\"\"
{vol_block}{prev_block}{notes_block}
Target setting: {self.config.target_country}, {self.config.target_era}
Genre: {self.config.genre}

Chinese source, chapter {index} — title: {raw_title}
\"\"\"{src}\"\"\"

Rewrite this chapter as American English fiction set in the target setting.
Relocate it completely: names, places, ranks, customs, objects. Keep the plot identical.
Pick up exactly where the previous chapter left off — same voice, and the forms of address
characters have reached by now (people who have grown close do not go back to surnames).
Format — prose first, then the JSON block, nothing else:

Chapter {index}: [Engaging English Chapter Title]
[Full novel prose paragraphs with natural dialogue and rich scene description]

```json
{{"summary": "2-3 sentences: what happened in this chapter, in English. The next chapter
              gets only this, so put in what it must not contradict.",
  "new_entities": [{{"name": "English Name", "zh": ["原文里的叫法"], "kind": "person|place|org|thing"}}]}}
```
"new_entities": only things you had to name yourself because they were missing from the
NAME LOCK table. Leave it empty when there were none. Never restate or rename an entry
that is already in the table — those are fixed for the whole book.
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
        # 先把尾部的 JSON 块摘出来再当正文处理 —— 不摘的话它会被当成正文存进缓存，
        # 一路印进书里。摘不到不算失败：摘要和增补都是锦上添花，正文才是交付物。
        meta_out, response = self._split_chapter_meta(response)

        # 提取标题与正文
        lines = response.strip().splitlines()
        first_line = lines[0].strip("# ").strip()
        body = "\n".join(lines[1:]).strip() if len(lines) > 1 else response

        # 校验再落盘。不校验的话模型的「请把原文给我」会被当成章节正文存下来，
        # 一路进缓存、进 epub、印到卖出去的书里 —— 而且因为有缓存，重跑也不会自愈。
        # 这里抛错：调用方会记下这一章失败并继续跑别的，缓存不写，下次重跑会重试。
        bad = looks_like_chatter(body)
        if bad:
            raise RuntimeError(
                f"第 {index} 章的改编结果不是正文：{bad}。"
                f"多半是送进去的原文本身没有正文 —— 原文开头：{raw_content[:60]!r}")

        # 名字回验。光在提示词里嘱咐不够 —— 必须改完回头核对，不然又是「看起来
        # 正常、实际上同一个人有五个名字」，而这种问题只能靠人工通读几十万词才发现。
        left = re.findall(r'[一-鿿]', body)
        if left:
            raise RuntimeError(
                f"第 {index} 章的英文正文里还有 {len(left)} 个中文字符"
                f"（{''.join(left[:8])}…），这一段没改编")
        # 名字一致性不再用字符串匹配去审判整章。
        #
        # 原来的规则是「中文在原文出现 3 次以上，译稿里就必须出现对应英文名，
        # 否则这一章作废」。它杀错的比杀对的多：注册表里只要混进一个像
        # 「向」「金」「江」这样的条目，子串匹配就让它命中几乎每一章，
        # 于是连着几十章被判死 —— 而那些译稿本身是好的。
        #
        # 靠加规则堵不住：今天是「向」，明天就是「王」「李」。判断「这个称呼指谁」
        # 本来就要读懂上下文，那是模型的事：表已经作为 NAME LOCK 注入提示词，
        # 表外的人模型会通过 new_entities 回报并命名，下一章就能用上。
        # 这里只记一笔给质检报告，让人去看，不替人做决定。
        missed = [f"{zh}->{en}" for zh, en in chapter_names.items()
                  if src.count(zh) >= 3 and not name_was_used(en, body)]
        return {"title": first_line or f"Chapter {index}", "content": body,
                "summary": meta_out.get("summary", ""),
                "new_entities": meta_out.get("new_entities", []),
                "name_notes": missed}

    @staticmethod
    def _split_chapter_meta(response: str):
        """把正文末尾那个 JSON 块切下来，返回 (解析出的 dict, 去掉块之后的正文)。

        模型不一定每次都带围栏、也不一定放在最后，所以找最后一个 ```json 块；
        找不到就当这次没给，正文原样返回 —— 摘要和增补缺了只是下一章少点上下文，
        为此把整章判死不值得。
        """
        blocks = list(re.finditer(r'```json\s*(\{.*?\})\s*```', response, re.DOTALL))
        if not blocks:
            return {}, response
        m = blocks[-1]
        try:
            got = json.loads(m.group(1))
        except Exception:
            return {}, (response[:m.start()] + response[m.end():]).strip()
        meta = {"summary": str(got.get("summary", "")).strip(), "new_entities": []}
        for e in (got.get("new_entities") or []):
            en = str((e or {}).get("name", "")).strip()
            zhs = [str(z).strip() for z in (e.get("zh") or []) if str(z).strip()]
            if zhs and en and en.isascii() and re.match(r'^[A-Za-z][\w\s\'.-]*$', en):
                meta["new_entities"].append({"name": en, "zh": zhs,
                                             "kind": e.get("kind", "person")})
        return meta, (response[:m.start()] + response[m.end():]).strip()

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
   Describe subject, setting, wardrobe, weather, light and mood. Photorealistic cinematic
   photography only — never ask for anime, manga, comic, cartoon, cel-shaded, oil painting
   or other stylised illustration. Compose with the main figure low in the frame and the
   top of the image open and quiet; title type is laid over that upper band.
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
                    f"Shot from low and slightly behind so he sits in the lower half of the frame, cold empty dusk sky filling the top. "
                    f"Snow flurries in the cold air, warm amber lantern glow from saloons in the background, dramatic natural lighting, moody, "
                    f"photorealistic cinematic film still, shallow depth of field, highly detailed --ar 1:1.6"
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

    # 交付物清单：(文件名, 说明)。报告里逐个查是否真的存在，不再写死 [x]。
    QC_DELIVERABLES = [
        ("01_English_Manuscript.docx", "英文正文母稿"),
        ("03_Publishing_Copy.txt", "上架文案纯文本"),
        ("05_Ebook_Cover.png", "封面"),
        ("12_Promotional_Poster.png", "宣传海报"),
        ("08_Adaptation_Bible.md", "改编档案"),
        ("11_Publishing_Metadata.json", "商品页元数据"),
    ]

    def build_qc_report(self, chapters: List[Dict[str, str]],
                        adapted: List[Dict[str, str]], bible_md: str,
                        proj_dir: Path,
                        registry: Optional[Dict[str, str]] = None) -> str:
        """真跑一遍检查再出报告。每条都有可复核的数字，不写任何没验证过的结论。"""
        issues, notes = [], []

        # 1) 章数。少一章是真丢内容，必须显眼。
        n_src, n_out = len(chapters), len(adapted)
        if n_out != n_src:
            issues.append(f"章数对不上：原文切出 {n_src} 章，成稿只有 {n_out} 章，"
                          f"差 {n_src - n_out} 章")
        cached = {int(f.stem) for f in (proj_dir / "_chapters").glob("*.json")
                  if f.stem.isdigit()}
        missing = [i for i in range(1, n_src + 1) if i not in cached]
        if missing:
            issues.append(f"有 {len(missing)} 章没有改编结果，章号："
                          f"{missing[:20]}{' …' if len(missing) > 20 else ''}")

        # 2) 模型没写正文、反而在跟人说话的章节
        chatter = [(i, looks_like_chatter(c.get("content", "")))
                   for i, c in enumerate(adapted, 1)]
        chatter = [(i, w) for i, w in chatter if w]
        if chatter:
            issues.append(f"{len(chatter)} 章不是正文（模型在跟你说话或正文过短）："
                          + "；".join(f"第 {i} 章 {w}" for i, w in chatter[:5]))

        # 3) 英文稿里残留中文 —— 说明那一段根本没改编
        cjk = re.compile(r'[一-鿿]')
        zh = [(i, len(cjk.findall(c.get("content", ""))))
              for i, c in enumerate(adapted, 1)]
        zh = [(i, n) for i, n in zh if n > 0]
        if zh:
            issues.append(f"{len(zh)} 章的英文正文里还残留中文字符："
                          + "；".join(f"第 {i} 章 {n} 个" for i, n in zh[:5]))

        # 4) 人名一致性：注册表里没有的全名，是某一章自己起的名字。
        #
        # 只看注册表里那些【姓】的人。原来是把所有「大写词 + 大写词」都当全名，
        # 实测一本书报出 5562 条，绝大多数是句首单词撞上人名：But Luke、When Luke、
        # And Luke、The Hollywood…… 报告里堆着几千条误报，等于没有报告 ——
        # 没人会去里面翻那几条真的。
        #
        # 现在只问一个问题：注册表里已知的姓，有没有配上注册表里没有的名？
        # 这正是「同一个人被某一章改了名」的形态，也是唯一值得人工去核的信号。
        known_full = {v for v in (registry or {}).values() if " " in v}
        known_last = {v.rsplit(" ", 1)[-1] for v in known_full}
        suspect = {}
        if known_last:
            pat = re.compile(
                r'\b([A-Z][a-z]{2,})\s+(' + "|".join(
                    sorted(map(re.escape, known_last), key=len, reverse=True)) + r')\b')
            for i, c in enumerate(adapted, 1):
                for m in pat.finditer(c.get("content", "")):
                    if m.group(0) not in known_full:
                        suspect.setdefault(m.group(0), set()).add(i)
        if suspect:
            top = sorted(suspect.items(), key=lambda kv: -len(kv[1]))[:8]
            issues.append(
                f"{len(suspect)} 个名字用了注册表里的姓、却配了表外的名"
                f"（多半是某章自己改了名，也可能只是句首单词撞上姓）："
                + "；".join(f"{k}（{len(v)} 章）" for k, v in top))
        elif not known_last:
            notes.append("注册表里没有「名 + 姓」形式的条目，跳过人名一致性检查")

        # 各章自己记下的疑点：表里有这个称呼、原文反复出现，但译稿里没找到对应英文名。
        # 这不一定是错 —— 可能是称呼在这一章指的是别的东西，也可能真漏了。
        # 所以只在报告里列出来让人看，不在改编时判死整章（那样杀错的比杀对的多）。
        flagged = {}
        for i, c in enumerate(adapted, 1):
            for n in (c.get("name_notes") or []):
                flagged.setdefault(n, set()).add(i)
        if flagged:
            top = sorted(flagged.items(), key=lambda kv: -len(kv[1]))[:8]
            notes.append(
                f"{len(flagged)} 条映射在译稿里没找到对应英文名（值得抽查，不一定是错）："
                + "；".join(f"{k}（{len(v)} 章）" for k, v in top))

        # 5) 交付物是否真的存在
        for fn, desc in self.QC_DELIVERABLES:
            if not (proj_dir / fn).exists():
                notes.append(f"缺少 {fn}（{desc}）")

        verdict = ("❌ 未通过 —— 下列问题会进入成书，先修再上架"
                   if issues else "✅ 自动检查未发现问题")
        lines = [
            "# 质检报告（自动检查，非人工通读）",
            "",
            f"- 书名：{self.config.book_title}",
            f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 原文章节：{n_src}　成稿章节：{n_out}",
            "",
            f"## 结论：{verdict}",
            "",
        ]
        if issues:
            lines.append("## 发现的问题")
            lines += [f"{i}. {t}" for i, t in enumerate(issues, 1)]
            lines.append("")
        if notes:
            lines.append("## 提示")
            lines += [f"- {t}" for t in notes]
            lines.append("")
        lines += [
            "## 这份报告查了什么",
            "- 章数是否与切分结果一致、有没有缺号",
            "- 有没有「模型在跟你说话」而不是正文的章节",
            "- 英文正文里有没有残留中文",
            "- 有没有档案映射表之外的人名（各章自行编名的迹象）",
            "- 同姓不同名（同一个人被改名的典型形态）",
            "- 交付文件是否真的存在",
            "",
            "## 这份报告没查什么",
            "- 情节是否忠于原著、有没有漏掉支线或番外",
            "- 译文质量、人物口吻、文化迁移是否到位",
            "- 政治/法律/制度等设定是否真的重构过，而不只是换了名称",
            "",
            "以上几项必须人工对照原著,自动检查给不出结论。",
        ]
        return "\n".join(lines)

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
            # 每卷单独转 KPF。分卷开着时上架的是各卷、不是全书版，所以 KPF 必须
            # 落到卷目录里 —— 只在全书那一层转的话，split_volumes 默认开着，
            # 全书 docx 压根不生成，KPF 一次都不会有，传上去的永远是 DOCX。
            # 代价是每卷 3-4 分钟的 Kindle Create，8 卷就是半小时，且期间别抢鼠标。
            if getattr(self.config, "kdp_make_kpf", False):
                self._make_kpf(vdir / "01_English_Manuscript.docx", vdir)
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
            # _wip_xxx / Untitled_Adaptation 都是书名还没定下来时的占位名，
            # 拿它当书名的话：目录永远不会改名、分卷会叫「Untitled Adaptation: Book 1」、
            # 封面上也印这串，而且下次重跑不会再去档案里取，坏书名就永久焊死了。
            if not self.config.book_title and not is_placeholder_title(proj_dir.name):
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
        plan_path = proj_dir / "13_Story_Plan.json"
        pass1 = None
        # 第一遍通读。两个下游都要它（档案、全书规划），两个都缓存命中时才跳过 ——
        # 它本身也有 _pass1/ 缓存，所以这里跳过省的是读缓存的时间，不是调用费。
        if not (bible_path.exists() and bible_path.stat().st_size > 200
                and plan_path.exists()):
            stage("通读全书")
            pass1 = self.read_source_pass1(chapters, proj_dir, cancel_event)
            if cancel_event and cancel_event.is_set():
                return proj_dir
        book_summary = "\n".join(
            f"[{n}] {s}" for n, s in sorted((pass1 or {}).get("summaries", {}).items()))

        if bible_path.exists() and bible_path.stat().st_size > 200:
            bible_md = bible_path.read_text("utf-8")
            self.log(f"复用已有的改编档案: {bible_path.name}（{len(bible_md)} 字符）")
        else:
            stage("生成改编档案")
            bible_md = self.generate_adaptation_bible(chapters, book_summary)
            bible_path.write_text(bible_md, "utf-8")
            self.log(f"已生成并保存改编档案: {bible_path.name}")

        # 第二遍：一次读完全部逐章摘要，做所有要全局视野的判断 ——
        # 分卷点、伏笔、时间跳跃、全书定名。缓存命中就直接用，不重跑。
        stage("规划全书")
        story_plan = self.plan_story(pass1 or {}, len(chapters), proj_dir)
        name_registry = self._flatten_registry(
            {"people": story_plan.get("people") or []})
        if not name_registry:
            # 规划失败就退回上一次落盘的表，别拿空表去跑几千章
            try:
                name_registry = self._flatten_registry(json.loads(
                    (proj_dir / "12_Name_Registry.json").read_text("utf-8")))
            except Exception:
                name_registry = {}
            if not name_registry:
                # 退无可退时必须停。带着空名册往下跑的代价是整本书：每章自己现起
                # 英文名，同一个角色在不同章叫不同名字，而这些都已经写进
                # _chapters/ 缓存 —— 事后补上名册也改不回来，只能整本重跑。
                # 实测一次 227 章烧掉 10 小时，最后质检报出 923 个表外人名。
                raise RuntimeError(
                    "全书规划失败，也没有可用的 12_Name_Registry.json。"
                    "这时候继续改编，每章会各自编名，成书里同一个角色会有好几个"
                    "英文名，且事后无法修正（_chapters/ 缓存整份复用）。"
                    "请重跑这一步 —— _pass1/ 缓存还在，不用重读全书。")
            self.log("⚠️ 全书规划没拿到名册，退回已落盘的映射表继续。")

        if progress_cb:
            progress_cb(0.2)
        if cancel_event and cancel_event.is_set():
            return proj_dir

        # 提取或确认最终书名，并把项目目录改成书名——续跑要靠目录名对得上
        # 占位名也要当成「还没定书名」重试一次：上一轮解析失败留下的
        # Untitled Adaptation 不能就这么一直带下去。
        if is_placeholder_title(self.config.book_title):
            self.config.book_title = self.extract_title(bible_md)
            if self.config.book_title:
                self.log(f"采用档案推荐的书名：{self.config.book_title}")
            else:
                self.config.book_title = PLACEHOLDER_TITLE
                self.log(f"⚠️ 没能从 08_Adaptation_Bible.md 里认出推荐书名，"
                         f"暂用「{PLACEHOLDER_TITLE}」。这个名字会印进分卷标题和封面，"
                         f"别就这么发出去 —— 用 --title \"你的书名\" 指定，"
                         f"或在界面「英文书名」里手填。")
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

        # 已落盘的直接读，没跑的按章号顺序串行补。
        # 串行不是为了省事，是两处顺序依赖决定的：第 N 章要带第 N-1 章的摘要才接得上，
        # 这一章新命名的人物要立刻补进注册表给第 N+1 章用。并行两样都做不到 ——
        # 两个 worker 同时遇到同一个新人物会各起一个名字，那正是名字分裂的成因。
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

        if todo:
            self.log(f"待改编 {len(todo)} 章，按章号串行补。")

        failed = []
        vols = story_plan.get("volumes") or []
        added = 0

        def brief_for(idx: int) -> str:
            for v in vols:
                if int(v.get("from", 0)) <= idx <= int(v.get("to", 0)):
                    return (f"Book \"{v.get('title', '')}\" covers chapters "
                            f"{v['from']}-{v['to']}. {v.get('arc', '')}")
            return story_plan.get("synopsis", "")[:MAX_SUMMARY_CHARS]

        def notes_for(idx: int) -> List[str]:
            """这一章的特殊指示。全部来自第二遍通读时的全局判断，不是就地猜的。"""
            out = []
            for v in vols:
                if idx == int(v.get("to", 0)):
                    out.append("This is the LAST chapter of this book. Land it on a close "
                               "that satisfies, while leaving the reader wanting the next one.")
                if idx == int(v.get("from", 0)) and idx > 1:
                    out.append("This OPENS a new book. Re-establish who and where we are "
                               "without recapping — a reader may start here.")
            for fs in (story_plan.get("foreshadow") or []):
                if int(fs.get("setup_ch", 0) or 0) == idx:
                    out.append(f"PLANT, do not explain: {fs.get('what', '')} — it pays off "
                               f"in chapter {fs.get('payoff_ch')}. Put the concrete detail on "
                               f"the page so it can be called back later.")
                if int(fs.get("payoff_ch", 0) or 0) == idx:
                    out.append(f"PAY OFF what was planted in chapter {fs.get('setup_ch')}: "
                               f"{fs.get('what', '')}")
            for tj in (story_plan.get("time_jumps") or []):
                if int(tj.get("at_ch", 0) or 0) == idx:
                    out.append(f"TIME JUMP of {tj.get('gap', '')} before this chapter. "
                               f"{tj.get('note', '')} Ages, circumstances and the way people "
                               f"speak to each other must all have moved on.")
            return out

        for idx, ch in todo:
            if cancel_event and cancel_event.is_set():
                break
            stage(f"改编第 {idx}/{total_ch} 章")
            # 前几章的摘要：优先用这一轮刚写的，续跑时从缓存里读
            prev = []
            for j in range(idx - PREV_SUMMARY_CHAPTERS, idx):
                if j < 1:
                    continue
                s = (by_idx.get(j) or {}).get("summary", "")
                if not s:
                    cp = ch_dir / f"{j:04d}.json"
                    if cp.exists():
                        try:
                            s = json.loads(cp.read_text("utf-8")).get("summary", "")
                        except Exception:
                            s = ""
                if s:
                    prev.append(s)
            try:
                adapted = self.adapt_chapter(idx, ch["title"], ch["content"], bible_md,
                                             name_registry, prev_summaries=prev,
                                             vol_brief=brief_for(idx), notes=notes_for(idx))
            except Exception as exc:
                # 一章挂掉不能把整本带崩：记下来继续跑，缓存不写，下次重跑会重试这一章
                failed.append((idx, str(exc)))
                self.log(f"  ✗ 第 {idx} 章跳过：{exc}")
                continue
            # 这一章新命名的实体立刻补进表，第 idx+1 章就能用上。
            # 只增不改：已经定下的名字一个字都不动，否则前面写完的章节全部对不上。
            for e in adapted.get("new_entities") or []:
                fresh = [z for z in e["zh"] if z not in name_registry]
                if not fresh:
                    continue
                for z in fresh:
                    name_registry[z] = e["name"]
                added += len(fresh)
                self.log(f"     + 第 {idx} 章新增：{'、'.join(fresh)} -> {e['name']}")
            # 立刻落盘：断在哪儿下次就从哪儿接着跑
            (ch_dir / f"{idx:04d}.json").write_text(
                json.dumps(adapted, ensure_ascii=False), "utf-8")
            by_idx[idx] = adapted
            if progress_cb:
                progress_cb(0.2 + 0.5 * (len(by_idx) / max(total_ch, 1)))

        if added:
            # 增补的条目要落盘，否则下次重跑又是从旧表开始，同一批人再命名一次
            self._persist_registry_additions(proj_dir, name_registry)
            self.log(f"改编途中新增 {added} 条称呼，已写回 12_Name_Registry.json")

        if cancel_event and cancel_event.is_set():
            self.log("收到中断信号，已完成的章节都存好了，下次点「开始改编」会接着跑。")

        # 失败的章要显眼地报出来。以前模型的「请把原文给我」会被当成正文存下来，
        # 一声不吭进了成书；现在挡下来了，但不报的话就变成默默缺章，同样看不见。
        if failed:
            self.log(f"⚠️ 有 {len(failed)} 章没改编成功，缓存没写，重跑会自动重试：")
            for idx, why in failed[:10]:
                self.log(f"     第 {idx} 章：{why}")
            if len(failed) > 10:
                self.log(f"     …另有 {len(failed) - 10} 章")

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
        #
        # 但跳过的前提是分卷真顶得上。只看 split_volumes 这个开关的话，规划那一步
        # 没给出分卷方案时就两头落空：全书版被跳过、分卷无卷可导，整本书一个正文
        # 文件都没有，几百分钟跑完到上架预检才发现「未找到正文文件」。
        # 所以先把卷算出来，按「有没有卷」决定，而不是按开关决定。
        # 注意别叫 vols —— 上面 brief_for/notes_for 两个闭包捕获着那个同名变量，
        # 而它装的是规划原样的 {from,to}，和这里导出用的 {n,start,end} 不是一个形状。
        export_vols = []
        if getattr(self.config, "split_volumes", True) and not chapter_range:
            # 分卷点用第二遍通读时定的那套，不再另切一次 —— 各章的卷末收束、
            # 卷首重新立场景都是按这套写的，导出时换一套切，钩子就落在错的章上。
            export_vols = [{"n": i, "start": int(v["from"]), "end": int(v["to"]),
                            "subtitle": v.get("title") or f"Book {i}",
                            "arc": v.get("arc", "")}
                           for i, v in enumerate(story_plan.get("volumes") or [], 1)]
            if not export_vols:
                self.log("⚠️ 全书规划里没有分卷方案，这一本改出全书版正文（01/07），不分卷。")
        whole_book = not export_vols
        if not whole_book:
            self.log(f"已开分卷（{len(export_vols)} 卷），跳过全书版正文。"
                     f"分卷各自的正文照常生成。")

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

        # KPF：用本地 Kindle Create 把母稿再转一遍。KPF 是本地排好版的成品，
        # 传上去 KDP 不再二次转换 —— 所见即所得。上传那边优先挑 KPF，没有才用 DOCX。
        if getattr(self.config, "kdp_make_kpf", False) and manuscript_path.exists():
            stage("生成 KPF")
            self._make_kpf(manuscript_path, proj_dir)

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

        # 12_Promotional_Poster.png：宣传海报。底图用的 poster_prompt 一直都在生成，
        # 但以前只写进 09 提示词文件、没人拿去画，交付清单里那一项始终是空的。
        # 排版直接复用封面那套：海报同样竖版、同样压书名和 tagline，没必要再写一套；
        # 用 2:3 而不是封面的 1:1.6，两者一眼能分清，不会互相冒充。
        stage("生成海报")
        poster_path = proj_dir / "12_Promotional_Poster.png"
        poster_art = proj_dir / "12_Poster_Art_Raw.png"
        if not poster_art.exists():
            self.generate_cover_art(meta["poster_prompt"], poster_art)
        kdp_formatter.create_cover_graphic(
            title=self.config.book_title,
            author=self.config.author_name,
            subtitle="",
            tagline=meta["tagline"],
            output_path=poster_path,
            width=1600, height=2400,
            background=poster_art if poster_art.exists() else None
        )
        self.log("-> 12_Promotional_Poster.png (1600x2400 海报就绪"
                 + ("，AI 底图 + 排版文字)" if poster_art.exists() else "，纯排版)"))

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

        # 10_Quality_Check_Report.md —— 真跑检查，不是套模板。
        # 旧版把「100% Complete」和每个 [x] 都写死在模板里，从来没检查过任何东西。
        # 实测某本书凭空多出第一章、丢了 8 篇番外、同一个人有四个英文名，
        # 而报告照样写「100% 完整、连续性通过」—— 假报告比没报告更害人。
        qc_md = proj_dir / "10_Quality_Check_Report.md"
        qc_md.write_text(
            self.build_qc_report(chapters, adapted_chapters, bible_md, proj_dir,
                                 name_registry),
            "utf-8")
        self.log("-> 10_Quality_Check_Report.md (已跑真实检查，结论见报告顶部)")

        if progress_cb:
            progress_cb(1.0)

        self.log(f"全部改编与出版物料已就绪！项目保存在: {proj_dir}")
        # 分卷：把全书切成若干本独立上架的英文书
        # export_vols 在导出交付文件那一步就算好了（没有卷时上面已经改出全书版正文）
        if export_vols:
            try:
                stage("导出各卷物料")
                made = self.export_volumes(export_vols, adapted_chapters, bible_md,
                                           proj_dir, proj_dir / "05_Ebook_Cover.png")
                self.log(f"分卷完成：{len(made)} 卷，各自可独立上架。"
                         f"每卷目录里有自己的 01/03/05/07 四个文件。")
            except Exception as exc:
                # 出版文案、封面、质检报告都已落盘，但正文只在卷目录里出 ——
                # 分卷开着时全书版正文是特意跳过的，所以这里挂了就一个正文都没有，
                # 上架预检会拦下来。说清楚，别让人以为只是少了分卷。
                self.log(f"⚠️ 分卷导出失败（{exc}）。出版文案和封面已就绪，"
                         f"但正文（01/07）只在卷目录里生成，这一本现在没有可上架的正文，"
                         f"重跑会接着导出。")

        return proj_dir

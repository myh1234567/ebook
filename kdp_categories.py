"""KDP 分类表：真实扒下来的分类数据 + 路径匹配。

以前 AI 是自己编分类字符串（"Fiction / Historical / United States"），编出来的名字
KDP 里根本不存在。现在改成「从表里选」——AI 只能挑 kdp_categories.json 里已有的组合。

分类弹层的真实结构（2026-09 实测，不是猜的）：
  · 一级是原生 <select class="a-native-dropdown">，option 的 value 是
    {"level":0,"key":"Romance","nodeId":"158566011"}
  · 选完一级，右边出现一组 checkbox，那就是可勾选的最终位置（Amazon 叫 Placement）
  · 勾一个，点「Save categories」结束

所以一条分类 = "一级 > Placement"，例如 "Romance > Romantic Comedy"。

数据来源：`python cli.py kdp-categories` 从真实弹层扒取，写进 kdp_categories.json。
"""
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

CATALOG_FILE = Path(__file__).parent / "kdp_categories.json"

SEP = " > "


def load_catalog(path: Optional[Path] = None) -> Dict[str, dict]:
    """读分类表，返回 {一级名: {"nodeId":..., "placements":[...]}}。"""
    f = Path(path) if path else CATALOG_FILE
    if not f.exists():
        raise FileNotFoundError(
            f"没有分类表 {f}。先跑一次 `python cli.py kdp-categories` 把它扒下来。")
    data = json.loads(f.read_text("utf-8"))
    return data.get("categories", {})


def save_catalog(categories: Dict[str, dict], path: Optional[Path] = None) -> Path:
    import datetime
    f = Path(path) if path else CATALOG_FILE
    f.write_text(json.dumps({
        "scraped_at": datetime.date.today().isoformat(),
        "source": "KDP「Choose categories」弹层（Kindle eBook, Amazon.com）",
        "note": "placements 为该一级分类下可直接勾选的最终位置；"
                "空列表表示必须先选子分类，本流程不支持",
        "categories": categories,
    }, ensure_ascii=False, indent=2), "utf-8")
    return f


def iter_paths(categories: Optional[Dict[str, dict]] = None) -> List[str]:
    """所有可选组合，形如 "Romance > Romantic Comedy"。

    没有 placement 的一级（如 Science Fiction & Fantasy）不出现在这里——
    那种必须先钻子分类，当前流程选不了，列出来只会让 AI 挑到选不中的东西。
    """
    cats = categories if categories is not None else load_catalog()
    out = []
    for top, info in cats.items():
        for p in info.get("placements", []):
            out.append(f"{top}{SEP}{_pname(p)}")
    return out


def split_path(path: str) -> List[str]:
    return [s.strip() for s in path.split(SEP) if s.strip()]


def _pname(p) -> str:
    """placement 条目兼容两种形态：新的 {"name","nodeId"} 和旧的纯字符串。"""
    return p["name"] if isinstance(p, dict) else str(p)


def node_id(top: str, categories: Optional[Dict[str, dict]] = None) -> Optional[str]:
    """一级分类的 nodeId。"""
    cats = categories if categories is not None else load_catalog()
    return (cats.get(top) or {}).get("nodeId")


def placement_id(top: str, name: str,
                 categories: Optional[Dict[str, dict]] = None) -> Optional[str]:
    """placement 的 nodeId —— 弹层里 checkbox 的 class 就是 checkbox-<nodeId>。

    按名字勾选不可靠（Amazon 的显示名会变），有 nodeId 就用 nodeId。
    """
    cats = categories if categories is not None else load_catalog()
    for p in (cats.get(top) or {}).get("placements", []):
        if isinstance(p, dict) and p.get("name") == name:
            return p.get("nodeId")
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _tokens(s: str) -> set:
    return set(_norm(s).split())


def match_path(raw: str, categories: Optional[Dict[str, dict]] = None) -> Optional[str]:
    """把 AI 返回的一串分类文字对到表里的真实组合上，对不上返回 None。

    依次试：整条全等 -> placement 名全等 -> 词重叠打分。AI 常写成
    "Fiction / Romance / Romantic Comedy"，所以先把分隔符统一、去掉 BISAC 的
    "Fiction" 头（KDP 分类里没有这一层）。
    """
    if not raw or not raw.strip():
        return None
    cats = categories if categories is not None else load_catalog()
    paths = iter_paths(cats)

    cleaned = re.sub(r"\s*[/>|»→]+\s*", SEP, raw.strip())
    segs = [s for s in split_path(cleaned)
            if _norm(s) not in ("fiction", "nonfiction", "non fiction")]
    cleaned = SEP.join(segs)
    if not cleaned:
        return None

    for p in paths:
        if _norm(p) == _norm(cleaned):
            return p

    leaf = segs[-1]
    hits = [p for p in paths if _norm(split_path(p)[-1]) == _norm(leaf)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        ctx = _tokens(cleaned)
        return max(hits, key=lambda p: len(_tokens(p) & ctx))

    want = _tokens(cleaned)
    if not want:
        return None
    best, best_score = None, 0.0
    for p in paths:
        have = _tokens(p)
        score = len(want & have) / len(have | want)
        if score > best_score:
            best, best_score = p, score
    return best if best_score >= 0.45 else None


def catalog_text(genre: str = "", categories: Optional[Dict[str, dict]] = None,
                 limit: int = 260) -> str:
    """给 LLM 提示词用的清单。按 genre 把相关的一级排到最前，再按 limit 截断。"""
    cats = categories if categories is not None else load_catalog()
    want = _tokens(genre)
    hot = [k for k in cats if _tokens(k) & want] if want else []
    ordered = hot + [k for k in cats if k not in hot]
    out = []
    for k in ordered:
        for p in cats[k].get("placements", []):
            out.append(f"{k}{SEP}{_pname(p)}")
    return "\n".join(out[:limit])

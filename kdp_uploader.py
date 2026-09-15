"""Amazon KDP 上传与上架助手模块。

提供两种模式：
1. 一键打包与快速填表辅助（Pre-flight 质检 + 字段格式化 + 剪贴板快速填充）。
2. Selenium 自动化浏览器上架（支持使用已有 Chrome Profile，一次扫码/2FA 永久复用登录凭据）。
"""
import json
import random
import re
import time
from pathlib import Path
from typing import Dict, Optional, Callable, List
from contextlib import contextmanager
from dataclasses import dataclass, field

import kdp_categories


@dataclass
class KDPMetadata:
    title: str = ""
    subtitle: str = ""
    author_first: str = ""
    author_last: str = ""
    description_html: str = ""
    description_text: str = ""
    keywords_7: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)
    adult_content: bool = False
    manuscript_path: str = ""
    cover_path: str = ""
    price: float = 2.99
    royalty: str = "70%"
    marketplace: str = "amazon.com"

    @classmethod
    def load_from_project_dir(cls, proj_dir: Path) -> "KDPMetadata":
        """从 03_Publishing_Copy.txt 和项目目录自动解析上架元数据。"""
        meta = cls()
        pub_txt = proj_dir / "03_Publishing_Copy.txt"
        if pub_txt.exists():
            text = pub_txt.read_text("utf-8", errors="replace")

            # 注意用 [ \t]* 而不是 \s*：字段留空时 \s* 会跨行把下一行的值吃进来
            m_title = re.search(r'^Title:[ \t]*(.+)', text, re.MULTILINE)
            if m_title:
                meta.title = m_title.group(1).strip()

            m_sub = re.search(r'^Subtitle:[ \t]*(.+)', text, re.MULTILINE)
            if m_sub and m_sub.group(1).strip() != "N/A":
                meta.subtitle = m_sub.group(1).strip()

            m_author = re.search(r'^Author:[ \t]*(.+)', text, re.MULTILINE)
            if m_author:
                author_full = m_author.group(1).strip()
                parts = author_full.split()
                if len(parts) >= 2:
                    meta.author_first = " ".join(parts[:-1])
                    meta.author_last = parts[-1]
                else:
                    meta.author_first = author_full
                    meta.author_last = ""

            m_html = re.search(
                r'AMAZON BOOK DESCRIPTION \(KDP HTML READY\):\s*\n(.*?)(?=\n-{10,}|\nSIX STORY KEYWORDS)',
                text,
                re.DOTALL
            )
            if m_html:
                meta.description_html = m_html.group(1).strip()

            m_plain = re.search(
                r'AMAZON BOOK DESCRIPTION \(PLAIN TEXT\):\s*\n(.*?)(?=\n-{10,}|\nAMAZON BOOK DESCRIPTION \(KDP HTML)',
                text,
                re.DOTALL
            )
            if m_plain:
                meta.description_text = m_plain.group(1).strip()

            # 7-box keywords
            kws = re.findall(r'Box\s*\d+:\s*(.+)', text)
            meta.keywords_7 = [k.strip() for k in kws if k.strip()][:7]

            # 分类：新格式是 "Category 1: Romance > Romantic Comedy"，直接可用。
            # 旧项目的文件是 "RECOMMENDED CATEGORIES:" 下面一堆 "- Books > Romance > ..."，
            # 这种交付文件不会重新生成，所以也要能吃，过一遍 match_path 归一化。
            cats = re.findall(r'^Category\s*\d+:[ \t]*(.+)', text, re.MULTILINE)
            if not cats:
                m_old = re.search(
                    r'RECOMMENDED CATEGORIES:\s*\n((?:[ \t]*-[ \t]*.+\n?)+)', text)
                if m_old:
                    raw = re.findall(r'^[ \t]*-[ \t]*(.+)', m_old.group(1), re.MULTILINE)
                    cats = [c for c in
                            (kdp_categories.match_path(r.strip()) for r in raw) if c]
            seen, picked = set(), []
            for c in (x.strip() for x in cats):
                if c and c not in seen:
                    seen.add(c)
                    picked.append(c)
            meta.categories = picked[:3]

        # 检查正文文件与封面文件。EPUB 优先：KDP 对 EPUB 的转换结果最可控。
        # 一律存绝对路径：往 <input type=file> send_keys 相对路径，Chrome 会直接
        # 抛 "path is not absolute"，而调用方传相对目录是很容易发生的事。
        for ms_name in ("07_Manuscript.epub", "01_English_Manuscript.docx"):
            ms_cand = proj_dir / ms_name
            if ms_cand.exists():
                meta.manuscript_path = str(ms_cand.resolve())
                break

        for cov_name in ("05_Ebook_Cover.png", "05_Ebook_Cover.jpg"):
            cov_cand = proj_dir / cov_name
            if cov_cand.exists():
                meta.cover_path = str(cov_cand.resolve())
                break

        return meta


# AI 申报：每本书从各自的池子里随机挑一个工具名报上去
AI_TOOL_POOLS = {
    "text": ["GPT", "Claude", "Gemini"],
    "images": ["Gemini"],
    "translations": ["GPT", "Gemini", "Claude", "Google Translate"],
}
# 申报的使用程度，三项都按「少量、基本未编辑」报
AI_AMOUNTS = {
    "text": "PARTIAL_AND_MINIMAL",
    "images": "FEW_AND_MINIMAL",
    "translations": "PARTIAL_AND_MINIMAL",
}


KDP_LOCK = Path(__file__).resolve().parent / "kdp_upload.lock"


def _pid_alive(pid: int) -> bool:
    import os
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


@contextmanager
def kdp_upload_lock(log=print, timeout: int = 4 * 3600):
    """KDP 上架的跨进程互斥锁。

    为什么必须串行：Chrome 调试端口写死成 9333，多个进程会**接管同一个浏览器**，
    在同一个标签页上填不同的书 —— 字段会串（A 书的简介配 B 书的封面），
    而且开了自动发布的话，串了的内容会被直接发到 Amazon，撤不回来。

    改编阶段不受影响：那一步各写各的目录，照常并行。只有上架这一步是共享资源。

    拿不到锁就等（不是跳过）：书已经改编好了，该轮到它就得传上去。
    锁文件里记 PID，进程崩了留下死锁，靠判活回收，否则后面的书永远排不上。
    """
    import os
    waited = 0
    while True:
        try:
            fd = os.open(str(KDP_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n{time.strftime('%F %T')}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                holder = int(KDP_LOCK.read_text().splitlines()[0])
            except Exception:
                holder = -1
            if holder > 0 and not _pid_alive(holder):
                log(f"  · 发现死锁（进程 {holder} 已退出），回收")
                KDP_LOCK.unlink(missing_ok=True)
                continue
            if waited == 0:
                log(f"  · 另一个进程正在上架（PID {holder}），排队等它…")
            if waited >= timeout:
                raise RuntimeError(f"等 KDP 上架锁超过 {timeout // 60} 分钟，放弃")
            time.sleep(20)
            waited += 20
    try:
        yield
    finally:
        KDP_LOCK.unlink(missing_ok=True)


class KDPPreflightChecker:
    """Amazon KDP 上架前质量合规检查器。"""
    @staticmethod
    def check(meta: KDPMetadata) -> List[str]:
        issues = []
        if not meta.title.strip():
            issues.append("【严重】书名 (Title) 不能为空！")
        if len(meta.title) > 200:
            issues.append(f"【警告】书名长度 ({len(meta.title)}) 接近或超出限制。")

        if not (meta.author_first or meta.author_last):
            issues.append("【警告】作者署名为空。")

        if not meta.description_html and not meta.description_text:
            issues.append("【严重】商品简介 (Description) 不能为空！")
        elif len(meta.description_html or meta.description_text) > 4000:
            issues.append(f"【严重】简介字符数 ({len(meta.description_html)}) 超出 KDP 4000 字符限制！")

        if len(meta.keywords_7) < 5:
            issues.append(f"【建议】KDP 提供 7 个搜索关键词槽位，当前仅配置 {len(meta.keywords_7)} 个。")

        for i, kw in enumerate(meta.keywords_7, 1):
            if len(kw) > 50:
                issues.append(f"【警告】关键词槽位 {i} 长度超过 50 个字符（可能被截断）。")

        if not meta.manuscript_path or not Path(meta.manuscript_path).exists():
            issues.append("【严重】未找到正文文件 (07_Manuscript.epub 或 01_English_Manuscript.docx)！")

        if not meta.cover_path or not Path(meta.cover_path).exists():
            issues.append("【严重】未找到封面文件 (05_Ebook_Cover.png/jpg)！")

        return issues


class KDPBrowserUploader:
    """使用 Selenium 自动化操作 Chrome 上传至 Amazon KDP。"""

    def __init__(self, user_data_dir: Optional[str] = None, log_func: Optional[Callable[[str], None]] = None):
        self.user_data_dir = user_data_dir
        self.log = log_func or print
        self.driver = None

    # 固定一个调试端口：同一个 profile 只能被一个 Chrome 占用，
    # 所以第二次上架要接管已经开着的那个，而不是再开一个（否则拿不到登录态）
    DEBUG_PORT = 9333
    CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    def _debug_alive(self) -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.DEBUG_PORT}/json/version", timeout=2):
                return True
        except Exception:
            return False

    def _attach(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        o = Options()
        o.debugger_address = f"127.0.0.1:{self.DEBUG_PORT}"
        self.driver = webdriver.Chrome(options=o)

    def start_browser(self):
        """挂上带登录态的 Chrome。已经开着就接管，没开才拉起来。

        关键：Chrome 的 user-data-dir 同一时刻只允许一个实例持有。硬开第二个的话，
        它会退回一个临时空 profile —— 表现就是「明明登录过却显示未登录」。
        """
        import subprocess

        if self._debug_alive():
            self._attach()
            self.log(f"接管已在运行的 Chrome（端口 {self.DEBUG_PORT}），沿用已有登录态。")
            return

        profile = Path(self.user_data_dir) if self.user_data_dir else \
            Path.home() / ".kdp_chrome_profile"
        profile.mkdir(parents=True, exist_ok=True)

        subprocess.Popen(
            [self.CHROME_BIN,
             f"--remote-debugging-port={self.DEBUG_PORT}",
             f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check",
             "--disable-blink-features=AutomationControlled",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)

        for _ in range(30):
            time.sleep(1)
            if self._debug_alive():
                break
        else:
            raise RuntimeError(
                f"Chrome 起不来。多半是 {profile} 已被另一个 Chrome 占用——"
                f"把那个窗口关掉再试。")
        self._attach()
        self.log(f"已启动 Chrome，profile: {profile}")

    # ---- 以下选择器都是 2026-09 在真实 KDP 页面上逐个验证过的，不是猜的 ----
    F_TITLE = "data-title"
    F_SUBTITLE = "data-subtitle"
    F_FIRST = "data-primary-author-first-name"
    F_LAST = "data-primary-author-last-name"
    F_KEYWORD = "data-keywords-%d"
    F_NON_PUBLIC_DOMAIN = "non-public-domain"
    F_CATEGORIES_BTN = "categories-modal-button"
    F_SUBMIT = "save-and-continue-announce"
    F_MANUSCRIPT = "data-assets-interior-file-upload-AjaxInput"
    F_COVER = "data-assets-cover-file-upload-AjaxInput"

    def _type(self, elem_id: str, value: str, label: str):
        """填一个输入框并回读校验。填不进去就抛错——静默失败比不填更坑人。"""
        el = self.driver.find_element("id", elem_id)
        el.clear()
        el.send_keys(value)
        got = (el.get_attribute("value") or "").strip()
        if got != value.strip():
            raise RuntimeError(f"{label} 没填进去（#{elem_id} 实际值：{got[:40]!r}）")
        self.log(f"  ✓ {label}")

    def _fill_details(self, meta: "KDPMetadata"):
        """第 1 步：书名、作者、简介、关键词、分类。"""
        self._type(self.F_TITLE, meta.title, f"书名 {meta.title}")
        if meta.subtitle:
            self._type(self.F_SUBTITLE, meta.subtitle, "副标题")
        if meta.author_first:
            self._type(self.F_FIRST, meta.author_first, "作者名")
        if meta.author_last:
            self._type(self.F_LAST, meta.author_last, "作者姓")

        # 简介是 CKEditor 富文本，走它自己的 API 灌 HTML，比往 iframe 里敲字符靠谱
        html = meta.description_html or meta.description_text
        inst = self.driver.execute_script(
            "var h=arguments[0];"
            "if(window.CKEDITOR&&CKEDITOR.instances){"
            " for(var k in CKEDITOR.instances){CKEDITOR.instances[k].setData(h);return k;}}"
            "return null;", html)
        if not inst:
            raise RuntimeError("没找到简介编辑器 CKEditor 实例")
        self.log(f"  ✓ 简介（{len(html)} 字符）")

        for i, kw in enumerate(meta.keywords_7[:7]):
            self._type(self.F_KEYWORD % i, kw, f"关键词 {i + 1}")

        # 出版权 + 成人内容，这两项不选提交会被打回
        self.driver.execute_script(
            "var r=document.getElementById(arguments[0]); if(r&&!r.checked){r.click();}",
            self.F_NON_PUBLIC_DOMAIN)
        self.log("  ✓ 出版权=非公有领域")

        self._ensure_category_prereqs()

    def _ensure_category_prereqs(self):
        """答掉「成人内容」那题——这是分类弹层唯一的前置条件。

        KDP 页面上写的是 "You must select your primary marketplace and audience first"，
        听着像要设两样东西，但 React 组件自己的文案只提了一条：
            categoriesButtonDisabledInfo: "Answer the Adult-only question before choosing a category."
        主要市场默认就是本账号的 homeMarketplace（组件 data-config 里写死 "US"），
        阅读年龄留空也不拦分类——实测两者都不用动。之前按标签文字去猜市场下拉
        的那段代码从来没找到过东西，已删。
        """
        self.driver.execute_script(
            "document.querySelectorAll('input[name=\"data[is_adult_content]-radio\"]')"
            ".forEach(function(e){if(e.value==='false'&&!e.checked)e.click();});")
        self.log("  ✓ 成人内容=否")

    # 弹层里每个节点的 data-value 都是一层 JSON 套一层：
    # {"stringVal":"{\"level\":0,\"key\":\"Romance\",\"nodeId\":\"158566011\"}"}
    # 原生 select 的 option.value 只有里层那串。下面的 nodeInfo 两种都吃。
    # ---- 分类弹层。以下结构是 2026-09 在真实页面上实测出来的 ----
    #
    # 弹层：div.a-popover.a-popover-modal[role=dialog]
    # 一级：弹层里第一个 <select class="a-native-dropdown">，option 的 value 是
    #       {"level":0,"key":"Romance","nodeId":"158566011"}
    # 选完一级，右边渲染出一组 checkbox，那就是可勾选的最终位置（Placement）
    # 面包屑 "Kindle Books › Romance" 是渲染完成的信号
    # 最后点「Save categories」
    #
    # 两个曾经踩过的坑，别再踩：
    # 1. 这是 React 组件，直接给 select 赋 .value 会被 React 的 value tracker 吞掉，
    #    必须用 HTMLSelectElement.prototype 上的原生 setter 再派发 change。
    # 2. 「Choose categories」按钮在组件加载完前是 disabled 且带 spinner，
    #    点它不报错但弹层不会开，后面每一步都在空 DOM 上白找。

    JS_MODAL = """
    function modal(){
      var save=null;
      document.querySelectorAll('button').forEach(function(b){
        if((b.textContent||'').trim()==='Save categories') save=b;});
      return save ? save.closest('.a-popover-modal') : null;
    }
    function crumb(mo){
      var c='';
      (mo.innerText||'').split('\\n').forEach(function(l){
        if(!c && l.indexOf('Kindle Books')===0) c=l.trim();});
      return c;
    }
    function boxes(mo){
      // checkbox 的 class 就是 nodeId：<input type="checkbox" class="checkbox-6487841011">
      var out=[];
      mo.querySelectorAll('input[type=checkbox]').forEach(function(c){
        var l=c.closest('label'); if(!l) return;
        var m=/(?:^|\\s)checkbox-(\\d+)(?:\\s|$)/.exec(c.className||'');
        out.push({el:c, label:(l.textContent||'').trim(), nodeId:m?m[1]:null});});
      return out;
    }
    """

    def _js(self, body: str, *args):
        return self.driver.execute_script(self.JS_MODAL + body, *args)

    def _wait_categories_button(self, timeout: int = 90):
        """等「Choose categories」真正可点。

        分类是 React 组件（div.react-categories）。组件初始化完成前按钮长这样：
            <button id="categories-modal-button" disabled>Choose categories <span class="a-spinner"></span></button>
        加载完 disabled 和 spinner 都会消失。另外 KDP 自己的文案写着
        "Answer the Adult-only question before choosing a category."，
        所以成人内容那题必须先答（_ensure_category_prereqs 里做了）。
        """
        for _ in range(timeout):
            st = self.driver.execute_script(
                "var b=document.getElementById(arguments[0]);"
                "if(!b) return 'missing';"
                "if(b.disabled||b.getAttribute('aria-disabled')==='true') return 'disabled';"
                "if(b.querySelector('.a-spinner')) return 'spinner';"
                "return 'ok';", self.F_CATEGORIES_BTN)
            if st == "ok":
                return
            if st == "missing":
                raise RuntimeError("页面上没有「Choose categories」按钮，KDP 可能改版了")
            time.sleep(1)
        raise RuntimeError(f"「Choose categories」等了 {timeout} 秒还是不可点（灰着或一直转圈）")

    def _open_category_modal(self):
        self._wait_categories_button()
        self.driver.execute_script(
            "document.getElementById(arguments[0]).click()", self.F_CATEGORIES_BTN)
        for _ in range(30):
            time.sleep(1)
            if self._js("var mo=modal(); return mo ? mo.querySelectorAll('select').length : 0;"):
                return
        raise RuntimeError("点了「Choose categories」但弹层没出来")

    def _top_level_keys(self) -> List[str]:
        return self._js(
            "var mo=modal(); if(!mo) return [];"
            "var s=mo.querySelector('select'); if(!s) return [];"
            "var out=[];"
            "for(var j=0;j<s.options.length;j++){"
            " try{var o=JSON.parse(s.options[j].value); if(o.key) out.push(o.key);}catch(e){}}"
            "return out;") or []

    def _crumb(self) -> str:
        return self._js("var mo=modal(); return mo?crumb(mo):'';") or ""

    def _select_top_level(self, key: str, node_id: Optional[str] = None) -> bool:
        """选一级分类。优先按 nodeId 认，认不到再按名字。

        为什么必须按 nodeId：Amazon 对同一个分类节点会轮换两套显示名，实测
            154821011 = "Business & Investing" 或 "Business & Money"
            156154011 = "Cooking, Food & Wine" 或 "Cookbooks, Food & Wine"
            158125011 = "Education & Reference" 或 "Education & Teaching"
        只按名字匹配，碰上另一套就选不中——而且是间歇性的，最难查。

        选完要等 React 重渲染，否则读到的 placement 还是上一个分类的。就绪信号用
        面包屑「变了且带 ›」，不比对具体名字，同样是为了绕开上面那个换名问题。
        """
        before = self._crumb()
        r = self._js(
            "var key=arguments[0], nid=arguments[1];"
            "var mo=modal(); if(!mo) return 'nomodal';"
            "var s=mo.querySelector('select'); if(!s) return 'nosel';"
            "var byId=null, byKey=null;"
            "for(var j=0;j<s.options.length;j++){"
            " try{ var o=JSON.parse(s.options[j].value);"
            "  if(nid && o.nodeId===nid) byId=s.options[j];"
            "  if(o.key===key) byKey=s.options[j];"
            " }catch(e){}}"
            "var hit=byId||byKey; if(!hit) return 'notfound';"
            "var setter=Object.getOwnPropertyDescriptor("
            "  window.HTMLSelectElement.prototype,'value').set;"
            "setter.call(s,hit.value);"
            "s.dispatchEvent(new Event('change',{bubbles:true}));"
            "return 'ok';", key, node_id)
        if r != "ok":
            return False

        # 第一步：等面包屑变掉，说明 React 认下了这次选择
        ok = False
        for _ in range(40):
            time.sleep(0.25)
            c = self._crumb()
            if (c and c != before and "\u203a" in c) or c.endswith(key):
                ok = True
                break
        if not ok:
            return False

        # 第二步：等 placement 的 checkbox 真的渲染出来。
        # 面包屑比 checkbox 先更新，只等面包屑就会读到空列表——这是真实流程里
        # 「该分类下可选：[]」的原因，手工测试时每步 sleep 掩盖了这个竞态。
        # 有 4 个一级（如 Science Fiction & Fantasy）确实没有 placement，
        # 所以等不到也不算失败，兜到 settle 上限就返回。
        for i in range(24):
            time.sleep(0.25)
            if self._js("var mo=modal(); return mo?boxes(mo).length:0;"):
                time.sleep(0.4)      # 让剩下的 checkbox 也渲染完
                return True
        return True

    def _placements(self) -> List[dict]:
        """当前一级下可勾选的 placement，形如 [{"name":"Romantic Comedy","nodeId":"6487841011"}]。"""
        return self._js(
            "var mo=modal(); if(!mo) return [];"
            "return boxes(mo).map(function(b){"
            " return {name:b.label, nodeId:b.nodeId};});") or []

    def _check_placement(self, name: str, node_id: Optional[str] = None) -> bool:
        """勾一个 placement。优先按 nodeId（checkbox 的 class），认不到再按名字。"""
        return bool(self._js(
            "var want=arguments[0], nid=arguments[1];"
            "var mo=modal(); if(!mo) return false;"
            "var byId=null, byName=null;"
            "boxes(mo).forEach(function(b){"
            " if(nid && b.nodeId===nid) byId=b.el;"
            " if(b.label===want) byName=b.el;});"
            "var hit=byId||byName; if(!hit) return false;"
            "hit.scrollIntoView({block:'center'});"
            "if(!hit.checked) hit.click();"
            "return !!hit.checked;", name, node_id))

    def _pick_categories(self, paths: List[str]):
        """按 "一级 > Placement" 选分类，选完保存。

        例如 "Romance > Romantic Comedy"：一级下拉选 Romance，右边勾 Romantic Comedy。
        """
        paths = [p for p in paths if p and p.strip()][:3]
        if not paths:
            raise RuntimeError("没有可用的分类")

        self._open_category_modal()

        done, current_top = [], None
        for path in paths:
            segs = kdp_categories.split_path(path)
            if len(segs) < 2:
                self.log(f"  · 分类「{path}」格式不对（要 \u300c一级 > Placement\u300d），跳过")
                continue
            top, placement = segs[0], segs[-1]

            if top != current_top:
                if not self._select_top_level(top, kdp_categories.node_id(top)):
                    self.log(f"  · 一级「{top}」选不上，可选：{self._top_level_keys()}")
                    continue
                current_top = top

            if self._check_placement(placement,
                                     kdp_categories.placement_id(top, placement)):
                done.append(path)
                time.sleep(1)
            else:
                avail = [x["name"] for x in self._placements()]
                self.log(f"  · 「{top}」下没有 Placement「{placement}」。"
                         f"该分类下可选（{len(avail)} 个）：{avail[:12]}")

        if not done:
            raise RuntimeError("一个分类都没选上，KDP 会拦住不让提交")
        self.log(f"  \u2713 分类：{'; '.join(done)}")

        saved = self._js(
            "var mo=modal(); if(!mo) return false;"
            "var bs=mo.querySelectorAll('button');"
            "for(var j=0;j<bs.length;j++){"
            " if((bs[j].innerText||'').trim()==='Save categories'){bs[j].click();return true;}}"
            "return false;")
        if not saved:
            raise RuntimeError("没找到「Save categories」按钮")
        time.sleep(4)

    def dump_categories(self, out_path: Optional[str] = None, cancel_event=None) -> Path:
        """把每个一级分类下可勾选的 Placement 扒下来，写成 kdp_categories.json。

        跑一次就行。之后 AI 挑分类、上传器选分类都用这份数据。
        """
        if not self.driver:
            self.start_browser()
        if not self._goto_new_book(cancel_event):
            return kdp_categories.CATALOG_FILE
        self._ensure_category_prereqs()
        self._open_category_modal()

        tops = self._js(
            "var mo=modal(); if(!mo) return [];"
            "var s=mo.querySelector('select'); var out=[];"
            "for(var j=0;j<s.options.length;j++){"
            " try{var o=JSON.parse(s.options[j].value);"
            "  if(o.key) out.push([o.key,o.nodeId]);}catch(e){}}"
            "return out;") or []
        self.log(f"一级分类 {len(tops)} 个，逐个读取 Placement…")

        cats = {}
        for i, (key, nid) in enumerate(tops, 1):
            if cancel_event and cancel_event.is_set():
                self.log("已取消。")
                break
            if not self._select_top_level(key, nid):
                self.log(f"  · [{i}/{len(tops)}] {key} 选不上或渲染超时，跳过")
                continue
            ps = self._placements()
            cats[key] = {"nodeId": nid, "placements": ps}
            miss = [x["name"] for x in ps if not x["nodeId"]]
            self.log(f"  \u2713 [{i}/{len(tops)}] {key}：{len(ps)} 个 Placement"
                     + (f"（{len(miss)} 个没拿到 nodeId）" if miss else ""))

        path = kdp_categories.save_catalog(
            cats, Path(out_path) if out_path else None)
        usable = sum(1 for v in cats.values() if v["placements"])
        self.log(f"分类表已写入 {path}"
                 f"（一级 {len(cats)} 个，其中 {usable} 个有 Placement，"
                 f"可用组合 {len(kdp_categories.iter_paths(cats))} 条）")
        return path

    @staticmethod
    def _cover_as_jpg(cover_path: str) -> str:
        """KDP 封面只收 JPG/TIFF，PNG 一律先转一份出来。"""
        p = Path(cover_path)
        if p.suffix.lower() in (".jpg", ".jpeg", ".tif", ".tiff"):
            return str(p)
        from PIL import Image
        out = p.with_suffix(".jpg")
        Image.open(p).convert("RGB").save(out, "JPEG", quality=92, optimize=True)
        return str(out)

    def _upload_files(self, meta: "KDPMetadata"):
        """第 2 步：正文和封面。文件输入框是隐藏的，send_keys 照样能塞，不弹系统对话框。"""
        if meta.manuscript_path:
            self.driver.find_element("id", self.F_MANUSCRIPT).send_keys(meta.manuscript_path)
            self.log(f"  · 正在上传正文 {Path(meta.manuscript_path).name} …")
            self._wait_for(lambda t: "uploaded successfully" in t.lower(),
                           "正文上传", 300, settle=self.UPLOAD_SETTLE)

        if meta.cover_path:
            # 「上传自有封面」是折叠的，不展开的话下面的 input 根本不生效
            self.driver.execute_script(
                "var hit=null;"
                "document.querySelectorAll('.a-accordion-row-a11y').forEach(function(e){"
                " if((e.textContent||'').indexOf('cover you already have')>-1) hit=e;});"
                "if(hit){hit.scrollIntoView({block:'center'}); hit.click();}")
            time.sleep(3)
            jpg = self._cover_as_jpg(meta.cover_path)
            self.driver.find_element("id", self.F_COVER).send_keys(jpg)
            self.log(f"  · 正在上传封面 {Path(jpg).name} …")
            self._wait_for(lambda t: "cover uploaded successfully" in t.lower(),
                           "封面上传", 300, settle=self.UPLOAD_SETTLE)

        # 两个文件都传完了再等转换。转换是针对正文+封面一起做的，
        # 所以放在最后等一次，而不是每传一个等一次。
        self._wait_processing_done()

    # 「上传成功」的字样出现之后，KDP 还要再渲染一阵（进度条收起、下一段表单挂上来）。
    # 这几秒是沉降时间——分类那次的教训就是：拿到就绪信号不等于 DOM 已经稳。
    UPLOAD_SETTLE = 6

    # 文件传完之后 KDP 还要把正文和封面转成 Kindle 格式，页面上会挂这些提示，
    # 官方措辞是 "can take several minutes"：
    #   Preparing your files
    #   KDP is processing your manuscript and book cover.
    #   Converting your files to Kindle format…
    # 「上传成功」只代表文件到了服务器，不代表能进行下一步。等错阶段会让后面的
    # AI 申报、DRM、定价全都操作在一个还在变的页面上。
    # 写成单数前缀，"...your file" 同时能命中 "...your files"。
    # 实测页面上出现过的是 "Processing your file..."（单数、且不带 manuscript），
    # 只按提示文案里的 "processing your manuscript" 去匹配会全部落空。
    PROCESSING_MARKERS = (
        "preparing your file",
        "converting your file",
        "processing your file",
        "processing your manuscript",
        "uploading your file",
    )

    def _wait_processing_done(self, timeout: int = 900):
        """等 KDP 转换完成——页面上不再有任何「处理中」提示。

        提示可能还没挂上来，所以不能看一眼没有就走：要求连续几次都干净才算完。
        """
        deadline = time.time() + timeout
        seen_busy = False
        clear = 0
        while time.time() < deadline:
            time.sleep(5)
            try:
                text = self.driver.find_element("tag name", "body").text.lower()
            except Exception:
                continue
            if any(m in text for m in self.PROCESSING_MARKERS):
                if not seen_busy:
                    self.log("  · KDP 正在转换文件，这步官方说要几分钟，等它…")
                seen_busy = True
                clear = 0
                continue
            clear += 1
            # 没见过处理提示时要多观望一会儿，防止提示还没渲染出来就误判完成
            if clear >= (2 if seen_busy else 4):
                if seen_busy:
                    self.log("  ✓ KDP 文件转换完成")
                return
        raise RuntimeError(f"等 KDP 转换文件超时（{timeout} 秒）")

    F_PREVIEW_BTN = "digital-preview-announce"

    def _run_previewer(self, flips: int = 6):
        """点开 Kindle 预览器，翻几页，再按 Back 回来。

        实测行为（不是猜的）：
          · 不开新标签页，同一个标签页跳到
            kdp.amazon.com/preview/kindle/index.html?...&returnUrl=<content 页>
          · 翻页控件是 button[aria-label="Next location"]，页面上有
            "Location 1 of 65391" 可以读出翻到哪了
          · 关闭是文本为 Back 的按钮

        选择器一律不用 css-84hro5 这类哈希类名——那是 emotion 生成的，换个构建就变。
        预览失败不该把整单带崩：书已经传好了，翻页只是走一遍流程，所以这里吞异常。
        """
        try:
            # 要等，不能只查一次。转换刚结束时这个按钮还是灰的，过一会儿才变可点；
            # 查一次就跳过的话，表现就是「预览器根本没点开」——和分类按钮那个
            # spinner 是同一类问题，那边已经是轮询等待的写法了。
            ready = False
            for _ in range(80):
                ready = bool(self.driver.execute_script(
                    "var b=document.getElementById(arguments[0]);"
                    "return !!(b && !b.disabled"
                    " && b.getAttribute('aria-disabled')!=='true'"
                    " && !b.querySelector('.a-spinner')"
                    " && b.offsetParent!==null);", self.F_PREVIEW_BTN))
                if ready:
                    break
                time.sleep(1.5)
            if not ready:
                self.log("  · 预览器按钮等了 2 分钟仍不可点，跳过预览")
                return

            self.driver.execute_script(
                "document.getElementById(arguments[0]).click()", self.F_PREVIEW_BTN)
            for _ in range(40):
                time.sleep(1.5)
                if "/preview/kindle" in self.driver.current_url:
                    break
            else:
                self.log("  · 预览器没打开，跳过")
                return
            time.sleep(6)   # 等书渲染出来，不然翻页按钮还没挂上

            JS_LOC = ("var m=/Location\\s+([\\d,]+)\\s+of\\s+([\\d,]+)/"
                      ".exec(document.body.innerText||''); return m?m[1]:'';")
            done = 0
            for _ in range(flips):
                r = self.driver.execute_script(
                    "var b=document.querySelector('button[aria-label=\"Next location\"]');"
                    "if(!b||b.disabled) return false; b.click(); return true;")
                if not r:
                    break
                done += 1
                time.sleep(random.uniform(1.4, 2.6))   # 翻页节奏别太机械
            self.log(f"  ✓ 预览器已翻 {done} 页（当前 Location {self.driver.execute_script(JS_LOC)}）")

            back = self.driver.execute_script(
                "var hit=null; document.querySelectorAll('button').forEach(function(b){"
                " if((b.textContent||'').trim()==='Back') hit=b;});"
                "if(!hit) return false;"
                "hit.scrollIntoView({block:'center'}); hit.click(); return true;")
            if not back:
                self.log("  · 没找到预览器的 Back 按钮，直接退回 content 页")
                self.driver.back()
            for _ in range(30):
                time.sleep(1.5)
                if "/preview/kindle" not in self.driver.current_url:
                    break
            time.sleep(4)
            self.log("  ✓ 已退出预览器")
        except Exception as exc:
            self.log(f"  · 预览这步出错，不影响上架，继续：{exc}")
            try:
                if "/preview/kindle" in self.driver.current_url:
                    self.driver.back()
                    time.sleep(6)
            except Exception:
                pass

    def _wait_step_done(self, url_part: str, label: str, timeout: int = 300):
        """点完「Save and Continue」后等页面真的跳过去。

        以前是 sleep 固定秒数就判断 URL，KDP 只要还在转文件就跳不过去，于是把
        「还没处理完」误报成「这一步没过」。现在轮询：还在处理就继续等，
        真出了校验错误（有报错框且不在处理中）就立刻报出来，不白等满。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            if url_part in self.driver.current_url:
                self.log(f"{label} Complete。")
                return
            text = self.driver.find_element("tag name", "body").text.lower()
            if any(m in text for m in self.PROCESSING_MARKERS):
                continue
            errs = [e.text.strip() for e in self.driver.find_elements(
                "css selector", ".a-alert-error .a-alert-content")
                if e.is_displayed() and e.text.strip()]
            if errs:
                raise RuntimeError(f"{label}没过：" + "; ".join(errs[:4]))
        raise RuntimeError(f"{label}等了 {timeout} 秒还没跳转（当前 {self.driver.current_url}）")

    def _wait_for(self, cond, label: str, timeout: int, settle: int = 0):
        """轮询直到条件成立。settle 是成立之后额外再等的秒数。

        用轮询而不是死等固定秒数：正文几十 MB 和几百 KB 的上传时间差着数量级，
        固定秒数要么白等要么不够。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            if cond(self.driver.find_element("tag name", "body").text):
                self.log(f"  ✓ {label}完成")
                if settle:
                    time.sleep(settle)
                return
        raise RuntimeError(f"{label}超时（{timeout} 秒）")

    def _fill_ai_questionnaire(self) -> Dict[str, str]:
        """AI 生成内容申报。选 Yes 之后三项都要填「程度 + 工具名」，还要勾确认框。

        工具名每本书从池子里随机取一个，返回这次报了什么，方便记档。
        """
        self.driver.execute_script(
            "document.querySelectorAll('a.a-accordion-row').forEach(function(a){"
            " var h=a.querySelector('h5');"
            " if(h && (h.textContent||'').trim()==='Yes'"
            "    && a.getAttribute('aria-expanded')!=='true') a.click();});")
        time.sleep(3)

        JS_TOOL = ("var s=document.getElementById(arguments[0]);"
                   "var box=s.closest('div').parentElement;"
                   "var inp=box.querySelector('input[type=text]');"
                   "if(!inp) return null;"
                   "var setter=Object.getOwnPropertyDescriptor("
                   " window.HTMLInputElement.prototype,'value').set;"
                   "setter.call(inp, arguments[1]);"
                   "['input','change','blur'].forEach(function(ev){"
                   " inp.dispatchEvent(new Event(ev,{bubbles:true}));});"
                   "return inp.value;")
        declared = {}
        for kind in ("text", "images", "translations"):
            sel_id = f"generative-ai-questionnaire-{kind}"
            amount = AI_AMOUNTS[kind]
            got = self.driver.execute_script(
                "var s=document.getElementById(arguments[0]); if(!s) return null;"
                "s.value=arguments[1]; s.dispatchEvent(new Event('change',{bubbles:true}));"
                "return s.value;", sel_id, amount)
            if got != amount:
                raise RuntimeError(f"AI 申报 {kind} 程度没设上（{got!r}）")
            time.sleep(1.5)
            if amount != "NONE":
                tool = random.choice(AI_TOOL_POOLS[kind])
                if self.driver.execute_script(JS_TOOL, sel_id, tool) != tool:
                    raise RuntimeError(f"AI 申报 {kind} 的工具名没填上")
                declared[kind] = f"{amount} / {tool}"
                time.sleep(1)
            else:
                declared[kind] = amount
            self.log(f"  ✓ AI 申报 {kind}: {declared[kind]}")

        # 确认框是 React 的 role=checkbox，不是原生 input
        cb = self.driver.execute_script(
            "var hit=null;"
            "document.querySelectorAll('[role=checkbox]').forEach(function(e){"
            " if((e.getAttribute('aria-labelledby')||'')"
            "     .indexOf('mdn-checkbox-label')===0 && !hit) hit=e;});"
            "return hit;")
        if cb is not None and cb.get_attribute("aria-checked") != "true":
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", cb)
            time.sleep(2)
            self.log("  ✓ 已勾选「确认答案属实」")
        return declared

    def _fill_pricing(self, meta: "KDPMetadata", kdp_select: bool = True):
        """第 3 步：KDP Select、版税方案、各站点定价。价格框要先选版税方案才解锁。"""
        if kdp_select:
            self.driver.execute_script(
                "var c=document.getElementById('data-is-select');"
                "if(c && !c.checked){c.scrollIntoView({block:'center'}); c.click();}")
            time.sleep(2)
            self.log("  ✓ 已加入 KDP Select（吃 Kindle Unlimited 借阅分成）")

        rate = "70_PERCENT" if str(meta.royalty).startswith("70") else "35_PERCENT"
        ok = self.driver.execute_script(
            # 注意：want 必须在 forEach 外面取。回调里的 arguments 是回调自己的，不是外层的
            "var want=arguments[0], done=false;"
            "document.querySelectorAll(\"input[name='data[digital][royalty_rate]-radio']\")"
            ".forEach(function(e){ if(e.value!==want) return;"
            "  if(!e.checked){ e.scrollIntoView({block:'center'}); e.click(); }"
            "  done=true; });"
            "return done;", rate)
        if not ok:
            raise RuntimeError(f"没找到版税方案 {rate}")
        self.log(f"  ✓ 版税方案 {meta.royalty}")

        got = self._set_price(f"{meta.price:.2f}")
        self.log(f"  ✓ 美国站定价 ${got}（其余站点由 Amazon 按汇率换算）")

    US_PRICE_SEL = "input[name='data[digital][channels][amazon][US][price_vat_inclusive]']"

    def _set_price(self, price: str, attempts: int = 6) -> str:
        """填美国站价格，写完回读校验，没留住就重来。

        选完版税方案 sleep 3 秒就写是不够的：这期间 KDP 还在重渲染价格区，
        打进去的值会被冲掉，表现就是「价格没填进去」。实测手动补写一次就能留住，
        说明方法没问题、是时机问题。所以改成：等字段可用 -> 写 -> 回读 -> 不对就重试。
        """
        # 先等字段出现并可用
        for _ in range(40):
            if self.driver.execute_script(
                    "var e=document.querySelector(arguments[0]);"
                    "return !!(e && !e.disabled && !e.readOnly && e.offsetParent!==null);",
                    self.US_PRICE_SEL):
                break
            time.sleep(1.5)
        else:
            raise RuntimeError("美国站价格输入框一直没就绪（版税方案可能没生效）")

        for i in range(attempts):
            try:
                # 每次重新找元素：重渲染之后旧引用会 stale
                el = self.driver.find_element("css selector", self.US_PRICE_SEL)
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'})", el)
                el.clear()
                el.send_keys(price)
            except Exception:
                time.sleep(2)
                continue
            time.sleep(3)
            got = (self.driver.execute_script(
                "var e=document.querySelector(arguments[0]); return e?e.value:'';",
                self.US_PRICE_SEL) or "").strip()
            if got:
                return got
            self.log(f"  · 价格没留住（第 {i + 1} 次），等一下重试…")
            time.sleep(3)
        raise RuntimeError(f"美国站价格写了 {attempts} 次都没留住")

    def publish(self, meta: "KDPMetadata"):
        """点下 Publish。不可逆：书会进 Amazon 审核队列，通过后就公开售卖了。"""
        issues = [i for i in KDPPreflightChecker.check(meta) if i.startswith("【严重】")]
        if issues:
            raise RuntimeError("预检有严重问题，拒绝自动发布：" + "; ".join(issues))
        btn = self.driver.find_elements("id", "save-and-publish-announce")
        if not btn:
            raise RuntimeError("没找到 Publish 按钮，可能还有必填项没过")
        if btn[0].get_attribute("disabled") or \
                btn[0].get_attribute("aria-disabled") == "true":
            raise RuntimeError("Publish 按钮是灰的，还有必填项没过，拒绝发布")

        self._wait_processing_done()      # 还在转文件时点发布不会生效
        before = self.driver.current_url
        self.log("正在点 Publish（不可逆）…")
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", btn[0])

        # 不能 sleep 固定秒数然后无条件报成功 —— 发布失败也会被说成成功，
        # 而这一步不可逆，报错了才有机会人工补救。所以等真实结果。
        for _ in range(40):
            time.sleep(3)
            url = self.driver.current_url
            body = self.driver.find_element("tag name", "body").text
            if any(m in body.lower() for m in self.PROCESSING_MARKERS):
                continue
            # 成功的标志：跳回书架，或页面上出现审核中的措辞
            low = body.lower()
            if "/bookshelf" in url or url != before or \
                    any(k in low for k in ("in review", "publishing", "submitted",
                                           "under review", "draft submitted")):
                self.log(f"已提交发布，当前页面：{url}")
                return url
            errs = [e.text.strip() for e in self.driver.find_elements(
                "css selector", ".a-alert-error .a-alert-content")
                if e.is_displayed() and e.text.strip()]
            if errs:
                raise RuntimeError("发布被 KDP 拦下：" + "; ".join(errs[:4]))
        raise RuntimeError(
            f"点了 Publish 但 2 分钟内没等到结果，页面仍在 {self.driver.current_url}。"
            f"去 KDP 书架确认这本书到底提交了没有，别重复发布。")

    def _logged_in(self) -> bool:
        """是否真的登录了。

        不能只看 URL 里有没有 bookshelf：登录页的 openid.return_to 参数里
        就带着 bookshelf 三个字，会把判断骗过去。
        """
        url = self.driver.current_url
        return url.startswith("https://kdp.amazon.com") and "/ap/signin" not in url

    def _goto_new_book(self, cancel_event=None) -> bool:
        """登录（必要时等人工 2FA），然后开一本新 Kindle 电子书的第 1 步页面。

        被取消时返回 False —— 取消是正常操作，不该当异常弹错误框。
        """
        self.log("打开 KDP 书架…")
        self.driver.get("https://kdp.amazon.com/en_US/bookshelf")
        time.sleep(3)
        if not self._logged_in():
            self.log(">>> 需要登录 Amazon。请在浏览器里完成登录和 2FA，")
            self.log(">>> 并务必勾选「Keep me signed in」，否则关掉浏览器登录态就没了。")
            for _ in range(150):
                if cancel_event and cancel_event.is_set():
                    self.log("已取消。")
                    return False
                time.sleep(2)
                if self._logged_in():
                    self.log("已登录，继续。")
                    break
            else:
                raise RuntimeError("等登录超时（5 分钟）")

        self.log("新建 Kindle 电子书…")
        self.driver.get("https://kdp.amazon.com/en_US/create")
        time.sleep(3)
        btn = [e for e in self.driver.find_elements("xpath", "//a|//button")
               if (e.text or "").strip() == "Create eBook"]
        if not btn:
            raise RuntimeError("没找到「Create eBook」入口，KDP 页面可能改版了")
        btn[0].click()
        time.sleep(6)
        return True

    # 03_Publishing_Copy.txt 里没写分类时的兜底，免得整单卡在分类上
    DEFAULT_CATEGORIES = ["Romance > Romantic Comedy", "Romance > Contemporary"]

    def upload_ebook(self, meta: KDPMetadata, cancel_event=None,
                     do_publish: bool = False):
        """建一本新的 Kindle 电子书草稿，填完第 1 步并传好正文与封面。

        不碰「AI 生成内容」申报，也不点发布——那两件事必须你自己来。
        """
        # 整个上架流程都在锁里：多进程会接管同一个 Chrome（端口写死 9333），
        # 不串行的话字段会互相覆盖，开了自动发布还会把串了的内容直接发出去。
        with kdp_upload_lock(self.log):
            self._upload_ebook_locked(meta, cancel_event, do_publish)

    def _upload_ebook_locked(self, meta: KDPMetadata, cancel_event, do_publish: bool):
        if not self.driver:
            self.start_browser()
        if not self._goto_new_book(cancel_event):
            return

        self.log("填写第 1 步（书名 / 作者 / 简介 / 关键词）…")
        self._fill_details(meta)
        self._pick_categories(meta.categories or self.DEFAULT_CATEGORIES)

        self.log("提交第 1 步…")
        self.driver.execute_script("document.getElementById(arguments[0]).click()", self.F_SUBMIT)
        time.sleep(10)
        if "/content" not in self.driver.current_url:
            errs = [e.text.strip() for e in self.driver.find_elements(
                "css selector", ".a-alert-content") if e.is_displayed() and e.text.strip()]
            raise RuntimeError("第 1 步没过：" + "; ".join(errs[:4]))
        self.log("第 1 步 Complete，草稿已建立。")

        self.log("上传正文与封面…")
        self._upload_files(meta)

        self.log("填写 AI 生成内容申报…")
        declared = self._fill_ai_questionnaire()

        # DRM 选「不加」：加了之后读者换设备麻烦，且以后想撤销不了
        self.driver.execute_script(
            "document.querySelectorAll(\"input[name='data[is_drm]-radio']\")"
            ".forEach(function(e){if(e.value==='false'&&!e.checked)e.click();});")
        time.sleep(1)

        self._wait_processing_done()        # 还在转换就点提交，页面不会跳转

        # 走一遍 Kindle 预览器再提交
        self.log("打开 Kindle 预览器翻几页…")
        self._run_previewer()

        self.log("提交第 2 步…")
        self.driver.execute_script("document.getElementById(arguments[0]).click()", self.F_SUBMIT)
        self._wait_step_done("/pricing", "第 2 步")   # 跳转成功会自己打日志

        self.log("填写第 3 步（KDP Select / 版税 / 定价）…")
        self._fill_pricing(meta)

        self.log("")
        self.log("=" * 56)
        self.log(f"草稿已就绪：{self.driver.current_url}")
        self.log(f"AI 申报：{declared}")
        self.log("三步全部填完，正文和封面都已上传。")
        if do_publish:
            self.log("=" * 56)
            self.publish(meta)
        else:
            self.log("最后一步「Publish」故意没点——发布是不可逆的，留给你自己确认后再按。")
        self.log("=" * 56)

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

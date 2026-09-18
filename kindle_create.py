"""用 Kindle Create 把 DOCX 转成 KPF。

为什么要这个：KDP 也收 DOCX，但转换在它服务器上做，排版结果我们看不见也管不着。
Kindle Create 导出的 KPF 是本地排好版的成品，传上去 KDP 不再转换 —— 所见即所得。

Kindle Create 没有命令行接口（翻过 app bundle，ConvGen/bin 里只有图片和 HTML 工具，
偏好文件里也没有可预设的书籍类型），所以只能驱动界面。下面每条规则都是在真实
界面上试出来的，不是照文档猜的：

1. 有名字的按钮走 AXPress，稳。但**不是所有按钮都有名字**：类型卡片、
   Get Started 这两个在辅助功能树里根本不存在（Qt 自绘），只能点坐标。

2. 点坐标必须发真实 CGEvent。System Events 的 `click at` 对 Qt 自绘控件无效 ——
   实测点了毫无反应，换 CGEvent 立刻生效。

3. **隐藏的控件仍然留在辅助功能树里。** 切到 REFLOWABLE 之后，COMICS 那一套
   单选框和输入框照样 "exists"，所以不能拿「某控件在不在」当状态判据 ——
   这点上栽过一次：一度以为没切过去，其实早就切好了。要认就认那些真正会变的东西
   （按钮名字集合、窗口名字）。

4. **绝对不要用 keystroke 输路径。** AppleScript 的 keystroke 会把下划线打成 "a"：
   Box_Office_Bluff 变成 BoxaOfficeaBluff，导航到一个不存在的目录，还不报错。
   一律用剪贴板 + Cmd+V。

5. 两个文件对话框不是一回事：导入用的是 **Qt 自己的**（有 text field 可以直接
   赋值），导出保存用的是 **macOS 原生的**（AXSplitGroup，只能靠 ⇧⌘G）。

实测耗时（含一本 3.66 MB 的 docx）：导入约 110 秒，导出约 2 分钟，整体 4 分钟上下。
"""
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

APP = "Kindle Create"
PROC = f'tell application "System Events" to tell process "{APP}" to '

# 这两个坐标是量出来的，都相对窗口而不是屏幕，窗口挪动不受影响。
REFLOWABLE_OFFSET = (72, 103)       # 类型选择页左栏第一张卡（REFLOWABLE）
GET_STARTED_FROM_BR = (122, 53)     # 自动分章窗口的 Get Started，按右下角锚定

IMPORT_TIMEOUT = 900    # 导入几百章的稿子可能很久，实测 110 秒
EXPORT_TIMEOUT = 900    # 导出实测 2 分钟


def osa(script: str, timeout: int = 30) -> str:
    """跑一段 AppleScript。出错返回空串不抛 —— 界面重绘时读取失败很常见，
    调用方本来就在轮询，单次失败不该让整个流程崩掉。"""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def click(x: float, y: float):
    """在屏幕坐标发一次真实左键点击。必须 CGEvent，见模块开头第 2 条。"""
    import Quartz
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, (x, y), 0))
    time.sleep(0.15)
    for ev in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(
            None, ev, (x, y), Quartz.kCGMouseButtonLeft))
        time.sleep(0.08)


def paste(text: str):
    """把文本经剪贴板输进当前焦点。见模块开头第 4 条：keystroke 会吃掉下划线。"""
    subprocess.run(["pbcopy"], input=text, text=True, check=True)
    osa(PROC + 'keystroke "v" using {command down}')
    time.sleep(0.6)


class KindleCreateError(RuntimeError):
    pass


class KindleCreate:
    def __init__(self, log: Callable[[str], None] = print):
        self.log = log

    # ---- 读状态 ----

    def windows(self) -> List[str]:
        raw = osa(PROC + "return name of windows")
        return [w.strip() for w in raw.split(",") if w.strip()]

    def buttons(self, window: str = "1") -> List[str]:
        ref = f"window {window}" if window == "1" else f'window "{window}"'
        raw = osa(PROC + f"return name of buttons of {ref}")
        return [b.strip() for b in raw.split(",")
                if b.strip() and b.strip() != "missing value"]

    def geom(self, window: str = "1", tries: int = 12):
        """读窗口的 (x, y, w, h)。读不到就重试。

        必须重试：界面正在重建的那一瞬间，position of window 1 会直接失败。
        实测就是在这儿栽过 —— 切换类型页时一次读取失败，整个流程当场抛错，
        而其实只要再等半秒就好了。
        """
        ref = f"window {window}" if window == "1" else f'window "{window}"'
        script = (f'tell application "System Events" to tell process "{APP}"\n'
                  f"  set p to position of {ref}\n"
                  f"  set s to size of {ref}\n"
                  f'  return (item 1 of p as text) & "," & (item 2 of p as text)'
                  f' & "," & (item 1 of s as text) & "," & (item 2 of s as text)\n'
                  f"end tell")
        for _ in range(tries):
            raw = osa(script)
            try:
                g = tuple(int(v.strip()) for v in raw.split(","))
                if len(g) == 4:
                    return g
            except Exception:
                pass
            time.sleep(0.5)
        return None

    # ---- 等待 ----

    def wait(self, ok: Callable[[], bool], what: str, timeout: int = 120,
             poll: float = 2.0) -> bool:
        """等到条件成立。每一步都要等，因为这个 app 各步骤耗时差很多：
        点一下按钮几百毫秒，导入一百多秒，导出两分钟 —— 固定 sleep 必然
        要么白等要么踩空。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if ok():
                    return True
            except Exception:
                pass
            time.sleep(poll)
        self.log(f"  ✗ 等「{what}」超时（{timeout} 秒）")
        return False

    def wait_button(self, name: str, timeout: int = 120, window: str = "1") -> bool:
        return self.wait(lambda: name in self.buttons(window),
                         f"按钮 {name}", timeout)

    def wait_window(self, name: str, timeout: int = 120) -> bool:
        return self.wait(lambda: name in self.windows(), f"窗口 {name}", timeout)

    # ---- 动作 ----

    def activate(self):
        osa(f'tell application "{APP}" to activate')
        time.sleep(1.2)

    def quit(self, grace: int = 8):
        """关掉 Kindle Create。先好好说，说不听就强杀。

        必须能强杀：导出完成后它停在「You are ready to publish」那一屏，
        那一屏是模态的、会把正常的 quit 挡掉，于是 app 一直挂在那儿，
        下一本永远等不到欢迎页 —— 实测第 2 卷就是这么超时的。
        这时候 KPF 已经落盘，工程文件丢了也不影响交付，可以放心强杀。
        """
        osa(f'tell application "{APP}" to quit')
        for _ in range(grace):
            if not self.running():
                return
            time.sleep(1)
        subprocess.run(["pkill", "-9", "-x", APP], check=False)
        time.sleep(2)

    def restart(self):
        """关掉重开，保证从欢迎页开始。"""
        if self.running():
            self.quit()
        subprocess.run(["open", "-a", APP], check=False)
        for _ in range(30):
            if self.running():
                break
            time.sleep(1)
        self.activate()

    def press(self, name: str, window: str = "1") -> bool:
        ref = f"window {window}" if window == "1" else f'window "{window}"'
        out = osa(PROC + f'perform action "AXPress" of '
                         f'(first button of {ref} whose name is "{name}")')
        ok = "AXPress" in out
        if not ok:
            self.log(f"  ✗ 点不到「{name}」")
        return ok

    def press_prefix(self, prefix: str, window: str = "1") -> bool:
        ref = f"window {window}" if window == "1" else f'window "{window}"'
        out = osa(PROC + f'perform action "AXPress" of '
                         f'(first button of {ref} whose name starts with "{prefix}")')
        return "AXPress" in out

    def click_in(self, window: str, dx: int, dy: int):
        g = self.geom(window)
        if not g:
            raise KindleCreateError(f"读不到窗口 {window} 的位置")
        click(g[0] + dx, g[1] + dy)

    def visible_fields(self) -> List[int]:
        """返回当前可见那一组输入框的序号。

        类型选择完之后，隐藏的 COMICS 面板那 3 个输入框仍留在树里（见开头第 3 条），
        按 1/2/3 盲填会填到看不见的那一组去。两组的 x 坐标不同，取 x 小的那组 ——
        REFLOWABLE 面板在左，实测 x=583，COMICS 的在 x=719。
        """
        # 必须写成完整的 tell...end tell：PROC 以 "to " 结尾，后面接多行语句块
        # 是非法 AppleScript，报的是 "end of line can't go after this to" —— 而
        # osa() 吞掉错误只返回空串，表现成「一个输入框都没有」，极难看出是语法问题。
        raw = osa(f'''tell application "System Events" to tell process "{APP}"
            set out to ""
            repeat with i from 1 to (count of text fields of window 1)
                set p to position of text field i of window 1
                set out to out & i & ":" & (item 1 of p as text) & ","
            end repeat
            return out
        end tell''')
        pairs = []
        for item in raw.split(","):
            if ":" in item:
                i, x = item.split(":")
                try:
                    pairs.append((int(i), int(x)))
                except ValueError:
                    pass
        if not pairs:
            return []
        left = min(x for _, x in pairs)
        return [i for i, x in pairs if x == left]

    def set_field(self, index: int, value: str) -> bool:
        safe = value.replace("\\", "\\\\").replace('"', '\\"')
        osa(PROC + f'set value of text field {index} of window 1 to "{safe}"')
        time.sleep(0.4)
        got = osa(PROC + f"return value of text field {index} of window 1")
        if got != value:
            self.log(f"  ✗ 第 {index} 个输入框没填进去（读回来 {got!r}）")
            return False
        return True

    # ---- 完整流程 ----

    def convert(self, docx: Path, out_dir: Path, title: str, author: str,
                publisher: str = "", quit_after: bool = True) -> Path:
        """把 docx 转成 kpf，返回生成的 .kpf 路径。

        每一步都先等到界面确实到位再动作。中途任何一步等不到都抛错 ——
        这种流程静默走偏的代价极大：点错一步可能建出一本空书还照样导出，
        文件在那儿、内容是错的，要等上架之后才发现。
        """
        docx = Path(docx).resolve()
        if not docx.exists():
            raise KindleCreateError(f"找不到稿件：{docx}")
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = docx.stem

        self.log(f"Kindle Create：{docx.name} -> KPF")
        # 每本都从重启开始，不管它现在是什么状态。
        # 导出完成后它停在「You are ready to publish」那一屏，不是欢迎页，而且
        # 那一屏是模态的、会把 quit 挡掉 —— 所以上一本转完它并没退出。
        # 结果下一本一直等「Create new」等到超时（实测第 2 卷就是这么挂的）。
        # 那一屏上有个 Close 可以点，但那又要多摸一套坐标；重启更省事也更可靠：
        # KPF 已经落盘，工程文件我们不需要。
        self.restart()
        if not self.wait_button("Create new", 60):
            raise KindleCreateError("Kindle Create 没停在欢迎页，先手工关掉它再重试")

        # 1) 欢迎页：选「新建」再确认
        self.press("Create new")
        time.sleep(1)
        self.press("Choose")
        if not self.wait_button("Continue", 30):
            raise KindleCreateError("没进到书籍类型选择页")

        # 2) 类型页：小说要 REFLOWABLE。这张卡不在辅助功能树里，只能点坐标。
        #    默认选中的是 COMICS，不切的话导出来的是漫画排版。
        self.activate()
        self.click_in("1", *REFLOWABLE_OFFSET)
        time.sleep(2)
        self.press("Continue")
        if not self.wait(lambda: any(b.startswith("Choose File")
                                     for b in self.buttons()), "选文件页", 60):
            raise KindleCreateError("没进到「填书名 + 选文件」那一页")

        # 3) 书名/作者/出版方。只能填可见那一组，隐藏的 COMICS 组也在树里。
        # 等输入框真的渲染出来。Choose File 按钮先出现、输入框稍后才挂上，
        # 拿按钮当判据会在这儿读到 0 个输入框 —— 实测踩过。
        fields: List[int] = []

        def fields_ready():
            nonlocal fields
            fields = self.visible_fields()
            return len(fields) >= 3

        if not self.wait(fields_ready, "书名/作者输入框出现", 60):
            raise KindleCreateError(f"这一页只找到 {len(fields)} 个可见输入框，认不出是哪三个")
        for idx, val in zip(fields[:3], (title, author, publisher or author)):
            if val:
                self.set_field(idx, val)
        self.log(f"  · 书名/作者/出版方已填（输入框 {fields[:3]}）")

        # 4) 选稿件。这个是 Qt 自己的文件框，有 text field 可以直接赋值。
        self.press_prefix("Choose File")
        if not self.wait_window("Import from file", 30):
            raise KindleCreateError("没弹出选文件对话框")
        safe = str(docx).replace('"', '\\"')
        osa(PROC + f'set value of text field 1 of window "Import from file" to "{safe}"')
        time.sleep(0.8)
        osa(PROC + 'set focused of text field 1 of window "Import from file" to true')
        time.sleep(0.4)
        osa(PROC + "key code 36")
        if not self.wait(lambda: "Import from file" not in self.windows(),
                         "文件对话框关闭", 30):
            raise KindleCreateError("文件对话框没关，路径多半没被接受")
        self.log(f"  · 已送入 {docx.name}，等它导入（几百章要一两分钟）…")

        # 5) 导入。期间按钮是 Cancel Import，完事变成 Continue。
        if not self.wait(lambda: "Continue" in self.buttons(), "导入完成",
                         IMPORT_TIMEOUT, poll=5):
            raise KindleCreateError("导入超时")
        self.press("Continue")

        # 6) 自动分章。Get Started 没有名字，只能按右下角锚点点。
        if self.wait_window("Automatic Chapter Titles", 60):
            g = self.geom("Automatic Chapter Titles")
            self.activate()
            self.click_in("Automatic Chapter Titles",
                          g[2] - GET_STARTED_FROM_BR[0], g[3] - GET_STARTED_FROM_BR[1])
            self.log("  · 已开始自动找章节标题")
            # 分析完会弹出建议面板；一章都没认出来时不弹，那也正常，往下走
            if self.wait_window("Suggested Chapter Titles", 600):
                self.press("Accept Selected", "Suggested Chapter Titles")
                self.wait(lambda: "Suggested Chapter Titles" not in self.windows(),
                          "章节建议面板关闭", 60)
                self.log("  · 已接受建议的章节标题")
        else:
            self.log("  · 没出现自动分章窗口，跳过")

        # 7) 导出。先确保勾的是 KPF —— EPUB 那个勾上会多导一份没人要的文件。
        if not self.wait_button("Export", 120):
            raise KindleCreateError("编辑器里没出现 Export 按钮")
        self.press("Export")
        if not self.wait(lambda: "Cancel" in self.buttons()
                         and "Export" in self.buttons(), "导出格式对话框", 30):
            raise KindleCreateError("没弹出导出格式对话框")
        if osa(PROC + "return value of checkbox 1 of window 1") == "0":
            osa(PROC + 'perform action "AXPress" of checkbox 1 of window 1')
            time.sleep(0.8)
        self.press("Export")

        # 8) 保存面板。这个是 macOS 原生的，只能 ⇧⌘G 导航；路径一律粘贴不敲。
        if not self.wait_window("Save file for Publication", 60):
            raise KindleCreateError("没弹出保存面板")
        self.activate()
        osa(PROC + 'keystroke "g" using {command down, shift down}')
        time.sleep(1.5)
        paste(str(out_dir))
        osa(PROC + "key code 36")
        time.sleep(2)
        osa(PROC + 'keystroke "a" using {command down}')
        time.sleep(0.5)
        paste(stem)
        osa(PROC + "key code 36")
        if not self.wait(lambda: "Save file for Publication" not in self.windows(),
                         "保存面板关闭", 60):
            raise KindleCreateError("保存面板没关，导出路径多半没被接受")

        # 9) 等 kpf 落地并且大小稳定 —— 文件一出现就去传，传的是半个文件。
        target = out_dir / "KPF"
        self.log("  · 正在导出 KPF（约两分钟）…")
        last = {}

        def done():
            found = list(target.glob("*.kpf")) + list(out_dir.glob("*.kpf"))
            if not found:
                return False
            p = max(found, key=lambda f: f.stat().st_mtime)
            size = p.stat().st_size
            stable = last.get(p) == size and size > 0
            last[p] = size
            return stable

        if not self.wait(done, "KPF 导出完成", EXPORT_TIMEOUT, poll=5):
            raise KindleCreateError("导出超时，或者没在目标目录里找到 .kpf")
        kpf = max(list(target.glob("*.kpf")) + list(out_dir.glob("*.kpf")),
                  key=lambda f: f.stat().st_mtime)
        self.log(f"  ✓ {kpf.name}（{kpf.stat().st_size / 1024 / 1024:.1f} MB）")

        if quit_after:
            # 用 self.quit() 而不是裸 quit：这会儿正停在模态的
            # 「You are ready to publish」上，裸 quit 关不掉
            self.quit()
        return kpf

    def running(self) -> bool:
        return subprocess.run(["pgrep", "-x", APP],
                              capture_output=True).returncode == 0

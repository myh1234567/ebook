"""KDP 自动化自检：确认浏览器真的被驱动了，以及页面到底长什么样。

只读、只导航，不点 Create eBook、不填任何字段、不建草稿。
跑的时候盯着那个 Chrome 窗口，标签页会真的跳转 —— 看得见就说明连上了。

用法（项目根目录）：
    python3 scripts/dump_kdp_create.py
"""
import time

from selenium import webdriver

PORT = 9333
CREATE_URL = "https://kdp.amazon.com/en_US/create"


def show(driver, when):
    print(f"\n[{when}] URL：{driver.current_url}")
    print(f"[{when}] 标题：{driver.title}")


def main():
    print("① 接管 9333 端口的 Chrome…")
    o = webdriver.ChromeOptions()
    o.debugger_address = f"127.0.0.1:{PORT}"
    try:
        driver = webdriver.Chrome(options=o)
    except Exception as exc:
        print(f"   ❌ 连不上：{exc}")
        print("   Chrome 没带调试端口起来。先跑 `python3 cli.py chrome` 再试。")
        return
    print("   ✅ 连上了")
    show(driver, "跳转前")

    print(f"\n② 打开 {CREATE_URL} …（盯着 Chrome，标签页应该会跳）")
    driver.get(CREATE_URL)
    time.sleep(6)
    show(driver, "跳转后")

    if "/ap/signin" in driver.current_url:
        print("   ❌ 被弹到登录页了。先在那个 Chrome 里手动登录 KDP。")
        return

    print("\n③ 页面上所有可见可点的东西（uploader 就是在这里面找 Create eBook）")
    for e in driver.find_elements(
            "css selector", "button, a, span.a-button-text"):
        try:
            txt = (e.get_attribute("textContent") or "").strip()
            if txt and len(txt) <= 60 and e.is_displayed():
                print(f"   <{e.tag_name}> {txt!r}")
        except Exception:
            continue

    # ---- 以下是为了查「element not interactable」才点进去的 ----
    # 点 Create eBook 只是跳到空白的新书表单页，KDP 要到 Save and Continue
    # 才会真的建草稿。所以这一步不会在你书架上留下任何东西。
    print("\n④ 点 Create eBook，进第 1 步表单…")
    clicked = driver.execute_script("""
        var els = document.querySelectorAll('button, a, span.a-button-text');
        for (var i = 0; i < els.length; i++) {
          if ((els[i].textContent || '').trim() !== 'Create eBook') continue;
          if (!els[i].offsetParent) continue;
          els[i].click();
          return true;
        }
        return false;
    """)
    if not clicked:
        print("   ❌ 没点到，停在这里")
        return
    print("   ✅ 点了")

    # 分档看：书名框是什么时候才变得可输入的。uploader 原来点完只等 6 秒，
    # 如果 10 秒那一档才转 True，那就是纯粹没等够。
    print("\n⑤ 书名框 #data-title 随时间的状态变化")
    elapsed = 0
    for t in (2, 5, 10, 20):
        time.sleep(t - elapsed)      # 睡差值，打印的秒数才是真的累计耗时
        elapsed = t
        states = driver.execute_script("""
            var out = [];
            document.querySelectorAll('[id="data-title"]').forEach(function(e){
              var s = window.getComputedStyle(e);
              out.push({tag: e.tagName, display: s.display,
                        visibility: s.visibility, disabled: e.disabled === true,
                        readOnly: e.readOnly === true, 可见: !!e.offsetParent});
            });
            return out;
        """)
        print(f"   第 {t} 秒：URL={driver.current_url}")
        print(f"          {states if states else '页面上还没有 #data-title'}")

    print("\n⑥ 表单里所有可见的输入框（书名框要是改了 id，在这能看出来）")
    for e in driver.find_elements("css selector", "input, textarea"):
        try:
            if not e.is_displayed():
                continue
            print(f"   <{e.tag_name}> id={e.get_attribute('id')!r} "
                  f"name={e.get_attribute('name')!r} type={e.get_attribute('type')!r}")
        except Exception:
            continue

    print("\n完成。没有点 Save and Continue，书架上不会多出草稿。")


if __name__ == "__main__":
    main()

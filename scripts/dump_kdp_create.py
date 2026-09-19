"""抓 KDP /create 页面的真实结构，用来修「没找到 Create eBook 入口」。

只读：接管已在 9333 端口跑着的 Chrome（沿用已有登录态），打开 /create，
把页面上所有可点的东西原样打印出来。不点击、不改动任何页面状态。

用法（在项目根目录）：
    python3 scripts/dump_kdp_create.py

把输出整段贴回来，对着真实结构改 kdp_uploader.py:1307 的选择器。
"""
import time

from selenium import webdriver

PORT = 9333
URL = "https://kdp.amazon.com/en_US/create"


def main():
    o = webdriver.ChromeOptions()
    o.debugger_address = f"127.0.0.1:{PORT}"
    driver = webdriver.Chrome(options=o)

    driver.get(URL)
    time.sleep(6)  # 比 uploader 的 3 秒宽，先排除「没加载完」这个可能

    print(f"当前 URL：{driver.current_url}")
    print(f"标题：{driver.title}")
    print("=" * 70)

    # 1) uploader 现在用的那一套：a / button，比对渲染文本
    print("\n[1] 所有 <a> 和 <button>（uploader 现在只看这些）")
    for e in driver.find_elements("xpath", "//a|//button"):
        txt = (e.text or "").strip()
        if not txt:
            continue
        print(f"  <{e.tag_name}> text={txt!r} "
              f"显示={e.is_displayed()} href={e.get_attribute('href')!r}")

    # 2) 放宽：页面上任何含 ebook 字样的元素，不限标签
    print("\n[2] 任何标签里含 'ebook'（不分大小写），看它到底是什么元素")
    xp = ("//*[contains(translate(text(),"
          "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'ebook')]")
    for e in driver.find_elements("xpath", xp):
        txt = (e.text or "").strip()
        if not txt or len(txt) > 120:
            continue
        print(f"  <{e.tag_name}> text={txt!r} 显示={e.is_displayed()} "
              f"id={e.get_attribute('id')!r} class={e.get_attribute('class')!r}")

    # 3) 真出不来就看原始 HTML —— 前面两项都空说明可能根本没到这个页面
    #    （被重定向到登录页、或者 Amazon 弹了验证）
    print("\n[3] 页面 HTML 前 3000 字符")
    print(driver.page_source[:3000])


if __name__ == "__main__":
    main()

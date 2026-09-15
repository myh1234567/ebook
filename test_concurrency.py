"""测不同并发下 CLI 的真实吞吐，用来定 chapter_workers 该填多少。

并发不是越高越好：撞上限流后会触发重试退避（30 秒、90 秒），
等待时间可能把并行省下的全吃回去，净吞吐反而比低并发还差。
所以这个值要测，不要猜。

用法：
    python test_concurrency.py                # 测 1 / 4 / 10 / 20
    python test_concurrency.py 1 8 16 32      # 自己指定要测的并发档位

跑完看「有效吞吐」那一列，取最高的那档。别只看耗时——
失败的调用不算数，只算成功返回的。
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import novel_adapter as na
from config import Settings

# 用真实写作任务，长度接近一个章节的量级；"回复OK"那种太短测不出限流
PROMPT = ("Write a vivid 200-word scene in English: a rainy night at a harbor dock, "
          "two people meeting to exchange something. Output only the prose.")


def run_once(eng, cfg):
    t0 = time.time()
    try:
        out = eng._call_cli(PROMPT, "You are a novelist.",
                            cfg.cli_command, cfg.cli_model, cfg.cli_args)
        return time.time() - t0, len(out or ""), None
    except Exception as exc:
        return time.time() - t0, 0, str(exc)[:80]


def bench(level: int, cfg):
    eng = na.NovelAdaptationEngine(cfg, log_func=lambda m: None)
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=level) as pool:
        futs = [pool.submit(run_once, eng, cfg) for _ in range(level)]
        for f in as_completed(futs):
            results.append(f.result())
    wall = time.time() - t0

    ok = [r for r in results if r[2] is None and r[1] > 50]
    fails = [r for r in results if r[2] is not None]
    avg = sum(r[0] for r in ok) / len(ok) if ok else 0
    tput = len(ok) / wall * 60 if wall else 0     # 每分钟成功多少次

    print(f"  并发 {level:>3}  墙钟 {wall:6.1f}s  单次均值 {avg:6.1f}s  "
          f"成功 {len(ok):>3}/{level}  有效吞吐 {tput:6.1f} 次/分钟")
    if fails:
        print(f"           失败 {len(fails)} 次，样例：{fails[0][2]}")
    return tput


def main():
    levels = [int(x) for x in sys.argv[1:]] or [1, 4, 10, 20]
    s = Settings.load()
    cfg = na.config_from_settings(s)
    if cfg.provider != "cli":
        raise SystemExit("接入方式不是 cli，这个测试只针对本机 CLI。")

    # 重试会掩盖限流：这里要看到裸的失败率，所以只试一次
    na.CLI_RETRIES = 1
    na.CLI_TIMEOUT = 180

    print(f"命令 {cfg.cli_command} / 模型 {cfg.cli_model}")
    print(f"参数 {cfg.cli_args}\n")
    print("注意：重试已关掉，失败数就是裸的限流信号。\n")

    best, best_level = 0, 1
    for lv in levels:
        tput = bench(lv, cfg)
        if tput > best:
            best, best_level = tput, lv
        time.sleep(5)      # 档位之间喘口气，别让上一档的限流影响下一档

    print(f"\n吞吐最高的是并发 {best_level}（{best:.1f} 次/分钟）")
    print(f"建议 settings.json 里 chapter_workers 填 {best_level}")
    print("如果高并发档位有失败，说明已经限流了，按成功率和吞吐一起权衡。")


if __name__ == "__main__":
    main()

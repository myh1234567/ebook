"""批量队列：Drive 文件夹里的每个 txt 是一个任务，一本一本串行跑。

为什么串行不并行：
  · 一本书几百章 LLM 调用，要跑几小时，并行只会互相抢配额；
  · KDP 那边只有一个账号和一个 Chrome profile，本来就不能并行上架。

断点续跑靠两层，都不是新造的机制：
  · 队列层 —— batch_state.json 记哪本做完了，重进跳过；
  · 单本层 —— 项目目录里已有的每章 JSON 缓存和 deliverables_fresh()，
    一本书跑到一半崩了，重进会从缺口那章接着补。

状态用 Drive 的 fileId 做 key，不用文件名：文件名随时会改，ID 不变。

GUI 和 CLI 都调这里，界面只负责显示和点按钮。
"""
import json
import os
import re
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import gdrive

STATE_FILE = Path(__file__).resolve().parent / "batch_state.json"
CACHE_DIR = Path(__file__).resolve().parent / "_drive_cache"

PENDING, RUNNING, DONE, FAILED = "待处理", "进行中", "已完成", "失败"


@dataclass
class Job:
    file_id: str
    name: str
    size: int
    folder: str = ""          # 所在子文件夹，如「仙侠」；只用来显示，不参与判定
    status: str = PENDING
    project_dir: str = ""
    kdp: str = ""
    error: str = ""
    updated_at: str = ""

    @property
    def size_mb(self) -> float:
        return self.size / 1024 / 1024


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def _state_lock():
    """给 batch_state.json 的读-改-写上互斥锁。

    不加锁的后果实测过：三个进程并发时，A 把某本标成「已完成」，B 拿着更早读到的
    状态写回去，直接把 A 的结果覆盖掉 —— 那本书会被当成没做过再跑一遍，
    并且在 Amazon 上建出重复的书。
    """
    import fcntl
    lf = STATE_FILE.with_suffix(".lock")
    with open(lf, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_state() -> Dict[str, dict]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            # 读到半截文件（别的进程正在写）不能当成「没有状态」——
            # 那会让已完成的书被重新跑。宁可抛错也别静默返回空。
            time.sleep(0.2)
            try:
                return json.loads(STATE_FILE.read_text("utf-8"))
            except Exception:
                return {}
    return {}


def save_state(state: Dict[str, dict]):
    # 先写临时文件再 rename：rename 是原子的，读的人要么看到旧的完整内容、
    # 要么看到新的完整内容，不会读到半截
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, STATE_FILE)


def list_jobs(folder: str, sa_path: str = "", svc=None) -> List[Job]:
    """列出队列。Drive 上的文件和本地状态合并，Drive 是事实来源。

    只读 Drive、只读状态文件，不产生副作用 —— GUI 刷新列表会频繁调它。
    """
    svc = svc or gdrive.service(sa_path)
    folder_id = _resolve_folder(svc, folder)
    state = load_state()
    jobs = []
    for f in gdrive.list_texts(svc, folder_id):
        st = state.get(f["id"], {})
        jobs.append(Job(
            file_id=f["id"], name=f["name"], size=int(f.get("size") or 0),
            folder=f.get("path", ""),
            status=st.get("status", PENDING),
            project_dir=st.get("project_dir", ""),
            kdp=st.get("kdp", ""), error=st.get("error", ""),
            updated_at=st.get("updated_at", "")))
    return jobs


def folder_info(folder: str, sa_path: str = "") -> Dict[str, str]:
    """把设置里那串 ID（或名字/链接）解析成 {id, name}，给界面显示用。"""
    svc = gdrive.service(sa_path)
    fid = _resolve_folder(svc, folder)
    return {"id": fid, "name": gdrive.folder_name(svc, fid) or folder}


CLAIM_DIR = Path(__file__).resolve().parent / "_claims"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def claim(job: Job) -> bool:
    """原子认领一本书。别的进程已经在跑就返回 False。

    多个终端各跑 `cli.py batch --limit 1` 时，光看 batch_state.json 的状态不够：
    它是读-改-写，两个进程能同时读到同一本还是「待处理」，然后一起开跑。
    这里用 O_CREAT|O_EXCL 建锁文件 —— 文件系统保证只有一个进程能建成功。

    锁文件里记 PID：进程崩了锁会留下，靠 PID 判活来回收，否则那本书就永远卡住了。
    """
    CLAIM_DIR.mkdir(exist_ok=True)
    f = CLAIM_DIR / f"{job.file_id}.lock"
    try:
        fd = os.open(str(f), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()}\n{_now()}\n{job.name}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        pass

    # 锁已存在。要判断持有者是死是活 —— 但这里有个坑（实测踩过）：
    # 上面 os.open 和 os.write 是两步，中间有个窗口，文件已存在但还是空的。
    # 这时读到空内容就判「死锁」抢过来的话，两个进程会同时处理同一本书。
    # 所以读不出 PID 时先重试几次，仍读不出且文件够旧才认定是死锁。
    pid = -1
    for _ in range(10):
        try:
            head = f.read_text().splitlines()
            if head and head[0].strip():
                pid = int(head[0])
                break
        except Exception:
            pass
        time.sleep(0.05)

    if pid > 0:
        if _alive(pid):
            return False              # 真的有进程在跑
    else:
        # 一直读不出 PID：文件够新就当成「别人正在写」，让给它
        try:
            if time.time() - f.stat().st_mtime < 60:
                return False
        except FileNotFoundError:
            return claim(job)         # 刚好被别人清掉了，重来

    f.unlink(missing_ok=True)         # 确认是死锁，抢过来
    return claim(job)


def release(job: Job):
    (CLAIM_DIR / f"{job.file_id}.lock").unlink(missing_ok=True)


def _mark(job: Job, status: str, **kw):
    # 整个读-改-写必须在锁里，否则并发下会丢更新
    with _state_lock():
        state = load_state()
        rec = state.get(job.file_id, {})
        rec.update(name=job.name, status=status, updated_at=_now(), **kw)
        state[job.file_id] = rec
        save_state(state)
    job.status = status


def status_of(file_id: str) -> str:
    """读某一本的最新状态。并发下必须现读，不能用开头那份快照。"""
    with _state_lock():
        return load_state().get(file_id, {}).get("status", "")


def _resolve_folder(svc, folder: str) -> str:
    """folder 可以是文件夹名，也可以直接是 Drive 的文件夹 ID。

    Drive ID 是一串没有空格的字母数字下划线连字符，且相当长；文件夹名一般不长这样。
    判断错也不要紧：按 ID 查不到会退回按名字查。
    """
    from_url = gdrive.folder_id_from_url(folder)
    if from_url:
        folder = from_url          # 直接粘 Drive 链接也认
    looks_like_id = (len(folder) >= 25
                     and re.fullmatch(r"[A-Za-z0-9_-]+", folder) is not None)
    if looks_like_id:
        try:
            svc.files().get(fileId=folder, fields="id",
                            supportsAllDrives=True).execute()
            return folder
        except Exception:
            pass
    return gdrive.find_folder(svc, folder)


def run_batch(folder: str,
              settings,
              log: Callable[[str], None] = print,
              cancel=None,
              sa_path: str = "",
              gap_minutes: float = 0.0,
              limit: int = 0) -> Dict[str, int]:
    """把队列跑完。返回 {已完成, 跳过, 失败} 计数。

    gap_minutes：每本之间隔多久。批量往一个 KDP 账号上架是有风控风险的，
    留这个口子让调用方决定节奏，默认 0 表示不等。
    """
    import novel_adapter

    svc = gdrive.service(sa_path)
    log(f"服务账号：{gdrive.sa_email(svc)}")
    jobs = list_jobs(folder, svc=svc)
    if not jobs:
        log("队列是空的：这个 Drive 文件夹里没有 txt。")
        return {"done": 0, "skip": 0, "fail": 0}

    todo = [j for j in jobs if j.status != DONE]
    log(f"队列共 {len(jobs)} 本，待处理 {len(todo)} 本"
        f"（已完成 {len(jobs) - len(todo)} 本会跳过）")
    if limit:
        todo = todo[:limit]
        log(f"本次只跑前 {len(todo)} 本")

    tally = {"done": 0, "skip": len(jobs) - len(todo), "fail": 0}
    CACHE_DIR.mkdir(exist_ok=True)

    for i, job in enumerate(todo, 1):
        if cancel and cancel.is_set():
            log("已取消，剩下的保持待处理。")
            break

        # todo 是开头算好的快照。多终端时，别的进程可能在这期间已经把这本做完了 ——
        # 锁只防「同时处理」，不防「做完之后又被processed一遍」。不重新读状态的话，
        # 一本书会被跑两次，而且会在 Amazon 上建出重复的书。
        if status_of(job.file_id) == DONE:
            log(f"  · 「{job.name}」已被别的进程完成，跳过")
            tally["skip"] += 1
            continue

        # 原子认领：别的终端正在跑这本就跳过，去拿下一本。
        # 这样多个终端各跑 `batch` 会自动分到不同的书，不用手工指定。
        if not claim(job):
            log(f"  · 「{job.name}」正被别的进程处理，跳过")
            tally["skip"] += 1
            continue

        # 拿到锁之后再确认一次：上面那次读状态和拿到锁之间仍有空隙，
        # 别的进程可能正好在这个空隙里完成并释放了锁。
        if status_of(job.file_id) == DONE:
            log(f"  · 「{job.name}」已被别的进程完成，跳过")
            release(job)
            tally["skip"] += 1
            continue

        label = f"{job.folder}/" if job.folder else ""
        log(f"\n{'=' * 56}\n[{i}/{len(todo)}] {label}{job.name}"
            f"（{job.size_mb:.1f} MB）\n{'=' * 56}")
        _mark(job, RUNNING)
        t0 = time.time()
        engine = None          # 下载阶段就失败的话，下面的异常分支要能判空
        try:
            local = gdrive.download(svc, job.file_id,
                                    CACHE_DIR / f"{job.file_id}_{job.name}", log=log)

            # 每本书独立配置：源文件换成刚下载的，其余沿用界面上的设置。
            cfg = novel_adapter.config_from_settings(settings)
            cfg.source_file = str(local)
            cfg.subtitle = ""
            cfg.series_info = ""
            # 书名留空让引擎从改编档案里取。但重试一本失败的书时要沿用上次的书名，
            # 否则 project_dir_for 会指到占位目录，已经改编好的章节全白跑。
            cfg.book_title = Path(job.project_dir).name if job.project_dir else ""
            # 占位目录按 Drive fileId 区分。不给的话所有新书共用
            # output/Novel_Adaptation_Project，多终端并行会把不同书的章节写到一起。
            cfg.work_id = job.file_id

            engine = novel_adapter.NovelAdaptationEngine(cfg, log_func=log)

            # 自动识别每本书自己的年代/国家/题材/受众。
            # 这一步不在 run_full_pipeline 里面，得显式调（GUI 的单本流程也是这么做的）。
            # 批量尤其需要：一队书里修仙、都市、宫斗混着，套同一份设定必然错。
            if getattr(settings, "novel_auto_detect", True):
                done_ch = list((novel_adapter.project_dir_for(cfg) / "_chapters").glob("*.json"))
                if done_ch:
                    # 续跑不能重新识别：模型两次给的目标设定未必一样，
                    # 前 50 章在好莱坞、后 749 章跑到摄政英国就废了
                    log(f"  · 已有 {len(done_ch)} 章进度，沿用上次的改编设定，不重新识别")
                else:
                    log("  · 正在识别这本书的改编设定…")
                    engine.analyze_source_settings()
                    log(f"    题材={cfg.genre} / {cfg.target_country} {cfg.target_era}")

            # 书名一确定就把项目目录记进状态。硬杀（Ctrl-C/关终端）时
            # 下面的 except 不会执行，只有这里回写过才能在重启后找回已改编的章节。
            proj_dir = engine.run_full_pipeline(
                project_cb=lambda d: _mark(job, RUNNING, project_dir=str(d)))
            mins = (time.time() - t0) / 60
            log(f"✅ 改编完成，耗时 {mins:.0f} 分钟 -> {proj_dir}")

            kdp_note = ""
            if getattr(settings, "kdp_auto_upload", False):
                import cli as _cli
                kdp_note = _cli.run_kdp_autopost(settings, proj_dir)
                log(kdp_note)

            _mark(job, DONE, project_dir=str(proj_dir), kdp=kdp_note, error="")
            tally["done"] += 1

        except Exception as exc:
            # 一本失败不能把整队带停：记下来，继续下一本。
            # 同时把已经定下来的项目目录记住 —— 重试时靠它找回已改编的章节，
            # 不然书名丢了就会从头再跑一遍。
            where = ""
            try:
                if engine and engine.config.book_title:
                    where = str(novel_adapter.project_dir_for(engine.config))
            except Exception:
                pass
            log(f"❌ 这本失败了：{exc}")
            log(traceback.format_exc(limit=3))
            _mark(job, FAILED, error=str(exc)[:300], project_dir=where)
            tally["fail"] += 1
        finally:
            release(job)

        if gap_minutes and i < len(todo):
            if cancel and cancel.is_set():
                break
            log(f"按设置等 {gap_minutes:g} 分钟再跑下一本…")
            waited = 0.0
            while waited < gap_minutes * 60:
                if cancel and cancel.is_set():
                    log("等待期间被取消。")
                    return tally
                time.sleep(2)
                waited += 2

    log(f"\n批量结束：完成 {tally['done']}，跳过 {tally['skip']}，失败 {tally['fail']}")
    return tally


def reset(file_id: Optional[str] = None):
    """把某一本（或全部）打回待处理，用于失败后重试。"""
    if file_id is None:
        STATE_FILE.unlink(missing_ok=True)
        return
    state = load_state()
    state.pop(file_id, None)
    save_state(state)

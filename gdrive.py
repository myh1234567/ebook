"""Google Drive 只读访问（服务账号）。

为什么用服务账号而不是 OAuth：drive.readonly 是 Google 分级里的「受限」scope，
OAuth 应用停在测试状态时 refresh token 7 天就失效，长跑任务会每周挂一次；
想转生产状态又要做 CASA 第三方安全评估。服务账号的 key 不过期、不走验证流程。
代价是要把 Drive 文件夹共享给它的邮箱（权限「查看者」即可）。

只读是故意的：产出留在本机，不往 Drive 写，所以不需要更大的权限。

密钥怎么来见 test_gdrive.py 开头的说明。
"""
from pathlib import Path
from typing import Dict, List, Optional

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# 源小说认这些后缀。Drive 里那一堆 txt 就是待处理队列。
TEXT_SUFFIXES = (".txt",)


class DriveError(RuntimeError):
    """Drive 这边的问题，和改编流程本身的错误区分开。"""


def _require_libs():
    try:
        from googleapiclient.discovery import build          # noqa: F401
        from google.oauth2 import service_account            # noqa: F401
    except ImportError as exc:
        raise DriveError(
            "缺少 Google Drive 依赖，先装：\n"
            "    pip install google-api-python-client google-auth") from exc


def find_sa_key(explicit: str = "") -> Optional[Path]:
    """定位服务账号密钥。

    显式指定优先；否则在项目根目录找一个 type=service_account 的 json。
    下载下来的密钥文件名是 Google 随机生成的（kdp-writer-xxxx-yyyy.json），
    让用户改名或每次填路径都很烦，所以支持自动认。
    """
    import json
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    here = Path(__file__).resolve().parent
    for p in sorted(here.glob("*.json")):
        try:
            if json.loads(p.read_text("utf-8")).get("type") == "service_account":
                return p
        except Exception:
            continue
    return None


def service(sa_path: str = ""):
    """建 Drive 客户端。找不到密钥就抛 DriveError，附带怎么弄。"""
    _require_libs()
    from googleapiclient.discovery import build
    from google.oauth2 import service_account

    key = find_sa_key(sa_path)
    if not key:
        raise DriveError(
            "没找到服务账号密钥。把 Google Cloud 下载的 JSON 放到项目根目录，"
            "或在设置里填它的路径。申请步骤见 test_gdrive.py 开头。")
    creds = service_account.Credentials.from_service_account_file(
        str(key), scopes=SCOPES)
    svc = build("drive", "v3", credentials=creds)
    svc._sa_email = creds.service_account_email   # 出错时好提示是哪个账号
    svc._sa_key = str(key)
    return svc


def sa_email(svc) -> str:
    return getattr(svc, "_sa_email", "(未知)")


def folder_id_from_url(s: str) -> str:
    """从 Drive 链接里抠出文件夹 ID。抠不出来返回空串。

    支持直接粘浏览器地址栏的 URL —— 比让人手工截那一串 ID 省事也不容易错：
        https://drive.google.com/drive/folders/1pHKSbpb...?usp=sharing
    """
    import re
    m = re.search(r"/folders/([A-Za-z0-9_-]{10,})", s or "")
    return m.group(1) if m else ""


def find_folder(svc, name: str) -> str:
    """按名字找文件夹，返回 id。"""
    q = ("mimeType='application/vnd.google-apps.folder' and trashed=false "
         f"and name='{name}'")
    hits = svc.files().list(
        q=q, fields="files(id,name)", pageSize=10,
        supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute().get("files", [])
    if not hits:
        raise DriveError(
            f"Drive 里没找到名为「{name}」的文件夹。\n"
            f"最常见原因：这个文件夹还没共享给 {sa_email(svc)}。\n"
            f"服务账号看不到任何没共享给它的内容。")
    return hits[0]["id"]


FOLDER_MIME = "application/vnd.google-apps.folder"


def _children(svc, folder_id: str) -> List[Dict]:
    out, token = [], None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,size,mimeType,modifiedTime)",
            pageSize=200, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        out.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return out


def folder_name(svc, folder_id: str) -> str:
    """取文件夹的真实名字。设置里存的是 ID（不怕改名），显示时换成人能看懂的名字。"""
    try:
        return svc.files().get(fileId=folder_id, fields="name",
                               supportsAllDrives=True).execute().get("name", "")
    except Exception:
        return ""


def list_texts(svc, folder_id: str, max_depth: int = 4) -> List[Dict]:
    """递归列出文件夹（含子文件夹）里的源小说 txt。

    实际的「小说同步」目录是按题材分子文件夹放的（仙侠/军事/历史/…），
    只扫一层会一本都找不到，所以要往下钻。每条结果多带一个 path 字段，
    记它在哪个子文件夹里，队列界面上显示出来方便你认。

    排序按 path + 文件名，同题材的书会排在一起，队列顺序就是这个顺序。
    """
    found = []

    def walk(fid: str, rel: str, depth: int):
        for f in _children(svc, fid):
            if f["mimeType"] == FOLDER_MIME:
                if depth < max_depth:
                    walk(f["id"], f"{rel}/{f['name']}" if rel else f["name"], depth + 1)
            elif f["name"].lower().endswith(TEXT_SUFFIXES):
                found.append({**f, "path": rel})

    walk(folder_id, "", 0)
    return sorted(found, key=lambda f: (f.get("path", ""), f["name"]))


def download(svc, file_id: str, dest: Path, log=None) -> Path:
    """下载到 dest。已存在且大小一致就跳过，重跑队列时不必重下几 MB。"""
    import io
    from googleapiclient.http import MediaIoBaseDownload

    meta = svc.files().get(fileId=file_id, fields="name,size",
                           supportsAllDrives=True).execute()
    want = int(meta.get("size") or 0)
    dest = Path(dest)
    if dest.exists() and want and dest.stat().st_size == want:
        if log:
            log(f"  · 本地已有 {dest.name}（{want/1024:.0f} KB），跳过下载")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = dl.next_chunk()
    dest.write_bytes(buf.getvalue())
    if log:
        log(f"  ✓ 已下载 {dest.name}（{dest.stat().st_size/1024:.0f} KB）")
    return dest

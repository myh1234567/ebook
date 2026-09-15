"""测试：能不能用服务账号从 Google Drive 读到上架所需的三个文件。

只验证，不动现有代码。跑完会明确说链路通不通、缺什么。

需要的三个文件：
    03_Publishing_Copy.txt   元数据（书名/简介/关键词/分类）
    07_Manuscript.epub       正文
    05_Ebook_Cover.png/jpg   封面

关键约束：正文和封面**必须落地成本地文件**。KDP 上传走的是 send_keys 往
<input type=file> 塞路径，只认真实本地绝对路径，喂 URL 没用（本项目实测撞过
"path is not absolute"）。所以链路只能是：

    Drive -> 下载到本地临时目录 -> send_keys 上传给 KDP

脚本最后就是验这一步：下载完直接喂给现有的 KDPMetadata.load_from_project_dir，
不改它一行，能解析出来就说明整条链路可行。

为什么用服务账号而不是 OAuth：drive.readonly 是 Google 分级里的「受限」scope，
OAuth 应用停在测试状态时 refresh token 7 天就失效，云机上的无人值守任务会每周挂一次；
想转生产状态又要做 CASA 第三方安全评估。服务账号的 key 不过期、不走验证流程。
代价只是要把 Drive 文件夹共享给它的邮箱。

本地和云机用法完全一致，同一个 sa.json 拷过去就能跑：
    python test_gdrive.py --folder-name Box_Office_Bluff
    python test_gdrive.py --folder-id 1AbCdEf...

Drive 文件夹 ID 就是地址栏 /folders/ 后面那串：
    https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrSt
                                           ^^^^^^^^^^^^^^^^^^^^^
"""
import argparse
import io
import sys
import tempfile
import zipfile
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
HERE = Path(__file__).resolve().parent

SETUP = """
申请服务账号（全程免费，不用绑卡，约 2 分钟）：

  1. 启用 Drive API
     https://console.cloud.google.com/apis/library/drive.googleapis.com
     左上角先把项目选对（没有就新建），点「启用」

  2. 建服务账号
     https://console.cloud.google.com/iam-admin/serviceaccounts
     「创建服务账号」-> 名字随便起（如 kdp-reader）-> 后面两步「继续」「完成」
     不用授予任何 IAM 角色：Drive 的访问权靠共享，不靠角色

  3. 建密钥
     点进刚建的账号 -> 「密钥」标签 -> 「添加密钥」->「创建新密钥」-> 选 JSON
     下载的文件存成：{sa}

  4. 把 Drive 文件夹共享给它   <- 最容易漏的一步
     复制服务账号邮箱（形如 kdp-reader@你的项目.iam.gserviceaccount.com）
     Drive 里右键那个书籍文件夹 -> 共享 -> 粘贴邮箱 -> 权限选「查看者」-> 发送
"""

# 每个角色按优先级找文件名，和 kdp_uploader 的规则保持一致
NEEDED = {
    "元数据": ["03_Publishing_Copy.txt"],
    "正文":   ["07_Manuscript.epub", "01_English_Manuscript.docx"],
    "封面":   ["05_Ebook_Cover.png", "05_Ebook_Cover.jpg"],
}

OK, BAD, DOT = "  ✓", "  ✗", "  ·"


def get_service(sa_path: Path):
    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
    except ImportError:
        raise SystemExit(f"{BAD} 缺库，先装：\n"
                         f"     pip install google-api-python-client google-auth")
    if not sa_path.exists():
        raise SystemExit(f"{BAD} 找不到服务账号密钥 {sa_path}\n"
                         + SETUP.format(sa=sa_path))
    creds = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=SCOPES)
    print(f"{OK} 服务账号：{creds.service_account_email}")
    print(f"{DOT} 它只能看到共享给这个邮箱的内容")
    return build("drive", "v3", credentials=creds)


def find_folder(svc, name: str) -> str:
    q = ("mimeType='application/vnd.google-apps.folder' and trashed=false "
         f"and name='{name}'")
    hits = svc.files().list(
        q=q, fields="files(id,name)", pageSize=10,
        supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute().get("files", [])
    if not hits:
        raise SystemExit(
            f"{BAD} 没找到名为 {name!r} 的文件夹。\n"
            f"     九成是第 4 步漏了：这个文件夹还没共享给服务账号的邮箱。\n"
            f"     （服务账号看不到你 Drive 里没共享给它的任何东西）")
    if len(hits) > 1:
        print(f"{DOT} 有 {len(hits)} 个同名文件夹，用第一个")
    print(f"{OK} 文件夹 {name!r} -> {hits[0]['id']}")
    return hits[0]["id"]


def list_files(svc, folder_id: str) -> dict:
    files = svc.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,size)", pageSize=300,
        supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute().get("files", [])
    if not files:
        raise SystemExit(f"{BAD} 文件夹里看不到文件。"
                         f"要么 ID 不对，要么没共享给服务账号。")
    print(f"{OK} 文件夹可访问，{len(files)} 个文件：")
    for f in sorted(files, key=lambda x: x["name"])[:15]:
        print(f"      {f['name']:34} {int(f.get('size') or 0) / 1024:9.1f} KB")
    if len(files) > 15:
        print(f"      …还有 {len(files) - 15} 个")
    return {f["name"]: f["id"] for f in files}


def download(svc, file_id: str, dest: Path) -> int:
    from googleapiclient.http import MediaIoBaseDownload
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = dl.next_chunk()
    dest.write_bytes(buf.getvalue())
    return dest.stat().st_size


def verify(svc, name_to_id: dict) -> bool:
    print("\n" + "=" * 60)
    print("下载三个文件并验证")
    print("=" * 60)
    tmp = Path(tempfile.mkdtemp(prefix="kdp_gdrive_"))

    missing = []
    for role, candidates in NEEDED.items():
        hit = next((n for n in candidates if n in name_to_id), None)
        if not hit:
            missing.append(f"{role}(找 {'/'.join(candidates)})")
            print(f"{BAD} {role}：没找到")
            continue
        kb = download(svc, name_to_id[hit], tmp / hit) / 1024
        print(f"{OK} {role}：{hit}  {kb:.1f} KB")
    if missing:
        print(f"\n{BAD} 缺文件：{'; '.join(missing)}\n（已下载的在 {tmp}）")
        return False

    print(f"\n{DOT} 文件内容是否有效")
    ms = next(tmp / n for n in NEEDED["正文"] if (tmp / n).exists())
    if ms.suffix == ".epub":
        try:
            with zipfile.ZipFile(ms) as z:
                assert "META-INF/container.xml" in z.namelist()
            print(f"{OK} EPUB 是合法 zip 且含 container.xml")
        except Exception as e:
            print(f"{BAD} EPUB 损坏：{e}")
            return False
    cov = next(tmp / n for n in NEEDED["封面"] if (tmp / n).exists())
    head = cov.read_bytes()[:4]
    if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff"):
        print(f"{OK} 封面是合法 PNG/JPEG")
    else:
        print(f"{BAD} 封面不是图片（开头 {head!r}）")
        return False

    # 决定性一步：直接喂给现有上架代码，一行都不改
    print(f"\n{DOT} 交给现有的 KDPMetadata.load_from_project_dir 解析")
    sys.path.insert(0, str(HERE))
    import kdp_uploader
    meta = kdp_uploader.KDPMetadata.load_from_project_dir(tmp)
    print(f"{OK if meta.title else BAD} 书名：{meta.title!r}")
    print(f"{OK if meta.keywords_7 else BAD} 关键词：{len(meta.keywords_7)} 个")
    print(f"{OK if meta.categories else BAD} 分类：{meta.categories}")
    print(f"{OK if meta.manuscript_path else BAD} 正文：{meta.manuscript_path}")
    print(f"{OK if meta.cover_path else BAD} 封面：{meta.cover_path}")

    paths = [p for p in (meta.manuscript_path, meta.cover_path) if p]
    abs_ok = bool(paths) and all(Path(p).is_absolute() and Path(p).exists() for p in paths)
    print(f"{OK if abs_ok else BAD} 都是真实存在的绝对路径（send_keys 的硬要求）")

    severe = [i for i in kdp_uploader.KDPPreflightChecker.check(meta)
              if i.startswith("【严重】")]
    print(f"{OK if not severe else BAD} 上架预检严重问题：{severe or '无'}")
    print(f"\n（临时文件在 {tmp}）")
    return bool(meta.title) and abs_ok and not severe


def main():
    ap = argparse.ArgumentParser(
        description="测试服务账号能否从 Google Drive 读取上架文件")
    ap.add_argument("--sa", default=str(HERE / "sa.json"), help="服务账号 JSON 路径")
    ap.add_argument("--folder-id", help="Drive 文件夹 ID")
    ap.add_argument("--folder-name", help="Drive 文件夹名")
    args = ap.parse_args()

    if not args.folder_id and not args.folder_name:
        raise SystemExit("必须给 --folder-id 或 --folder-name")

    print("=" * 60)
    print("Google Drive 读取测试（服务账号）")
    print("=" * 60)
    svc = get_service(Path(args.sa))
    folder_id = args.folder_id or find_folder(svc, args.folder_name)
    ok = verify(svc, list_files(svc, folder_id))

    print("\n" + "=" * 60)
    print("结论：" + ("链路通 —— Drive 上的文件下载后可直接喂给现有上架流程，"
                   "现有代码不用改" if ok else "链路不通，看上面标 ✗ 的项"))
    print("=" * 60)


if __name__ == "__main__":
    main()

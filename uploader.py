"""YouTube 上传接口（预留）。

目前不接凭证就不会跑，GUI 上点“上传”会提示缺什么。等你在 Google Cloud
建好项目后，按下面两步就能直接用：

1) pip install google-api-python-client google-auth-oauthlib
2) 到 Google Cloud Console → 启用 YouTube Data API v3 → 创建 OAuth 客户端 ID
   （类型选「桌面应用」）→ 下载 JSON，改名成 client_secret.json 放到本项目根目录。

首次上传会弹浏览器授权，token 存到 token.json，之后不再需要手动授权。
配额提醒：一次上传约消耗 1600 点，默认每天 10000 点，也就是一天 6 条左右。
未验证的应用上传的视频会被强制锁成私享，这也是这里默认 private 的原因。
"""
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CLIENT_SECRET = APP_DIR / "client_secret.json"
TOKEN_FILE = APP_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def ready() -> bool:
    """凭证和依赖是否都齐了。"""
    try:
        import googleapiclient  # noqa: F401
        import google_auth_oauthlib  # noqa: F401
    except ImportError:
        return False
    return CLIENT_SECRET.exists()


def upload(video_path, title: str, description: str = "", tags=None,
           privacy: str = "private", log=print) -> str:
    """上传并返回视频 URL。缺依赖或缺凭证时抛出带操作指引的异常。"""
    if not ready():
        raise RuntimeError(
            "上传功能还没开通。需要：\n"
            "  1. pip install google-api-python-client google-auth-oauthlib\n"
            f"  2. 把 OAuth 桌面客户端的 JSON 放到 {CLIENT_SECRET}\n"
            "详细步骤见 uploader.py 顶部说明。"
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            log("打开浏览器完成 Google 授权…")
            creds = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET), SCOPES).run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), "utf-8")

    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags or [],
            "categoryId": "22",          # People & Blogs
        },
        "status": {
            "privacyStatus": privacy,    # private / unlisted / public
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024,
                            resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body,
                                      media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log(f"上传中 {int(status.progress() * 100)}%")
    url = f"https://youtu.be/{response['id']}"
    log(f"上传完成：{url}")
    return url

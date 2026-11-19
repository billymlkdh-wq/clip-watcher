# /// script
# requires-python = ">=3.10"
# dependencies = ["google-api-python-client>=2", "google-auth-oauthlib>=1", "google-auth-httplib2"]
# ///
"""
Upload de clips pro YouTube (Shorts) via YouTube Data API v3.

  uv run youtube_upload.py auth                    # 1a vez: login Google no navegador
  uv run youtube_upload.py upload clips/x.mp4      # sobe 1 clip
  uv run youtube_upload.py pending clips/          # sobe todos os clips ainda nao postados
  uv run youtube_upload.py pending clips/ --dry-run

Credenciais ficam em ./.youtube/ (client_secret.json + token.json).
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CRED_DIR = HERE / ".youtube"
CLIENT_SECRET = CRED_DIR / "client_secret.json"
TOKEN = CRED_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

UPLOAD_LOCK = CRED_DIR / "upload.lock"
LAST_UPLOAD = CRED_DIR / "last_upload"
GAP_MINUTES = 10.0  # intervalo minimo entre uploads, somando TODOS os processos

DEFAULT_TITLE = "{hook} | {channel} #shorts"
DEFAULT_DESC = "Momento ao vivo de twitch.tv/{channel}\n\n#shorts #{channel} #twitch"


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} [youtube] {msg}", flush=True)


class QuotaExceeded(Exception):
    pass


# ---------------------------------------------------------------- auth
def get_credentials(interactive: bool = True):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_token(creds)
            return creds
        except Exception as e:
            log(f"token expirado e nao renovou ({e}), precisa logar de novo")
    if not interactive:
        raise RuntimeError("sem login valido no YouTube: rode `uv run youtube_upload.py auth`")
    if not CLIENT_SECRET.exists():
        sys.exit(
            f"Falta {CLIENT_SECRET}\n"
            "Crie um OAuth Client ID (tipo 'Desktop app') no Google Cloud Console "
            "e salve o JSON baixado nesse caminho. Passo a passo no README."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    save_token(creds)
    return creds


def save_token(creds) -> None:
    CRED_DIR.mkdir(exist_ok=True)
    TOKEN.write_text(creds.to_json())
    os.chmod(TOKEN, 0o600)


def service(interactive: bool = True):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=get_credentials(interactive), cache_discovery=False)


# ---------------------------------------------------------------- metadata
def clean(text: str) -> str:
    text = re.sub(r"[<>]", "", text)  # YouTube rejeita < >
    return re.sub(r"\s+", " ", text).strip()


def make_hook(meta: dict) -> str:
    tx = clean(meta.get("transcript", ""))
    if tx:
        words = tx.split()
        hook = " ".join(words[:9])
        return hook + ("..." if len(words) > 9 else "")
    chat = [clean(m) for m in meta.get("chat_sample", []) if len(clean(m)) >= 6 and not clean(m).replace(" ", "").isdigit()]
    if chat:
        return max(set(chat), key=chat.count)[:60]
    return "momento ao vivo"


def build_metadata(meta: dict, title_tpl: str, desc_tpl: str, tags: list[str]) -> dict:
    fields = {"channel": meta.get("channel", "twitch"), "hook": make_hook(meta)}
    title = clean(title_tpl.format(**fields))
    if len(title) > 100:
        extra = len(title) - 100 + 3
        fields["hook"] = fields["hook"][: max(10, len(fields["hook"]) - extra)].rstrip() + "..."
        title = clean(title_tpl.format(**fields))[:100]
    desc = desc_tpl.format(**fields).replace("<", "").replace(">", "")[:4900]
    return {"title": title, "description": desc, "tags": [t for t in tags if t][:30]}


# ---------------------------------------------------------------- upload
def upload(yt, video: Path, md: dict, privacy: str, category: str = "20") -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": md["title"], "description": md["description"],
            "tags": md["tags"], "categoryId": category,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True),
    )
    resp, attempt = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except HttpError as e:
            reason = ""
            try:
                reason = json.loads(e.content)["error"]["errors"][0]["reason"]
            except Exception:
                pass
            if reason in ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"):
                raise QuotaExceeded(reason) from e
            if e.resp.status not in (500, 502, 503, 504) or attempt >= 6:
                raise
            attempt += 1
            wait = min(60, 2 ** attempt) + random.random()
            log(f"erro {e.resp.status}, tentando de novo em {wait:.0f}s")
            time.sleep(wait)
        except (ConnectionError, TimeoutError, OSError) as e:
            if attempt >= 6:
                raise
            attempt += 1
            wait = min(60, 2 ** attempt) + random.random()
            log(f"rede caiu ({e}), tentando de novo em {wait:.0f}s")
            time.sleep(wait)
    return resp["id"]


def upload_clip(yt, video: Path, opts, dry_run: bool = False) -> str | None:
    meta_path = video.with_suffix(".json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if meta.get("youtube_id"):
        log(f"{video.name} ja postado: https://youtube.com/shorts/{meta['youtube_id']}")
        return meta["youtube_id"]
    tags = [meta.get("channel", "")] + [t.strip() for t in opts.yt_tags.split(",")]
    title_tpl, desc_tpl = opts.yt_title, opts.yt_desc
    if title_tpl == DEFAULT_TITLE or desc_tpl == DEFAULT_DESC:
        try:  # usa titulo/descricao do canal em campaigns.json (ex.: comando `pending`)
            from content_rewards import campaign_for
            camp = campaign_for(meta.get("channel", "")) or {}
        except Exception:
            camp = {}
        if title_tpl == DEFAULT_TITLE and camp.get("title"):
            title_tpl = camp["title"]
        if desc_tpl == DEFAULT_DESC and camp.get("desc"):
            desc_tpl = camp["desc"]
    md = build_metadata(meta, title_tpl, desc_tpl, tags)
    if dry_run:
        log(f"[dry-run] {video.name} -> {opts.yt_privacy}: {md['title']!r}")
        return None
    gap = getattr(opts, "yt_gap", GAP_MINUTES) * 60
    import fcntl
    CRED_DIR.mkdir(exist_ok=True)
    with open(UPLOAD_LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)  # um upload por vez entre todos os processos
        try:
            last = float(LAST_UPLOAD.read_text()) if LAST_UPLOAD.exists() else 0.0
        except ValueError:
            last = 0.0
        wait = last + gap - time.time()
        if wait > 0:
            log(f"{video.name}: esperando {wait / 60:.0f} min (intervalo de {gap / 60:.0f} min entre uploads)")
            time.sleep(wait)
        log(f"subindo {video.name} ({opts.yt_privacy}): {md['title']}")
        try:
            vid = upload(yt, video, md, opts.yt_privacy)
        finally:
            LAST_UPLOAD.write_text(str(time.time()))  # bloqueio do YouTube tambem conta
    meta.update({"youtube_id": vid, "youtube_url": f"https://youtube.com/shorts/{vid}",
                 "youtube_privacy": opts.yt_privacy, "youtube_title": md["title"]})
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    log(f"POSTADO https://youtube.com/shorts/{vid}")
    return vid


def add_cli_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--yt-privacy", choices=["public", "unlisted", "private"], default="public")
    ap.add_argument("--yt-title", default=DEFAULT_TITLE, help="template: {hook} {channel}")
    ap.add_argument("--yt-desc", default=DEFAULT_DESC, help="template: {hook} {channel}")
    ap.add_argument("--yt-tags", default="shorts,twitch,clips,live", help="tags separadas por virgula")
    ap.add_argument("--yt-gap", type=float, default=GAP_MINUTES, help="minutos minimos entre uploads (todos os processos juntos)")


class AutoUploader:
    """Fila em background: clip-watcher entrega o .mp4 pronto, isto posta."""

    def __init__(self, opts, dry_run: bool = False, on_posted=None):
        self.opts, self.dry = opts, dry_run
        self.on_posted = on_posted  # callback(Path do .json) depois de postar
        self.q: queue.Queue[Path | None] = queue.Queue()
        self.yt = None if dry_run else service(interactive=False)  # falha cedo se nao logado
        self.blocked_until = 0.0
        self.pending: list[Path] = []
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        log("upload automatico ligado" + (" (dry-run)" if dry_run else f" ({opts.yt_privacy})"))

    def submit(self, video: Path) -> None:
        self.q.put(video)

    def finish(self) -> None:
        self.q.put(None)
        self.thread.join()

    RETRY = 30 * 60  # bloqueado pelo YouTube: tenta de novo a cada 30 min

    def _loop(self) -> None:
        while True:
            try:
                item = self.q.get(timeout=60)
            except queue.Empty:
                item = "tick"
            if item is None:
                if self.pending:
                    log(f"{len(self.pending)} clip(s) ficaram pendentes: uv run youtube_upload.py pending clips/")
                return
            if item != "tick":
                self.pending.append(item)
            if time.time() < self.blocked_until:
                if item != "tick":
                    mins = int((self.blocked_until - time.time()) / 60) + 1
                    log(f"{item.name} na fila (YouTube bloqueado, nova tentativa em ~{mins} min)")
                continue
            while self.pending:
                video = self.pending[0]
                try:
                    vid = upload_clip(self.yt, video, self.opts, self.dry)
                    self.pending.pop(0)
                    if vid and self.on_posted:
                        self.on_posted(video.with_suffix(".json"))
                except QuotaExceeded as e:
                    self.blocked_until = time.time() + self.RETRY
                    what = ("limite diario de uploads do CANAL" if "upload" in str(e)
                            else "cota diaria da API")
                    log(f"{what} atingido ({e}); {len(self.pending)} clip(s) na fila, "
                        f"tento de novo em 30 min")
                    break
                except Exception as e:
                    self.pending.pop(0)
                    log(f"falhou {video.name}: {e}")


# ---------------------------------------------------------------- cli
def main() -> None:
    ap = argparse.ArgumentParser(description="Upload de clips pro YouTube Shorts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="login Google (abre navegador)")
    up = sub.add_parser("upload", help="sobe arquivos .mp4")
    up.add_argument("files", nargs="+", type=Path)
    pe = sub.add_parser("pending", help="sobe todos os clips da pasta ainda nao postados")
    pe.add_argument("folder", type=Path)
    pe.add_argument("--watch", type=float, default=0, help="fica rodando e tenta de novo a cada N minutos")
    for p in (up, pe):
        add_cli_args(p)
        p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.cmd == "auth":
        TOKEN.unlink(missing_ok=True)  # sempre login novo (permite trocar de canal)
        get_credentials(interactive=True)
        log(f"login ok, token salvo em {TOKEN}")
        return
    yt = None if a.dry_run else service()
    while True:
        files = a.files if a.cmd == "upload" else sorted((
            p for p in a.folder.glob("*.mp4") if not p.name.endswith("_raw.mp4")
            and p.with_suffix(".json").exists()  # so clips prontos (com .json)
            and not json.loads(p.with_suffix(".json").read_text()).get("youtube_id")
        ), key=lambda p: re.sub(r"^.*?_(\d{8}_\d{6})_.*$", r"\1", p.stem))  # ordem cronologica, mistura canais
        if a.cmd == "pending":
            log(f"{len(files)} clip(s) pendentes")
        for f in files:
            try:
                upload_clip(yt, f, a, a.dry_run)
            except QuotaExceeded as e:
                what = "limite diario de uploads do CANAL" if "upload" in str(e) else "cota diaria da API"
                log(f"{what} atingido ({e})")
                break
            except Exception as e:
                log(f"falhou {f.name}: {e}")
        if not getattr(a, "watch", 0):
            break
        log(f"proxima tentativa em {a.watch:.0f} min")
        time.sleep(a.watch * 60)


if __name__ == "__main__":
    main()

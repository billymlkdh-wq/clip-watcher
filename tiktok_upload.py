# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.45"]
# ///
"""
Posta os clips no TikTok pelo TikTok Studio (site), num Chrome com perfil proprio.

  uv run tiktok_upload.py login                     # abre Chrome normal, voce loga no TikTok, Cmd+Q
  uv run tiktok_upload.py post clips/x.mp4 --dry-run --show   # preenche e NAO publica (mostra navegador)
  uv run tiktok_upload.py pending clips/ --watch 30 # posta tudo que falta, 1 a cada 15 min

Intervalo minimo entre posts (todos os processos juntos): --gap (default 15 min).
Atencao: automacao do site pode ir contra os termos do TikTok (risco de bloqueio da conta).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".tiktok_profile"
SHOTS = HERE / ".tiktok_shots"
LOCK = HERE / ".tiktok_post.lock"
LAST = HERE / ".tiktok_last_post"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=webapp&lang=en"
GAP_MIN = 15.0


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} [tiktok ] {msg}", flush=True)


def login() -> None:
    PROFILE.mkdir(exist_ok=True)
    p = subprocess.Popen([CHROME, f"--user-data-dir={PROFILE}", "--no-first-run",
                          "--no-default-browser-check", "https://www.tiktok.com/login"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("faca login no TikTok no Chrome que abriu. Depois FECHE o Chrome (Cmd+Q).")
    p.wait()
    log(f"perfil salvo em {PROFILE}")


def caption_for(meta: dict) -> str:
    """Legenda: titulo do YouTube (ou gancho) trocando #shorts por tags do TikTok."""
    title = meta.get("youtube_title")
    if not title:
        from youtube_upload import DEFAULT_TITLE, build_metadata
        try:
            from content_rewards import campaign_for
            tpl = (campaign_for(meta.get("channel", "")) or {}).get("title", DEFAULT_TITLE)
        except Exception:
            tpl = DEFAULT_TITLE
        title = build_metadata(meta, tpl, "", [])["title"]
    title = re.sub(r"\s*#shorts\b", "", title, flags=re.I).strip()
    cap = f"{title} #fyp #gta6 #clips"
    seen, out = set(), []
    for w in cap.split():  # tira hashtag repetida (#GTA6 / #gta6)
        if w.startswith("#"):
            if w.lower() in seen:
                continue
            seen.add(w.lower())
        out.append(w)
    return " ".join(out)[:2000]


def _launch(p, headless: bool):
    return p.chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=headless, locale="en-US",
        viewport={"width": 1280, "height": 900},
        ignore_default_args=["--use-mock-keychain", "--enable-automation"],
    )


def _dismiss_popups(page) -> None:
    """Fecha pop-ups do TikTok Studio sem mudar configuracao da conta."""
    for _ in range(4):
        closed = False
        for m in page.locator(".TUXModal-overlay [role=dialog], [role=dialog]").all():
            try:
                if not m.is_visible():
                    continue
                txt = m.inner_text()
            except Exception:
                continue
            if re.search(r"content checks|verifica", txt, re.I):
                btn = m.get_by_role("button", name=re.compile(r"^(Cancel|Cancelar)$"))
            else:
                btn = m.get_by_role("button", name=re.compile(r"^(Got it|OK|Entendi|Close|Fechar)$"))
            if btn.count():
                btn.first.click()
                closed = True
                page.wait_for_timeout(800)
        tip = page.get_by_role("button", name=re.compile(r"^(Got it|Entendi)$"))
        if tip.count() and tip.first.is_visible():
            tip.first.click()
            closed = True
            page.wait_for_timeout(500)
        if not closed:
            return


def post(video: Path, caption: str, dry_run: bool = False, headless: bool = True) -> bool:
    from playwright.sync_api import sync_playwright

    if not PROFILE.exists():
        raise RuntimeError("sem login no TikTok: rode `uv run tiktok_upload.py login`")
    SHOTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with sync_playwright() as p:
        ctx = _launch(p, headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(UPLOAD_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            if "login" in page.url:
                raise RuntimeError("sessao do TikTok expirou: rode `uv run tiktok_upload.py login`")
            page.locator('input[type="file"]').first.set_input_files(str(video))
            # editor da legenda aparece depois que o arquivo e aceito
            editor = page.locator('div[contenteditable="true"]').first
            editor.wait_for(timeout=120_000)
            page.wait_for_timeout(3000)
            _dismiss_popups(page)
            editor.click()
            page.keyboard.press("Meta+A")
            page.keyboard.press("Backspace")
            page.keyboard.insert_text(caption)
            page.keyboard.press("Escape")  # fecha sugestao de hashtag
            # espera o upload terminar (botao Post habilitado)
            btn = page.locator('button[data-e2e="post_video_button"]')
            if not btn.count():
                btn = page.get_by_role("button", name=re.compile(r"^(Post|Publicar)$"))
            deadline = time.time() + 300
            while time.time() < deadline:
                if btn.count() and btn.first.is_enabled() and btn.first.get_attribute("aria-disabled") != "true":
                    break
                page.wait_for_timeout(2000)
            else:
                raise RuntimeError("upload nao terminou em 5 min")
            page.wait_for_timeout(1500)
            _dismiss_popups(page)
            page.screenshot(path=str(SHOTS / f"{stamp}_{'dryrun' if dry_run else 'antes'}.png"))
            if dry_run:
                log(f"[dry-run] {video.name}: formulario pronto, NAO publicado")
                return False
            btn.first.click()
            # pode aparecer um aviso "Continuar postando?" (checagem de conteudo)
            for _ in range(30):
                page.wait_for_timeout(2000)
                body = page.locator("body").inner_text()
                if re.search(r"(Your video has been uploaded|Video published|Manage your posts|has been posted)", body, re.I) \
                        or "tiktokstudio/content" in page.url:
                    page.screenshot(path=str(SHOTS / f"{stamp}_postado.png"))
                    log(f"POSTADO no TikTok: {video.name}")
                    return True
                post_now = page.get_by_role("button", name=re.compile(r"^(Post now|Postar agora)$"))
                if post_now.count():
                    post_now.first.click()
            page.screenshot(path=str(SHOTS / f"{stamp}_incerto.png"))
            raise RuntimeError("nao confirmei a publicacao (veja .tiktok_shots/)")
        finally:
            ctx.close()


def post_clip(video: Path, gap_min: float = GAP_MIN, dry_run: bool = False, headless: bool = True) -> bool:
    meta_path = video.with_suffix(".json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if meta.get("tiktok_posted"):
        return True
    cap = caption_for(meta)
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)  # um post por vez entre todos os processos
        if not dry_run:
            last = float(LAST.read_text()) if LAST.exists() else 0.0
            wait = last + gap_min * 60 - time.time()
            if wait > 0:
                log(f"{video.name}: esperando {wait / 60:.0f} min (intervalo de {gap_min:.0f} min)")
                time.sleep(wait)
        log(f"postando {video.name}: {cap}")
        try:
            ok = post(video, cap, dry_run, headless)
        finally:
            if not dry_run:
                LAST.write_text(str(time.time()))
    if ok:
        meta.update({"tiktok_posted": True, "tiktok_caption": cap,
                     "tiktok_at": datetime.now().isoformat(timespec="seconds")})
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return ok


def pending_files(folder: Path) -> list[Path]:
    out = []
    for p in folder.glob("*.mp4"):
        j = p.with_suffix(".json")
        if p.name.endswith("_raw.mp4") or not j.exists():
            continue
        if not json.loads(j.read_text()).get("tiktok_posted"):
            out.append(p)
    return sorted(out, key=lambda p: re.sub(r"^.*?_(\d{8}_\d{6})_.*$", r"\1", p.stem))


def main() -> None:
    ap = argparse.ArgumentParser(description="Posta clips no TikTok")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    po = sub.add_parser("post")
    po.add_argument("files", nargs="+", type=Path)
    pe = sub.add_parser("pending")
    pe.add_argument("folder", type=Path)
    pe.add_argument("--watch", type=float, default=0, help="fica rodando, checa a pasta a cada N min")
    for x in (po, pe):
        x.add_argument("--gap", type=float, default=GAP_MIN, help="minutos entre posts (default 15)")
        x.add_argument("--dry-run", action="store_true")
        x.add_argument("--show", action="store_true", help="mostra o navegador")
    a = ap.parse_args()
    if a.cmd == "login":
        return login()
    while True:
        files = a.files if a.cmd == "post" else pending_files(a.folder)
        if a.cmd == "pending":
            log(f"{len(files)} clip(s) pra postar no TikTok")
        fails = 0
        for f in files:
            try:
                post_clip(f, a.gap, a.dry_run, not a.show)
                fails = 0
            except Exception as e:
                log(f"falhou {f.name}: {e}")
                fails += 1
                if "login" in str(e) or fails >= 3:
                    log("parando esta rodada (muitas falhas seguidas)")
                    break
        if a.cmd == "post" or not a.watch:
            break
        log(f"proxima checagem em {a.watch:.0f} min")
        time.sleep(a.watch * 60)


if __name__ == "__main__":
    main()

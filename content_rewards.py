# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.45"]
# ///
"""
Envio automatico de clips pras campanhas da Content Rewards.

A Content Rewards nao tem API publica pra isso, entao o envio e feito
num Chrome com perfil proprio (./.content_rewards_profile) onde voce
faz login uma vez:

  uv run content_rewards.py login                       # abre o Chrome, voce loga
  uv run content_rewards.py submit clix https://youtube.com/shorts/XXXX
  uv run content_rewards.py submit clix https://youtube.com/shorts/XXXX --dry-run
  uv run content_rewards.py pending clips/              # envia Shorts postados e ainda nao enviados

Campanhas ficam em campaigns.json (canal da Twitch -> campanha).
Regra da plataforma: o clip precisa ser enviado ate 30 min depois de postado.
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".content_rewards_profile"
CAMPAIGNS = HERE / "campaigns.json"
SHOTS = HERE / ".content_rewards_shots"
MIN_GAP = 20  # segundos minimos entre envios, pra nao parecer disparo em massa


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} [rewards] {msg}", flush=True)


def load_campaigns() -> dict:
    if not CAMPAIGNS.exists():
        return {}
    return {k.lower(): v for k, v in json.loads(CAMPAIGNS.read_text()).items() if not k.startswith("_")}


def campaign_for(channel: str) -> dict | None:
    return load_campaigns().get(channel.lower())


def _launch(p, headless: bool):
    # sem --use-mock-keychain: os cookies do login (feito no Chrome normal) sao
    # cifrados com o Keychain real do macOS; o mock nao consegue ler.
    return p.chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=headless,
        viewport={"width": 1100, "height": 900}, locale="en-US",
        ignore_default_args=["--use-mock-keychain"],
    )


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def login() -> None:
    """Abre um Chrome NORMAL (sem automacao) no perfil dedicado.

    Google e outros logins bloqueiam navegador controlado por Playwright
    ("este navegador pode nao ser seguro"), entao o login e feito fora dele;
    depois o Playwright reaproveita os cookies do mesmo perfil.
    """
    import subprocess

    PROFILE.mkdir(exist_ok=True)
    proc = subprocess.Popen([
        CHROME, f"--user-data-dir={PROFILE}", "--no-first-run",
        "--no-default-browser-check", "https://contentrewards.com/c/settings",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("faca login no Chrome que abriu (qualquer metodo). Depois FECHE o Chrome (Cmd+Q).")
    proc.wait()
    log(f"perfil salvo em {PROFILE}")


def submit(campaign_url: str, clip_url: str, dry_run: bool = False, headless: bool = True) -> bool:
    """Abre a campanha, cola o link, marca 'li os requisitos' e envia."""
    from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

    if not PROFILE.exists():
        raise RuntimeError("sem login na Content Rewards: rode `uv run content_rewards.py login`")
    SHOTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with sync_playwright() as p:
        ctx = _launch(p, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(campaign_url, wait_until="domcontentloaded")
            if "sign" in page.url.lower() or page.get_by_role("link", name="Sign in").count():
                raise RuntimeError("sessao expirou: rode `uv run content_rewards.py login`")
            page.get_by_role("button", name="Submit clip").last.click(timeout=30_000)
            dialog = page.get_by_role("dialog", name="Submit clip")
            try:
                dialog.wait_for(timeout=15_000)
            except PWTimeout:
                if page.get_by_text("not linked", exact=False).count():
                    raise RuntimeError("conta social nao vinculada na Content Rewards")
                raise
            dialog.get_by_role("textbox", name="Clip link").fill(clip_url)
            box = dialog.get_by_role("checkbox")
            if not box.is_checked():
                # checkbox estilizada: o span por cima intercepta o clique, entao clica no texto
                dialog.get_by_text("read the requirements", exact=False).click()
                if not box.is_checked():
                    box.check(force=True)
            if not box.is_checked():
                raise RuntimeError("nao consegui marcar 'li os requisitos'")
            send = dialog.get_by_role("button", name="Submit clip")
            page.wait_for_timeout(1500)  # deixa o site validar o link
            if dry_run:
                page.screenshot(path=str(SHOTS / f"{stamp}_dryrun.png"))
                log(f"[dry-run] formulario preenchido, NAO enviado ({clip_url})")
                return False
            if send.is_disabled():
                page.screenshot(path=str(SHOTS / f"{stamp}_rejeitado.png"))
                msg = dialog.inner_text()[:300].replace("\n", " ")
                raise RuntimeError(f"site nao aceitou o link: {msg}")
            send.click()
            page.wait_for_timeout(4000)
            page.screenshot(path=str(SHOTS / f"{stamp}_enviado.png"))
            if dialog.count() and dialog.is_visible():
                txt = dialog.inner_text()[:300].replace("\n", " ")
                if "error" in txt.lower() or "invalid" in txt.lower() or "already" in txt.lower():
                    raise RuntimeError(f"envio recusado: {txt}")
            log(f"ENVIADO {clip_url} -> {campaign_url}")
            return True
        finally:
            ctx.close()


def submit_clip(video_json: Path, dry_run: bool = False) -> bool:
    """Le o .json do clip (precisa ter youtube_url) e envia pra campanha do canal."""
    meta = json.loads(video_json.read_text())
    if meta.get("rewards_submitted"):
        log(f"{video_json.stem} ja enviado")
        return True
    url = meta.get("youtube_url")
    camp = campaign_for(meta.get("channel", ""))
    if not url or not camp:
        log(f"{video_json.stem}: sem link do YouTube ou sem campanha pro canal {meta.get('channel')}")
        return False
    if meta.get("youtube_privacy", "public") != "public":
        log(f"{video_json.stem}: video nao e publico, campanha nao aceita. Pulando.")
        return False
    ok = submit(camp["url"], url, dry_run=dry_run)
    if ok:
        meta.update({"rewards_submitted": True, "rewards_campaign": camp["url"],
                     "rewards_at": datetime.now().isoformat(timespec="seconds")})
        video_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return ok


class AutoSubmitter:
    """Fila em background: recebe o .json do clip postado no YouTube e envia na campanha."""

    def __init__(self, dry_run: bool = False):
        self.dry = dry_run
        self.q: queue.Queue[Path | None] = queue.Queue()
        self.last = 0.0
        if not dry_run and not PROFILE.exists():
            raise RuntimeError("sem login na Content Rewards: rode `uv run content_rewards.py login`")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        log("envio automatico pra Content Rewards ligado" + (" (dry-run)" if dry_run else ""))

    def submit(self, video_json: Path) -> None:
        self.q.put(video_json)

    def finish(self) -> None:
        self.q.put(None)
        self.thread.join()

    def _loop(self) -> None:
        while (j := self.q.get()) is not None:
            wait = MIN_GAP - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            try:
                submit_clip(j, self.dry)
            except Exception as e:
                log(f"falhou {j.stem}: {e}")
            self.last = time.time()


def main() -> None:
    ap = argparse.ArgumentParser(description="Envio de clips pra Content Rewards")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="abre o Chrome pra voce logar")
    s = sub.add_parser("submit", help="envia 1 link pra campanha de um canal")
    s.add_argument("channel")
    s.add_argument("url")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--show", action="store_true", help="mostra o navegador")
    pe = sub.add_parser("pending", help="envia todos os clips postados e ainda nao enviados")
    pe.add_argument("folder", type=Path)
    pe.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.cmd == "login":
        login()
    elif a.cmd == "submit":
        camp = campaign_for(a.channel)
        if not camp:
            sys.exit(f"canal '{a.channel}' nao esta em {CAMPAIGNS.name}")
        submit(camp["url"], a.url, dry_run=a.dry_run, headless=not a.show)
    else:
        for j in sorted(a.folder.glob("*.json")):
            try:
                submit_clip(j, a.dry_run)
            except Exception as e:
                log(f"falhou {j.stem}: {e}")
            time.sleep(MIN_GAP)


if __name__ == "__main__":
    main()

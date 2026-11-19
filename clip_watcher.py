# /// script
# requires-python = ">=3.10"
# dependencies = ["streamlink>=7", "playwright>=1.45", "google-api-python-client>=2", "google-auth-oauthlib>=1", "google-auth-httplib2"]
# ///
"""
clip-watcher: clipa uma live da Twitch ENQUANTO ela ainda esta no ar.

  1. Streamlink grava a live desde o primeiro segundo (em segmentos de 10s).
  2. Le o chat da Twitch anonimamente (sem login) e conta msgs a cada 10s.
  3. Quando o chat explode, FFmpeg corta 30s em volta do momento.
  4. Whisper gera legenda palavra por palavra e queima no video vertical 9:16.

Uso:
  uv run clip_watcher.py <canal> [opcoes]
  uv run clip_watcher.py kaicenat --mult 2.5 --min-msgs 40
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import queue
import random
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

FFMPEG_FULL = "/opt/homebrew/opt/ffmpeg-full/bin"


def log(tag: str, msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} [{tag:<7}] {msg}", flush=True)


def find_bin(name: str) -> str:
    cand = Path(FFMPEG_FULL) / name
    if cand.exists():
        return str(cand)
    found = shutil.which(name)
    if not found:
        sys.exit(f"'{name}' nao encontrado no PATH")
    return found


# ---------------------------------------------------------------- recorder
@dataclass
class Segment:
    path: Path
    start: float  # tempo do stream (s)
    end: float
    wall_done: float  # relogio quando o segmento fechou
    gen: int = 0  # qual gravacao (reinicia se a conexao cair)


class Recorder:
    """streamlink -> ffmpeg segmenter (copy, sem reencode) em pedacos de ~10s."""

    def __init__(self, channel: str, workdir: Path, quality: str, keep_minutes: float, platform: str = "twitch"):
        self.channel = channel
        self.platform = platform
        self.dir = workdir / "segments"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.list_path = self.dir / "segments.csv"
        self.quality = quality
        self.keep_seconds = keep_minutes * 60
        self.segments: list[Segment] = []
        self.lock = threading.Lock()
        self.ended = threading.Event()
        self.list_done = threading.Event()
        self.procs: list[subprocess.Popen] = []
        self.gen = 0
        self.stopping = False

    def start(self) -> None:
        self._launch()
        threading.Thread(target=self._watch_procs, daemon=True).start()
        log("rec", f"gravando {self.platform}/{self.channel} ({self.quality}) desde o segundo 1")

    def _launch(self) -> None:
        g = self.gen
        list_path = self.dir / f"segments_{g}.csv"
        sl_cmd = [
            sys.executable, "-m", "streamlink",
            *(["--twitch-low-latency"] if self.platform == "twitch" else []),
            "--stream-segment-threads", "2",
            "--loglevel", "error",
            "--hls-segment-stream-data", "--stream-timeout", "30",
            "-O", f"https://{'kick.com' if self.platform == 'kick' else 'twitch.tv'}/{self.channel}", self.quality,
        ]
        ff_cmd = [
            find_bin("ffmpeg"), "-hide_banner", "-loglevel", "fatal",
            "-fflags", "+discardcorrupt+genpts", "-err_detect", "ignore_err",
            "-i", "pipe:0", "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", "10", "-reset_timestamps", "0",
            "-segment_list", str(list_path), "-segment_list_type", "csv",
            str(self.dir / f"seg_{g}_%06d.ts"),
        ]
        sl = subprocess.Popen(sl_cmd, stdout=subprocess.PIPE)
        ff = subprocess.Popen(ff_cmd, stdin=sl.stdout, stderr=subprocess.DEVNULL)
        sl.stdout.close()
        self.procs = [sl, ff]
        self.gen_done = threading.Event()
        threading.Thread(target=self._watch_list, args=(g, list_path, self.gen_done), daemon=True).start()

    def _watch_procs(self) -> None:
        """Se a gravacao cair (rede, dado corrompido) com a live no ar, reinicia sozinha."""
        fails = 0
        while True:
            sl, ff = self.procs
            ff.wait()
            if sl.poll() is None:
                sl.terminate()
                try:
                    sl.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    sl.kill()
            self.gen_done.set()
            if self.stopping:
                break
            # gravacao curta demais = live offline (streamlink nao achou stream)
            with self.lock:
                got = any(s.gen == self.gen for s in self.segments)
            fails = 0 if got else fails + 1
            if fails >= 3:
                break
            self.gen += 1
            log("rec", f"gravacao caiu, reconectando (tentativa {self.gen})")
            time.sleep(3)
            self._launch()
        self.ended.set()
        log("rec", "gravacao encerrada (live acabou ou caiu)")

    def _watch_list(self, g: int, list_path: Path, done: threading.Event) -> None:
        seen = 0
        offset = None
        while True:
            final = done.is_set()
            if list_path.exists():
                rows = list(csv.reader(list_path.open()))
                for row in rows[seen:]:
                    if len(row) < 3:
                        continue
                    a, b = float(row[1]), float(row[2])
                    if offset is None:
                        # continua a linha do tempo da gravacao anterior
                        with self.lock:
                            prev = self.segments[-1] if self.segments else None
                        offset = 0.0 if prev is None else prev.end + (time.time() - (b - a) - prev.wall_done) - a
                    seg = Segment(self.dir / row[0], a + offset, b + offset, time.time(), g)
                    with self.lock:
                        self.segments.append(seg)
                    if len(self.segments) == 1:
                        log("rec", "primeiro segmento gravado")
                seen = len(rows)
                self._prune()
            if final:
                if self.stopping or self.ended.is_set():
                    self.list_done.set()
                break
            time.sleep(0.5)

    def _prune(self) -> None:
        if self.keep_seconds <= 0:
            return
        with self.lock:
            if not self.segments:
                return
            limit = self.segments[-1].end - self.keep_seconds
            for s in self.segments:
                if s.end < limit and s.path.exists():
                    s.path.unlink()

    def stream_time_at(self, wall: float) -> float | None:
        """Converte horario do relogio -> tempo do stream gravado."""
        with self.lock:
            if not self.segments:
                return None
            last = self.segments[-1]
        return last.end + (wall - last.wall_done)

    def available_until(self) -> float:
        with self.lock:
            return self.segments[-1].end if self.segments else 0.0

    def segments_for(self, t0: float, t1: float) -> list[Segment]:
        with self.lock:
            segs = [s for s in self.segments if s.end > t0 and s.start < t1 and s.path.exists()]
        if segs:  # nao mistura gravacoes diferentes num mesmo corte
            g = segs[-1].gen
            segs = [s for s in segs if s.gen == g]
        return segs

    def stop(self) -> None:
        # para so o streamlink; ffmpeg recebe EOF e fecha o ultimo segmento
        self.stopping = True
        sl, ff = self.procs
        if sl.poll() is None:
            sl.terminate()
        for p in (sl, ff):
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        self.ended.wait(timeout=15)
        self.ended.set()
        self.list_done.set()


# ---------------------------------------------------------------- chat
class Chat:
    """IRC da Twitch como usuario anonimo justinfan (sem login, read-only)."""

    def __init__(self, channel: str):
        self.channel = channel.lower()
        self.lock = threading.Lock()
        self.count = 0
        self.msgs: list[str] = []
        self.stop_ev = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def take(self) -> tuple[int, list[str]]:
        with self.lock:
            c, m = self.count, self.msgs
            self.count, self.msgs = 0, []
        return c, m

    def _run(self) -> None:
        pat = re.compile(r"PRIVMSG #\S+ :(.*)")
        while not self.stop_ev.is_set():
            try:
                s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=300)
                nick = f"justinfan{random.randint(10000, 99999)}"
                s.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
                log("chat", f"conectado ao chat #{self.channel} (anonimo)")
                buf = b""
                while not self.stop_ev.is_set():
                    data = s.recv(8192)
                    if not data:
                        raise ConnectionError("socket fechado")
                    buf += data
                    *lines, buf = buf.split(b"\r\n")
                    for raw in lines:
                        line = raw.decode("utf-8", "ignore")
                        if line.startswith("PING"):
                            s.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                            continue
                        m = pat.search(line)
                        if m:
                            with self.lock:
                                self.count += 1
                                self.msgs.append(m.group(1))
            except Exception as e:  # reconecta
                if self.stop_ev.is_set():
                    return
                log("chat", f"reconectando ({e})")
                time.sleep(3)


# ---------------------------------------------------------------- audio (Kick)
class AudioMeter:
    """Sem chat: mede o volume de cada segmento gravado (pico de grito/risada/explosao).

    take() devolve um 'score' = amplitude RMS x1000 dos segmentos novos, na mesma
    interface do Chat, pra reaproveitar o Detector (dobrar o score ~ +6 dB).
    """

    def __init__(self, rec: "Recorder"):
        self.rec = rec
        self.seen = 0
        self.ffmpeg = find_bin("ffmpeg")
        self.stop_ev = threading.Event()
        self.last_db = None

    def start(self) -> None:
        log("audio", "detector por audio ligado (Kick: sem leitura de chat)")

    def take(self) -> tuple[int, list[str]] | tuple[None, list]:
        with self.rec.lock:
            new = self.rec.segments[self.seen:]
            self.seen = len(self.rec.segments)
        dbs = [d for d in (self._mean_db(s.path) for s in new) if d is not None]
        if not dbs:
            return None, []
        db = max(dbs)
        self.last_db = db
        return int(round(10 ** (db / 20) * 1000)), [f"{db:.1f} dB"]

    def _mean_db(self, path: Path) -> float | None:
        r = subprocess.run([self.ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-vn",
                            "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
        return float(m.group(1)) if m else None


# ---------------------------------------------------------------- detector
class Detector:
    def __init__(self, mult: float, min_msgs: int, warmup: int, window: int, cooldown: float):
        self.hist: collections.deque[int] = collections.deque(maxlen=window)
        self.mult, self.min_msgs, self.warmup, self.cooldown = mult, min_msgs, warmup, cooldown
        self.last_spike = 0.0

    def feed(self, count: int, now: float) -> tuple[bool, float]:
        base = statistics.mean(self.hist) if self.hist else 0.0
        ready = len(self.hist) >= self.warmup
        spike = (
            ready
            and count >= self.min_msgs
            and count >= base * self.mult
            and now - self.last_spike >= self.cooldown
        )
        if spike:
            self.last_spike = now
        else:
            # pico nao entra na baseline pra ela nao inflar
            self.hist.append(count)
        return spike, base


# ---------------------------------------------------------------- clipper
@dataclass
class ClipJob:
    idx: int
    t0: float
    t1: float
    count: int
    base: float
    msgs: list[str] = field(default_factory=list)


class Clipper:
    def __init__(self, rec: Recorder, outdir: Path, args, uploader=None):
        self.rec, self.out, self.args = rec, outdir, args
        self.uploader = uploader
        self.transcript = ""
        self.q: queue.Queue[ClipJob | None] = queue.Queue()
        self.ffmpeg = find_bin("ffmpeg")
        self.whisper = shutil.which("whisper") if not args.no_captions else None
        if not args.no_captions and not self.whisper:
            log("clip", "whisper nao encontrado, clips sairao sem legenda")
        self.last_t1 = 0.0
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def submit(self, job: ClipJob) -> None:
        self.last_t1 = max(self.last_t1, job.t1)
        self.q.put(job)

    def finish(self) -> None:
        self.q.put(None)
        self.thread.join()

    def _loop(self) -> None:
        while (job := self.q.get()) is not None:
            try:
                self._make(job)
            except Exception as e:
                log("clip", f"clip {job.idx} falhou: {e}")

    def _make(self, job: ClipJob) -> None:
        # espera a gravacao alcancar o fim do clip
        while self.rec.available_until() < job.t1 and not self.rec.ended.is_set():
            time.sleep(1)
        if self.rec.ended.is_set():
            self.rec.list_done.wait(timeout=5)
        segs = self.rec.segments_for(job.t0, job.t1)
        if not segs:
            log("clip", f"clip {job.idx}: sem segmentos gravados pra esse trecho")
            return
        t0 = max(job.t0, segs[0].start)
        dur = min(job.t1, segs[-1].end) - t0
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = self.out / f"{self.args.channel}_{stamp}_clip{job.idx:03d}"
        raw = base.with_name(base.name + "_raw.mp4")
        final = base.with_suffix(".mp4")

        t_start = time.time()
        concat = "concat:" + "|".join(str(s.path) for s in segs)
        run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{t0 - segs[0].start:.3f}", "-i", concat, "-t", f"{dur:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(raw),
        ])
        log("clip", f"clip {job.idx}: FFmpeg cortou {dur:.0f}s")

        ass = None
        self.transcript = ""
        if self.whisper:
            ass = self._captions(raw, base)
            if ass:
                log("clip", f"clip {job.idx}: Whisper gerou legendas")

        if self.args.format == "vertical":
            vf = (
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                "boxblur=25:2,eq=brightness=-0.12[bg];"
                "[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"
            )
        else:
            vf = "[0:v]null"
        if ass:
            vf += f",ass='{esc_filter_path(ass)}'"
        vf += "[v]"
        run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
            "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "copy",
            "-movflags", "+faststart", str(final),
        ])
        if not self.args.keep_raw:
            raw.unlink(missing_ok=True)
        meta = {
            "channel": self.args.channel, "clip": final.name,
            "stream_time": [round(t0, 1), round(t0 + dur, 1)],
            "chat_msgs_10s": job.count, "baseline": round(job.base, 1),
            "transcript": self.transcript,
            "chat_sample": job.msgs[:40], "render_seconds": round(time.time() - t_start, 1),
        }
        base.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        live = "" if self.rec.ended.is_set() else " (live ainda no ar)"
        log("clip", f"PRONTO {final}{live}")
        if self.uploader:
            self.uploader.submit(final)

    def _captions(self, raw: Path, base: Path) -> Path | None:
        wdir = base.parent / (base.name + "_whisper")
        wdir.mkdir(exist_ok=True)
        cmd = [
            self.whisper, str(raw), "--model", self.args.whisper_model,
            "--output_format", "json", "--output_dir", str(wdir),
            "--word_timestamps", "True", "--fp16", "False", "--verbose", "False",
        ]
        if self.args.language:
            cmd += ["--language", self.args.language]
        try:
            run(cmd)
            data = json.loads(next(wdir.glob("*.json")).read_text())
        except Exception as e:
            log("clip", f"whisper falhou: {e}")
            return None
        self.transcript = (data.get("text") or "").strip()
        words = [w for seg in data.get("segments", []) for w in seg.get("words", [])]
        shutil.rmtree(wdir, ignore_errors=True)
        if not words:
            return None
        ass = base.with_suffix(".ass")
        ass.write_text(build_ass(words, self.args.format == "vertical"), encoding="utf-8")
        return ass


def build_ass(words: list[dict], vertical: bool) -> str:
    w, h = (1080, 1920) if vertical else (1920, 1080)
    size, margin = (88, 560) if vertical else (72, 90)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,3,2,60,60,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def ts(t: float) -> str:
        t = max(t, 0)
        return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"

    # agrupa em blocos de ate 3 palavras; palavra falada fica amarela
    chunks, cur = [], []
    for wd in words:
        cur.append(wd)
        if len(cur) == 3 or wd["word"].strip().endswith((".", "?", "!", ",")):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    lines = []
    for chunk in chunks:
        toks = [c["word"].strip().upper().replace("{", "").replace("}", "") for c in chunk]
        for i, wd in enumerate(chunk):
            end = chunk[i + 1]["start"] if i + 1 < len(chunk) else wd["end"]
            text = " ".join(
                f"{{\\c&H00E5FF&}}{t}{{\\c&HFFFFFF&}}" if j == i else t for j, t in enumerate(toks)
            )
            lines.append(f"Dialogue: 0,{ts(wd['start'])},{ts(max(end, wd['start'] + 0.05))},Cap,,0,0,0,,{text}")
    return head + "\n".join(lines) + "\n"


def esc_filter_path(p: Path) -> str:
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def run(cmd: list[str]) -> None:
    # sessao propria: Ctrl+C no clip-watcher nao mata ffmpeg/whisper no meio do clip
    r = subprocess.run(cmd, capture_output=True, text=True, start_new_session=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-600:])


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="Clipa live da Twitch em tempo real pelo pico do chat")
    ap.add_argument("channel", help="canal da Twitch (ex: kaicenat)")
    ap.add_argument("--out", default="clips", help="pasta de saida (default: ./clips)")
    ap.add_argument("--quality", default="720p60,720p,best", help="qualidade streamlink (default 720p60; best = mais banda)")
    ap.add_argument("--bucket", type=float, default=10, help="janela de contagem do chat em s (default 10)")
    ap.add_argument("--mult", type=float, default=2.0, help="pico = msgs >= baseline x mult (default 2.0)")
    ap.add_argument("--min-msgs", type=int, default=30, help="minimo de msgs na janela pra contar como pico")
    ap.add_argument("--warmup", type=int, default=6, help="janelas antes de comecar a detectar (default 6 = 1min)")
    ap.add_argument("--baseline-window", type=int, default=30, help="janelas na media movel (default 30 = 5min)")
    ap.add_argument("--cooldown", type=float, default=45, help="s minimos entre clips")
    ap.add_argument("--pre", type=float, default=22, help="s antes do pico do chat (default 22)")
    ap.add_argument("--post", type=float, default=8, help="s depois do pico do chat (default 8)")
    ap.add_argument("--format", choices=["vertical", "original"], default="vertical")
    ap.add_argument("--whisper-model", default="small", help="tiny/base/small/medium (default small)")
    ap.add_argument("--language", default=None, help="forca idioma do whisper (pt, en...)")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--keep-raw", action="store_true", help="guarda o corte bruto 16:9 tambem")
    ap.add_argument("--keep-minutes", type=float, default=10, help="minutos de gravacao mantidos em disco (0 = tudo)")
    ap.add_argument("--max-clips", type=int, default=0, help="para depois de N clips (0 = sem limite)")
    ap.add_argument("--youtube", action="store_true", help="posta cada clip no YouTube assim que fica pronto")
    ap.add_argument("--rewards", action="store_true", help="envia cada Short postado pra campanha da Content Rewards do canal (campaigns.json)")
    ap.add_argument("--rewards-dry-run", action="store_true", help="preenche o formulario da campanha mas nao envia")
    ap.add_argument("--yt-dry-run", action="store_true", help="simula o upload (mostra titulo, nao posta)")
    from youtube_upload import add_cli_args
    add_cli_args(ap)
    ap.add_argument("--test-clip", type=float, default=0, help="forca um clip apos N segundos (testar setup)")
    ap.add_argument("--max-runtime", type=float, default=0, help="para depois de N segundos (0 = ate a live acabar)")
    args = ap.parse_args()
    raw = args.channel.lower().strip("/")
    args.platform = "kick" if "kick.com/" in raw else "twitch"
    args.channel = re.sub(r"^(https?://)?(www\.)?(twitch\.tv|kick\.com)/", "", raw)
    if args.platform == "kick":
        # sem chat: score de audio; dobrar volume ~ +6 dB, entao 1.8x ~ +5 dB
        if "--mult" not in sys.argv:
            args.mult = 1.8
        if "--min-msgs" not in sys.argv:
            args.min_msgs = 0

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = out / f".work_{args.channel}"
    rec = Recorder(args.channel, work, args.quality, args.keep_minutes, args.platform)
    chat = AudioMeter(rec) if args.platform == "kick" else Chat(args.channel)
    det = Detector(args.mult, args.min_msgs, args.warmup, args.baseline_window, args.cooldown)
    uploader = submitter = None
    from content_rewards import campaign_for
    camp = campaign_for(args.channel)
    if camp and "--yt-title" not in sys.argv:
        args.yt_title = camp.get("title", args.yt_title)
        if "--yt-desc" not in sys.argv and camp.get("desc"):
            args.yt_desc = camp["desc"]
        if not args.language and camp.get("language"):
            args.language = camp["language"]
        log("main", f"campanha: {camp['name']} | titulo: {args.yt_title}")
    if args.rewards or args.rewards_dry_run:
        if not camp:
            sys.exit(f"canal {args.channel} nao tem campanha em campaigns.json")
        if not (args.youtube or args.yt_dry_run):
            sys.exit("--rewards precisa de --youtube (a campanha recebe o link do Short)")
        if args.yt_privacy != "public" and not args.rewards_dry_run:
            sys.exit("--rewards precisa de --yt-privacy public (campanha so aceita video publico)")
        from content_rewards import AutoSubmitter
        try:
            submitter = AutoSubmitter(dry_run=args.rewards_dry_run)
        except Exception as e:
            sys.exit(f"Content Rewards: {e}")
    if args.youtube or args.yt_dry_run:
        from youtube_upload import AutoUploader
        try:
            uploader = AutoUploader(args, dry_run=args.yt_dry_run,
                                    on_posted=submitter.submit if submitter else None)
        except Exception as e:
            sys.exit(f"YouTube: {e}")
    clipper = Clipper(rec, out, args, uploader)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    rec.start()
    chat.start()
    t_begin = time.time()
    n = 0
    next_tick = time.time() + args.bucket
    while not stop.is_set() and not rec.ended.is_set():
        if stop.wait(max(0, next_tick - time.time())):
            break
        now = next_tick
        next_tick += args.bucket
        count, msgs = chat.take()
        if count is None:
            continue  # audio: nenhum segmento novo ainda
        spike, base = det.feed(count, now)
        if args.test_clip and n == 0 and now - t_begin >= args.test_clip:
            spike = True  # clip de teste
        bar = "#" * min(40, count // 3)
        if args.platform == "kick":
            bar = "#" * min(40, count // 4)
            log("audio", f"{msgs[0]:>9}  score {count:4d}  base {base:6.1f}  {bar}{'  << PICO' if spike else ''}")
        else:
            log("chat", f"{count:4d} msgs/{args.bucket:.0f}s  base {base:6.1f}  {bar}{'  << PICO' if spike else ''}")
        if spike:
            st = rec.stream_time_at(now)
            if st is None:
                log("clip", "pico detectado mas a gravacao ainda nao comecou")
            else:
                n += 1
                top = collections.Counter(m.strip().lower() for m in msgs).most_common(3)
                what = "AUDIO ESTOUROU" if args.platform == "kick" else "CHAT EXPLODIU"
                log("clip", f"{what} ({count} vs {base:.0f}) -> clip {n}: {', '.join(t for t, _ in top)}")
                clipper.submit(ClipJob(n, max(0, st - args.pre), st + args.post, count, base, msgs))
        if args.max_clips and n >= args.max_clips:
            log("main", f"limite de {args.max_clips} clips atingido")
            break
        if args.max_runtime and time.time() - t_begin >= args.max_runtime:
            log("main", "tempo maximo atingido")
            break

    chat.stop_ev.set()
    # deixa a gravacao cobrir o fim do ultimo clip antes de parar
    if n and not rec.ended.is_set():
        log("main", "esperando a gravacao cobrir o ultimo clip...")
        deadline = time.time() + args.post + 30
        while rec.available_until() < clipper.last_t1 and time.time() < deadline and not rec.ended.is_set():
            time.sleep(1)
    rec.stop()
    clipper.finish()
    if uploader:
        uploader.finish()
    if submitter:
        submitter.finish()
    if args.keep_minutes > 0:
        shutil.rmtree(work, ignore_errors=True)
    log("main", f"fim. {n} clip(s) em {out}")


if __name__ == "__main__":
    main()

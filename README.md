# clip-watcher

Clipa live da Twitch **enquanto ela ainda está no ar**, pelo pico do chat.

```
Streamlink grava desde o 1º segundo (segmentos de 10s)
      │
Chat da Twitch lido anônimo (sem login), msgs contadas a cada 10s
      │
Chat explode (msgs ≥ média dos últimos 5min × 2.5)
      │
FFmpeg corta 30s (22s antes do pico, 8s depois) → vertical 9:16 com fundo desfocado
      │
Whisper transcreve → legenda palavra por palavra queimada no vídeo
      │
clips/<canal>_<data>_clipNNN.mp4  (+ .json com amostra do chat)
```

Tudo grátis e local. Requisitos (já instalados nesta máquina): `uv`, `ffmpeg-full` (libass), `whisper`.

## Uso

```bash
cd ~/clip-watcher
uv run clip_watcher.py <canal>
```

Ex.: `uv run clip_watcher.py caseoh_` — roda até a live acabar ou Ctrl+C. Clips aparecem em `./clips` assim que ficam prontos (~1 min depois do pico).

Testar setup sem esperar pico real (força 1 clip aos 50s):

```bash
uv run clip_watcher.py caseoh_ --test-clip 50 --max-clips 1
```

## Ajustes principais

| Flag | Default | O que faz |
|---|---|---|
| `--mult` | 2.5 | quanto acima da média o chat precisa subir |
| `--min-msgs` | 30 | mínimo de msgs em 10s pra contar como pico (subir em live gigante) |
| `--cooldown` | 45 | segundos mínimos entre clips |
| `--pre` / `--post` | 22 / 8 | segundos antes/depois do pico (chat reage com atraso) |
| `--format` | vertical | `original` mantém 16:9 |
| `--whisper-model` | small | `base` = mais rápido, legenda pior |
| `--language` | auto | força idioma (`pt`, `en`) |
| `--quality` | best | `720p60`, `480p`... |
| `--keep-minutes` | 10 | minutos de gravação mantidos em disco (0 = guarda tudo) |
| `--keep-raw` | off | guarda também o corte bruto 16:9 |
| `--no-captions` | off | pula Whisper (clip sai mais rápido) |

Live muito grande (100k+ viewers, tipo lançamento de GTA 6): chat já é alto o tempo todo, use `--mult 1.8 --min-msgs 300`.

## Postar automático no YouTube Shorts

Com `--youtube`, cada clip vai pro YouTube assim que fica pronto (fila em background, não atrasa os próximos cortes). Título sai da fala do clip: `"<primeiras palavras da fala>... | <canal> #shorts"`. Resultado (link) fica gravado no `.json` do clip.

```bash
uv run clip_watcher.py caseoh_ --youtube
```

### Setup (uma vez só, ~10 min)

1. https://console.cloud.google.com → criar projeto (ex.: `clip-watcher`).
2. **APIs e serviços → Biblioteca** → ativar **YouTube Data API v3**.
3. **Tela de consentimento OAuth** → tipo *Externo* → preencher nome e e-mail → em *Usuários de teste* adicionar o e-mail da conta do canal. Depois clicar **Publicar app** (em modo "Teste" o login expira a cada 7 dias).
4. **Credenciais → Criar credenciais → ID do cliente OAuth → App para computador** → baixar JSON.
5. Salvar como `~/clip-watcher/.youtube/client_secret.json`.
6. Logar (abre navegador, escolher o canal):
   ```bash
   uv run youtube_upload.py auth
   ```

### Limites do YouTube (importante)

(Conferido na documentação oficial em 18/11/2026.)

- **Upload público direto funciona.** A doc atual do `videos.insert` diz que vídeos de projetos de API não verificados *não* ficam mais travados em privado. `--yt-privacy public` (padrão) posta público.
- **Cota: 100 uploads por dia.** Upload tem cota própria ("Video Uploads"): 100 chamadas/dia, 1 unidade cada, separada das 10.000 unidades das outras funções. Estourou, os clips seguintes ficam pendentes; no outro dia:
  ```bash
  uv run youtube_upload.py pending clips/
  ```
- **Mais de 100/dia:** pedir no formulário de auditoria e extensão de cota: https://support.google.com/youtube/contact/yt_api_form (opção "Quota Extension Request"). Negado: https://support.google.com/youtube/contact/yt_api_appeals
- **Limite do canal:** o YouTube também limita uploads por canal por dia. Verifique o telefone em https://www.youtube.com/verify.
- **Direitos autorais:** clip de stream de outra pessoa pode tomar Content ID / strike. Gameplay de lançamento (GTA 6) pode ter trilha licenciada.

### Flags

| Flag | Default | O que faz |
|---|---|---|
| `--youtube` | off | liga o upload automático |
| `--yt-dry-run` | off | simula: mostra o título, não posta |
| `--yt-privacy` | public | `public`, `unlisted`, `private` |
| `--yt-title` | `{hook} \| {channel} #shorts` | template do título |
| `--yt-desc` | ver código | template da descrição |
| `--yt-tags` | shorts,twitch,clips,live | tags |

Comandos avulsos: `uv run youtube_upload.py upload clips/arquivo.mp4` · `uv run youtube_upload.py pending clips/ --dry-run`

## Kick

```bash
uv run clip_watcher.py https://kick.com/<canal> --youtube
```

Na Kick o pico é detectado pelo **áudio da live** (volume de cada trecho de 10s contra a média dos últimos 5 min), não pelo chat. Padrão: `--mult 1.8` (~+5 dB). Muitos clips falsos → suba `--mult` (2.2 ≈ +7 dB) ou `--cooldown`.

## TikTok (automação do TikTok Studio)

```bash
uv run tiktok_upload.py login                       # 1x: Chrome separado, loga no TikTok, Cmd+Q
caffeinate -i uv run tiktok_upload.py pending clips/ --watch 30 --gap 15
```

Posta todo clip pronto em `clips/` que ainda não foi pro TikTok, 1 a cada 15 min (vale somando todos os processos). Legenda = título do YouTube com `#fyp #gta6 #clips`. Prints em `.tiktok_shots/`. Teste sem publicar: `uv run tiktok_upload.py post clips/x.mp4 --dry-run --show`.

⚠️ Automação do site vai contra os termos do TikTok: risco de restrição da conta.

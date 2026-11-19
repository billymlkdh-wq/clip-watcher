---
name: clip-watcher
description: Opera o sistema de cortes ao vivo do Rafael (~/clip-watcher) que assiste lives da Twitch/Kick, corta os picos em Shorts 9:16 legendados e posta no YouTube (canal Five Star Clips) e no TikTok (@wantedlevelfiveclips), com envio opcional pra campanhas pagas da Content Rewards. Use quando o Rafael mandar um link de live (twitch.tv/... ou kick.com/...), pedir pra "rodar os cortes", "clipar essa live", ver a fila de upload, o status dos posts, cota/limite do YouTube, postagem no TikTok, campanhas de clipping, ou consertar erro do clip-watcher.
---

# clip-watcher

Sistema em `~/clip-watcher` (repo: github.com/billymlkdh-wq/clip-watcher). Tudo roda com `uv run` (dependências inline nos scripts). Rodar processos longos em **abas do Terminal do app** (`run_in_terminal`), nunca em Bash foreground.

## Peças

| Arquivo | O que faz |
|---|---|
| `clip_watcher.py` | Grava a live (Streamlink → segmentos de 10s), detecta pico, corta 30s (FFmpeg), legenda (Whisper), gera Short 1080x1920 |
| `youtube_upload.py` | Upload YouTube Data API. `auth`, `upload`, `pending clips/ --watch 30` |
| `tiktok_upload.py` | Posta no TikTok via TikTok Studio (Playwright, Chrome dedicado). `login`, `post`, `pending clips/ --watch 30 --gap 15` |
| `content_rewards.py` | Envia o link do Short pra campanha da Content Rewards. `login`, `submit`, `pending` |
| `campaigns.json` | Por canal: título/descrição/idioma do Short + URL da campanha (vazio = sem campanha paga) |
| `branding/` | Arte do canal (art.html → avatar/banner) |

Segredos e sessões ficam fora do git: `.youtube/` (OAuth), `.tiktok_profile/`, `.content_rewards_profile/`.

## Fluxo padrão quando o Rafael manda uma live

1. Ver se está ao vivo e o jogo:
   `uvx streamlink --json https://twitch.tv/<canal>` → `metadata.category`, `title`.
2. Garantir preset em `campaigns.json` pro canal (title com nome do streamer + hashtags, `language` pt/en). Canal em inglês → `"language": "en"`.
3. Procurar campanha paga ativa: Content Rewards → https://contentrewards.com/c/discover/clipping (clicar "Load more" até o fim e filtrar pelo nome). Ler regras da Reach em reachclipping.com/<Pagina>. Se tiver, preencher `url` no campaigns.json e usar `--rewards`.
4. Rodar numa aba própria:
   - Twitch: `uv run clip_watcher.py <canal> --youtube`
   - Kick: `uv run clip_watcher.py https://kick.com/<canal> --youtube --mult 2.5 --cooldown 120`
5. Conferir a aba depois de ~1 min (chat contando, "primeiro segmento gravado").

Filas que devem ficar rodando (cada uma numa aba, com `caffeinate -i`):
- `caffeinate -i uv run youtube_upload.py pending clips/ --watch 30`
- `caffeinate -i uv run tiktok_upload.py pending clips/ --watch 30 --gap 15`

## Detecção

- **Twitch**: chat anônimo (justinfan, sem login). Pico = msgs/10s ≥ média 5 min × `--mult` (padrão **2.0**) e ≥ `--min-msgs` (30). Chat lento (sub-only) → não subir `--min-msgs`.
- **Kick**: chat da Kick é API interna não oficial — o classificador de segurança bloqueia acessar; **não contornar**. Usa **áudio** (volume de cada segmento). Áudio de GTA é barulhento → `--mult 2.5 --cooldown 120`.
- Clip = 22s antes + 8s depois do pico. Whisper padrão `small` (melhor legenda; com 3 lives a CPU fica no limite, ~2-3 min por clip → `--whisper-model base` se a fila atrasar).
- Qualidade padrão `720p60` (3 lives em 1080p saturam a rede e corrompem a gravação). Gravação reconecta sozinha se cair.

## Limites e regras aprendidas

- **YouTube API**: 100 uploads/dia (bucket próprio, 1 unidade cada). Projeto não auditado **não** trava mais em privado (doc oficial 2026).
- **Limite do canal ≠ cota da API**: erro `uploadLimitExceeded` = canal sem "Advanced features". Studio → Configurações → Canal → Feature eligibility → Advanced features (verificação por vídeo/ID, feita pelo Rafael).
- Intervalo global entre uploads: YouTube **10 min** (`--yt-gap`), TikTok **15 min** (`--gap`). Trava por arquivo vale entre todos os processos.
- **OAuth YouTube em modo Teste** → token expira em 7 dias: `uv run youtube_upload.py auth` (isso apaga o token e pede login de novo — escolher o canal Five Star Clips).
- **Content Rewards**: clip tem que ser enviado até **30 min** depois de postado, da conta social vinculada. Reach exige público ≥50% anglófono e nicho compatível. Vincular conta: Settings → Accounts → Connect (popup do Google às vezes falha no painel do app → usar Chrome).
- **TikTok**: automação do site vai contra os termos (Rafael aceitou o risco). Pop-up "Turn on automatic content checks?" é fechado com Cancel (não mudar config). Username só muda a cada 30 dias.
- Logins dos Chromes dedicados (`content_rewards.py login`, `tiktok_upload.py login`) abrem Chrome **normal** (Google bloqueia navegador automatizado). Cookies só gravam ao fechar com Cmd+Q; se o usuário não fechar, `pkill -TERM -f "user-data-dir=.../.tiktok_profile"`. Playwright usa `ignore_default_args=["--use-mock-keychain"]` pra ler esses cookies.
- Hook RTK reescreve saída de `curl` JSON → usar Python pra ler APIs.

## Diagnóstico rápido

| Sintoma | Causa provável | Ação |
|---|---|---|
| "sem segmentos gravados pra esse trecho" | gravação caiu | ver aba; versão atual reconecta sozinha |
| picos visíveis mas sem clip | `--mult` alto pro chat atual | `--mult 1.8–2.0` |
| muitos clips na Kick | áudio barulhento | `--mult 2.5`+, `--cooldown 120`+ |
| `uploadLimitExceeded` | limite do canal | Advanced features; fila tenta a cada 30 min |
| título "1 \| ..." | Whisper falhou e caiu em msg curta do chat | já filtrado; renomear no Studio |
| TikTok "sessao expirou" | cookie venceu | `uv run tiktok_upload.py login` + Cmd+Q |

## Contagem rápida da fila

```bash
cd ~/clip-watcher && python3 -c "
import json,glob
a=[json.load(open(f)) for f in glob.glob('clips/*.json')]
print('YT', sum(1 for d in a if d.get('youtube_id')), '/', len(a), '| TikTok', sum(1 for d in a if d.get('tiktok_posted')))"
```

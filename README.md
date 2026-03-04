# mayble-news-digest (MBL)

Digest automático de notícias de **Valorant** (VLR.gg + THESPIKE.GG) enviado para um **Discord Webhook**.  
Pensado para rodar no **GitHub Actions** (recomendado), com configuração por variáveis de ambiente e **segredo no GitHub Secrets**.

---

## O que ele faz

- Busca notícias:
  - **VLR.gg** via RSS
  - **THESPIKE.GG** via página de notícias (scraping leve)
- Monta uma mensagem no Discord com:
  - **Header** (opcional) com banner da MBL
  - **Embeds** das notícias com título, resumo, fonte e link
  - Imagem da notícia via **og:image** quando disponível
- Evita repost via `posted_cache.json` (anti-duplicado)
- (Opcional) Tradução EN → PT via LibreTranslate, com cache em `translate_cache.json`

---

## Segurança (importante)

✅ **NUNCA** coloque seu webhook em arquivo versionado (README, .yml com valor “hardcoded”, .py, etc.).  
✅ Use **GitHub Actions Secrets**: `DISCORD_WEBHOOK_URL`.

Se você suspeitar que o webhook vazou:
1) Revogue/regenere o webhook no Discord  
2) Atualize o Secret no GitHub

---

## Setup recomendado (GitHub Actions)

### 1) Criar o Secret do webhook
No GitHub:
- Repo → **Settings** → **Secrets and variables** → **Actions**
- **New repository secret**
  - Name: `DISCORD_WEBHOOK_URL`
  - Value: (seu webhook)

### 2) Adicionar o workflow
Coloque o workflow em:
- `.github/workflows/mayble-news.yml`

E rode manualmente em:
- **Actions** → *Mayble News Digest (MBL)* → **Run workflow**

> Observação: o workflow commita/atualiza `posted_cache.json` e `translate_cache.json` para manter o anti-duplicado entre execuções.

---

## Rodar localmente (opcional)

### Linux/macOS
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python bot_noticias_digest_ptbr.py

# -*- coding: utf-8 -*-
"""
Mayble eSports (MBL) - News Digest
VLR.gg (RSS) + THESPIKE.GG (listing) -> Discord Webhook (GitHub Actions)

Principais objetivos:
- 1 execução = 1 mensagem (com header + até N notícias)
- Embeds com branding MBL (icon + thumbnail + footer)
- Imagem da notícia via og:image quando disponível
- Anti-duplicado por posted_cache.json (persistido via commit no workflow)
- Cache determinístico + escrita atômica (menos conflitos no Git)
- Bootstrap anti-spam: se cache estiver vazio, só posta itens recentes (configurável)
- Segurança: não loga webhook e suporta DRY_RUN
"""

from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

# -------------------------------------------------
# Helpers ENV / Time
# -------------------------------------------------
ROOT = Path(__file__).resolve().parent


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name, str(default)) or "").strip()
    try:
        return int(raw)
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name, "1" if default else "0") or "").strip().lower()
    return v not in {"0", "false", "no", "off"}


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _iso_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _truncate(text: str, limit: int) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 1)].rstrip() + "…"


def clean_text(text: str) -> str:
    t = (text or "").strip()
    t = _html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def strip_html(html_text: str) -> str:
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "lxml")
    return clean_text(soup.get_text(" "))


def normalize_url(url: str) -> str:
    """
    Normaliza minimamente para dedupe (remove fragment e espaços).
    Não remove query por padrão (alguns sites usam query como identificador).
    """
    u = (url or "").strip()
    if not u:
        return ""
    parts = urlsplit(u)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


# -------------------------------------------------
# Config
# -------------------------------------------------
@dataclass(frozen=True)
class Config:
    # Webhook
    webhook_url: str
    dry_run: bool

    # Brand / Visual (MBL)
    embed_color: int
    brand_name: str
    brand_url: str
    footer_text: str
    brand_icon_url: str
    brand_thumbnail_url: str
    banner_url: str
    use_brand_thumbnail: bool
    post_header_embed: bool

    # Posting limits (news items)
    max_posts_per_run: int
    max_vlr_per_run: int
    max_thespike_per_run: int

    # Bootstrap anti-spam
    bootstrap_hours: int  # se cache vazio, só posta itens publicados nos últimos X horas (0 desativa)

    # HTTP
    http_timeout_sec: int
    http_retries: int
    http_backoff_base_sec: float

    # Cache
    cache_file: Path
    cache_keep_days: int
    cache_max_items: int  # 0 = sem limite

    # Sources
    enable_vlr: bool
    enable_thespike: bool
    thespike_locale: str  # br|en

    # Translation
    translate_pt: bool
    translate_only_vlr: bool
    libretranslate_url: str
    libretranslate_api_key: str
    translate_cache_file: Path
    translate_cache_max_items: int  # 0 = sem limite

    # UI details
    channel_label: str
    tagline: str

    @staticmethod
    def load() -> "Config":
        webhook = _env_str("DISCORD_WEBHOOK_URL") or _env_str("WEBHOOK_URL")

        # Defaults (MBL assets)
        default_logo = (
            "https://cdn.discordapp.com/attachments/1440760869964484738/1478779402614608024/"
            "logo_4k-Photoroom.png"
        )
        default_banner = (
            "https://cdn.discordapp.com/attachments/1440760869964484738/1478854923075453155/"
            "ChatGPT_Image_4_de_mar._de_2026_16_57_54.png"
        )

        # Prefer new envs; keep legacy fallback if someone ainda usa
        legacy_logo = _env_str("VORAX_LOGO_URL")  # fallback apenas (não usar no branding do texto)

        brand_icon = _env_str("BRAND_ICON_URL") or _env_str("MBL_ICON_URL") or legacy_logo or default_logo
        brand_thumb = _env_str("BRAND_THUMBNAIL_URL") or _env_str("MBL_THUMBNAIL_URL") or brand_icon
        banner = _env_str("BRAND_BANNER_URL") or _env_str("MBL_BANNER_URL") or default_banner

        return Config(
            webhook_url=webhook,
            dry_run=_env_bool("DRY_RUN", False),
            embed_color=_env_int("EMBED_COLOR", 16742912),
            brand_name=_env_str("BRAND_NAME", "Mayble eSports (MBL)"),
            brand_url=_env_str("BRAND_URL", "https://mayble.com.br"),
            footer_text=_env_str("FOOTER_TEXT", "Mayble eSports • @mayblegg"),
            brand_icon_url=brand_icon,
            brand_thumbnail_url=brand_thumb,
            banner_url=banner,
            use_brand_thumbnail=_env_bool("USE_BRAND_THUMBNAIL", True),
            post_header_embed=_env_bool("POST_HEADER_EMBED", True),
            max_posts_per_run=_env_int("MAX_POSTS_PER_RUN", 4),
            max_vlr_per_run=_env_int("MAX_VLR_PER_RUN", 2),
            max_thespike_per_run=_env_int("MAX_THESPIKE_PER_RUN", 2),
            bootstrap_hours=_env_int("BOOTSTRAP_HOURS", 72),
            http_timeout_sec=_env_int("HTTP_TIMEOUT_SEC", 25),
            http_retries=_env_int("HTTP_RETRIES", 2),
            http_backoff_base_sec=float(_env_int("HTTP_BACKOFF_BASE_MS", 600)) / 1000.0,
            cache_file=Path(_env_str("CACHE_FILE", str(ROOT / "posted_cache.json"))),
            cache_keep_days=_env_int("CACHE_KEEP_DAYS", 30),
            cache_max_items=_env_int("CACHE_MAX_ITEMS", 2000),
            enable_vlr=_env_bool("ENABLE_VLR", True),
            enable_thespike=_env_bool("ENABLE_THESPIKE", True),
            thespike_locale=_env_str("THESPIKE_LOCALE", "br").lower(),
            translate_pt=_env_bool("TRANSLATE_PT", True),
            translate_only_vlr=_env_bool("TRANSLATE_ONLY_VLR", True),
            libretranslate_url=_env_str("LIBRETRANSLATE_URL", "https://libretranslate.de/translate"),
            libretranslate_api_key=_env_str("LIBRETRANSLATE_API_KEY", ""),
            translate_cache_file=Path(_env_str("TRANSLATE_CACHE_FILE", str(ROOT / "translate_cache.json"))),
            translate_cache_max_items=_env_int("TRANSLATE_CACHE_MAX_ITEMS", 2000),
            channel_label=_env_str("CHANNEL_LABEL", "#noticias-da-comunidade"),
            tagline=_env_str("TAGLINE", "Curadoria • Disciplina • Competitividade"),
        )


# -------------------------------------------------
# Logging
# -------------------------------------------------
LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(levelname)s: %(message)s")
log = logging.getLogger("mbl-news-digest")


# -------------------------------------------------
# HTTP
# -------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "Mozilla/5.0 MaybleNewsDigest/7.0",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
)


def _request(
    cfg: Config,
    method: str,
    url: str,
    *,
    json_body: Any | None = None,
    data: Any | None = None,
) -> requests.Response:
    """
    HTTP wrapper com:
    - retry (exponencial leve)
    - rate limit handling (Discord 429)
    - tolerância básica a 5xx
    """
    last_exc: Optional[Exception] = None

    for attempt in range(cfg.http_retries + 1):
        try:
            resp = SESSION.request(
                method=method,
                url=url,
                timeout=cfg.http_timeout_sec,
                json=json_body,
                data=data,
            )

            # Discord rate limit
            if resp.status_code == 429:
                retry_after = 1.0
                try:
                    j = resp.json()
                    retry_after = float(j.get("retry_after", retry_after))
                except Exception:
                    ra = resp.headers.get("Retry-After", "")
                    try:
                        retry_after = float(ra) if ra else retry_after
                    except Exception:
                        pass

                if attempt < cfg.http_retries:
                    time.sleep(min(15.0, max(0.8, retry_after)))
                    continue
                resp.raise_for_status()

            # Retry em 5xx (best-effort)
            if 500 <= resp.status_code < 600:
                if attempt < cfg.http_retries:
                    backoff = cfg.http_backoff_base_sec * (2 ** attempt)
                    time.sleep(min(6.0, max(0.6, backoff)))
                    continue

            resp.raise_for_status()
            return resp

        except Exception as ex:
            last_exc = ex
            if attempt < cfg.http_retries:
                backoff = cfg.http_backoff_base_sec * (2 ** attempt)
                time.sleep(min(6.0, max(0.6, backoff)))
                continue
            raise last_exc


def safe_get(cfg: Config, url: str) -> str:
    return _request(cfg, "GET", url).text


# -------------------------------------------------
# Cache (posted + translate)
# -------------------------------------------------
def _load_json_file(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as ex:
        log.warning(f"JSON inválido em {path.name}, ignorando: {ex}")
        return None


def load_posted_cache(cfg: Config) -> dict[str, int]:
    raw = _load_json_file(cfg.cache_file)
    if raw is None:
        return {}

    if isinstance(raw, dict):
        out: dict[str, int] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = int(v)
            except Exception:
                continue
        return out

    # Compat: lista antiga vira dict com timestamp "agora"
    if isinstance(raw, list):
        now = _now_ts()
        return {str(x): now for x in raw}

    return {}


def save_posted_cache(cfg: Config, cache: dict[str, int]) -> None:
    cutoff = _now_ts() - (cfg.cache_keep_days * 24 * 3600)
    pruned = {k: v for k, v in cache.items() if v >= cutoff}

    # Ordem determinística (menos conflito/diff)
    items = sorted(pruned.items(), key=lambda kv: (kv[1], kv[0]))  # old->new

    # Cap de tamanho (mantém mais recentes)
    if cfg.cache_max_items and len(items) > cfg.cache_max_items:
        items = items[-cfg.cache_max_items :]

    ordered = dict(items)
    payload = json.dumps(ordered, ensure_ascii=False, indent=2)

    # Escrita atômica
    tmp = cfg.cache_file.with_suffix(cfg.cache_file.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(cfg.cache_file)


def load_translate_cache(cfg: Config) -> dict[str, str]:
    raw = _load_json_file(cfg.translate_cache_file)
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return {}


def save_translate_cache(cfg: Config, cache: dict[str, str]) -> None:
    if cfg.translate_cache_max_items and len(cache) > cfg.translate_cache_max_items:
        excess = len(cache) - cfg.translate_cache_max_items
        for k in list(cache.keys())[:excess]:
            cache.pop(k, None)

    ordered = dict(sorted(cache.items(), key=lambda kv: kv[0]))
    payload = json.dumps(ordered, ensure_ascii=False, indent=2)

    tmp = cfg.translate_cache_file.with_suffix(cfg.translate_cache_file.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(cfg.translate_cache_file)


# -------------------------------------------------
# Data model
# -------------------------------------------------
@dataclass(frozen=True)
class NewsItem:
    uid: str
    title: str
    url: str
    source: str
    description: str = ""
    published_ts: Optional[int] = None


# -------------------------------------------------
# Meta extraction (og:image, og:desc, published)
# -------------------------------------------------
_META_CACHE: dict[str, tuple[str, Optional[str], Optional[int], str]] = {}


def extract_meta(cfg: Config, page_url: str) -> tuple[str, Optional[str], Optional[int], str]:
    """
    Returns: (description, og_image_url, published_ts, og_title)
    """
    page_url = normalize_url(page_url)
    if page_url in _META_CACHE:
        return _META_CACHE[page_url]

    try:
        html_text = safe_get(cfg, page_url)
        soup = BeautifulSoup(html_text, "lxml")

        def _meta(prop: str = "", name: str = "") -> Optional[str]:
            tag = None
            if prop:
                tag = soup.find("meta", attrs={"property": prop})
            if not tag and name:
                tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return str(tag["content"]).strip()
            return None

        og_title = clean_text(_meta(prop="og:title") or "")
        desc = clean_text(
            _meta(prop="og:description")
            or _meta(name="description")
            or _meta(name="twitter:description")
            or ""
        )

        img = _meta(prop="og:image") or _meta(prop="og:image:secure_url") or _meta(name="twitter:image")
        if img:
            img = urljoin(page_url, img)

        published_ts: Optional[int] = None
        pub = _meta(prop="article:published_time") or _meta(prop="og:updated_time") or ""
        if pub:
            try:
                pub_norm = pub.replace("Z", "+00:00") if pub.endswith("Z") else pub
                dt = datetime.fromisoformat(pub_norm)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                published_ts = int(dt.astimezone(timezone.utc).timestamp())
            except Exception:
                published_ts = None

        out = (desc, img, published_ts, og_title)
        _META_CACHE[page_url] = out
        return out

    except Exception:
        out = ("", None, None, "")
        _META_CACHE[page_url] = out
        return out


# -------------------------------------------------
# Translation (best-effort)
# -------------------------------------------------
def translate_pt(cfg: Config, text: str, tcache: dict[str, str]) -> str:
    txt = clean_text(text)
    if not txt or not cfg.translate_pt:
        return txt

    key = f"pt::{txt}"
    if key in tcache:
        return tcache[key]

    try:
        payload = {"q": txt, "source": "en", "target": "pt", "format": "text"}
        if cfg.libretranslate_api_key:
            payload["api_key"] = cfg.libretranslate_api_key

        r = _request(cfg, "POST", cfg.libretranslate_url, data=payload)
        out = clean_text(r.json().get("translatedText", "")) or txt
        tcache[key] = out
        return out
    except Exception:
        return txt


# -------------------------------------------------
# Sources
# -------------------------------------------------
def fetch_vlr_rss(cfg: Config, limit: int = 80) -> list[NewsItem]:
    xml_text = safe_get(cfg, "https://www.vlr.gg/rss")
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []

    out: list[NewsItem] = []
    for it in channel.findall("item")[:limit]:
        title = clean_text(it.findtext("title") or "VLR News")
        link = normalize_url(clean_text(it.findtext("link") or ""))
        desc = strip_html(it.findtext("description") or "")
        pub = it.findtext("pubDate") or ""

        pub_ts: Optional[int] = None
        try:
            if pub:
                pub_dt = parsedate_to_datetime(pub).astimezone(timezone.utc)
                pub_ts = int(pub_dt.timestamp())
        except Exception:
            pub_ts = None

        if link:
            out.append(
                NewsItem(
                    uid=f"vlr::{link}",
                    title=title,
                    url=link,
                    source="VLR.gg",
                    description=desc,
                    published_ts=pub_ts,
                )
            )
    return out


def _looks_like_thespike_article(href: str) -> bool:
    if not href or "/valorant/news/" not in href:
        return False
    last = href.rstrip("/").split("/")[-1]
    return last.isdigit()


def fetch_thespike_listing(cfg: Config, limit: int = 80) -> list[NewsItem]:
    base = "https://www.thespike.gg"
    urls = (
        [f"{base}/br/valorant/news", f"{base}/valorant/news"]
        if cfg.thespike_locale == "br"
        else [f"{base}/valorant/news", f"{base}/br/valorant/news"]
    )

    for listing_url in urls:
        try:
            html_text = safe_get(cfg, listing_url)
            soup = BeautifulSoup(html_text, "lxml")

            out: list[NewsItem] = []
            seen: set[str] = set()

            for a in soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                if not _looks_like_thespike_article(href):
                    continue

                url = normalize_url(urljoin(listing_url, href))
                if url in seen:
                    continue
                seen.add(url)

                title = clean_text(a.get_text(" ", strip=True))
                if title.lower() in {"loading...", "thespike news"}:
                    title = ""

                out.append(NewsItem(uid=f"spike::{url}", title=title, url=url, source="THESPIKE.GG"))
                if len(out) >= limit:
                    break

            if out:
                return out
        except Exception:
            continue

    return []


# -------------------------------------------------
# Discord embeds
# -------------------------------------------------
def _author_block(cfg: Config) -> dict[str, Any]:
    block: dict[str, Any] = {"name": f"{cfg.brand_name} • News Digest", "url": cfg.brand_url}
    if cfg.brand_icon_url:
        block["icon_url"] = cfg.brand_icon_url
    return block


def _source_home(cfg: Config, source: str) -> tuple[str, str]:
    if source == "VLR.gg":
        return ("VLR.gg", "https://www.vlr.gg/news")
    return (
        "THESPIKE.GG",
        "https://www.thespike.gg/br/valorant/news" if cfg.thespike_locale == "br" else "https://www.thespike.gg/valorant/news",
    )


def build_header_embed(cfg: Config, posted_count: int) -> dict[str, Any]:
    now = _now_ts()
    emb: dict[str, Any] = {
        "author": _author_block(cfg),
        "title": "🗞️ Digest de Notícias",
        "description": _truncate(
            f"**Mayble eSports (MBL)** • Curadoria automática\n\n"
            f"⏱️ Atualizado: <t:{now}:F> • <t:{now}:R>\n"
            f"📦 Itens no cache (aprox): **{posted_count}**\n"
            f"📍 Canal: **{cfg.channel_label}**",
            4096,
        ),
        "color": cfg.embed_color,
        "timestamp": _iso_from_ts(now),
        "footer": {"text": cfg.footer_text},
    }
    if cfg.banner_url:
        emb["image"] = {"url": cfg.banner_url}
    if cfg.use_brand_thumbnail and cfg.brand_thumbnail_url:
        emb["thumbnail"] = {"url": cfg.brand_thumbnail_url}
    return emb


def build_news_embed(cfg: Config, item: NewsItem, tcache: dict[str, str]) -> dict[str, Any]:
    meta_desc, meta_img, meta_pub_ts, meta_title = extract_meta(cfg, item.url)

    raw_title = clean_text(item.title)
    if raw_title.lower() in {"loading...", "", "thespike news"}:
        raw_title = meta_title or "Notícia"

    desc_raw = clean_text(item.description) or meta_desc or "Clique para abrir a notícia."

    do_translate = cfg.translate_pt and (
        item.source == "VLR.gg" or (not cfg.translate_only_vlr and cfg.thespike_locale == "en")
    )
    title_final = translate_pt(cfg, raw_title, tcache) if do_translate else raw_title
    desc_final = translate_pt(cfg, desc_raw, tcache) if do_translate else desc_raw

    ts = item.published_ts or meta_pub_ts or _now_ts()
    source_label, source_home = _source_home(cfg, item.source)

    embed: dict[str, Any] = {
        "author": _author_block(cfg),
        "title": _truncate(title_final, 256),
        "url": item.url,
        "description": _truncate(f"**Resumo:**\n> {_truncate(desc_final, 900)}", 4096),
        "color": cfg.embed_color,
        "timestamp": _iso_from_ts(ts),
        "footer": {"text": cfg.footer_text},
        "fields": [
            {"name": "🗞️ Fonte", "value": f"[{source_label}]({source_home})", "inline": True},
            {"name": "🕒 Publicado", "value": f"<t:{ts}:R>", "inline": True},
            {"name": "🔗 Link", "value": f"[Abrir notícia]({item.url})", "inline": True},
        ],
    }

    if cfg.use_brand_thumbnail and cfg.brand_thumbnail_url:
        embed["thumbnail"] = {"url": cfg.brand_thumbnail_url}

    if meta_img:
        embed["image"] = {"url": meta_img}

    return embed


def post_digest(cfg: Config, items: list[NewsItem], tcache: dict[str, str], posted_cache_size: int) -> None:
    now = _now_ts()

    # Discord limita a 10 embeds por mensagem.
    embeds: list[dict[str, Any]] = []

    if cfg.post_header_embed:
        embeds.append(build_header_embed(cfg, posted_cache_size))

    # mantém até 9 notícias se tiver header; senão 10
    max_news_embeds = 9 if cfg.post_header_embed else 10
    news_items = items[: min(cfg.max_posts_per_run, max_news_embeds)]

    embeds.extend([build_news_embed(cfg, x, tcache) for x in news_items])

    payload = {
        "content": f"🗞️ **Mayble News Digest** • <t:{now}:t> • <t:{now}:R>",
        "allowed_mentions": {"parse": []},
        "embeds": embeds,
    }

    if cfg.dry_run:
        log.info("DRY_RUN=1 (não enviando para o Discord).")
        log.info(f"Embeds preparados: {len(embeds)} | News: {len(news_items)}")
        return

    _request(cfg, "POST", cfg.webhook_url, json_body=payload)


# -------------------------------------------------
# Queue / seleção
# -------------------------------------------------
def _sort_news(items: list[NewsItem]) -> list[NewsItem]:
    return sorted(items, key=lambda x: (x.published_ts or 0), reverse=True)


def _apply_bootstrap_filter(cfg: Config, items: list[NewsItem], cache_is_empty: bool) -> list[NewsItem]:
    if not cache_is_empty or cfg.bootstrap_hours <= 0:
        return items
    cutoff = _now_ts() - (cfg.bootstrap_hours * 3600)
    filtered = [x for x in items if (x.published_ts or 0) >= cutoff]
    return filtered


def build_post_queue(cfg: Config, vlr_items: list[NewsItem], spike_items: list[NewsItem], cache: dict[str, int]) -> list[NewsItem]:
    cache_is_empty = len(cache) == 0

    vlr_items = _apply_bootstrap_filter(cfg, vlr_items, cache_is_empty)
    spike_items = _apply_bootstrap_filter(cfg, spike_items, cache_is_empty)

    vlr_new = [x for x in _sort_news(vlr_items) if x.uid not in cache][: max(0, cfg.max_vlr_per_run)]
    spike_new = [x for x in _sort_news(spike_items) if x.uid not in cache][: max(0, cfg.max_thespike_per_run)]

    queue: list[NewsItem] = []
    seen_urls: set[str] = set()

    i = 0
    while len(queue) < cfg.max_posts_per_run and (vlr_new or spike_new):
        if i % 2 == 0:
            item = vlr_new.pop(0) if vlr_new else spike_new.pop(0)
        else:
            item = spike_new.pop(0) if spike_new else vlr_new.pop(0)
        i += 1

        nu = normalize_url(item.url)
        if nu and nu in seen_urls:
            continue
        if nu:
            seen_urls.add(nu)

        queue.append(item)

    return queue


# -------------------------------------------------
# Main
# -------------------------------------------------
def _validate_webhook(url: str) -> bool:
    u = (url or "").strip()
    return ("/api/webhooks/" in u) and ("discord" in u)


def main() -> None:
    cfg = Config.load()

    if not cfg.webhook_url or not _validate_webhook(cfg.webhook_url):
        raise SystemExit("Webhook não definido/ inválido. Crie o Secret 'DISCORD_WEBHOOK_URL' no GitHub.")

    # Não logar webhook nunca.
    log.info(f"Start • brand={cfg.brand_name} • locale={cfg.thespike_locale} • dry_run={int(cfg.dry_run)}")

    posted_cache = load_posted_cache(cfg)
    tcache = load_translate_cache(cfg)

    vlr_items: list[NewsItem] = []
    spike_items: list[NewsItem] = []

    if cfg.enable_vlr:
        try:
            vlr_items = fetch_vlr_rss(cfg)
        except Exception as ex:
            log.warning(f"Falha ao buscar VLR: {ex}")

    if cfg.enable_thespike:
        try:
            spike_items = fetch_thespike_listing(cfg)
        except Exception as ex:
            log.warning(f"Falha ao buscar THESPIKE: {ex}")

    log.info(f"Encontrados: VLR={len(vlr_items)} | THESPIKE={len(spike_items)} | cache={len(posted_cache)}")

    queue = build_post_queue(cfg, vlr_items, spike_items, posted_cache)
    if not queue:
        log.info("Nada novo para postar (ou filtrado por bootstrap/cache).")
        return

    post_digest(cfg, queue, tcache, posted_cache_size=len(posted_cache))

    now = _now_ts()
    for item in queue:
        posted_cache[item.uid] = now

    # Só escreve cache se não estiver em dry-run
    if not cfg.dry_run:
        save_posted_cache(cfg, posted_cache)
        save_translate_cache(cfg, tcache)

    log.info(f"Done • postados={len(queue)}")


if __name__ == "__main__":
    main() 
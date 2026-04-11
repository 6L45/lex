#!/usr/bin/env python3
"""
Mise à jour incrémentale via les flux RSS de codes.droit.org.

Contrairement à sync (full re-crawl XML), pull ne récupère que les articles
modifiés depuis le dernier run, en utilisant les flux RSS par code.

Fonctionnement:
  1. Lit la page d'accueil pour récupérer la date de dernière modification
     de chaque code (data-edit) et l'URL de son flux RSS.
  2. Compare avec l'état sauvegardé dans .lex_pull_state.json.
  3. Ne fetch que les RSS des codes modifiés depuis le dernier pull.
  4. Upsert uniquement les articles dont le contenu a changé (content_hash).
  5. Sauvegarde le nouvel état.

Limites vs sync:
  - La hiérarchie (Partie > Livre > Titre...) n'est pas dans le RSS.
    Elle est préservée pour les articles déjà en base, NULL pour les nouveaux.
  - L'état (VIGUEUR / ABROGE) n'est pas dans le RSS non plus.
  Pour ces cas, relance ./lex sync pour une réindexation complète.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import psycopg
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

CODES_ROOT = os.getenv("LEX_CODES_ROOT", "https://codes.droit.org/")
REQUEST_TIMEOUT = int(os.getenv("LEX_REQUEST_TIMEOUT", "30"))
BATCH_SIZE = int(os.getenv("LEX_BATCH_SIZE", "200"))

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("LEX_DB_PORT", os.getenv("DB_PORT", "5432")))
DB_NAME = os.getenv("POSTGRES_DB", "lex")
DB_USER = os.getenv("POSTGRES_USER", "lex_user")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "change_me")

STATE_FILE = Path(__file__).parent.parent / ".lex_pull_state.json"

# Extrait LEGIARTI000XXXXXXXXX depuis une URL legifrance
_CID_RE = re.compile(r"(LEGIARTI\d+)", re.IGNORECASE)
# Extrait "article X du Code Y (date)" depuis le titre RSS
_TITLE_RE = re.compile(
    r"(?:Modification\s+)?article\s+(\S+)\s+du\s+(.+?)\s+\((\d{4}-\d{2}-\d{2})\)",
    re.IGNORECASE,
)


@dataclass
class PullRecord:
    article_cid: str
    content_hash: str
    last_sync_date: datetime
    raw_text: str
    code_juridique: str | None
    numero_article: str | None


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


# ---------------------------------------------------------------------------
# Lecture de l'état persisté
# ---------------------------------------------------------------------------

def load_state() -> dict[str, str]:
    """Retourne {code_name: last_edit_date_str}."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict[str, str]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Scraping homepage
# ---------------------------------------------------------------------------

@dataclass
class CodeEntry:
    name: str
    rss_url: str
    last_edit: date


def list_codes(root_url: str) -> list[CodeEntry]:
    resp = requests.get(root_url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    codes: list[CodeEntry] = []
    for dl in soup.find_all("dl", attrs={"data-name": True}):
        edit_str = dl.get("data-edit", "")
        rss_tag = dl.find("a", class_="rss", href=True)
        if not rss_tag or not edit_str:
            continue

        # data-name est en minuscules, on récupère le vrai nom via le lien RSS
        rss_href = rss_tag["href"]
        # ex: "feeds/Code%20civil.rss" → "Code civil"
        name_raw = Path(urlparse(rss_href).path).stem  # "Code civil"
        from urllib.parse import unquote
        name = unquote(name_raw)

        try:
            last_edit = date.fromisoformat(edit_str)
        except ValueError:
            continue

        codes.append(CodeEntry(
            name=name,
            rss_url=urljoin(root_url, rss_href),
            last_edit=last_edit,
        ))

    return codes


# ---------------------------------------------------------------------------
# Parse RSS
# ---------------------------------------------------------------------------

def parse_rss(rss_url: str, code_name: str) -> list[PullRecord]:
    resp = requests.get(rss_url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.content, "xml")
    records: list[PullRecord] = []

    for item in soup.find_all("item"):
        link = item.find("link")
        title = item.find("title")
        description = item.find("description")

        if not (link and title and description):
            continue

        link_text = link.get_text(strip=True)
        title_text = title.get_text(strip=True)
        desc_text = clean_text(description.get_text())

        if not desc_text:
            continue

        # article_cid depuis l'URL
        m_cid = _CID_RE.search(link_text)
        if not m_cid:
            continue
        article_cid = m_cid.group(1)

        # numero_article depuis le titre
        numero_article: str | None = None
        m_title = _TITLE_RE.search(title_text)
        if m_title:
            numero_article = m_title.group(1)

        records.append(PullRecord(
            article_cid=article_cid,
            content_hash=hashlib.sha256(desc_text.encode("utf-8")).hexdigest(),
            last_sync_date=datetime.now(),
            raw_text=desc_text,
            code_juridique=code_name,
            numero_article=numero_article,
        ))

    return records


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def upsert_records(conn: psycopg.Connection, records: list[PullRecord]) -> int:
    """
    Upsert sans toucher à hierarchie ni etat (champs absents du RSS).
    Si l'article existe déjà, on ne met à jour que si le texte a changé.
    """
    if not records:
        return 0

    query = """
        INSERT INTO legal_articles (
            article_cid, content_hash, last_sync_date,
            raw_text, embedding, code_juridique, numero_article
        ) VALUES (
            %(article_cid)s, %(content_hash)s, %(last_sync_date)s,
            %(raw_text)s, NULL, %(code_juridique)s, %(numero_article)s
        )
        ON CONFLICT (article_cid)
        DO UPDATE SET
            content_hash   = EXCLUDED.content_hash,
            last_sync_date = EXCLUDED.last_sync_date,
            raw_text       = EXCLUDED.raw_text,
            embedding      = NULL,
            code_juridique = EXCLUDED.code_juridique,
            numero_article = EXCLUDED.numero_article
        WHERE legal_articles.content_hash IS DISTINCT FROM EXCLUDED.content_hash;
    """

    payload = [
        {
            "article_cid": r.article_cid,
            "content_hash": r.content_hash,
            "last_sync_date": r.last_sync_date,
            "raw_text": r.raw_text,
            "code_juridique": r.code_juridique,
            "numero_article": r.numero_article,
        }
        for r in records
    ]

    with conn.cursor() as cur:
        cur.executemany(query, payload)

    return len(records)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def pull(conn: psycopg.Connection) -> int:
    """
    Exécute le pull RSS et retourne le nombre d'articles modifiés en base.
    Peut être appelé depuis d'autres scripts (ex: après pull, lancer embed).
    """
    state = load_state()
    last_global_pull = state.get("_last_pull", "1970-01-01")

    print(f"Dernier pull: {last_global_pull}")
    print(f"Lecture des codes depuis {CODES_ROOT}...")

    codes = list_codes(CODES_ROOT)
    if not codes:
        print("Aucun code trouvé sur la page d'accueil.", file=sys.stderr)
        return 0

    to_fetch = [
        c for c in codes
        if str(c.last_edit) > state.get(c.name, "1970-01-01")
    ]

    if not to_fetch:
        print(f"Aucun code modifié depuis {last_global_pull}. Base à jour.")
        return 0

    print(f"{len(to_fetch)}/{len(codes)} codes ont des modifications à récupérer.")

    total_updated = 0
    new_state = dict(state)
    batch: list[PullRecord] = []

    for idx, code in enumerate(to_fetch, start=1):
        try:
            records = parse_rss(code.rss_url, code.name)
        except Exception as exc:
            print(f"[WARN] RSS ignoré ({code.name}): {exc}", file=sys.stderr)
            continue

        batch.extend(records)
        new_state[code.name] = str(code.last_edit)

        if len(batch) >= BATCH_SIZE:
            total_updated += upsert_records(conn, batch)
            conn.commit()
            print(f"  [{idx}/{len(to_fetch)}] {code.name} — {total_updated} articles traités")
            batch.clear()
        else:
            print(f"  [{idx}/{len(to_fetch)}] {code.name} — {len(records)} articles dans le flux")

    if batch:
        total_updated += upsert_records(conn, batch)
        conn.commit()

    new_state["_last_pull"] = str(date.today())
    save_state(new_state)

    print(f"\nPull terminé: {total_updated} articles mis à jour.")
    return total_updated


def main() -> int:
    dsn = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"
    with psycopg.connect(dsn) as conn:
        updated = pull(conn)

    # Embed ciblé post-pull si un provider est configuré
    provider_name = os.getenv("LEX_EMBEDDING_PROVIDER", "")
    if updated > 0 and provider_name:
        try:
            from embed import PROVIDERS, get_connection, get_db_embedding_dim, run_embed
            from pgvector.psycopg import register_vector
        except ImportError:
            print("[WARN] embed.py non disponible, skip embedding.", file=sys.stderr)
            return 0

        print(f"\nEmbed ciblé: {updated} articles avec embedding NULL...")
        try:
            provider = PROVIDERS[provider_name]()
        except (KeyError, ValueError, ImportError) as exc:
            print(f"[WARN] Provider '{provider_name}' indisponible: {exc} — skip embedding.", file=sys.stderr)
            return 0

        embed_conn = get_connection()
        try:
            db_dim = get_db_embedding_dim(embed_conn)
            if db_dim and db_dim != provider.dimension:
                print(
                    f"[WARN] Dimension mismatch: DB={db_dim}d, provider={provider.dimension}d. "
                    "Lance './lex embed full' manuellement pour migrer.",
                    file=sys.stderr,
                )
            else:
                run_embed(provider, embed_conn, full=False)
        finally:
            embed_conn.close()
    elif updated > 0 and not provider_name:
        print(
            "\n[INFO] LEX_EMBEDDING_PROVIDER non configuré dans .env — "
            "embeddings non mis à jour.\n"
            "Lance './lex embed' quand tu veux les régénérer."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

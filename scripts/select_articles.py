#!/usr/bin/env python3
"""
Selection quotidienne d'articles "insolites" et "utiles" via un LLM.

Pipeline :
    1. Tirage aleatoire de N candidats parmi les articles `selected = FALSE
       AND done = FALSE` (filtre etat optionnel).
    2. Envoi au LLM pour qu'il choisisse les K plus pertinents par categorie.
    3. UPDATE selected = TRUE sur les article_cid retenus.
    4. Append d'une ligne par article au fichier de sortie configure :
       <ISO8601>|<categorie>|<article_cid>

L'autre agent (a venir) lit ce fichier, fait son job, flippe done = TRUE
puis nettoie ses lignes traitees.

Usage :
    python scripts/select_articles.py
    python scripts/select_articles.py --provider openai --count 2 --sample 80

Variables tunables : voir scripts/config.py.
Credentials (cles API) : .env uniquement.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv

import config
from llm_provider import CandidateArticle, build_provider

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("LEX_DB_PORT", os.getenv("DB_PORT", "5432")))
DB_NAME = os.getenv("POSTGRES_DB", "lex")
DB_USER = os.getenv("POSTGRES_USER", "lex_user")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "change_me")

ROOT_DIR = Path(__file__).resolve().parent.parent


def get_connection() -> psycopg.Connection:
    dsn = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"
    return psycopg.connect(dsn)


def fetch_candidates(
    conn: psycopg.Connection,
    sample_size: int,
    filter_etat: str | None,
    truncate: int,
) -> list[CandidateArticle]:
    where = ["NOT selected", "NOT done", "raw_text IS NOT NULL"]
    params: list[object] = []
    if filter_etat:
        where.append("etat = %s")
        params.append(filter_etat)

    query = f"""
        SELECT
            article_cid,
            code_juridique,
            numero_article,
            LEFT(raw_text, %s) AS raw_text
        FROM legal_articles
        WHERE {' AND '.join(where)}
        ORDER BY random()
        LIMIT %s;
    """
    params = [truncate, *params, sample_size]

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    return [
        CandidateArticle(cid=cid, code_juridique=code, numero_article=num, raw_text=text or "")
        for cid, code, num, text in rows
    ]


def mark_selected(conn: psycopg.Connection, cids: list[str]) -> int:
    if not cids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE legal_articles SET selected = TRUE "
            "WHERE article_cid = ANY(%s) AND NOT selected AND NOT done;",
            (cids,),
        )
        return cur.rowcount or 0


def append_output(path: Path, entries: list[tuple[str, str]]) -> None:
    """entries = [(category, article_cid), ...]"""
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a", encoding="utf-8") as fh:
        for category, cid in entries:
            fh.write(f"{ts}|{category}|{cid}\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Selectionne des articles via un LLM et les marque selected=TRUE."
    )
    parser.add_argument("--provider", default=None, help="anthropic | openai (defaut: config.LLM_PROVIDER)")
    parser.add_argument("--sample", type=int, default=config.SELECT_SAMPLE_SIZE,
                        help="Taille du pool tire aleatoirement.")
    parser.add_argument("--count", type=int, default=config.SELECT_COUNT_PER_CATEGORY,
                        help="Nb d'articles par categorie.")
    parser.add_argument("--output", default=None, help="Chemin du fichier de sortie (append).")
    parser.add_argument("--dry-run", action="store_true",
                        help="N'ecrit rien (pas d'UPDATE, pas d'append). Affiche la selection.")
    args = parser.parse_args()

    sample_size = args.sample
    count = args.count
    output_path = Path(args.output or config.SELECT_OUTPUT_PATH)
    if not output_path.is_absolute():
        output_path = ROOT_DIR / output_path

    print(f"Selection  : {count} article(s) par categorie {list(config.SELECT_CATEGORIES)}")
    print(f"Pool       : {sample_size} candidats (etat={config.SELECT_FILTER_ETAT or '*'})")
    print(f"Sortie     : {output_path}")
    if args.dry_run:
        print("DRY-RUN    : aucune ecriture (DB ni fichier).")

    try:
        provider = build_provider(args.provider)
    except (ValueError, ImportError) as exc:
        print(f"Erreur provider LLM : {exc}", file=sys.stderr)
        return 1

    with get_connection() as conn:
        candidates = fetch_candidates(
            conn, sample_size, config.SELECT_FILTER_ETAT, config.SELECT_RAW_TEXT_TRUNCATE
        )
        if len(candidates) < count * len(config.SELECT_CATEGORIES):
            print(
                f"Pas assez de candidats : {len(candidates)} dispos pour "
                f"{count * len(config.SELECT_CATEGORIES)} requis.",
                file=sys.stderr,
            )
            return 2
        print(f"Candidats  : {len(candidates)} articles tires.")

        try:
            selection = provider.select(candidates, list(config.SELECT_CATEGORIES), count)
        except Exception as exc:
            print(f"Erreur LLM : {exc}", file=sys.stderr)
            return 3

        entries: list[tuple[str, str]] = []
        for category in config.SELECT_CATEGORIES:
            for item in selection.get(category, []):
                cid = item["cid"]
                reason = item.get("reason", "")
                print(f"  [{category:8}] {cid}  — {reason}")
                entries.append((category, cid))

        if args.dry_run:
            print("DRY-RUN termine.")
            return 0

        cids = [cid for _, cid in entries]
        updated = mark_selected(conn, cids)
        conn.commit()
        print(f"DB         : {updated} article(s) marque(s) selected=TRUE.")

    append_output(output_path, entries)
    print(f"Fichier    : {len(entries)} ligne(s) ajoutee(s) a {output_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

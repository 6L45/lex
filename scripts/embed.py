#!/usr/bin/env python3
"""
Génération des embeddings RAG.

Usage:
    python scripts/embed.py                              # provider via LEX_EMBEDDING_PROVIDER
    python scripts/embed.py --provider openai
    python scripts/embed.py --provider sentence-transformers
    python scripts/embed.py --provider cohere

Providers et dépendances:
    openai                : pip install langchain-openai          (OPENAI_API_KEY requis)
    sentence-transformers : pip install sentence-transformers     (local, pas de clé)
    cohere                : pip install langchain-cohere          (COHERE_API_KEY requis)

Si tu changes de provider avec des dimensions différentes, relance d'abord:
    ALTER TABLE legal_articles ALTER COLUMN embedding TYPE vector(<nouvelle_dim>);
    UPDATE legal_articles SET embedding = NULL;
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Protocol, runtime_checkable

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("LEX_DB_PORT", os.getenv("DB_PORT", "5432")))
DB_NAME = os.getenv("POSTGRES_DB", "lex")
DB_USER = os.getenv("POSTGRES_USER", "lex_user")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "change_me")

RAG_BATCH_SIZE = int(os.getenv("LEX_RAG_BATCH_SIZE", "64"))


# ---------------------------------------------------------------------------
# Protocol commun à tous les providers
# ---------------------------------------------------------------------------

@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class OpenAIProvider:
    name = "openai"

    def __init__(self) -> None:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise ValueError("OPENAI_API_KEY manquant dans .env")
        try:
            from langchain_openai import OpenAIEmbeddings
        except ImportError:
            raise ImportError("pip install langchain-openai")

        model = os.getenv("LEX_EMBEDDING_MODEL", "text-embedding-3-small")
        _dims = {
            "text-embedding-3-small": 1536,
            "text-embedding-3-large": 3072,
            "text-embedding-ada-002": 1536,
        }
        self.dimension = _dims.get(model, 1536)
        self._client = OpenAIEmbeddings(model=model, api_key=api_key)
        print(f"Provider : OpenAI — {model} ({self.dimension}d)")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._client.embed_documents(texts)


class SentenceTransformersProvider:
    name = "sentence-transformers"

    def __init__(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError("pip install sentence-transformers")

        model = os.getenv("LEX_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        self._model = SentenceTransformer(model)
        self.dimension = self._model.get_sentence_embedding_dimension()
        print(f"Provider : sentence-transformers — {model} ({self.dimension}d, local)")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, show_progress_bar=False).tolist()


class CohereProvider:
    name = "cohere"

    def __init__(self) -> None:
        api_key = os.getenv("COHERE_API_KEY", "")
        if not api_key:
            raise ValueError("COHERE_API_KEY manquant dans .env")
        try:
            from langchain_cohere import CohereEmbeddings
        except ImportError:
            raise ImportError("pip install langchain-cohere")

        model = os.getenv("LEX_EMBEDDING_MODEL", "embed-multilingual-v3.0")
        _dims = {
            "embed-multilingual-v3.0": 1024,
            "embed-multilingual-light-v3.0": 384,
            "embed-english-v3.0": 1024,
        }
        self.dimension = _dims.get(model, 1024)
        self._client = CohereEmbeddings(model=model, cohere_api_key=api_key)
        print(f"Provider : Cohere — {model} ({self.dimension}d)")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._client.embed_documents(texts)


PROVIDERS: dict[str, type] = {
    "openai": OpenAIProvider,
    "sentence-transformers": SentenceTransformersProvider,
    "cohere": CohereProvider,
}


# ---------------------------------------------------------------------------
# Helpers DB
# ---------------------------------------------------------------------------

def get_connection() -> psycopg.Connection:
    dsn = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"
    conn = psycopg.connect(dsn)
    register_vector(conn)
    return conn


def get_db_embedding_dim(conn: psycopg.Connection) -> int | None:
    """Retourne la dimension actuelle de la colonne embedding, ou None si pas typée."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT (regexp_match(format_type(atttypid, atttypmod), '\\d+'))[1]::int
            FROM pg_attribute
            WHERE attrelid = 'legal_articles'::regclass
              AND attname = 'embedding';
        """)
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def count_targets(conn: psycopg.Connection, full: bool) -> int:
    with conn.cursor() as cur:
        if full:
            cur.execute("SELECT COUNT(*) FROM legal_articles;")
        else:
            cur.execute("SELECT COUNT(*) FROM legal_articles WHERE embedding IS NULL;")
        return cur.fetchone()[0]


def fetch_batch(conn: psycopg.Connection, after_cid: str | None, full: bool) -> list[tuple[str, str]]:
    """Keyset pagination. full=True parcourt tout, full=False uniquement embedding IS NULL."""
    with conn.cursor() as cur:
        if full:
            if after_cid is None:
                cur.execute(
                    "SELECT article_cid, raw_text FROM legal_articles ORDER BY article_cid LIMIT %s;",
                    (RAG_BATCH_SIZE,),
                )
            else:
                cur.execute(
                    "SELECT article_cid, raw_text FROM legal_articles"
                    " WHERE article_cid > %s ORDER BY article_cid LIMIT %s;",
                    (after_cid, RAG_BATCH_SIZE),
                )
        else:
            if after_cid is None:
                cur.execute(
                    "SELECT article_cid, raw_text FROM legal_articles"
                    " WHERE embedding IS NULL ORDER BY article_cid LIMIT %s;",
                    (RAG_BATCH_SIZE,),
                )
            else:
                cur.execute(
                    "SELECT article_cid, raw_text FROM legal_articles"
                    " WHERE embedding IS NULL AND article_cid > %s ORDER BY article_cid LIMIT %s;",
                    (after_cid, RAG_BATCH_SIZE),
                )
        return cur.fetchall()


def store_batch(conn: psycopg.Connection, rows: list[tuple[str, str]], vectors: list[list[float]]) -> None:
    with conn.cursor() as cur:
        for (article_cid, _), vector in zip(rows, vectors, strict=True):
            cur.execute(
                "UPDATE legal_articles SET embedding = %s WHERE article_cid = %s;",
                (vector, article_cid),
            )
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_embed(provider: EmbeddingProvider, conn: psycopg.Connection, full: bool) -> int:
    """Lance l'indexation et retourne le nombre d'articles traités."""
    total_targets = count_targets(conn, full)
    if total_targets == 0:
        label = "à (ré)indexer" if full else "sans embedding"
        print(f"Aucun article {label}. Rien à faire.")
        return 0

    label = "(ré)indexer" if full else "indexer (embedding NULL)"
    print(f"{total_targets} articles à {label} (batch size: {RAG_BATCH_SIZE})")
    total = 0
    last_cid: str | None = None

    while True:
        rows = fetch_batch(conn, last_cid, full)
        if not rows:
            break

        vectors = provider.embed([row[1] for row in rows])
        store_batch(conn, rows, vectors)
        total += len(rows)
        last_cid = rows[-1][0]
        print(f"  {total}/{total_targets} embeddings générés...")

    print(f"\nTerminé. {total} embeddings stockés.")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Génère les embeddings RAG dans PostgreSQL.")
    parser.add_argument(
        "--provider",
        choices=list(PROVIDERS),
        default=os.getenv("LEX_EMBEDDING_PROVIDER", "openai"),
        help="Provider d'embedding (défaut: LEX_EMBEDDING_PROVIDER ou 'openai')",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        default=False,
        help="Réindexe tous les articles (écrase les embeddings existants). Sans ce flag: uniquement embedding IS NULL.",
    )
    args = parser.parse_args()

    try:
        provider: EmbeddingProvider = PROVIDERS[args.provider]()
    except (ValueError, ImportError) as exc:
        print(f"Erreur provider '{args.provider}': {exc}", file=sys.stderr)
        return 1

    conn = get_connection()
    try:
        db_dim = get_db_embedding_dim(conn)
        if db_dim and db_dim != provider.dimension:
            print(f"Dimension actuelle: vector({db_dim}) → migration vers vector({provider.dimension})")
            with conn.cursor() as cur:
                cur.execute(
                    f"ALTER TABLE legal_articles ALTER COLUMN embedding TYPE vector({provider.dimension});"
                )
            conn.commit()
            print("Migration colonne OK.")

        run_embed(provider, conn, full=args.full)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

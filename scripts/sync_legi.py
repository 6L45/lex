#!/usr/bin/env python3
"""Ingestion des XML de codes.droit.org vers PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from urllib.parse import urljoin

import psycopg
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from lxml import etree

load_dotenv()

CODES_ROOT = os.getenv("LEX_CODES_ROOT", "https://codes.droit.org/")
MAX_XML_FILES = int(os.getenv("LEX_MAX_XML_FILES", "0"))
REQUEST_TIMEOUT = int(os.getenv("LEX_REQUEST_TIMEOUT", "30"))
BATCH_SIZE = int(os.getenv("LEX_BATCH_SIZE", "200"))

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("LEX_DB_PORT", os.getenv("DB_PORT", "5432")))
DB_NAME = os.getenv("POSTGRES_DB", "lex")
DB_USER = os.getenv("POSTGRES_USER", "lex_user")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "change_me")


@dataclass
class ArticleRecord:
    article_cid: str
    content_hash: str
    last_sync_date: datetime
    raw_text: str
    code_juridique: str | None
    numero_article: str | None
    hierarchie: list[str]
    etat: str | None


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def local_name_upper(node: etree._Element) -> str | None:
    tag = getattr(node, "tag", None)
    if not isinstance(tag, str):
        return None
    if "}" in tag:
        return tag.rsplit("}", 1)[1].upper()
    return tag.upper()


def get_attr_text(el: etree._Element, *names: str) -> str | None:
    if not getattr(el, "attrib", None):
        return None

    wanted = {name.upper() for name in names}
    for key, value in el.attrib.items():
        if key.upper() in wanted:
            txt = clean_text(value)
            if txt:
                return txt
    return None


def get_first_text(el: etree._Element, *tags: str) -> str | None:
    upper_tags = {tag.upper() for tag in tags}
    for child in el.iter():
        lname = local_name_upper(child)
        if lname and lname in upper_tags:
            txt = clean_text(" ".join(child.itertext()))
            if txt:
                return txt
    return None


def parse_article_element(article_el: etree._Element, code_name: str | None) -> ArticleRecord | None:
    article_cid = get_attr_text(article_el, "CID", "ID", "ID_ARTI", "ARTICLE_CID")
    if not article_cid:
        article_cid = get_first_text(article_el, "ID", "ID_ARTI", "CID", "ARTICLE_CID")
    if not article_cid:
        return None

    numero_article = get_attr_text(article_el, "NUM", "NUM_ARTICLE")
    if not numero_article:
        numero_article = get_first_text(article_el, "NUM", "NUM_ARTICLE", "ARTICLE")

    etat = get_attr_text(article_el, "ETAT", "STATUT")
    if not etat:
        etat = get_first_text(article_el, "ETAT", "STATUT")

    raw_text = clean_text(" ".join(article_el.itertext()))
    if not raw_text:
        raw_text = get_first_text(article_el, "BLOC_TEXTUEL", "CONTENU", "TEXTE", "ARTICLE")

    if not raw_text:
        return None

    hierarchy_parts: list[str] = []
    parent = article_el.getparent()
    while parent is not None:
        p_title = get_attr_text(parent, "TITLE", "TITRE", "INTITULE", "NUM", "NATURE")
        if not p_title:
            p_title = get_first_text(parent, "TITRE", "INTITULE", "NUM", "NATURE")
        if p_title:
            hierarchy_parts.append(p_title)
        parent = parent.getparent()

    hierarchy_parts.reverse()

    return ArticleRecord(
        article_cid=article_cid,
        content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        last_sync_date=datetime.now(),
        raw_text=raw_text,
        code_juridique=code_name,
        numero_article=numero_article,
        hierarchie=hierarchy_parts,
        etat=etat,
    )


def list_xml_urls(root_url: str) -> list[str]:
    visited: set[str] = set()
    queue: deque[str] = deque([root_url])
    xml_urls: list[str] = []

    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)

        try:
            response = requests.get(current, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except Exception as exc:
            print(f"[WARN] Impossible de lire {current}: {exc}", file=sys.stderr)
            continue

        content_type = response.headers.get("content-type", "")
        if current.lower().endswith(".xml") or "xml" in content_type:
            xml_urls.append(current)
            if MAX_XML_FILES > 0 and len(xml_urls) >= MAX_XML_FILES:
                return xml_urls
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.startswith("?") or href.startswith("#"):
                continue
            url = urljoin(current, href)
            if not url.startswith(root_url):
                continue
            if url in visited:
                continue

            if url.endswith("/") or url.lower().endswith(".xml"):
                queue.append(url)  # deque.append is O(1)

    return xml_urls


def extract_articles_from_xml(xml_bytes: bytes) -> Iterable[ArticleRecord]:
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(xml_bytes, parser=parser)

    code_name = get_attr_text(root, "NOM", "TITLE")
    if not code_name:
        code_name = get_first_text(root, "TITRE", "INTITULE", "CODE")

    for el in root.iter():
        lname = local_name_upper(el)
        if lname in {"ARTICLE", "LEGIARTI"}:
            record = parse_article_element(el, code_name)
            if record:
                yield record


def download_xml_to_temp(url: str) -> str:
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    with tempfile.NamedTemporaryFile(prefix="lex_xml_", suffix=".xml", delete=False) as tmp:
        tmp.write(response.content)
        return tmp.name


def upsert_records(conn: psycopg.Connection, records: list[ArticleRecord]) -> int:
    if not records:
        return 0

    query = """
        INSERT INTO legal_articles (
            article_cid,
            content_hash,
            last_sync_date,
            raw_text,
            embedding,
            code_juridique,
            numero_article,
            hierarchie,
            etat
        ) VALUES (
            %(article_cid)s,
            %(content_hash)s,
            %(last_sync_date)s,
            %(raw_text)s,
            NULL,
            %(code_juridique)s,
            %(numero_article)s,
            %(hierarchie)s,
            %(etat)s
        )
        ON CONFLICT (article_cid)
        DO UPDATE SET
            content_hash = EXCLUDED.content_hash,
            last_sync_date = EXCLUDED.last_sync_date,
            raw_text = EXCLUDED.raw_text,
            embedding = NULL,
            code_juridique = EXCLUDED.code_juridique,
            numero_article = EXCLUDED.numero_article,
            hierarchie = EXCLUDED.hierarchie,
            etat = EXCLUDED.etat
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
            "hierarchie": json.dumps(r.hierarchie, ensure_ascii=False),
            "etat": r.etat,
        }
        for r in records
    ]

    with conn.cursor() as cur:
        cur.executemany(query, payload)

    return len(records)


def main() -> int:
    print(f"Exploration XML depuis {CODES_ROOT}")
    xml_urls = list_xml_urls(CODES_ROOT)
    if not xml_urls:
        print("Aucun XML trouve.")
        return 1

    print(f"XML detectes: {len(xml_urls)}")

    inserted = 0
    batch: list[ArticleRecord] = []

    dsn = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"
    with psycopg.connect(dsn) as conn:
        for idx, url in enumerate(xml_urls, start=1):
            tmp_file_path: str | None = None
            try:
                tmp_file_path = download_xml_to_temp(url)
                with open(tmp_file_path, "rb") as f:
                    xml_records = list(extract_articles_from_xml(f.read()))
            except Exception as exc:
                print(f"[WARN] XML ignore ({url}): {exc}", file=sys.stderr)
                continue
            finally:
                if tmp_file_path and os.path.exists(tmp_file_path):
                    os.remove(tmp_file_path)

            batch.extend(xml_records)
            if len(batch) >= BATCH_SIZE:
                inserted += upsert_records(conn, batch)
                conn.commit()
                print(f"Progression: {idx}/{len(xml_urls)} XML, {inserted} articles UPSERT")
                batch.clear()

        if batch:
            inserted += upsert_records(conn, batch)
            conn.commit()

    print(f"Ingestion terminee: {inserted} articles traites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

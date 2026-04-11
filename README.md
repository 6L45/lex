# lex

Base PostgreSQL conteneurisee pour textes de loi + colonne embedding RAG.

## Prerequis

- Docker
- Optionnel: `curl` ou `wget` (pour auto-installer `uv` si absent)

## Fichiers clefs

- `init`: initialise l'image, le volume, le container, le schema SQL
- `lex-start`: demarre le container (ou lance `./init` s'il n'existe pas)
- `lex-stop`: stoppe le container
- `lex-delete`: supprime container + image + volume du projet
- `scripts/sync_legi.py`: recupere les XML de https://codes.droit.org/ et upsert les articles
- `scripts/build_rag_index.py`: genere les embeddings via LangChain + LangGraph

## Mise en place rapide

1. Copier le fichier d'environnement:

```bash
cp .env.example .env
```

2. Ajuster les secrets dans `.env`, en particulier:

- `POSTGRES_PASSWORD`
- `OPENAI_API_KEY` (si generation embeddings)

3. Initialiser:

```bash
./init
```

`./init` est standalone: il installe automatiquement `uv` (si besoin), cree `.venv`, installe les dependances Python, initialise PostgreSQL et lance la sync complete.

Le container sera accessible sur `localhost:${LEX_DB_PORT}` (par defaut 5432).

## Synchronisation donnees

Deux modes:

- Manuel:

```bash
./.venv/bin/python scripts/sync_legi.py
./.venv/bin/python scripts/build_rag_index.py
```

- Automatique au `./init`:

Dans `.env`:

```env
LEX_RUN_SYNC_ON_INIT=1
```

Par defaut, `LEX_MAX_XML_FILES=0`, donc ingestion de tous les XML.

## Schema SQL principal

Table `legal_articles`:

- `article_cid` (PK)
- `content_hash`
- `last_sync_date`
- `raw_text`
- `embedding vector(1536)`
- `code_juridique`
- `numero_article`
- `hierarchie JSONB`
- `etat`

## Exemples de commande lifecycle

```bash
./lex-start
./lex-stop
./lex-delete
```

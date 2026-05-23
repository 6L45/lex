# lex

Base PostgreSQL conteneurisee pour textes de loi + colonne embedding RAG +
pipeline de selection quotidienne d'articles via LLM.

## Prerequis

- Docker
- Optionnel: `curl` ou `wget` (pour auto-installer `uv` si absent)

## Fichiers clefs

- `init`: initialise l'image, le volume, le container, le schema SQL
- `lex-start`: demarre le container (ou lance `./init` s'il n'existe pas)
- `lex-stop`: stoppe le container
- `lex-delete`: supprime container + image + volume du projet
- `scripts/sync_legi.py`: recupere les XML de https://codes.droit.org/ et upsert les articles
- `scripts/embed.py`: genere les embeddings RAG (multi-provider)
- `scripts/select_articles.py`: selectionne quotidiennement N articles "insolites" / "utiles" via LLM
- `scripts/llm_provider.py`: abstraction des providers LLM (Anthropic, OpenAI)
- `scripts/config.py`: variables globales tunables (taille de pool, nb par categorie, etc.)

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
./.venv/bin/python scripts/embed.py
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
- `selected BOOLEAN` — flippe a `TRUE` par l'agent de selection
- `done BOOLEAN` — flippe a `TRUE` par l'agent suivant (pas encore implemente)
- `CHECK (selected OR NOT done)` — l'etat `selected=FALSE + done=TRUE` est interdit

Etats valides :

| selected | done  | signification                                            |
| :------: | :---: | -------------------------------------------------------- |
| `false`  | false | candidat (target du tirage)                              |
| `true`   | false | retenu, en attente du 2e agent                           |
| `true`   | true  | traite avec succes                                       |
| `false`  | true  | **interdit** par contrainte (etat illogique)             |

## Selection quotidienne (LLM)

```bash
./lex select                       # 1 insolite + 1 utile (defaut)
./lex select --count 2             # 2 par categorie
./lex select --provider openai     # autre provider
./lex select --dry-run             # affiche sans rien ecrire
```

- Tire `SELECT_SAMPLE_SIZE=50` candidats au hasard (configurable dans
  [`scripts/config.py`](scripts/config.py)).
- Demande au LLM de choisir N articles "insolites" et N articles "utiles".
- `UPDATE selected = TRUE` sur les articles retenus.
- Append au fichier [`out/selected.txt`](out/) au format
  `<ISO8601>|<categorie>|<article_cid>` (une ligne par article).

Credentials : `ANTHROPIC_API_KEY` ou `OPENAI_API_KEY` dans `.env`.
Provider par defaut : `LEX_LLM_PROVIDER=anthropic`.

### Cron (a configurer manuellement)

Exemple d'entree crontab — 1 sortie par jour a 8h :

```cron
0 8 * * * cd /chemin/vers/lex && ./lex select >> /var/log/lex-select.log 2>&1
```

## Exemples de commande lifecycle

```bash
./lex-start
./lex-stop
./lex-delete
```

## Etat du projet

Fait :

- [x] Ingestion XML -> PostgreSQL (`./lex sync`)
- [x] Embeddings RAG multi-provider stockes en colonne `embedding` (`./lex embed`)
- [x] Schema avec colonnes `selected` / `done` + contrainte d'integrite
- [x] Agent de selection LLM (`./lex select`) -> `out/selected.txt`

A faire (suite du pipeline) :

- [ ] **Agent consommateur** : lit `out/selected.txt`, fait son traitement
      (publication / generation de contenu / etc.), fait
      `UPDATE legal_articles SET done = TRUE WHERE article_cid = ?` sur les
      articles traites, puis nettoie les lignes consommees du fichier.
- [ ] Mise en place effective du cron quotidien.

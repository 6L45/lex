"""
Variables globales tunables du pipeline.

Les *credentials* (cles API) restent dans .env (gitignored), pas ici.
Ce fichier ne contient que des valeurs non-sensibles facilement ajustables.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Selection d'articles (scripts/select_articles.py)
# ---------------------------------------------------------------------------

# Taille du pool tire au hasard puis presente au LLM.
SELECT_SAMPLE_SIZE: int = 50

# Nombre d'articles selectionnes *par categorie* a chaque run.
# (1 insolite + 1 utile par defaut = 2 articles par run)
SELECT_COUNT_PER_CATEGORY: int = 1

# Categories demandees au LLM. Chaque categorie produira SELECT_COUNT_PER_CATEGORY
# articles a chaque run.
SELECT_CATEGORIES: tuple[str, ...] = ("insolite", "utile")

# Filtre sur la colonne `etat` au moment du tirage. Mettre None pour ne pas filtrer.
SELECT_FILTER_ETAT: str | None = "VIGUEUR"

# Chemin du fichier de sortie (append-only). L'autre agent lira et nettoiera ce fichier.
# Format d'une ligne : <ISO8601>|<categorie>|<article_cid>
SELECT_OUTPUT_PATH: str = "out/selected.txt"

# Longueur max du raw_text envoye au LLM (caracteres). Au-dela, troncature.
# Limite la consommation de tokens sur les articles tres longs.
SELECT_RAW_TEXT_TRUNCATE: int = 2000


# ---------------------------------------------------------------------------
# LLM (scripts/llm_provider.py)
# ---------------------------------------------------------------------------

# Provider par defaut. Override possible via env LEX_LLM_PROVIDER.
# Valeurs : "anthropic" | "openai"
LLM_PROVIDER: str = "anthropic"

# Modeles par provider. Override possible via env LEX_LLM_MODEL.
LLM_MODEL_ANTHROPIC: str = "claude-sonnet-4-6"
LLM_MODEL_OPENAI: str = "gpt-4o-mini"

# Temperature de generation. Bas = selections plus stables, haut = plus de variete.
LLM_TEMPERATURE: float = 0.7

# Timeout HTTP des appels LLM (secondes).
LLM_TIMEOUT: int = 60

# Max tokens en sortie.
LLM_MAX_TOKENS: int = 1024

"""
Abstraction des providers LLM utilises pour la selection d'articles.

Pattern aligne sur scripts/embed.py : un Protocol + une classe par provider.
Les credentials sont lus depuis l'environnement (.env), jamais codes en dur.

Usage rapide :
    provider = build_provider()                      # via config + env
    result   = provider.select(articles, ["insolite", "utile"], count_per_cat=1)
    # result == {"insolite": [{"cid": "...", "reason": "..."}], "utile": [...]}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import config  # scripts/ est sur sys.path quand on lance python scripts/xxx.py


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class CandidateArticle:
    cid: str
    code_juridique: str | None
    numero_article: str | None
    raw_text: str


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def select(
        self,
        articles: list[CandidateArticle],
        categories: list[str],
        count_per_category: int,
    ) -> dict[str, list[dict[str, str]]]: ...


# ---------------------------------------------------------------------------
# Prompt commun
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Tu es un juriste francais charge de selectionner des articles de loi remarquables "
    "parmi une liste de candidats. Pour chaque categorie demandee, tu dois choisir "
    "exactement N articles distincts qui s'y rattachent le mieux. Tu reponds uniquement "
    "en JSON strict, sans aucun texte hors du bloc JSON."
)

CATEGORY_HINTS = {
    "insolite": (
        "article surprenant, etrange, vestige historique, formulation cocasse, "
        "regle peu connue ou paradoxale"
    ),
    "utile": (
        "article a fort impact pratique sur la vie quotidienne, droits/devoirs "
        "souvent meconnus du grand public et qu'il vaut mieux connaitre"
    ),
}


def _build_user_prompt(
    articles: list[CandidateArticle],
    categories: list[str],
    count_per_category: int,
) -> str:
    cat_lines: list[str] = []
    for cat in categories:
        hint = CATEGORY_HINTS.get(cat, cat)
        cat_lines.append(f'- "{cat}" : {hint}')
    cats_block = "\n".join(cat_lines)

    art_lines: list[str] = []
    for art in articles:
        head = f"[{art.cid}] {art.code_juridique or '?'} art. {art.numero_article or '?'}"
        art_lines.append(f"{head}\n{art.raw_text}")
    arts_block = "\n\n---\n\n".join(art_lines)

    schema_lines = [f'  "{c}": [{{"cid": "<article_cid>", "reason": "<phrase courte>"}}]' for c in categories]
    schema = "{\n" + ",\n".join(schema_lines) + "\n}"

    return (
        f"Voici {len(articles)} articles candidats. Choisis exactement "
        f"{count_per_category} article(s) par categorie. Les categories sont :\n"
        f"{cats_block}\n\n"
        f"Reponds avec ce schema JSON exact (aucun texte hors JSON) :\n"
        f"{schema}\n\n"
        f"Le `cid` doit etre repris a l'identique parmi ceux fournis. "
        f"Articles candidats :\n\n"
        f"{arts_block}"
    )


def _parse_json(text: str) -> dict:
    """Tolerant : extrait le premier objet JSON du texte si du bruit l'entoure."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"Pas de JSON exploitable dans la reponse LLM : {text[:200]!r}")
        return json.loads(text[start : end + 1])


def _validate(
    parsed: dict,
    categories: list[str],
    count_per_category: int,
    candidate_cids: set[str],
) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    for cat in categories:
        items = parsed.get(cat)
        if not isinstance(items, list) or len(items) != count_per_category:
            raise ValueError(
                f"Categorie '{cat}' : attendu {count_per_category} articles, "
                f"recu {len(items) if isinstance(items, list) else 'autre chose'}."
            )
        validated: list[dict[str, str]] = []
        for item in items:
            cid = (item or {}).get("cid")
            reason = (item or {}).get("reason", "")
            if not isinstance(cid, str) or cid not in candidate_cids:
                raise ValueError(f"Categorie '{cat}' : cid invalide ou hors candidats : {cid!r}")
            validated.append({"cid": cid, "reason": str(reason)})
        out[cat] = validated
    return out


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY manquant dans .env")
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError("pip install anthropic") from exc

        self._model = os.getenv("LEX_LLM_MODEL", config.LLM_MODEL_ANTHROPIC)
        self._client = anthropic.Anthropic(api_key=api_key, timeout=config.LLM_TIMEOUT)
        print(f"LLM      : Anthropic — {self._model}")

    def select(
        self,
        articles: list[CandidateArticle],
        categories: list[str],
        count_per_category: int,
    ) -> dict[str, list[dict[str, str]]]:
        user = _build_user_prompt(articles, categories, count_per_category)
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=config.LLM_MAX_TOKENS,
            temperature=config.LLM_TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user},
                {"role": "assistant", "content": "{"},  # prefill JSON
            ],
        )
        text = "{" + "".join(
            block.text for block in msg.content if getattr(block, "type", None) == "text"
        )
        parsed = _parse_json(text)
        return _validate(parsed, categories, count_per_category, {a.cid for a in articles})


class OpenAIProvider:
    name = "openai"

    def __init__(self) -> None:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise ValueError("OPENAI_API_KEY manquant dans .env")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError("pip install openai") from exc

        self._model = os.getenv("LEX_LLM_MODEL", config.LLM_MODEL_OPENAI)
        self._client = OpenAI(api_key=api_key, timeout=config.LLM_TIMEOUT)
        print(f"LLM      : OpenAI — {self._model}")

    def select(
        self,
        articles: list[CandidateArticle],
        categories: list[str],
        count_per_category: int,
    ) -> dict[str, list[dict[str, str]]]:
        user = _build_user_prompt(articles, categories, count_per_category)
        resp = self._client.chat.completions.create(
            model=self._model,
            max_tokens=config.LLM_MAX_TOKENS,
            temperature=config.LLM_TEMPERATURE,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        )
        text = resp.choices[0].message.content or ""
        parsed = _parse_json(text)
        return _validate(parsed, categories, count_per_category, {a.cid for a in articles})


PROVIDERS: dict[str, type] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def build_provider(name: str | None = None) -> LLMProvider:
    """Construit un provider depuis le nom donne, ou depuis l'env / config."""
    chosen = name or os.getenv("LEX_LLM_PROVIDER", config.LLM_PROVIDER)
    cls = PROVIDERS.get(chosen)
    if cls is None:
        raise ValueError(
            f"Provider LLM inconnu : '{chosen}'. Valeurs acceptees : {list(PROVIDERS)}"
        )
    return cls()

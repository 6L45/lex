CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS legal_articles (
    -- 1. Identifiants et Synchro
    article_cid VARCHAR(255) PRIMARY KEY,
    content_hash VARCHAR(64),
    last_sync_date TIMESTAMP,

    -- 2. La donnee pour le RAG
    raw_text TEXT NOT NULL,
    embedding vector(1536),

    -- 3. Les Metadonnees pour tes Agents (Filtrage)
    code_juridique VARCHAR(255),
    numero_article VARCHAR(255),
    hierarchie JSONB,
    etat VARCHAR(50)
);

CREATE INDEX IF NOT EXISTS idx_legal_articles_code_juridique ON legal_articles (code_juridique);
CREATE INDEX IF NOT EXISTS idx_legal_articles_numero_article ON legal_articles (numero_article);
CREATE INDEX IF NOT EXISTS idx_legal_articles_etat ON legal_articles (etat);

-- Index partiel pour accelerer la requete "WHERE embedding IS NULL ORDER BY last_sync_date DESC"
-- utilisee par build_rag_index.py. Ne couvre que les lignes sans embedding.
CREATE INDEX IF NOT EXISTS idx_legal_articles_no_embedding
ON legal_articles (last_sync_date DESC)
WHERE embedding IS NULL;

-- Index IVFFLAT pour la recherche vectorielle.
-- Le nombre de lists peut etre ajuste selon la volumetrie.
CREATE INDEX IF NOT EXISTS idx_legal_articles_embedding_ivfflat
ON legal_articles USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);

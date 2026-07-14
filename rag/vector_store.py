"""
Vector Store module — FAISS-based vector index for similarity search.
"""

import json
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import faiss
except ImportError:
    faiss = None
    logging.warning("FAISS not installed. Install with: pip install faiss-cpu")

from ai_chatbot.config import RAG_TOP_K, RAG_MIN_RELEVANCE
from tenancy import get_current_tenant, tenant_faiss_dir

logger = logging.getLogger(__name__)


class VectorStore:
    """
    A FAISS-backed vector store for storing and searching document chunk embeddings.
    
    The store maintains:
    - A FAISS index for fast similarity search
    - A metadata list mapping each vector position to chunk information
    """
    
    def __init__(self):
        self.index: Optional[object] = None
        self.metadata: list[dict] = []  # Maps index position -> chunk info
        self.dimension: int = 0
    
    def build_index(self, embeddings: np.ndarray, metadata: list[dict]):
        """
        Build a new FAISS index from embeddings and metadata.
        
        Args:
            embeddings: numpy array of shape (n, dim) with float32 embeddings.
            metadata: list of dicts with chunk info (entry_id, category, title, text, chunk_id).
        """
        if faiss is None:
            raise RuntimeError("FAISS is not installed. Run: pip install faiss-cpu")
        
        if len(embeddings) == 0:
            logger.warning("No embeddings provided. Creating empty index.")
            from ai_chatbot.rag.embeddings import LOCAL_EMBEDDING_DIM
            self.dimension = LOCAL_EMBEDDING_DIM
            self.index = faiss.IndexFlatIP(self.dimension)
            self.metadata = []
            return

        # ולידציה — מספר embeddings חייב להתאים למספר metadata (E7)
        if len(metadata) != len(embeddings):
            raise ValueError(
                f"Embeddings count ({len(embeddings)}) != metadata count ({len(metadata)})"
            )

        self.dimension = embeddings.shape[1]
        self.metadata = metadata

        # נרמול ל-cosine similarity — על עותק כדי לא לשנות את המערך המקורי (E3)
        normed = embeddings.copy()
        faiss.normalize_L2(normed)

        # Use IndexFlatIP (inner product) on normalized vectors = cosine similarity
        self.index = faiss.IndexFlatIP(self.dimension)
        self.index.add(normed)
        
        logger.info(
            "Built FAISS index with %s vectors of dimension %s",
            self.index.ntotal,
            self.dimension,
        )
    
    def search(self, query_embedding: np.ndarray, top_k: int = None) -> list[dict]:
        """
        Search for the most similar chunks to a query embedding.
        
        Args:
            query_embedding: numpy array of shape (dim,) with the query embedding.
            top_k: Number of results to return (defaults to config RAG_TOP_K).
        
        Returns:
            List of dicts with chunk info and similarity score.
        """
        if self.index is None or self.index.ntotal == 0:
            logger.warning("Index is empty. No results.")
            return []
        
        if top_k is None:
            top_k = RAG_TOP_K
        
        # ולידציה — dimension של ה-query חייב להתאים לאינדקס (E8)
        query = query_embedding.reshape(1, -1).astype(np.float32)
        if query.shape[1] != self.dimension:
            raise ValueError(
                f"Query embedding dimension ({query.shape[1]}) != index dimension ({self.dimension})"
            )
        faiss.normalize_L2(query)
        
        # Search
        k = min(top_k, self.index.ntotal)
        scores, indices = self.index.search(query, k)
        
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            if score < RAG_MIN_RELEVANCE:
                continue
            
            result = {
                **self.metadata[idx],
                "score": float(score)
            }
            results.append(result)
        
        return results
    
    def save(self, path: str = None):
        """Save the index and metadata to disk (לתיקיית ה-tenant הנוכחי)."""
        if self.index is None:
            logger.warning("No index to save.")
            return

        save_path = Path(path or tenant_faiss_dir())
        save_path.mkdir(parents=True, exist_ok=True)
        
        faiss.write_index(self.index, str(save_path / "index.faiss"))
        
        with open(save_path / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, ensure_ascii=False)
        
        with open(save_path / "config.json", "w", encoding="utf-8") as f:
            json.dump({"dimension": self.dimension}, f, ensure_ascii=False)
        
        logger.info("Saved FAISS index to %s", save_path)
    
    def load(self, path: str = None) -> bool:
        """
        Load the index and metadata from disk.
        
        Returns:
            True if loaded successfully, False otherwise.
        """
        load_path = Path(path or tenant_faiss_dir())
        
        index_file = load_path / "index.faiss"
        metadata_json_file = load_path / "metadata.json"
        legacy_metadata_file = load_path / "metadata.pkl"
        config_file = load_path / "config.json"
        
        if not all(f.exists() for f in [index_file, config_file]):
            logger.info("No saved index found.")
            return False
        if not metadata_json_file.exists():
            if legacy_metadata_file.exists():
                logger.warning(
                    "Legacy metadata.pkl found but loading pickle is disabled for security. "
                    "Please rebuild the RAG index to regenerate metadata.json."
                )
            else:
                logger.info("No saved metadata found.")
            return False
        
        try:
            self.index = faiss.read_index(str(index_file))
            
            with open(metadata_json_file, "r", encoding="utf-8") as f:
                self.metadata = json.load(f)
            
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
                self.dimension = config["dimension"]
            
            logger.info("Loaded FAISS index with %s vectors", self.index.ntotal)
            return True
        except Exception as e:
            logger.error("Failed to load index: %s", e)
            return False


# ─── Registry פר-tenant (multi-tenant שלב 2) ────────────────────────────
# במקום singleton יחיד — מילון tenant→VectorStore עם LRU: אינדקסים "חמים"
# נשארים בזיכרון, והישן ביותר מפונה כשעוברים את התקרה. ה-KB של עסק קטן
# הוא עשרות רשומות, כך שגם עשרות אינדקסים חמים הם זיכרון זניח — התקרה
# היא בעיקר קו הגנה מפני גדילה בלתי מוגבלת.
_MAX_HOT_STORES = 32
_stores: OrderedDict[str, VectorStore] = OrderedDict()
_stores_lock = threading.Lock()


def get_vector_store() -> VectorStore:
    """ה-VectorStore של ה-tenant הנוכחי (נטען עצלה, LRU בין tenants)."""
    tenant = get_current_tenant()
    with _stores_lock:
        store = _stores.get(tenant)
        if store is not None:
            _stores.move_to_end(tenant)
            return store
    # טעינה מחוץ ל-lock — קריאת דיסק של FAISS יכולה להיות איטית ואסור
    # לחסום איתה tenants אחרים. במקרה קצה של מרוץ, שני threads יטענו
    # ואחד ידרוס — זהה סמנטית (אותו קובץ), רק עבודה כפולה חד-פעמית.
    store = VectorStore()
    store.load()
    with _stores_lock:
        _stores[tenant] = store
        _stores.move_to_end(tenant)
        while len(_stores) > _MAX_HOT_STORES:
            evicted, _ = _stores.popitem(last=False)
            logger.info("vector store evicted from memory (LRU): tenant=%s", evicted)
    return store


def reset_vector_store(all_tenants: bool = False):
    """איפוס ה-store של ה-tenant הנוכחי (או כולם) — טעינה מחדש בשימוש הבא."""
    with _stores_lock:
        if all_tenants:
            _stores.clear()
        else:
            _stores.pop(get_current_tenant(), None)

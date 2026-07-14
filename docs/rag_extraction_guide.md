# מדריך חילוץ מימוש RAG / מאגר ידע לפרויקט אחר

מסמך זה מתעד את המימוש של מאגר הידע (Knowledge Base) ו-RAG (Retrieval-Augmented Generation) שבריפו הנוכחי, באופן עצמאי, כך שניתן יהיה להעתיק אותו לפרויקט אחר.

המסמך כולל את **קוד הליבה במלואו** (chunker, embeddings, vector store, engine), ותקצירי עזר (DB helpers, prompt building, admin endpoints). הוצאו כל התלויות בפיצ'רים שלא קשורים ל-RAG (Telegram/WhatsApp, follow-up questions, handoff, business hours וכו').

---

## 1. סקירה כללית

הצינור עובד בשלבים הבאים:

```
טקסט מקור (KB entry)
    │
    ▼  [Chunker]   חלוקה היררכית: פסקה → משפט → מילה (עם tiktoken)
chunks
    │
    ▼  [Embeddings]   OpenAI Embeddings API (batch של 100, fallback מקומי)
vectors
    │
    ▼  [Vector Store]   FAISS IndexFlatIP על וקטורים מנורמלים (= cosine similarity)
index על דיסק + metadata
    │
    ▼  שאלה של משתמש  →  embedding שאלה  →  FAISS.search(top_k)  →  chunks רלוונטיים
    │
    ▼  [LLM Prompt]   הזרקת ה-chunks כקונטקסט לתוך system prompt + שאלת המשתמש
תשובה
```

**רכיבים מרכזיים:**

| תפקיד | טכנולוגיה | מקור בפרויקט |
|---|---|---|
| Embeddings | `openai` SDK + מודל `text-embedding-3-small` (מימד 1536) | `rag/embeddings.py` |
| Tokenization | `tiktoken` (חשוב במיוחד לעברית — כל תו ≈ 2 tokens) | `rag/chunker.py` |
| Vector index | `faiss-cpu` — `IndexFlatIP` | `rag/vector_store.py` |
| Storage | SQLite — שתי טבלאות (`kb_entries`, `kb_chunks`) | `database.py` |
| Persistence של האינדקס | קבצים: `index.faiss`, `metadata.json`, `config.json` | `FAISS_INDEX_PATH` |
| Orchestration | retrieve / rebuild incremental / cache | `rag/engine.py` |

---

## 2. תלויות (`requirements.txt`)

```
openai          # SDK לקריאות embeddings + chat completions
tiktoken        # ספירת tokens מדויקת (חשוב לעברית)
faiss-cpu       # אינדקס וקטורי
numpy           # מערכים נומריים
```

**הערה לפרודקשן:** עבור מאות אלפי וקטורים, FAISS תומך גם באינדקסים יעילים יותר (`IndexIVFFlat`, `IndexHNSWFlat`). `IndexFlatIP` מבצע חיפוש exhaustive — מספיק עד אלפי וקטורים בודדים.

---

## 3. משתני סביבה (Configuration)

מהקובץ `config.py` — כל הערכים שרלוונטיים ל-RAG:

```python
import os
from pathlib import Path

# נתיבים
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "chatbot.db"))).resolve()
FAISS_INDEX_PATH = Path(os.getenv("FAISS_INDEX_PATH", str(DATA_DIR / "faiss_index"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
FAISS_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)

# מודלים
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
# OPENAI_BASE_URL — אופציונלי, לחיבור ל-Google Gemini / Azure / ספק תואם OpenAI

# פרמטרים של RAG
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "10"))               # כמה chunks להחזיר לכל שאילתה
RAG_MIN_RELEVANCE = float(os.getenv("RAG_MIN_RELEVANCE", "0.3"))  # סף similarity מינימלי (cosine)
CHUNK_MAX_TOKENS = int(os.getenv("CHUNK_MAX_TOKENS", "300"))      # גודל chunk מקסימלי
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))         # max_tokens של תשובת ה-LLM
```

**סודות:**
- `OPENAI_API_KEY` — נטען אוטומטית מהסביבה ע"י ה-SDK של OpenAI.

---

## 4. סכמת בסיס הנתונים

שתי טבלאות בלבד:

```sql
-- רשומות מאגר ידע — המקור (טקסט מלא של מסמך/פיסקת ידע)
CREATE TABLE IF NOT EXISTS kb_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    TEXT DEFAULT '{}',     -- JSON חופשי
    is_active   INTEGER DEFAULT 1,     -- soft delete flag
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- chunks + embeddings (BLOB של numpy float32)
CREATE TABLE IF NOT EXISTS kb_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_text  TEXT NOT NULL,
    embedding   BLOB,                  -- np.ndarray.tobytes() (float32)
    created_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (entry_id) REFERENCES kb_entries(id) ON DELETE CASCADE
);

-- אינדקסים
CREATE INDEX IF NOT EXISTS idx_kb_entries_category ON kb_entries(category);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_entry ON kb_chunks(entry_id);
```

**הערות:**
- `ON DELETE CASCADE` — מחיקת רשומה מ-`kb_entries` מוחקת אוטומטית את כל ה-chunks המקושרים.
- `embedding` נשמר בינארית: `np.ndarray.tobytes()` (little-endian float32). שחזור: `np.frombuffer(blob, dtype=np.float32)`.
- ה-embeddings ב-DB **כבר מנורמלים** (L2) — תואם לאינדקס FAISS.
- אם משתמשים ב-Postgres: ניתן להחליף `BLOB` ב-`BYTEA` (או אפילו `vector` אם משתמשים ב-pgvector).

---

## 5. קוד הליבה

### 5.1 — Chunker (`rag/chunker.py`)

חלוקה היררכית: מנסה לשמור על גבולות פסקה, נופל למשפטים, ואז למילים.

```python
"""
Chunker — splits text into chunks suitable for embedding and retrieval.

האלגוריתם:
1. אם הטקסט כולו <= max_tokens — מחזיר chunk יחיד.
2. מפצל לפסקאות (\n\s*\n) — מצרף פסקאות עד שמתקרב לגבול.
3. אם פסקה בודדת חורגת — מפצל למשפטים.
4. אם משפט בודד חורג — מפצל למילים.
5. אם מילה בודדת חורגת — נשמרת ב-chunk נפרד (קצה קיצוני).

הערה לעברית: tiktoken מחזיר ספירה מדויקת. ה-fallback ההיוריסטי (אורך/3)
שמרני כי בעברית יש פחות תווים לכל token מאשר באנגלית.
"""

import re

try:
    import tiktoken
except Exception:
    tiktoken = None

# קונפיג: מודל ה-LLM שבשבילו סופרים tokens, וגודל chunk מקסימלי
from config import CHUNK_MAX_TOKENS, OPENAI_MODEL

_ENCODING = None  # None=unknown, False=unavailable, else=tiktoken.Encoding


def _get_encoding():
    global _ENCODING
    if _ENCODING is False:
        return None
    if _ENCODING is not None:
        return _ENCODING
    if tiktoken is None:
        _ENCODING = False
        return None
    try:
        _ENCODING = tiktoken.encoding_for_model(OPENAI_MODEL)
        return _ENCODING
    except Exception:
        try:
            _ENCODING = tiktoken.get_encoding("cl100k_base")
            return _ENCODING
        except Exception:
            _ENCODING = False
            return None


def estimate_tokens(text: str) -> int:
    """אומדן מספר tokens. מעדיף tiktoken (מדויק לעברית); נופל להיוריסטיקה."""
    if not text:
        return 0
    enc = _get_encoding()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    # היוריסטיקה שמרנית: בעברית כל ~3 תווים = token אחד
    return max(1, len(text) // 3)


def chunk_text(text: str, max_tokens: int = None) -> list[str]:
    """מפצל טקסט ל-chunks באורך עד max_tokens."""
    if max_tokens is None:
        max_tokens = CHUNK_MAX_TOKENS

    if estimate_tokens(text) <= max_tokens:
        return [text.strip()] if text.strip() else []

    paragraphs = re.split(r'\n\s*\n', text)
    chunks = []
    current_chunk = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        candidate = para if not current_chunk else f"{current_chunk}\n\n{para}"
        if estimate_tokens(candidate) <= max_tokens:
            current_chunk = candidate
            continue

        if current_chunk:
            chunks.append(current_chunk)
            current_chunk = ""

        if estimate_tokens(para) <= max_tokens:
            current_chunk = para
            continue

        # פסקה ארוכה מדי: פיצול למשפטים
        sentences = re.split(r"(?<=[.!?])\s+", para)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            candidate = sentence if not current_chunk else f"{current_chunk} {sentence}"
            if estimate_tokens(candidate) <= max_tokens:
                current_chunk = candidate
                continue

            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""

            if estimate_tokens(sentence) <= max_tokens:
                current_chunk = sentence
                continue

            # משפט ארוך מדי: פיצול למילים
            words = sentence.split()
            for word in words:
                word = word.strip()
                if not word:
                    continue

                if estimate_tokens(word) > max_tokens:
                    if current_chunk:
                        chunks.append(current_chunk)
                        current_chunk = ""
                    chunks.append(word)
                    continue

                candidate = word if not current_chunk else f"{current_chunk} {word}"
                if estimate_tokens(candidate) <= max_tokens:
                    current_chunk = candidate
                else:
                    if current_chunk:
                        chunks.append(current_chunk)
                    current_chunk = word

    if current_chunk:
        chunks.append(current_chunk)

    return [c.strip() for c in chunks if c.strip()]


def create_chunks_for_entry(entry_id: int, category: str, title: str, content: str) -> list[dict]:
    """
    יוצר chunks לרשומת KB, עם הקדמת מטא-דאטה לכל chunk.
    הקידומת `[category — title]` משפרת את ה-embedding כי היא נותנת לקונטקסט
    הסמנטי של ה-chunk עוגן.
    """
    raw_chunks = chunk_text(content)
    result = []
    for i, chunk in enumerate(raw_chunks):
        contextualized = f"[{category} — {title}]\n{chunk}"
        result.append({
            "index": i,
            "text": contextualized,
            "entry_id": entry_id,
            "category": category,
            "title": title,
        })
    return result
```

**טיפ קריטי:** הקידומת `[category — title]` שמשתרשרת לכל chunk היא קריטית לאיכות. ה-embedding "מתעגן" סמנטית לקטגוריה ולכותרת, ולכן שאילתות שמזכירות אותן מקבלות ציון גבוה יותר.

---

### 5.2 — Embeddings (`rag/embeddings.py`)

```python
"""
Embeddings — generation via OpenAI-compatible API + local fallback for testing.
"""

import hashlib
import logging
import re
import numpy as np

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from config import EMBEDDING_MODEL

logger = logging.getLogger(__name__)

LOCAL_EMBEDDING_DIM = 1536  # תואם text-embedding-3-small
_client = None
_warned_fallback = False
_API_KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{10,}")


def _get_client():
    global _client
    if _client is None:
        if OpenAI is None:
            raise RuntimeError("openai package not installed")
        import os
        base_url = os.getenv("OPENAI_BASE_URL")
        _client = OpenAI(base_url=base_url) if base_url else OpenAI()
    return _client


def _sanitize_error(err: Exception) -> str:
    """מסיר API keys משגיאות לפני לוג — חובה לאבטחה."""
    return _API_KEY_RE.sub("sk-***REDACTED***", str(err))


def _local_embedding(text: str) -> np.ndarray:
    """
    Fallback hash-based deterministic — לטסטים בלבד.
    זה לא embedding סמנטי, רק וקטור עקבי לכל טקסט.
    """
    global _warned_fallback
    if not _warned_fallback:
        logger.warning("FALLBACK EMBEDDINGS ACTIVE — חיפוש לא יהיה סמנטי, רק לטסטים")
        _warned_fallback = True

    text_bytes = text.encode("utf-8")
    vector = []
    for i in range(LOCAL_EMBEDDING_DIM // 16 + 1):
        h = hashlib.md5(text_bytes + i.to_bytes(4, "big")).digest()
        for byte in h:
            vector.append((byte / 255.0) * 2 - 1)
    vec = np.array(vector[:LOCAL_EMBEDDING_DIM], dtype=np.float32)
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def get_embedding(text: str) -> np.ndarray:
    """Embedding בודד. נופל ל-fallback אם ה-API נכשל."""
    text = text.replace("\n", " ").strip() or "empty"
    try:
        response = _get_client().embeddings.create(input=[text], model=EMBEDDING_MODEL)
        return np.array(response.data[0].embedding, dtype=np.float32)
    except Exception as e:
        logger.warning("OpenAI embedding API failed: %s. Fallback.", _sanitize_error(e))
        return _local_embedding(text)


def get_embeddings_batch(texts: list[str]) -> np.ndarray:
    """Embeddings בקבוצות של 100 — חוסך קריאות API משמעותית בעת rebuild."""
    cleaned = [t.replace("\n", " ").strip() or "empty" for t in texts]
    try:
        all_embeddings = []
        batch_size = 100
        for i in range(0, len(cleaned), batch_size):
            batch = cleaned[i:i + batch_size]
            response = _get_client().embeddings.create(input=batch, model=EMBEDDING_MODEL)
            all_embeddings.extend([item.embedding for item in response.data])
        return np.array(all_embeddings, dtype=np.float32)
    except Exception as e:
        logger.warning("Batch embedding API failed: %s. Fallback.", _sanitize_error(e))
        return np.array([_local_embedding(t) for t in cleaned], dtype=np.float32)
```

**נקודות מפתח:**
- **batching של 100** — OpenAI מקבל עד 2048 inputs בקריאה אחת אבל 100 הוא איזון טוב בין latency ל-rate limits.
- **sanitization של שגיאות** — מונע דליפת API keys ללוגים. חובה.
- **fallback מקומי** — מאפשר טסטים אופליין; **לא לשימוש בפרודקשן**.
- **`OPENAI_BASE_URL`** — מאפשר חיבור לספקים תואמים (Gemini via OpenAI-compatible endpoint, Azure OpenAI וכו').

---

### 5.3 — Vector Store (`rag/vector_store.py`)

```python
"""
Vector Store — FAISS-backed similarity search.
"""

import json
import logging
from pathlib import Path
from typing import Optional
import numpy as np

try:
    import faiss
except ImportError:
    faiss = None

from config import FAISS_INDEX_PATH, RAG_TOP_K, RAG_MIN_RELEVANCE

logger = logging.getLogger(__name__)

LOCAL_EMBEDDING_DIM = 1536


class VectorStore:
    """
    FAISS index + metadata רשימה מקבילה.
    metadata[i] = מידע על ה-chunk שב-position i באינדקס.
    """

    def __init__(self):
        self.index: Optional[object] = None
        self.metadata: list[dict] = []
        self.dimension: int = 0

    def build_index(self, embeddings: np.ndarray, metadata: list[dict]):
        """בונה אינדקס חדש מ-embeddings + metadata."""
        if faiss is None:
            raise RuntimeError("faiss-cpu not installed")

        if len(embeddings) == 0:
            self.dimension = LOCAL_EMBEDDING_DIM
            self.index = faiss.IndexFlatIP(self.dimension)
            self.metadata = []
            return

        if len(metadata) != len(embeddings):
            raise ValueError(
                f"embeddings ({len(embeddings)}) != metadata ({len(metadata)})"
            )

        self.dimension = embeddings.shape[1]
        self.metadata = metadata

        # נרמול L2 על עותק — שומר על ה-array המקורי ללא שינוי
        normed = embeddings.copy()
        faiss.normalize_L2(normed)

        # IndexFlatIP על וקטורים מנורמלים = cosine similarity
        self.index = faiss.IndexFlatIP(self.dimension)
        self.index.add(normed)
        logger.info("Built FAISS index: %d vectors, dim=%d", self.index.ntotal, self.dimension)

    def search(self, query_embedding: np.ndarray, top_k: int = None) -> list[dict]:
        """חיפוש top-k. מחזיר רק תוצאות מעל RAG_MIN_RELEVANCE."""
        if self.index is None or self.index.ntotal == 0:
            return []

        if top_k is None:
            top_k = RAG_TOP_K

        query = query_embedding.reshape(1, -1).astype(np.float32)
        if query.shape[1] != self.dimension:
            raise ValueError(
                f"query dim ({query.shape[1]}) != index dim ({self.dimension})"
            )

        faiss.normalize_L2(query)
        k = min(top_k, self.index.ntotal)
        scores, indices = self.index.search(query, k)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or score < RAG_MIN_RELEVANCE:
                continue
            results.append({**self.metadata[idx], "score": float(score)})
        return results

    def save(self, path: str = None):
        if self.index is None:
            return
        save_path = Path(path or FAISS_INDEX_PATH)
        save_path.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(save_path / "index.faiss"))
        with open(save_path / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, ensure_ascii=False)
        with open(save_path / "config.json", "w", encoding="utf-8") as f:
            json.dump({"dimension": self.dimension}, f, ensure_ascii=False)

    def load(self, path: str = None) -> bool:
        load_path = Path(path or FAISS_INDEX_PATH)
        index_file = load_path / "index.faiss"
        metadata_file = load_path / "metadata.json"
        config_file = load_path / "config.json"

        if not all(f.exists() for f in [index_file, metadata_file, config_file]):
            return False

        try:
            self.index = faiss.read_index(str(index_file))
            with open(metadata_file, "r", encoding="utf-8") as f:
                self.metadata = json.load(f)
            with open(config_file, "r", encoding="utf-8") as f:
                self.dimension = json.load(f)["dimension"]
            return True
        except Exception as e:
            logger.error("Failed to load index: %s", e)
            return False


# Singleton
_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore()
        _store.load()
    return _store


def reset_vector_store():
    global _store
    _store = None
```

**נקודות מפתח:**
- **`IndexFlatIP` + normalize_L2** — הדרך הסטנדרטית לקבל cosine similarity ב-FAISS. אינדקס "flat" מבצע חיפוש exhaustive — הכי מדויק; לאלפי וקטורים זה מהיר מספיק. למיליונים — לעבור ל-`IndexIVFFlat` או `IndexHNSWFlat`.
- **metadata נשמר ב-JSON, לא pickle** — בכוונה. pickle מסוכן אבטחתית (RCE על קבצים מזיקים). JSON בטוח לחלוטין.
- **שמירת `dimension`** — מאפשר לזהות אי-התאמה אם מודל ה-embedding משתנה.
- **חיפוש מחזיר רק תוצאות מעל הסף** (`RAG_MIN_RELEVANCE`) — מונע "הזיות" של ה-LLM על קונטקסט חלש.

---

### 5.4 — RAG Engine (`rag/engine.py`)

זה הקובץ עם הלוגיקה הכי מתוחכמת. כולל:
- **rebuild אינקרמנטלי** — בונה embeddings מחדש רק לרשומות שהשתנו (חיסכון משמעותי ב-API calls).
- **stale flag** מסונכרן בין תהליכים (fcntl).
- **query cache** עם TTL.
- **rebuild אוטומטי** אם המימד של המודל השתנה.

```python
"""
RAG Engine — orchestrates retrieve + incremental index rebuild.
"""

import logging
import threading
import time
from pathlib import Path
from contextlib import contextmanager
import numpy as np

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # Windows — נופל ל-no-op lock

import database as db
from config import FAISS_INDEX_PATH, RAG_TOP_K
from rag.chunker import create_chunks_for_entry
from rag.embeddings import get_embedding, get_embeddings_batch
from rag.vector_store import get_vector_store, reset_vector_store

logger = logging.getLogger(__name__)

_INDEX_STALE_FLAG: Path = FAISS_INDEX_PATH / ".stale"
_INDEX_STATE_LOCK_FILE: Path = FAISS_INDEX_PATH / ".index_state.lock"
_REBUILD_LOCK = threading.RLock()

# Query cache — חוסך embedding+search לשאלות זהות
_QUERY_CACHE_TTL = 300
_QUERY_CACHE_MAX_SIZE = 256
_query_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}
_query_cache_lock = threading.Lock()


# ─── Stale flag (סנכרון בין תהליכים) ────────────────────────────────────────

@contextmanager
def _index_state_lock():
    """Cross-process lock על קבצי מצב האינדקס (fcntl על Unix)."""
    FAISS_INDEX_PATH.mkdir(parents=True, exist_ok=True)
    f = _INDEX_STATE_LOCK_FILE.open("a+", encoding="utf-8")
    try:
        if _fcntl:
            try:
                _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
            except OSError:
                pass
        yield
    finally:
        if _fcntl:
            try:
                _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def _stale_token() -> int | None:
    try:
        return _INDEX_STALE_FLAG.stat().st_mtime_ns
    except (FileNotFoundError, OSError):
        return None


def _maybe_clear_stale(start_token: int | None) -> None:
    """מנקה את ה-stale flag רק אם לא נגעו בו במהלך ה-rebuild."""
    if start_token is None:
        return
    with _index_state_lock():
        if _stale_token() == start_token:
            try:
                _INDEX_STALE_FLAG.unlink()
            except FileNotFoundError:
                pass


def mark_index_stale() -> None:
    """לקרוא אחרי כל שינוי ב-KB (add/update/delete) — מסמן ש-rebuild נחוץ."""
    with _index_state_lock():
        FAISS_INDEX_PATH.mkdir(parents=True, exist_ok=True)
        _INDEX_STALE_FLAG.touch(exist_ok=True)


def is_index_stale() -> bool:
    with _index_state_lock():
        return _INDEX_STALE_FLAG.exists()


# ─── Rebuild אינקרמנטלי ──────────────────────────────────────────────────

def rebuild_index(force_full: bool = False):
    """
    בונה את אינדקס FAISS מחדש מכל ה-KB entries הפעילים.

    אופטימיזציה — incremental embedding: לכל entry, משווה את ה-chunks החדשים
    לאלו שכבר ב-DB. רק entries שהשתנו עוברים embedding מחדש. שאר ה-embeddings
    נטענים מ-DB.

    force_full=True מאלץ embedding מחדש לכולם (שימושי כשמודל ה-embedding השתנה).
    """
    with _REBUILD_LOCK:
        logger.info("Rebuilding RAG index...")
        with _index_state_lock():
            start_stale_token = _stale_token()

        entries = db.get_all_kb_entries(active_only=True)
        if not entries:
            store = get_vector_store()
            store.build_index(np.array([]), [])
            store.save()
            _maybe_clear_stale(start_stale_token)
            return

        # שלב 1: יצירת chunks לכל ה-entries
        all_chunks = []
        chunks_by_entry: dict[int, list[dict]] = {}
        for entry in entries:
            chunks = create_chunks_for_entry(
                entry_id=entry["id"],
                category=entry["category"],
                title=entry["title"],
                content=entry["content"],
            )
            all_chunks.extend(chunks)
            chunks_by_entry[entry["id"]] = chunks

        if not all_chunks:
            store = get_vector_store()
            store.build_index(np.array([]), [])
            store.save()
            _maybe_clear_stale(start_stale_token)
            return

        # שלב 2: טעינת chunks קיימים מ-DB
        entry_ids = list(chunks_by_entry.keys())
        stored_chunks = db.get_chunks_for_entries(entry_ids)

        # שלב 2.5: בדיקת מימד — אם מודל ה-embedding השתנה, embeddings ישנים לא תקפים
        _force = force_full
        if not _force:
            sample = None
            for chunks_list in stored_chunks.values():
                for c in chunks_list:
                    if c.get("embedding"):
                        sample = c["embedding"]
                        break
                if sample is not None:
                    break
            if sample is not None:
                stored_dim = len(np.frombuffer(sample, dtype=np.float32))
                current_dim = get_embedding("test").shape[0]
                if stored_dim != current_dim:
                    logger.warning(
                        "Embedding dim changed (%d → %d). Forcing full re-embed.",
                        stored_dim, current_dim,
                    )
                    _force = True

        # שלב 3: זיהוי entries שהשתנו (השוואת טקסטים)
        changed_entry_ids: set[int] = set()
        unchanged_entry_ids: set[int] = set()
        if _force:
            changed_entry_ids = set(chunks_by_entry.keys())
        else:
            for eid, new_chunks in chunks_by_entry.items():
                old_chunks = stored_chunks.get(eid, [])
                new_texts = [c["text"] for c in new_chunks]
                old_texts = [c["chunk_text"] for c in old_chunks]
                if new_texts == old_texts and len(old_chunks) == len(new_chunks):
                    unchanged_entry_ids.add(eid)
                else:
                    changed_entry_ids.add(eid)

        logger.info(
            "Incremental: %d unchanged, %d need re-embedding",
            len(unchanged_entry_ids), len(changed_entry_ids),
        )

        # שלב 4: בנייה — שימוש חוזר ב-unchanged, יצירה חדשה ל-changed
        all_embeddings = []
        all_metadata = []
        entries_to_save: dict[int, list[dict]] = {}

        for chunk in all_chunks:
            all_metadata.append({
                "entry_id": chunk["entry_id"],
                "chunk_index": chunk["index"],
                "category": chunk["category"],
                "title": chunk["title"],
                "text": chunk["text"],
            })

        new_embed_indices: list[int] = []
        new_embed_texts: list[str] = []

        # מיפוי מהיר של chunks ישנים — O(1) lookup
        _old_chunk_map: dict[tuple[int, int], dict] = {}
        for eid, chunks_list in stored_chunks.items():
            for c in chunks_list:
                _old_chunk_map[(eid, c["chunk_index"])] = c

        for i, chunk in enumerate(all_chunks):
            eid = chunk["entry_id"]
            if eid in unchanged_entry_ids:
                old = _old_chunk_map.get((eid, chunk["index"]))
                if old and old["embedding"] and old["chunk_text"] == chunk["text"]:
                    emb = np.frombuffer(old["embedding"], dtype=np.float32).copy()
                    all_embeddings.append(emb)
                    continue
                # fallback אם משהו השתנה
                changed_entry_ids.add(eid)
                unchanged_entry_ids.discard(eid)

            all_embeddings.append(None)  # placeholder
            new_embed_indices.append(i)
            new_embed_texts.append(chunk["text"])

        # יצירת embeddings רק לחדשים
        if new_embed_texts:
            new_embeddings = get_embeddings_batch(new_embed_texts)
            for j, idx in enumerate(new_embed_indices):
                all_embeddings[idx] = new_embeddings[j]
            logger.info("Generated %d new (reused %d)",
                        len(new_embed_texts), len(all_chunks) - len(new_embed_texts))

        embeddings_array = np.array(all_embeddings, dtype=np.float32)

        # שלב 5: בניית האינדקס + שמירה לדיסק
        reset_vector_store()
        store = get_vector_store()
        store.build_index(embeddings_array, all_metadata)
        store.save()

        # נרמול ל-DB — שומרים מנורמלים לעקביות עם ה-index
        import faiss as _faiss
        _faiss.normalize_L2(embeddings_array)

        # שלב 6: שמירת chunks ל-DB רק לאלה שהשתנו
        for i, chunk in enumerate(all_chunks):
            eid = chunk["entry_id"]
            if eid in changed_entry_ids:
                entries_to_save.setdefault(eid, []).append({
                    "index": chunk["index"],
                    "text": chunk["text"],
                    "embedding": embeddings_array[i].tobytes(),
                })

        for entry_id, entry_chunks in entries_to_save.items():
            db.save_chunks(entry_id, entry_chunks)

        _maybe_clear_stale(start_stale_token)
        with _query_cache_lock:
            _query_cache.clear()
        logger.info("RAG index rebuild complete")


# ─── Retrieve (ה-API הציבורי לחיפוש) ────────────────────────────────────

def retrieve(query: str, top_k: int = None) -> list[dict]:
    """
    מחזיר את ה-chunks הכי רלוונטיים לשאילתה.
    כולל rebuild אוטומטי אם האינדקס stale או המימד לא תואם.
    """
    if is_index_stale():
        with _REBUILD_LOCK:
            if is_index_stale():
                try:
                    rebuild_index()
                except Exception:
                    logger.exception("Failed rebuilding stale index; using existing.")

    effective_top_k = top_k if top_k is not None else RAG_TOP_K
    cache_key = (query, effective_top_k)

    # בדיקת cache
    with _query_cache_lock:
        cached = _query_cache.get(cache_key)
        if cached:
            ts, results = cached
            if time.time() - ts < _QUERY_CACHE_TTL:
                return list(results)

    store = get_vector_store()
    if store.index is None or store.index.ntotal == 0:
        rebuild_index()
        store = get_vector_store()
        if store.index is None or store.index.ntotal == 0:
            return []

    query_embedding = get_embedding(query)

    try:
        results = store.search(query_embedding, top_k=effective_top_k)
    except ValueError as e:
        if "dim" in str(e):
            # מודל השתנה — rebuild אוטומטי
            reset_vector_store()
            rebuild_index()
            store = get_vector_store()
            if store.index is None or store.index.ntotal == 0:
                return []
            results = store.search(query_embedding, top_k=effective_top_k)
        else:
            raise

    # שמירה ב-cache עם הגבלת גודל
    with _query_cache_lock:
        _query_cache[cache_key] = (time.time(), list(results))
        if len(_query_cache) > _QUERY_CACHE_MAX_SIZE:
            oldest_key = min(_query_cache, key=lambda k: _query_cache[k][0])
            del _query_cache[oldest_key]

    return results


def format_context(chunks: list[dict]) -> str:
    """פורמט קונטקסט להזרקה ל-system prompt."""
    if not chunks:
        return "No relevant information found in the knowledge base."
    parts = []
    for i, chunk in enumerate(chunks, 1):
        source = f"{chunk['category']} — {chunk['title']}"
        parts.append(f"--- Context {i} (Source: {source}) ---\n{chunk['text']}")
    return "\n\n".join(parts)
```

**נקודות מפתח (לקחים מהפרויקט):**
- **rebuild אינקרמנטלי**: חיסכון של 95%+ ב-API calls כשמעדכנים entry בודד מתוך מאות. ההשוואה היא על הטקסט של ה-chunk עצמו (כולל הקידומת `[category — title]`).
- **stale flag** במקום rebuild סינכרוני אחרי שינוי: מאפשר עדכוני KB מהירים, ו-rebuild מתבצע lazy ב-retrieve הבא. ה-`fcntl` מסנכרן בין תהליכים (חשוב במיוחד אם יש כמה workers של flask/gunicorn).
- **rebuild אוטומטי על dimension mismatch**: כשמעבירים מ-`text-embedding-3-small` (1536) ל-`text-embedding-3-large` (3072) — האינדקס נבנה אוטומטית מחדש בקריאה הבאה ל-`retrieve`.
- **query cache עם TTL ו-max size**: מונע גם redundant work וגם memory leak.

---

## 6. שכבת DB — פונקציות העזר

מתוך `database.py`. שמירה ב-SQLite עם context manager. בפרויקט אחר אפשר להחליף בקלות ב-Postgres / MySQL / ORM.

```python
import json
import sqlite3
from contextlib import contextmanager
from typing import Optional
from config import DB_PATH


@contextmanager
def get_connection():
    """SQLite connection עם row_factory ו-commit אוטומטי. WAL mode מומלץ."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """יוצר טבלאות אם לא קיימות. לקרוא פעם אחת בעליית האפליקציה."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS kb_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT NOT NULL,
                title       TEXT NOT NULL,
                content     TEXT NOT NULL,
                metadata    TEXT DEFAULT '{}',
                is_active   INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS kb_chunks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id    INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text  TEXT NOT NULL,
                embedding   BLOB,
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (entry_id) REFERENCES kb_entries(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_kb_entries_category ON kb_entries(category);
            CREATE INDEX IF NOT EXISTS idx_kb_chunks_entry ON kb_chunks(entry_id);
        """)


# ─── KB Entries ──────────────────────────────────────────────────────────

def add_kb_entry(category: str, title: str, content: str, metadata: dict = None) -> int:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO kb_entries (category, title, content, metadata) VALUES (?, ?, ?, ?)",
            (category, title, content, json.dumps(metadata or {})),
        )
        return cur.lastrowid


def update_kb_entry(entry_id: int, category: str, title: str, content: str, metadata: dict = None):
    with get_connection() as conn:
        conn.execute(
            """UPDATE kb_entries
               SET category=?, title=?, content=?, metadata=?, updated_at=datetime('now')
               WHERE id=?""",
            (category, title, content, json.dumps(metadata or {}), entry_id),
        )


def delete_kb_entry(entry_id: int):
    """מוחק entry ו-CASCADE מוחק את ה-chunks."""
    with get_connection() as conn:
        conn.execute("DELETE FROM kb_entries WHERE id=?", (entry_id,))


def get_kb_entry(entry_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM kb_entries WHERE id=?", (entry_id,)).fetchone()
        return dict(row) if row else None


def get_all_kb_entries(category: str = None, active_only: bool = True) -> list[dict]:
    with get_connection() as conn:
        query = "SELECT * FROM kb_entries WHERE 1=1"
        params = []
        if active_only:
            query += " AND is_active=1"
        if category:
            query += " AND category=?"
            params.append(category)
        query += " ORDER BY category, title"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


# ─── Chunks ──────────────────────────────────────────────────────────────

def save_chunks(entry_id: int, chunks: list[dict]):
    """דורס chunks קיימים של entry. כל chunk הוא {index, text, embedding (bytes)}."""
    with get_connection() as conn:
        conn.execute("DELETE FROM kb_chunks WHERE entry_id=?", (entry_id,))
        conn.executemany(
            "INSERT INTO kb_chunks (entry_id, chunk_index, chunk_text, embedding) VALUES (?, ?, ?, ?)",
            [(entry_id, c["index"], c["text"], c.get("embedding")) for c in chunks],
        )


def get_chunks_for_entries(entry_ids: list[int]) -> dict[int, list[dict]]:
    """
    מחזיר chunks (עם embeddings) מקובצים לפי entry_id.
    מסנן רק chunks עם embedding לא-NULL — מתאים לשימוש חוזר ב-incremental rebuild.
    """
    if not entry_ids:
        return {}
    with get_connection() as conn:
        placeholders = ",".join("?" for _ in entry_ids)
        rows = conn.execute(
            f"""SELECT c.id, c.entry_id, c.chunk_index, c.chunk_text, c.embedding,
                       e.category, e.title
                FROM kb_chunks c
                JOIN kb_entries e ON c.entry_id = e.id
                WHERE c.entry_id IN ({placeholders}) AND c.embedding IS NOT NULL
                ORDER BY c.entry_id, c.chunk_index""",
            entry_ids,
        ).fetchall()
        result: dict[int, list[dict]] = {}
        for r in rows:
            d = dict(r)
            result.setdefault(d["entry_id"], []).append(d)
        return result
```

---

## 7. שילוב עם LLM — בניית prompt ושאילתה

זה התרשים הבסיסי, ללא הפיצ'רים הספציפיים לפרויקט. המפתח: הזרקת הקונטקסט שאוחזר ב-system prompt, עם הוראה מפורשת להסתמך **רק** עליו.

```python
"""
מינימליסטי — מקבל שאלה, מאחזר chunks, בונה prompt, קורא ל-LLM.
"""

from openai import OpenAI
from config import OPENAI_MODEL, LLM_MAX_TOKENS
from rag.engine import retrieve, format_context


SYSTEM_PROMPT_TEMPLATE = """אתה עוזר חכם המבוסס על מאגר ידע.

הוראות:
1. ענה על שאלות המשתמש אך ורק על בסיס המידע שמופיע ב"מידע הקשר" למטה.
2. אם המידע לא קיים בקונטקסט — אמור "אין לי מידע על זה" ואל תמציא.
3. ציין בסוף התשובה את המקורות שעליהם הסתמכת (Category — Title).
4. תשובה תמציתית ומדויקת.

── מידע הקשר ──

{context}
"""


def generate_answer(query: str, top_k: int = None) -> dict:
    """
    Pipeline מלא: retrieve → build prompt → LLM → response.
    """
    # שלב 1: אחזור chunks
    chunks = retrieve(query, top_k=top_k)
    context = format_context(chunks)
    sources = list({f"{c['category']} — {c['title']}" for c in chunks})

    # שלב 2: בניית messages
    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT_TEMPLATE.format(context=context),
        },
        {
            "role": "user",
            "content": query,
        },
    ]

    # שלב 3: קריאה ל-LLM
    client = OpenAI()
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.3,            # נמוך — דבקות במקור
        max_tokens=LLM_MAX_TOKENS,
    )

    return {
        "answer": response.choices[0].message.content.strip(),
        "sources": sources,
        "chunks_used": len(chunks),
    }
```

**הרחבות אפשריות (קיימות בפרויקט המקור):**

- **היסטוריית שיחה**: לפני שאלת המשתמש, להזריק עד N הודעות אחרונות (`role: user / assistant`).
- **conversation summary**: כשההיסטוריה ארוכה מדי, לתחזק טבלת `conversation_summaries` עם תמצית של ההודעות הישנות.
- **prompt sanitization**: לפני שליחה — לסנן HTML/קוד זדוני מהשאלה.
- **multiple LLM providers**: דרך `OPENAI_BASE_URL` ניתן לחבר Gemini או ספק תואם OpenAI.

---

## 8. ניהול KB — דוגמה (Flask endpoints)

כל פעולת mutation על `kb_entries` חייבת לקרוא ל-`mark_index_stale()`. ה-rebuild יקרה lazy בקריאה הבאה ל-`retrieve`.

```python
from flask import Flask, request, jsonify
import database as db
from rag.engine import mark_index_stale, rebuild_index, retrieve

app = Flask(__name__)


@app.route("/kb", methods=["GET"])
def kb_list():
    category = request.args.get("category")
    return jsonify(db.get_all_kb_entries(category=category))


@app.route("/kb", methods=["POST"])
def kb_add():
    data = request.json
    entry_id = db.add_kb_entry(
        category=data["category"],
        title=data["title"],
        content=data["content"],
    )
    mark_index_stale()   # חשוב!
    return jsonify({"id": entry_id}), 201


@app.route("/kb/<int:entry_id>", methods=["PUT"])
def kb_update(entry_id):
    data = request.json
    db.update_kb_entry(
        entry_id,
        category=data["category"],
        title=data["title"],
        content=data["content"],
    )
    mark_index_stale()
    return jsonify({"ok": True})


@app.route("/kb/<int:entry_id>", methods=["DELETE"])
def kb_delete(entry_id):
    db.delete_kb_entry(entry_id)
    mark_index_stale()
    return jsonify({"ok": True})


@app.route("/kb/rebuild", methods=["POST"])
def kb_rebuild():
    """כפיית rebuild מלא — שימושי אחרי שינוי של מודל ה-embedding."""
    rebuild_index(force_full=True)
    return jsonify({"ok": True})


@app.route("/kb/search", methods=["GET"])
def kb_search():
    """חיפוש סמנטי לבדיקה (מתחת למכסה — RAG retrieve)."""
    query = request.args.get("q", "")
    top_k = int(request.args.get("k", 10))
    return jsonify(retrieve(query, top_k=top_k))


@app.route("/ask", methods=["POST"])
def ask():
    from llm import generate_answer
    query = request.json["query"]
    return jsonify(generate_answer(query))
```

---

## 9. סדר ההפעלה הראשונית

```python
# main.py
from database import init_db
from rag.engine import rebuild_index

if __name__ == "__main__":
    # 1. יצירת טבלאות
    init_db()

    # 2. (אופציונלי) seed data — הוספת KB entries התחלתיים
    # ... db.add_kb_entry(...)

    # 3. בניית האינדקס בפעם הראשונה
    rebuild_index()

    # 4. הרצת השרת
    # app.run(...)
```

---

## 10. אופטימיזציות חשובות לזכור

| אופטימיזציה | מימוש | חיסכון |
|---|---|---|
| **batch embeddings** | `get_embeddings_batch` שולח קבוצות של 100 | פי ~50 פחות round-trips ל-API |
| **incremental rebuild** | השוואת חתימת טקסט לפני re-embed | 95%+ פחות קריאות API בעדכון יחיד |
| **query cache** | LRU עם TTL 5 דקות, max 256 entries | תשובה מיידית לשאלות חוזרות |
| **lazy stale rebuild** | `mark_index_stale()` במקום rebuild סינכרוני | UI מהיר; rebuild רק בקריאה הבאה |
| **cross-process lock** | `fcntl` על קובץ `.index_state.lock` | בטוח עם כמה workers (gunicorn/flask) |
| **dimension auto-detect** | ב-`retrieve` תופס `ValueError` ומ-rebuild | הגירה למודל embedding חדש ללא ידני |
| **normalized embeddings** | `IndexFlatIP` + `normalize_L2` | cosine similarity מובנה ב-FAISS |

---

## 11. דברים שיש להחליט / להחליף בפרויקט החדש

### חובה לעדכן:
- **`config.py`** — נתיבים, מודלים, ספים.
- **`database.py`** — אם משתמשים ב-Postgres/MySQL במקום SQLite. החלף `BLOB` ב-`BYTEA`. ה-SQL בקוד למעלה הוא שמרני וברובו תואם ANSI.
- **`SYSTEM_PROMPT_TEMPLATE`** — להתאים לשפה ולדומיין של הפרויקט החדש.

### שיקולים:
- **גודל KB עתידי**: עד ~10K chunks — `IndexFlatIP` מצוין. מעבר לזה — לעבור ל-`IndexHNSWFlat` (חיפוש מקורב מהיר) או ל-pgvector.
- **multilanguage**: אם המאגר רב-לשוני, `text-embedding-3-small` של OpenAI מצוין; מודלים מקומיים (sentence-transformers) דורשים החלפת `_get_client` ב-`SentenceTransformer.encode`.
- **persistence עבור הרצה ענן**: מומלץ לחבר `DATA_DIR` ל-volume קבוע. בלי זה, האינדקס ייבנה מחדש בכל restart.
- **observability**: לוגים יש בכל מקום, אבל אין metrics. שווה להוסיף Prometheus counters ל-`retrieve` (latency, cache hit rate, sources).
- **rate limiting** של API: ה-batch של 100 שומר על מרבית המגבלות, אבל לחשוב על exponential backoff אם משתמשים מבצעים rebuilds תכופים.
- **rebuild concurrency**: ה-`_REBUILD_LOCK` מונע race condition בתוך תהליך אחד; ה-`fcntl` מסנכרן בין תהליכים. אבל ב-rebuild גדול מאוד (~10K entries), עדיף להעביר אותו ל-background worker (Celery/RQ) ולא להריץ inline.

---

## 12. קבצים מקוריים בריפו הנוכחי

לרפרנס — היכן הקוד הזה נמצא במלואו ב-`ai-business-bot`:

| תיאור | נתיב |
|---|---|
| Chunker | `rag/chunker.py` |
| Embeddings + OpenAI client factory | `rag/embeddings.py`, `openai_client.py` |
| Vector Store | `rag/vector_store.py` |
| Engine (retrieve + rebuild) | `rag/engine.py` |
| סכמת DB + פונקציות KB | `database.py` (קווים 43–69, 1016–1152) |
| בניית prompt + generate_answer | `llm.py` |
| Admin routes לניהול KB | `admin/app.py` (קווים 1433–1568) |
| Tests | `tests/test_chunker.py`, `tests/test_engine.py` וכו' |

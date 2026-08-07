"""Ultra-fast local voice runtime for zero-latency interactions."""

import time
import json
import os
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

import numpy as np
import chromadb
from chromadb.utils import embedding_functions
import speech_recognition as sr

from voice.sentiment import write_conv_emotion

try:
    import os as _os
    _os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame
    pygame.mixer.init()
except ImportError:
    pygame = None

APP_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(APP_DIR / ".env")

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")


# ── Domain router ─────────────────────────────────────────────────────────────
# Routes a transcript to one domain BEFORE vector retrieval so that, e.g.,
# "what is the time right now" never competes with event utterances like
# "what time is the sports meeting".

# Clock questions only — deliberately narrow so "what time is the meeting"
# still routes to events.
_TIME_RE = re.compile(
    r"\b("
    r"what time is it"
    r"|what(?:'s| is) the time"
    r"|tell me the time"
    r"|current time"
    r"|time (?:right )?now"
    r"|do you have the time"
    r"|what does the clock say"
    r")\b",
    re.IGNORECASE,
)

_NAVIGATE_RE = re.compile(
    r"\b("
    r"where(?:'s| is| are)"
    r"|take me to"
    r"|directions? to"
    r"|how (?:do|can) i (?:get|go) to"
    r"|how to (?:get|go) to"
    r"|navigate to"
    r"|find the"
    r"|show me the way"
    r"|way to the"
    r"|looking for the"
    r"|guide me to"
    r")\b",
    re.IGNORECASE,
)

_SMALLTALK_RE = re.compile(
    r"\b("
    r"hi|hello|hey|yo|sup|what's up"
    r"|good (?:morning|afternoon|evening|night)"
    r"|who are you|what are you|your name|introduce yourself|who am i talking to"
    r"|are you (?:a robot|an ai|ok|okay|there|listening|working)"
    r"|how are you|how(?:'s| is) it going|how do you feel|you good|how have you been"
    r"|what can you do|how can you help|what do you know|help me|can you help"
    r"|what can i ask|your features"
    r"|thank you|thanks|appreciate it|cheers"
    r"|bye|goodbye|see you|talk to you later|catch you later"
    r"|can you hear me|is anyone there|you there|testing|test test"
    r"|nice|cool|awesome|great|interesting|okay|ok|alright"
    r")\b",
    re.IGNORECASE,
)


_KNOWLEDGE_RE = re.compile(
    r"\b("
    r"what is|what are|tell me about|explain|describe"
    r"|who is|who are|how old is|when was"
    r"|history of|about the|about this"
    r"|university of moratuwa|uom|faculty of it|fit"
    r"|undergraduate|postgraduate|admission|research"
    r"|courses|programs|degrees|contact|location|address"
    r")\b",
    re.IGNORECASE,
)


def route_domain(text: str) -> str:
    """Route a transcript to: 'tool_time', 'knowledge', 'navigate', 'smalltalk', or 'events'."""
    if _TIME_RE.search(text):
        return "tool_time"
    if _NAVIGATE_RE.search(text):
        return "navigate"
    if _KNOWLEDGE_RE.search(text):
        return "knowledge"
    if _SMALLTALK_RE.search(text):
        return "smalltalk"
    return "events"


def get_time_reply() -> str:
    """Dynamic reply for the get_time tool."""
    now = datetime.now().strftime("%I:%M %p").lstrip("0")
    return f"It's {now} right now."


# Per-domain acceptance thresholds (L2 distance — lower is better) and the
# minimum top-1/top-2 margin required when the two nearest hits belong to
# DIFFERENT intents (ambiguity rejection).
DOMAIN_THRESHOLDS = {
    "events": 0.8,
    "smalltalk": 0.9,
    "navigate": 0.75,
    "knowledge": 0.85,
}
AMBIGUITY_MARGIN = 0.15

# Intent JSON files per domain (all live in voice/event_db/)
DOMAIN_SOURCES = {
    "events": "compiled_intents.json",
    "smalltalk": "smalltalk_intents.json",
    "navigate": "navigate_intents.json",
    "knowledge": "knowledge_intents.json",
}


# ── Number word → digit map used by hashmap expander ─────────────────────────
_NUM_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}

# Prefixes to strip when generating hashmap variants
_STRIP_PREFIXES = re.compile(
    r"^(can you |could you |please |i want to |i'd like to |i need to "
    r"|i am looking for |i'm looking for |take me to |bring me to "
    r"|guide me to |show me the way to |navigate me to "
    r"|find the |find me |show me )",
    re.IGNORECASE,
)


class IntentMatcher:
    """Intent matcher with two-tier lookup: O(1) hashmap → O(N·D) numpy L2."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        # ChromaDB is deferred until actually needed (avoids loading ONNX model
        # when the numpy fast-path cache already exists).
        self.client = None
        self.collection = None
        self._ef = None

        self.intents_map: dict = {}
        self.exact_match_map: dict = {}

        # numpy fast-path (Option 1)
        self._np_embeddings: np.ndarray | None = None   # shape (N, D)
        self._np_meta: list[dict] = []                  # [{intent_id, domain}, ...]
        self._embed_model = None                        # lazy SentenceTransformer

    def _ensure_chromadb(self) -> bool:
        """Lazily init ChromaDB client. Returns True if available."""
        if self.client is not None:
            return True
        try:
            self.client = chromadb.PersistentClient(path=str(self._db_path))
            self._ef = embedding_functions.DefaultEmbeddingFunction()
            self.collection = self.client.get_or_create_collection(
                name="compiled_intents",
                embedding_function=self._ef,
            )
            return True
        except Exception as e:
            print(f"  [Matcher] ChromaDB init failed: {e}")
            self.client = None
            self.collection = None
            self._ef = None
            return False

    # ── Tier-0: exact hashmap lookup ─────────────────────────────────────────

    def match_exact(self, text: str) -> dict | None:
        """Instant exact lookup for pre-defined queries (0.01ms cost) to bypass vector search."""
        norm_text = text.lower().strip(" \t\r\n.,?!;:")
        if not norm_text:
            return None
        match = self.exact_match_map.get(norm_text)
        if match:
            return match

        # Strip common conversational prefix greetings (e.g. "hi", "hey", "hello", "robot", "nema")
        stripped = re.sub(r"^(hi|hey|hello|okay|ok|robot|nema|please)[,.\s]+", "", norm_text).strip(" \t\r\n.,?!;:")
        if stripped and stripped != norm_text:
            match = self.exact_match_map.get(stripped)
            if match:
                return match
        return None

    # ── Option 3: auto-expand hashmap with surface-form variants ─────────────

    def _expand_exact_map(self) -> None:
        """Generate extra hashmap entries for number words, stripped prefixes, etc.
        
        Runs once at load time — no runtime cost.
        """
        new_entries: dict = {}

        for utt, intent in list(self.exact_match_map.items()):
            # 1. Replace written-out numbers with digits: "lab six" → "lab 6"
            num_variant = utt
            for word, digit in _NUM_WORDS.items():
                num_variant = re.sub(rf"\b{word}\b", digit, num_variant)
            if num_variant != utt:
                new_entries[num_variant] = intent

            # 2. Strip common request prefixes: "take me to lab 6" → "lab 6"
            stripped = _STRIP_PREFIXES.sub("", utt).strip(" \t\r\n.,?!;:")
            if stripped and stripped != utt:
                new_entries[stripped] = intent
                # Also combine: strip prefix AND convert number words
                num_stripped = stripped
                for word, digit in _NUM_WORDS.items():
                    num_stripped = re.sub(rf"\b{word}\b", digit, num_stripped)
                if num_stripped != stripped:
                    new_entries[num_stripped] = intent

            # 3. Common abbreviations for navigate intents
            abbrev = utt
            abbrev = re.sub(r"\blaborator(?:y|ies)\b", "lab", abbrev)
            abbrev = re.sub(r"\blecture hall\b", "lh", abbrev)
            abbrev = re.sub(r"\bauditorium\b", "audi", abbrev)
            if abbrev != utt:
                new_entries[abbrev] = intent
                # Also strip prefix from abbreviation
                abbrev_stripped = _STRIP_PREFIXES.sub("", abbrev).strip(" \t\r\n.,?!;:")
                if abbrev_stripped and abbrev_stripped != abbrev:
                    new_entries[abbrev_stripped] = intent

        before = len(self.exact_match_map)
        self.exact_match_map.update(new_entries)
        added = len(self.exact_match_map) - before
        print(f"  [Matcher] Hashmap expanded: {before} → {len(self.exact_match_map)} entries (+{added} variants)")

    def _load_intent_lists(self, compiled_json_path: Path) -> list[tuple[str, dict]]:
        """Load all domain intent files. Returns a list of (domain, intent) pairs."""
        db_dir = compiled_json_path.parent
        pairs: list[tuple[str, dict]] = []
        for domain, filename in DOMAIN_SOURCES.items():
            path = db_dir / filename
            if not path.exists():
                continue
            for intent in json.loads(path.read_text(encoding="utf-8")):
                pairs.append((domain, intent))
        return pairs

    def _cache_is_current(self, expected_docs: int) -> bool:
        """True if Chroma already holds the expected docs WITH domain metadata."""
        if self.collection is None or expected_docs == 0:
            return False
        try:
            if self.collection.count() != expected_docs:
                return False
            sample = self.collection.peek(limit=1)
            metadatas = sample.get("metadatas") or []
            return bool(metadatas) and "domain" in metadatas[0]
        except Exception:
            return False

    # ── Option 1: numpy embedding cache helpers ───────────────────────────────

    def _npy_cache_paths(self, db_dir: Path) -> tuple[Path, Path]:
        return db_dir / "embeddings_cache.npy", db_dir / "embeddings_meta.json"

    def _npy_cache_is_current(self, db_dir: Path, expected_docs: int) -> bool:
        npy, meta = self._npy_cache_paths(db_dir)
        if not npy.exists() or not meta.exists():
            return False
        try:
            stored = json.loads(meta.read_text())
            return len(stored) == expected_docs
        except Exception:
            return False

    def _build_npy_cache(self, db_dir: Path) -> None:
        """Extract embeddings from ChromaDB and save as fast numpy arrays."""
        if self.collection is None:
            print("  [Matcher] Cannot build numpy cache: ChromaDB collection not available.")
            return
        print("  [Matcher] Building numpy embedding cache from ChromaDB...")
        all_items = self.collection.get(include=["embeddings", "metadatas"])  # type: ignore
        raw_embs = all_items.get("embeddings")
        raw_meta = all_items.get("metadatas")
        if raw_embs is None or len(raw_embs) == 0:
            print("  [Matcher] No embeddings found in ChromaDB to cache.")
            return
        arr = np.array(raw_embs, dtype=np.float32)
        meta_list = list(raw_meta) if raw_meta is not None else []
        npy, meta_path = self._npy_cache_paths(db_dir)
        np.save(str(npy), arr)
        meta_path.write_text(json.dumps(meta_list), encoding="utf-8")
        self._np_embeddings = arr
        self._np_meta = meta_list
        print(f"  [Matcher] Numpy cache saved: {arr.shape[0]} embeddings × {arr.shape[1]}d")

    def _build_npy_cache_direct(self, db_dir: Path, pairs: list[tuple[str, dict]]) -> None:
        """Build numpy embedding cache directly with SentenceTransformer (no ChromaDB).

        Used as a fallback when ChromaDB is unavailable but we need to create
        the embedding cache for the first time.
        """
        documents = []
        meta_list = []
        for domain, intent in pairs:
            for utt in intent.get("utterances") or []:
                documents.append(utt)
                meta_list.append({"intent_id": intent["id"], "domain": domain})

        if not documents:
            print("  [Matcher] No utterances to embed.")
            return

        print(f"  [Matcher] Embedding {len(documents)} utterances with SentenceTransformer...")
        model = self._get_embed_model()
        embeddings = model.encode(documents, show_progress_bar=False, normalize_embeddings=False)
        arr = np.array(embeddings, dtype=np.float32)

        npy, meta_path = self._npy_cache_paths(db_dir)
        np.save(str(npy), arr)
        meta_path.write_text(json.dumps(meta_list), encoding="utf-8")
        self._np_embeddings = arr
        self._np_meta = meta_list
        print(f"  [Matcher] Numpy cache built directly: {arr.shape[0]} embeddings × {arr.shape[1]}d")

    def _load_npy_cache(self, db_dir: Path) -> None:
        """Load pre-built numpy embedding cache from disk."""
        npy, meta_path = self._npy_cache_paths(db_dir)
        self._np_embeddings = np.load(str(npy)).astype(np.float32)
        self._np_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        print(f"  [Matcher] Numpy cache loaded: {self._np_embeddings.shape[0]} embeddings")

    def _get_embed_model(self):
        """Lazy-load the SentenceTransformer embedding model (same as DefaultEmbeddingFunction)."""
        if self._embed_model is None:
            from sentence_transformers import SentenceTransformer
            self._embed_model = SentenceTransformer("all-MiniLM-L6-v2")
            print("  [Matcher] SentenceTransformer model loaded (all-MiniLM-L6-v2)")
        return self._embed_model

    def load_cache(self, compiled_json_path: Path):
        pairs = self._load_intent_lists(compiled_json_path)
        if not pairs:
            print("No compiled intents found. Run intent_compiler.py first.")
            return

        self.exact_match_map = {}
        for _, intent in pairs:
            self.intents_map[intent["id"]] = intent
            for utt in intent.get("utterances") or []:
                norm_utt = utt.lower().strip(" \t\r\n.,?!;:")
                if norm_utt:
                    self.exact_match_map[norm_utt] = intent

        expected_docs = sum(len(i.get("utterances") or []) for _, i in pairs)
        db_dir = compiled_json_path.parent

        # ── Option 3: expand hashmap with auto-generated variants ──
        self._expand_exact_map()

        # ── Option 1: build/load numpy embedding cache ─────────────
        if self._npy_cache_is_current(db_dir, expected_docs):
            # Fast path: numpy cache already up to date — skip ChromaDB entirely
            self._load_npy_cache(db_dir)
            print(
                f"Loaded {len(self.intents_map)} intents "
                f"({expected_docs} utterances) — numpy fast-path active (ChromaDB skipped)."
            )
            return

        # Numpy cache missing or stale — need to rebuild from ChromaDB.
        # Only now do we initialise the ChromaDB client (which loads the ONNX model).
        if not self._ensure_chromadb():
            print("  [Matcher] ChromaDB unavailable and numpy cache missing — "
                  "building numpy cache directly with SentenceTransformer...")
            self._build_npy_cache_direct(db_dir, pairs)
            return

        if not self._cache_is_current(expected_docs):
            print(
                f"Indexing {len(pairs)} intents into ChromaDB "
                f"({expected_docs} utterances)..."
            )
            try:
                self.client.delete_collection("compiled_intents")
            except Exception:
                pass
            self.collection = self.client.get_or_create_collection(
                name="compiled_intents",
                embedding_function=embedding_functions.DefaultEmbeddingFunction(),
            )

            ids, documents, metadatas = [], [], []
            for domain, intent in pairs:
                for i, utt in enumerate(intent.get("utterances") or []):
                    ids.append(f"{intent['id']}_{i}")
                    documents.append(utt)
                    metadatas.append({"intent_id": intent["id"], "domain": domain})

            if ids:
                batch = 200
                for start in range(0, len(ids), batch):
                    end = start + batch
                    self.collection.add(
                        ids=ids[start:end],
                        documents=documents[start:end],
                        metadatas=metadatas[start:end],
                    )
            print(f"Successfully indexed {len(ids)} utterances.")
        else:
            print(
                f"Loaded {len(self.intents_map)} intents "
                f"({expected_docs} utterances) from ChromaDB cache."
            )

        # Extract embeddings from ChromaDB into numpy for fast future queries
        self._build_npy_cache(db_dir)

    # ── Option 1: numpy fast matching path ───────────────────────────────────

    def _match_numpy(self, text: str, domain: str) -> tuple[float, float, str, str] | None:
        """Embed query, compute L2 against domain rows, return (d1, d2, top_id, second_id).
        
        Returns None if fewer than 1 candidate found in domain.
        """
        assert self._np_embeddings is not None

        # Filter rows belonging to this domain
        domain_indices = [i for i, m in enumerate(self._np_meta) if m["domain"] == domain]
        if not domain_indices:
            return None

        domain_embs = self._np_embeddings[domain_indices]  # (K, D)

        # Embed query with the same model as indexing (all-MiniLM-L6-v2)
        model = self._get_embed_model()
        q = model.encode(text, normalize_embeddings=False, show_progress_bar=False)
        q = np.array(q, dtype=np.float32)

        # L2 squared distances (consistent with ChromaDB default metric)
        diff = domain_embs - q          # (K, D)
        dists = np.einsum("ij,ij->i", diff, diff)  # (K,) — faster than np.sum

        if len(dists) == 1:
            idx0 = domain_indices[0]
            return float(dists[0]), float(dists[0]) + 1.0, self._np_meta[idx0]["intent_id"], ""

        top2_local = np.argpartition(dists, 2)[:2]
        top2_local = top2_local[np.argsort(dists[top2_local])]
        idx0 = domain_indices[int(top2_local[0])]
        idx1 = domain_indices[int(top2_local[1])]
        return (
            float(dists[top2_local[0]]),
            float(dists[top2_local[1]]),
            self._np_meta[idx0]["intent_id"],
            self._np_meta[idx1]["intent_id"],
        )

    def match(self, text: str, domain: str | None = None) -> dict | None:
        """Match a transcript against ONE domain's intents with a confidence gate.

        Accept only when the nearest utterance is below the domain threshold
        AND — if the second-nearest hit belongs to a different intent — the
        distance margin between them is at least AMBIGUITY_MARGIN.
        """
        if not text.strip():
            return None

        if domain is None:
            domain = route_domain(text)
        if domain == "tool_time":
            # Tools are handled by the caller, not by retrieval.
            return None

        # ── Fast numpy path (Option 1) ─────────────────────────────────────────
        if self._np_embeddings is not None:
            np_result = self._match_numpy(text, domain)
            if np_result is None:
                print(f"  [Matcher] domain={domain}: no candidates.")
                return None
            d1, d2, top_intent, second_intent = np_result
            metadatas_0 = [
                {"intent_id": top_intent},
                {"intent_id": second_intent},
            ]
            distances_0 = [d1, d2]
        else:
            # ── ChromaDB fallback path ─────────────────────────────────────────
            if self.collection is None:
                print(f"  [Matcher] domain={domain}: no numpy cache or ChromaDB available.")
                return None
            results = self.collection.query(
                query_texts=[text],
                n_results=2,
                where={"domain": domain},
            )
            metadatas_0 = (results.get("metadatas") or [[]])[0]
            distances_0 = (results.get("distances") or [[]])[0]
            if not metadatas_0:
                print(f"  [Matcher] domain={domain}: no candidates.")
                return None
            d1 = distances_0[0]
            top_intent = metadatas_0[0]["intent_id"]

        threshold = DOMAIN_THRESHOLDS.get(domain, 1.2)
        d1 = distances_0[0]
        top_intent = metadatas_0[0]["intent_id"]

        if len(metadatas_0) > 1:
            d2 = distances_0[1]
            second_intent = metadatas_0[1]["intent_id"]
            margin = d2 - d1
            print(
                f"  [Matcher] domain={domain} d1={d1:.2f} d2={d2:.2f} "
                f"margin={margin:.2f} ({top_intent} vs {second_intent})"
            )
            # Two different intents too close together → ambiguous, abstain or clarify.
            # Only trigger clarification if top match is NOT already a strong/explicit hit (d1 > 0.30).
            if second_intent != top_intent and margin < AMBIGUITY_MARGIN and d1 > 0.30:
                if d1 < threshold:
                    intent1 = self.intents_map.get(top_intent)
                    intent2 = self.intents_map.get(second_intent)
                    if intent1 and intent2:
                        dest1 = intent1.get("action", {}).get("destination")
                        dest2 = intent2.get("action", {}).get("destination")
                        if dest1 and dest2:
                            def get_dest_category(d: str) -> str:
                                dl = d.lower()
                                if "auditorium" in dl:
                                    return "auditorium"
                                if "laboratory" in dl or "labratory" in dl or "lab" in dl:
                                    return "laboratory"
                                if "lecture hall" in dl or "lecturehall" in dl or "lh" in dl:
                                    return "lecture hall"
                                if "washroom" in dl or "toilet" in dl or "bathroom" in dl:
                                    return "washroom"
                                if "office" in dl:
                                    return "office"
                                return "location"

                            cat1 = get_dest_category(dest1)
                            cat2 = get_dest_category(dest2)
                            category = cat1 if cat1 == cat2 else "location"

                            clean_dest1 = dest1.replace("floor_", "Floor ")
                            clean_dest2 = dest2.replace("floor_", "Floor ")

                            # Find all matching destinations for this category
                            matching_dests = []
                            if category != "location":
                                for item in self.intents_map.values():
                                    action = item.get("action", {}) or {}
                                    if action.get("action") == "navigate":
                                        d = action.get("destination")
                                        if d and get_dest_category(d) == category:
                                            clean_d = d.replace("floor_", "Floor ")
                                            if clean_d and clean_d[0].islower():
                                                clean_d = clean_d[0].upper() + clean_d[1:]
                                            if clean_d not in matching_dests:
                                                matching_dests.append(clean_d)

                            matching_dests.sort()

                            if category != "location" and len(matching_dests) > 1:
                                if len(matching_dests) <= 4:
                                    if len(matching_dests) == 2:
                                        options = f"{matching_dests[0]} or {matching_dests[1]}"
                                    elif len(matching_dests) == 3:
                                        options = f"{matching_dests[0]}, {matching_dests[1]}, or {matching_dests[2]}"
                                    else:
                                        options = f"{matching_dests[0]}, {matching_dests[1]}, {matching_dests[2]}, or {matching_dests[3]}"
                                    response_text = f"Which {category} would you like to go to? {options}?"
                                else:
                                    c1 = clean_dest1[0].upper() + clean_dest1[1:] if clean_dest1 else ""
                                    c2 = clean_dest2[0].upper() + clean_dest2[1:] if clean_dest2 else ""
                                    response_text = f"Which {category} would you like to go to? {c1} or {c2}?"
                            else:
                                c1 = clean_dest1[0].upper() + clean_dest1[1:] if clean_dest1 else ""
                                c2 = clean_dest2[0].upper() + clean_dest2[1:] if clean_dest2 else ""
                                response_text = f"Did you mean {c1} or {c2}?"

                            buttons = matching_dests if len(matching_dests) > 0 and len(matching_dests) <= 4 else ([c1, c2] if c1 and c2 else [])
                            
                            print(f"  [Matcher] Ambiguity clarification: {response_text}")
                            return {
                                "id": "ambiguous_clarification",
                                "response_text": response_text,
                                "audio_file": None,
                                "action": {
                                    "action": "speak",
                                    "text": response_text,
                                    "suggested_buttons": buttons
                                },
                                "ambiguity_category": category
                            }
                # ── Events domain ambiguity: show all events instead of rejecting ──
                if domain == "events" and d1 < threshold:
                    print(f"  [Matcher] Events ambiguous → returning top match + show_events hint")
                    top = self.intents_map.get(top_intent)
                    if top:
                        # Return the top event but flag it so nlu_server can inject all event buttons
                        result = dict(top)
                        result["_events_ambiguous"] = True
                        return result
                print("  [Matcher] Rejected: ambiguous top-2 candidates.")
                return None
        else:
            print(f"  [Matcher] domain={domain} d1={d1:.2f} (single candidate)")

        if d1 >= threshold:
            print(f"  [Matcher] Rejected: distance {d1:.2f} >= {threshold}.")
            return None

        return self.intents_map.get(top_intent)


def transcribe_audio(audio_data: bytes) -> str:
    """Send raw WAV/PCM data to Deepgram for extremely fast STT."""
    if not DEEPGRAM_API_KEY:
        print("Missing DEEPGRAM_API_KEY")
        return ""
        
    url = "https://api.deepgram.com/v1/listen?model=nova-2&smart_format=true"
    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type": "audio/wav" 
    }
    req = urllib.request.Request(url, data=audio_data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as response:
            raw_response = response.read()
            result = json.loads(raw_response)
            
            # Navigate Deepgram JSON safely
            try:
                transcript = result["results"]["channels"][0]["alternatives"][0]["transcript"]
                if not transcript:
                    print(f"  [STT] Deepgram heard no words. (Raw: {result['results']})")
                
                # Strip noise pops (like "undefined", "undefine", "undefines") from transcripts
                if transcript:
                    words = transcript.strip().split()
                    cleaned = [w for w in words if w.lower().strip(".,?!;:") not in ("undefined", "undefine", "undefines")]
                    transcript = " ".join(cleaned)
                
                return transcript
            except KeyError:
                print(f"  [STT] Unexpected Deepgram JSON: {raw_response}")
                return ""
                
    except Exception as e:
        print(f"  [STT] Request Error: {e}")
        return ""


def play_audio(audio_path: Path, block: bool = False) -> bool:
    """Plays an audio file using mpv or ffplay.

    Non-blocking by default (block=False) — matches Pepper/Temi industrial pattern.
    Returns True immediately after launching the audio subprocess.
    The caller should set conv_state='listening' right after calling this.

    Args:
        audio_path: Path to the audio file to play.
        block: If True, waits for playback to finish (legacy behaviour).
               Always False in kiosk NLU mode so the mic opens immediately.
    """
    import subprocess
    import shutil

    if not audio_path.exists():
        return False

    # 1. Try mpv (extremely low latency and quiet)
    if shutil.which("mpv"):
        try:
            if block:
                subprocess.run(
                    ["mpv", "--no-video", "--really-quiet", str(audio_path)],
                    check=True
                )
            else:
                subprocess.Popen(
                    ["mpv", "--no-video", "--really-quiet", str(audio_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return True
        except Exception as e:
            print(f"  [Audio] mpv error: {e}")

    # 2. Try ffplay
    if shutil.which("ffplay"):
        try:
            if block:
                subprocess.run(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(audio_path)],
                    check=True
                )
            else:
                subprocess.Popen(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(audio_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return True
        except Exception as e:
            print(f"  [Audio] ffplay error: {e}")

    # 3. Pygame last-resort fallback (always blocking — it has no async API)
    global pygame
    if pygame is not None:
        try:
            pygame.mixer.music.load(str(audio_path))
            pygame.mixer.music.play()
            if block:
                while pygame.mixer.music.get_busy():
                    pygame.time.Clock().tick(10)
            return True
        except Exception as e:
            print(f"  [Audio] Pygame error: {e}")

    return False



class OfflineVoiceRuntime:
    def __init__(self, bb):
        self.bb = bb
        self.db_path = APP_DIR / "voice" / "event_db"
        self.matcher = IntentMatcher(self.db_path)
        self.matcher.load_cache(self.db_path / "compiled_intents.json")
        # Warm up embedding model in background so first query has zero latency
        self.matcher._get_embed_model()
            
    def process_text_input(self, text: str):
        """Processes a transcript, matches intent, updates Blackboard, and plays audio."""
        self.bb.write(conv_state="thinking", user_text=text)
        write_conv_emotion(self.bb, text, is_agent=False, log_prefix="Vader NLU")

        # Tool routes (e.g. clock) bypass retrieval entirely
        if route_domain(text) == "tool_time":
            reply = get_time_reply()
            print(f"\n🕐 Tool: get_time → {reply}")
            write_conv_emotion(self.bb, reply, is_agent=True, log_prefix="Vader NLU")
            self.bb.write(
                conv_state="speaking",
                agent_speaking=True,
                agent_text=reply,
                current_action={},
            )
            print(f"🔊 [Audio Playback] {reply}")
            self.bb.write(conv_state="listening", agent_speaking=False)
            return

        # Shortcut: If user tapped a button with an exact room name, skip NLU
        # This prevents ambiguity loops when users tap "Auditorium 1" exactly.
        try:
            if not hasattr(self, "_wayfinder"):
                from voice.wayfinding import Wayfinder
                self._wayfinder = Wayfinder()
            exact_room = self._wayfinder.find_room(text)
            # Only use shortcut when the query contains a digit (e.g. "Auditorium 1")
            # so generic words like "auditorium" still go through the full NLU flow.
            import re as _re
            text_has_number = bool(_re.search(r'\d', text))
            label_matches = exact_room is not None and (
                text.lower().strip() in exact_room["label"].lower()
                or exact_room["label"].lower() in text.lower().strip()
            )
            if exact_room is not None and text_has_number and label_matches:
                print(f"  [Navigate Shortcut] Exact room match: {exact_room['label']}")
                # Bypass matcher, go straight to navigation
                try:
                    result = self._wayfinder.find_path(exact_room['label'])
                    if result and "directions" in result:
                        reply_text = result["directions"]
                        action = {
                            "action": "navigate",
                            "destination": result["destination"],
                            "floor": result.get("floor", "floor_1"),
                            "path": result["path_coords"],
                            "path_ids": result.get("path_ids", []),
                            "directions": result["directions"],
                            "nodes": [
                                {
                                    "id": n["id"],
                                    "label": n["label"],
                                    "type": n.get("type", "room"),
                                    "world": n["world"],
                                    "building": n.get("building"),
                                    "size": n.get("size", [1, 1, 1]),
                                    "floor": n.get("floor", result.get("floor", "floor_1")),
                                }
                                for n in result["nodes"]
                            ],
                            "buildings": result["buildings"],
                        }
                        write_conv_emotion(self.bb, reply_text, is_agent=True, log_prefix="Vader NLU")
                        self.bb.write(
                            conv_state="speaking", 
                            agent_speaking=True,
                            agent_text=reply_text,
                            current_action=action
                        )
                        print(f"🔊 [Audio Playback] {reply_text}")
                        self.bb.write(conv_state="listening", agent_speaking=False)
                        return
                except Exception as exc:
                    print(f"⚠️ Shortcut navigation failed: {exc}")
        except Exception:
            pass

        # 1. Match Intent instantly via ChromaDB
        intent = self.matcher.match(text)
        
        # 1b. Fallback for STT mishearings (e.g. heard "Jerry's" instead of "Where is")
        # If the strict regex router sent it to the wrong domain, the vector database
        # will return no matches. In that case, we trust the ChromaDB AI embeddings 
        # to find it in the other domains.
        if not intent:
            original_domain = route_domain(text)
            for fallback_domain in ["navigate", "events", "smalltalk"]:
                if fallback_domain != original_domain:
                    intent = self.matcher.match(text, domain=fallback_domain)
                    if intent:
                        print(f"  [Fallback] AI matched in '{fallback_domain}' domain despite missing trigger words!")
                        break
        
        # Minimum thinking duration (400ms) so emotion engine renders thinking face
        time.sleep(0.4)

        if intent:
            print(f"\n✅ Match Found! Action: {intent['action']}")
            reply_text = intent.get("response_text", "")
            action = intent.get("action", {})
            
            # Dynamic wayfinding integration for offline runtime
            if action.get("action") == "navigate" and action.get("destination"):
                try:
                    from voice.wayfinding import Wayfinder
                    if not hasattr(self, "_wayfinder"):
                        self._wayfinder = Wayfinder()
                    result = self._wayfinder.find_path(action["destination"])
                    if result and "directions" in result:
                        reply_text = result["directions"]
                        action = {
                            "action": "navigate",
                            "destination": result["destination"],
                            "floor": result.get("floor", "floor_1"),
                            "path": result["path_coords"],
                            "path_ids": result.get("path_ids", []),
                            "directions": result["directions"],
                            "nodes": [
                                {
                                    "id": n["id"],
                                    "label": n["label"],
                                    "type": n.get("type", "room"),
                                    "world": n["world"],
                                    "building": n.get("building"),
                                    "size": n.get("size", [1, 1, 1]),
                                    "floor": n.get("floor", result.get("floor", "floor_1")),
                                }
                                for n in result["nodes"]
                            ],
                            "buildings": result["buildings"],
                        }
                except Exception as exc:
                    print(f"⚠️ Offline dynamic wayfinding failed: {exc}")

            write_conv_emotion(self.bb, reply_text, is_agent=True, log_prefix="Vader NLU")

            # 2. Write UI Action to Blackboard (Frontend updates screen instantly)
            self.bb.write(
                conv_state="speaking", 
                agent_speaking=True,
                agent_text=reply_text,
                current_action=action
            )
            
            # 3. Play Pre-Recorded Audio (Zero latency) - only if not navigate (since navigate has dynamic speech)
            audio_file = intent.get("audio_file")
            audio_path = APP_DIR / "assets" / "audio_cache" / audio_file if (audio_file and action.get("action") != "navigate") else None
            
            from voice.local_speaker import is_enabled as local_speaker_enabled
            if local_speaker_enabled() and audio_path and audio_path.exists():
                print(f"🔊 Playing local speaker audio: {audio_file}")
                play_audio(audio_path)
            else:
                print(f"🔊 [Audio Playback] {reply_text}")
                
        else:
            print("\n❌ No match found. (Out of Domain)")
            fallback_text = "I'm a campus guide! Try asking me about events or locations."
            write_conv_emotion(self.bb, fallback_text, is_agent=True, log_prefix="Vader NLU")
            self.bb.write(
                conv_state="speaking",
                agent_speaking=True,
                agent_text=fallback_text,
                current_action={}
            )
            audio_path = APP_DIR / "assets" / "audio_cache" / "intent_fallback.wav"
            if not audio_path.exists():
                audio_path = APP_DIR / "assets" / "audio_cache" / "intent_fallback.mp3"
            from voice.local_speaker import is_enabled as local_speaker_enabled
            if local_speaker_enabled() and audio_path.exists():
                print("🔊 Playing local speaker fallback audio...")
                play_audio(audio_path)
            else:
                print("🔊 [Audio Playback] I'm a campus guide! Try asking me about events or locations.")
            
        self.bb.write(conv_state="waiting", agent_speaking=False, user_speaking=False)

    def start_hardware_loop(self):
        """Zero-latency hardware microphone loop using SpeechRecognition VAD."""
        r = sr.Recognizer()
        
        # Optimize for fast interactions
        r.energy_threshold = 1200        # slightly higher = fewer false starts on Pi
        r.dynamic_energy_threshold = False
        r.pause_threshold = 0.5          # 0.5 s → ~300 ms faster response than 0.8
        
        print("\n=== Offline Voice Runtime Started ===")
        with sr.Microphone() as source:
            print("Adjusting for ambient noise for 1 second...")
            r.adjust_for_ambient_noise(source, duration=1)
            print("\n✅ Microphone is live! Start speaking to the robot.")
            
            while True:
                try:
                    self.bb.write(conv_state="listening")
                    
                    # Listen until the user stops talking
                    audio = r.listen(source, timeout=None, phrase_time_limit=15)
                    
                    print("\n[🎙️ VAD] Speech captured! Sending to Deepgram...")
                    self.bb.write(
                        conv_state="thinking",
                        user_text="",
                        agent_text="",
                        current_action={}
                    )
                    
                    wav_data = audio.get_wav_data()
                    transcript = transcribe_audio(wav_data)
                    
                    if transcript:
                        print(f"Transcript: '{transcript}'")
                        
                        # Stop microphone stream to prevent feedback loops
                        try:
                            source.stream.stop_stream()
                        except Exception:
                            pass
                            
                        self.process_text_input(transcript)
                        
                        # Flush buffered audio frames and restart mic stream
                        try:
                            import time
                            time.sleep(0.3)  # Wait for reverb/click to settle
                            if hasattr(source.stream, "get_read_available"):
                                while source.stream.get_read_available() > 0:
                                    source.stream.read(source.CHUNK, exception_on_overflow=False)
                            source.stream.start_stream()
                        except Exception:
                            pass
                        
                except sr.WaitTimeoutError:
                    continue
                except Exception as e:
                    print(f"Audio loop error: {e}")

    def simulate_listen_loop(self):
        """For development: Type to test the Intent Matcher without a microphone."""
        print("\n=== Offline Voice Runtime Started ===")
        print("Type a question (or 'quit') to simulate voice input.")
        self.bb.write(conv_state="listening")
        
        while True:
            text = input("\nUser (Mic Transcript): ")
            if text.lower() == "quit":
                break
            self.process_text_input(text)


def start_text_loop(bb=None):
    """Text-based simulator for when audio drivers or LiveKit are broken."""
    print("\n=== Offline Voice Simulator ===")
    print("Type your message and press ENTER. Type 'quit' to exit.")
    
    runtime = OfflineVoiceRuntime(bb)
    while True:
        try:
            text = input("\n[You]: ")
            if text.lower() in ['quit', 'exit']:
                break
            if text.strip():
                runtime.process_text_input(text)
        except KeyboardInterrupt:
            break

if __name__ == "__main__":
    # Test wrapper for running the script directly
    class DummyBB:
        def write(self, **kwargs):
            print(f"[Blackboard Update] {kwargs}")
            
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "text":
        start_text_loop(DummyBB())
    else:
        print("\nStarting hardware microphone loop. (If this crashes, run: python runtime.py text)")
        runtime = OfflineVoiceRuntime(DummyBB())
        runtime.start_hardware_loop()

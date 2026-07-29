"""
PLC Agent - Caching Module (SQLite-backed)

Two-level cache system with SQLite persistence:
1. Tool-level cache: Caches tool execution results (same args → same result)
2. Query-level cache: Caches full agent responses for semantically similar questions

All cache data persists to disk. Restarting the application preserves cached results.
"""
import hashlib
import json
import time
import re
import sqlite3
from pathlib import Path
from typing import Optional


class SQLiteCache:
    """
    SQLite-backed cache with TTL support.
    Replaces the previous in-memory LRUCache with persistent storage.
    
    Schema:
        cache_key TEXT PRIMARY KEY
        value TEXT
        created_at REAL
        last_accessed REAL
        hit_count INTEGER
    """
    
    def __init__(self, db_path: str, table_name: str, max_size: int = 500, ttl_seconds: float = 86400):
        """
        Args:
            db_path: Path to SQLite database file
            table_name: Name of the cache table
            max_size: Maximum number of entries (LRU eviction)
            ttl_seconds: Time-to-live in seconds
        """
        self.db_path = db_path
        self.table_name = table_name
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._create_table()
    
    def _create_table(self):
        """Create the cache table if it doesn't exist."""
        try:
            self._conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table_name} (
                    cache_key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_accessed REAL NOT NULL,
                    hit_count INTEGER DEFAULT 0
                )
            """)
            self._conn.commit()
        except Exception:
            pass  # If table creation fails, cache will degrade gracefully
    
    def get(self, key: str) -> Optional[str]:
        """Get a value from cache. Returns None if not found, expired, or on DB error."""
        try:
            now = time.time()
            
            row = self._conn.execute(
                f"SELECT value, created_at FROM {self.table_name} WHERE cache_key = ?",
                (key,)
            ).fetchone()
            
            if row is None:
                return None
            
            value, created_at = row
            
            # Check TTL
            if (now - created_at) > self.ttl_seconds:
                self._conn.execute(
                    f"DELETE FROM {self.table_name} WHERE cache_key = ?", (key,)
                )
                self._conn.commit()
                return None
            
            # Update access time and hit count
            self._conn.execute(
                f"UPDATE {self.table_name} SET last_accessed = ?, hit_count = hit_count + 1 WHERE cache_key = ?",
                (now, key)
            )
            self._conn.commit()
            return value
        except Exception:
            # DB error — gracefully degrade to cache miss
            return None
    
    def put(self, key: str, value: str):
        """Store a value in cache. Silently skips on DB error."""
        try:
            now = time.time()
            
            # Upsert
            self._conn.execute(f"""
                INSERT INTO {self.table_name} (cache_key, value, created_at, last_accessed, hit_count)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(cache_key) DO UPDATE SET
                    value = excluded.value,
                    created_at = excluded.created_at,
                    last_accessed = excluded.last_accessed,
                    hit_count = 0
            """, (key, value, now, now))
            self._conn.commit()
            
            # Evict if over max size (remove least recently accessed)
            count = self._conn.execute(
                f"SELECT COUNT(*) FROM {self.table_name}"
            ).fetchone()[0]
            
            if count > self.max_size:
                excess = count - self.max_size
                self._conn.execute(f"""
                    DELETE FROM {self.table_name} WHERE cache_key IN (
                        SELECT cache_key FROM {self.table_name}
                        ORDER BY last_accessed ASC LIMIT ?
                    )
                """, (excess,))
                self._conn.commit()
        except Exception:
            # DB error — silently skip cache write
            pass
    
    def clear(self):
        """Clear all cache entries."""
        try:
            self._conn.execute(f"DELETE FROM {self.table_name}")
            self._conn.commit()
        except Exception:
            pass
    
    @property
    def stats(self) -> dict:
        """Get cache statistics."""
        try:
            row = self._conn.execute(f"""
                SELECT 
                    COUNT(*) as size,
                    COALESCE(SUM(hit_count), 0) as total_hits
                FROM {self.table_name}
            """).fetchone()
            size, total_hits = row
        except Exception:
            size, total_hits = 0, 0
        
        return {
            "size": size,
            "max_size": self.max_size,
            "total_hits": total_hits,
            "ttl_seconds": self.ttl_seconds,
            "db_path": self.db_path,
            "persistent": True,
        }


# ================================================================
# Tool-Level Cache
# ================================================================

class ToolCache:
    """
    Caches tool execution results.
    Key = tool_name + normalized_args
    
    Since the knowledge base is static (loaded once at startup),
    tool results for the same args will always be the same.
    """
    
    def __init__(self, db_path: str, max_size: int = 1000, ttl_seconds: float = 604800):
        # 7-day TTL (knowledge base doesn't change unless re-extracted)
        self._cache = SQLiteCache(
            db_path=db_path,
            table_name="tool_cache",
            max_size=max_size,
            ttl_seconds=ttl_seconds,
        )
    
    def _make_key(self, tool_name: str, **kwargs) -> str:
        """Generate cache key from tool name and arguments."""
        args_str = json.dumps(kwargs, sort_keys=True, default=str)
        raw_key = f"{tool_name}:{args_str}"
        return hashlib.md5(raw_key.encode()).hexdigest()
    
    def get(self, tool_name: str, **kwargs) -> Optional[str]:
        """Try to get cached tool result."""
        key = self._make_key(tool_name, **kwargs)
        return self._cache.get(key)
    
    def put(self, tool_name: str, result: str, **kwargs):
        """Cache a tool result."""
        key = self._make_key(tool_name, **kwargs)
        self._cache.put(key, result)
    
    @property
    def stats(self) -> dict:
        return self._cache.stats


# ================================================================
# Query-Level Cache (Semantic)
# ================================================================

class QueryCache:
    """
    Caches full agent responses for similar queries.
    
    Uses normalized query matching:
    - Extracts device names (F1, M7, X1A, etc.) as the semantic key
    - Normalizes question intent (trigger/cause/reset/list/overview)
    - Same device + same intent = cache hit
    
    Only caches single-turn responses (not multi-turn context-dependent).
    """
    
    def __init__(self, db_path: str, max_size: int = 500, ttl_seconds: float = 86400):
        # 24-hour TTL for LLM responses
        self._cache = SQLiteCache(
            db_path=db_path,
            table_name="query_cache",
            max_size=max_size,
            ttl_seconds=ttl_seconds,
        )
    
    def _normalize_query(self, query: str) -> Optional[str]:
        """
        Normalize a query into a canonical cache key.
        Returns None if the query is not cacheable (e.g., context-dependent).
        """
        query_lower = query.strip().lower()
        
        # Skip caching for context-dependent queries (pronouns, references)
        context_markers = [
            "这个", "那个", "上面", "前面", "它", "该",
            "this", "that", "it", "above", "previous",
        ]
        for marker in context_markers:
            if marker in query_lower:
                return None  # Not cacheable
        
        # Extract device names (F1, M7, X1A, T0, D500, B16FD, etc.)
        # Use lookaround instead of \b for Chinese text compatibility
        devices = re.findall(r'(?<![A-Z0-9])([FMXYTCDRBLW]\d+[A-F0-9]*)(?![A-Z0-9])', query.upper())
        devices = sorted(set(devices))
        
        # Detect query intent
        intent = self._detect_intent(query_lower)
        
        if not devices and intent not in ("overview", "list_all"):
            return None  # Can't create meaningful cache key
        
        # Build normalized key
        key = f"{intent}:{','.join(devices)}"
        return key
    
    def _detect_intent(self, query_lower: str) -> str:
        """Detect the user's query intent for cache key generation."""
        # List / overview (check BEFORE trace, since "多少报警" contains "报警")
        if any(w in query_lower for w in [
            "列出", "多少", "概览", "所有", "list", "overview", "how many",
            "全部", "统计", "总览", "系统"
        ]):
            if any(w in query_lower for w in ["section", "段", "servo", "water", "cv"]):
                return "list_section"
            return "list_all"
        
        # Reset / clear (check before trace, since "报警怎么清除" contains "报警")
        if any(w in query_lower for w in [
            "清除", "复位", "reset", "clear", "消除", "怎么解除", "怎么消"
        ]):
            return "reset"
        
        # Alarm trace / cause
        if any(w in query_lower for w in [
            "原因", "触发", "为什么", "cause", "trigger", "why", "root",
            "怎么回事", "什么情况", "报警"
        ]):
            return "trace"
        
        # Device info
        if any(w in query_lower for w in [
            "是什么", "什么设备", "信息", "info", "what is", "哪里用"
        ]):
            return "device_info"
        
        # Related rules
        if any(w in query_lower for w in [
            "规则", "使用", "涉及", "关联", "rules", "related", "involve",
            "影响", "哪些", "depend"
        ]):
            return "related"
        
        # Default: treat as trace if device found
        return "trace"
    
    def get(self, query: str) -> Optional[str]:
        """Try to get a cached response for this query."""
        key = self._normalize_query(query)
        if key is None:
            return None  # Not cacheable
        return self._cache.get(key)
    
    def put(self, query: str, response: str):
        """Cache a response for this query."""
        key = self._normalize_query(query)
        if key is None:
            return  # Don't cache context-dependent queries
        self._cache.put(key, response)
    
    def is_cacheable(self, query: str) -> bool:
        """Check if a query is cacheable."""
        return self._normalize_query(query) is not None
    
    @property
    def stats(self) -> dict:
        return self._cache.stats


# ================================================================
# Global Cache Instances (SQLite-backed, persistent)
# ================================================================

def _get_cache_db_path() -> str:
    """Get the path to the cache database file."""
    from plc_agent.config import PROJECT_ROOT
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    return str(data_dir / "cache.db")


# Lazy initialization to avoid circular imports
_tool_cache: Optional[ToolCache] = None
_query_cache: Optional[QueryCache] = None


def get_tool_cache() -> ToolCache:
    """Get the global tool cache instance."""
    global _tool_cache
    if _tool_cache is None:
        db_path = _get_cache_db_path()
        _tool_cache = ToolCache(db_path=db_path, max_size=1000, ttl_seconds=604800)
    return _tool_cache


def get_query_cache() -> QueryCache:
    """Get the global query cache instance."""
    global _query_cache
    if _query_cache is None:
        db_path = _get_cache_db_path()
        _query_cache = QueryCache(db_path=db_path, max_size=500, ttl_seconds=86400)
    return _query_cache


def get_all_cache_stats() -> dict:
    """Get statistics for all caches."""
    return {
        "tool_cache": get_tool_cache().stats,
        "query_cache": get_query_cache().stats,
    }


def clear_all_caches():
    """Clear all caches."""
    get_tool_cache()._cache.clear()
    get_query_cache()._cache.clear()

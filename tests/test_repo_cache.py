"""Tests for the Git repository LRU cache (file handle management).

These tests verify the _LRURepoCache class behavior to prevent EMFILE errors
under heavy load. Addresses GitHub issue #59.

Reference: mcp_agent_mail-jto (Bug: File handle exhaustion)
"""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import MagicMock, patch

from mcp_agent_mail.storage import (
    _LRURepoCache,
    clear_repo_cache,
    get_repo_cache_stats,
)


class TestLRURepoCacheBasics:
    """Test basic LRU cache operations."""

    def test_cache_default_maxsize_is_16(self):
        """Default maxsize should be 16 (increased from 8 for better concurrency)."""
        cache = _LRURepoCache()
        assert cache._maxsize == 16

    def test_cache_custom_maxsize(self):
        """Custom maxsize should be respected."""
        cache = _LRURepoCache(maxsize=4)
        assert cache._maxsize == 4

    def test_cache_minimum_maxsize_is_1(self):
        """Maxsize should be at least 1."""
        cache = _LRURepoCache(maxsize=0)
        assert cache._maxsize == 1
        cache = _LRURepoCache(maxsize=-5)
        assert cache._maxsize == 1

    def test_put_and_get(self):
        """Basic put and get operations should work."""
        cache = _LRURepoCache(maxsize=4)
        mock_repo = MagicMock()

        cache.put("/path/to/repo", mock_repo)
        assert cache.get("/path/to/repo") is mock_repo
        assert len(cache) == 1

    def test_peek_does_not_update_lru_order(self):
        """Peek should not update LRU order."""
        cache = _LRURepoCache(maxsize=4)
        repo1 = MagicMock()
        repo2 = MagicMock()

        cache.put("repo1", repo1)
        cache.put("repo2", repo2)

        # Peek at repo1 - should NOT move it to end
        assert cache.peek("repo1") is repo1

        # Order should still be [repo1, repo2] (oldest first)
        assert cache._order == ["repo1", "repo2"]

    def test_get_updates_lru_order(self):
        """Get should update LRU order (move to most recently used)."""
        cache = _LRURepoCache(maxsize=4)
        repo1 = MagicMock()
        repo2 = MagicMock()

        cache.put("repo1", repo1)
        cache.put("repo2", repo2)

        # Get repo1 - should move it to end
        assert cache.get("repo1") is repo1

        # Order should now be [repo2, repo1]
        assert cache._order == ["repo2", "repo1"]

    def test_contains(self):
        """Contains check should work."""
        cache = _LRURepoCache(maxsize=4)
        mock_repo = MagicMock()

        cache.put("repo1", mock_repo)
        assert "repo1" in cache
        assert "repo2" not in cache


class TestLRURepoCacheEviction:
    """Test LRU eviction behavior."""

    def test_eviction_at_capacity(self):
        """Oldest repos should be evicted when at capacity."""
        cache = _LRURepoCache(maxsize=2)
        repo1 = MagicMock()
        repo2 = MagicMock()
        repo3 = MagicMock()

        cache.put("repo1", repo1)
        cache.put("repo2", repo2)

        # Verify repo1 is in cache before eviction
        assert "repo1" in cache
        assert len(cache) == 2

        cache.put("repo3", repo3)  # This should evict repo1

        assert len(cache) == 2
        assert "repo1" not in cache  # Evicted from cache
        assert "repo2" in cache
        assert "repo3" in cache
        # repo1 was evicted - it's either in _evicted list or was cleaned up
        # (depending on refcount at cleanup time). Key assertion is it's no longer in cache.

    def test_evicted_repos_added_to_evicted_list(self):
        """Evicted repos should be tracked for later cleanup with timestamps."""
        cache = _LRURepoCache(maxsize=1)
        repo1 = MagicMock()
        repo2 = MagicMock()

        cache.put("repo1", repo1)

        # Mock cleanup to prevent immediate cleanup and verify eviction mechanism
        evicted_during_put: list = []
        original_cleanup = cache._cleanup_evicted
        def tracking_cleanup(**kwargs: Any) -> int:
            # Record what's in evicted list before cleanup runs
            evicted_during_put.extend(cache._evicted)
            return original_cleanup(**kwargs)
        cache_any = cast(Any, cache)
        cache_any._cleanup_evicted = tracking_cleanup

        cache.put("repo2", repo2)  # Evicts repo1

        assert len(cache) == 1
        assert "repo2" in cache
        # Verify repo1 was added to evicted list as (repo, timestamp) tuple
        evicted_repos = [r for r, _ts in evicted_during_put]
        assert repo1 in evicted_repos

    def test_duplicate_put_updates_lru_order(self):
        """Putting same key again should update LRU order without eviction."""
        cache = _LRURepoCache(maxsize=2)
        repo1 = MagicMock()
        repo2 = MagicMock()

        cache.put("repo1", repo1)
        cache.put("repo2", repo2)
        cache.put("repo1", repo1)  # Update LRU order, don't evict

        assert len(cache) == 2
        assert cache._order == ["repo2", "repo1"]


class TestLRURepoCacheCleanup:
    """Test cleanup behavior for evicted repos."""

    def test_cleanup_evicted_returns_count(self):
        """_cleanup_evicted should return count of closed repos after grace period."""
        cache = _LRURepoCache(maxsize=1)

        # Add a mock repo to evicted list with a timestamp far in the past
        mock_repo = MagicMock()
        cache._evicted.append((mock_repo, time.monotonic() - cache.EVICTION_GRACE_SECONDS - 10))

        closed = cache._cleanup_evicted()

        assert closed == 1
        mock_repo.close.assert_called_once()
        assert len(cache._evicted) == 0

    def test_cleanup_keeps_recently_evicted_repos(self):
        """Repos still within their grace period should not be closed."""
        cache = _LRURepoCache(maxsize=1)

        mock_repo = MagicMock()
        # Evicted just now -- well within the grace period
        cache._evicted.append((mock_repo, time.monotonic()))

        closed = cache._cleanup_evicted()

        assert closed == 0
        mock_repo.close.assert_not_called()
        evicted_repos = [r for r, _ts in cache._evicted]
        assert mock_repo in evicted_repos

    def test_force_cleanup_ignores_grace_period(self):
        """force=True should close repos regardless of grace period."""
        cache = _LRURepoCache(maxsize=1)

        mock_repo = MagicMock()
        # Evicted just now -- still in grace period
        cache._evicted.append((mock_repo, time.monotonic()))

        closed = cache._cleanup_evicted(force=True)

        assert closed == 1
        mock_repo.close.assert_called_once()
        assert len(cache._evicted) == 0

    def test_clear_closes_all_repos(self):
        """Clear should close all cached and evicted repos."""
        cache = _LRURepoCache(maxsize=4)
        repo1 = MagicMock()
        repo2 = MagicMock()
        evicted_repo = MagicMock()

        cache.put("repo1", repo1)
        cache.put("repo2", repo2)
        cache._evicted.append((evicted_repo, time.monotonic()))

        count = cache.clear()

        assert count == 3
        repo1.close.assert_called_once()
        repo2.close.assert_called_once()
        evicted_repo.close.assert_called_once()
        assert len(cache) == 0
        assert len(cache._evicted) == 0


class TestLRURepoCacheStats:
    """Test statistics and monitoring."""

    def test_evicted_count_property(self):
        """evicted_count should return number of evicted repos."""
        cache = _LRURepoCache(maxsize=1)
        assert cache.evicted_count == 0

        cache._evicted.append((MagicMock(), time.monotonic()))
        cache._evicted.append((MagicMock(), time.monotonic()))
        assert cache.evicted_count == 2

    def test_stats_property(self):
        """stats should return cache statistics."""
        cache = _LRURepoCache(maxsize=8)
        cache.put("repo1", MagicMock())
        cache._evicted.append((MagicMock(), time.monotonic()))

        stats = cache.stats
        assert stats == {"cached": 1, "evicted": 1, "maxsize": 8}


class TestLRURepoCacheOpportunisticCleanup:
    """Test opportunistic cleanup on get operations."""

    def test_cleanup_triggered_every_4th_get(self):
        """Cleanup should run every 4th get operation."""
        cache = _LRURepoCache(maxsize=4)
        repo = MagicMock()
        cache.put("repo", repo)

        # Track cleanup calls
        cleanup_calls = 0
        original_cleanup = cache._cleanup_evicted
        def tracking_cleanup():
            nonlocal cleanup_calls
            cleanup_calls += 1
            return original_cleanup()
        cache_any = cast(Any, cache)
        cache_any._cleanup_evicted = tracking_cleanup

        # 3 gets - no cleanup yet
        cache.get("repo")
        cache.get("repo")
        cache.get("repo")
        assert cleanup_calls == 0

        # 4th get triggers cleanup
        cache.get("repo")
        assert cleanup_calls == 1

        # Next 4 gets trigger another cleanup
        cache.get("repo")
        cache.get("repo")
        cache.get("repo")
        cache.get("repo")
        assert cleanup_calls == 2


class TestModuleLevelFunctions:
    """Test module-level cache functions."""

    def test_clear_repo_cache_returns_count(self):
        """clear_repo_cache should return count of closed repos."""
        # This uses the global cache - just verify it doesn't crash
        count = clear_repo_cache()
        assert isinstance(count, int)
        assert count >= 0

    def test_get_repo_cache_stats_returns_dict(self):
        """get_repo_cache_stats should return statistics dict."""
        stats = get_repo_cache_stats()
        assert isinstance(stats, dict)
        assert "cached" in stats
        assert "evicted" in stats
        assert "maxsize" in stats
        assert stats["maxsize"] == 16  # Default is now 16


class TestLRURepoCacheWarningLogging:
    """Test warning logs when evicted list grows large."""

    def test_warning_logged_when_evicted_exceeds_maxsize(self):
        """Warning should be logged when evicted list exceeds maxsize."""
        cache = _LRURepoCache(maxsize=2)

        # Add many repos to evicted list, all recently evicted (within grace period)
        now = time.monotonic()
        for _ in range(5):
            mock_repo = MagicMock()
            cache._evicted.append((mock_repo, now))

        # Repos are within grace period so they won't be cleaned up,
        # and the warning should fire because len(still_pending) > maxsize
        with patch("mcp_agent_mail.storage._logger") as mock_logger:
            cache._cleanup_evicted()

            # Warning should have been logged
            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args
            assert call_args[0][0] == "repo_cache.evicted_backlog"

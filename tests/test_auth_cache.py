"""
Tests for AuthorizationCache with thread-safety.
"""

import time
import pytest
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

from app.utils.auth_cache import AuthorizationCache, get_auth_cache, check_authorization_cached


class TestAuthorizationCache:
    """Tests for AuthorizationCache class."""
    
    @pytest.fixture
    def cache(self):
        """Create a fresh AuthorizationCache for each test."""
        return AuthorizationCache(ttl=5, maxsize=10)
    
    def test_init_default_values(self):
        """Test initialization with default values."""
        cache = AuthorizationCache()
        assert cache.ttl == 60
        assert cache.maxsize == 1000
        assert cache.size() == 0
    
    def test_init_custom_values(self):
        """Test initialization with custom values."""
        cache = AuthorizationCache(ttl=120, maxsize=500)
        assert cache.ttl == 120
        assert cache.maxsize == 500
    
    def test_get_cache_miss(self, cache):
        """Test get returns cache miss for unknown key."""
        is_cached, result = cache.get("auth_1", "99999")
        assert is_cached is False
        assert result is False
    
    def test_set_and_get(self, cache):
        """Test set followed by get returns correct value."""
        cache.set("auth_1", "12345", True)
        
        is_cached, result = cache.get("auth_1", "12345")
        
        assert is_cached is True
        assert result is True
    
    def test_set_false_value(self, cache):
        """Test caching False authorization result."""
        cache.set("auth_1", "12345", False)
        
        is_cached, result = cache.get("auth_1", "12345")
        
        assert is_cached is True
        assert result is False
    
    def test_ttl_expiration(self):
        """Test that cache entries expire after TTL."""
        cache = AuthorizationCache(ttl=1, maxsize=10)  # 1 second TTL
        
        cache.set("auth_1", "12345", True)
        
        # Should be cached immediately
        is_cached, _ = cache.get("auth_1", "12345")
        assert is_cached is True
        
        # Wait for expiration
        time.sleep(1.5)
        
        # Should be expired now
        is_cached, _ = cache.get("auth_1", "12345")
        assert is_cached is False
    
    def test_maxsize_eviction(self):
        """Test that oldest entries are evicted when maxsize is reached."""
        cache = AuthorizationCache(ttl=60, maxsize=3)
        
        # Fill cache
        cache.set("auth_1", "naan_1", True)
        cache.set("auth_2", "naan_2", True)
        cache.set("auth_3", "naan_3", True)
        
        assert cache.size() == 3
        
        # Add one more, should evict oldest
        cache.set("auth_4", "naan_4", True)
        
        assert cache.size() == 3
        
        # First entry should be evicted
        is_cached, _ = cache.get("auth_1", "naan_1")
        assert is_cached is False
        
        # Later entries should still be cached
        is_cached, _ = cache.get("auth_4", "naan_4")
        assert is_cached is True
    
    def test_clear(self, cache):
        """Test clearing the cache."""
        cache.set("auth_1", "12345", True)
        cache.set("auth_2", "67890", False)
        
        assert cache.size() == 2
        
        cache.clear()
        
        assert cache.size() == 0
        is_cached, _ = cache.get("auth_1", "12345")
        assert is_cached is False
    
    def test_size(self, cache):
        """Test size method."""
        assert cache.size() == 0
        
        cache.set("auth_1", "naan_1", True)
        assert cache.size() == 1
        
        cache.set("auth_2", "naan_2", True)
        assert cache.size() == 2
    
    def test_different_keys_independent(self, cache):
        """Test that different authority/naan combinations are independent."""
        cache.set("auth_1", "naan_1", True)
        cache.set("auth_1", "naan_2", False)
        cache.set("auth_2", "naan_1", False)
        
        _, result1 = cache.get("auth_1", "naan_1")
        _, result2 = cache.get("auth_1", "naan_2")
        _, result3 = cache.get("auth_2", "naan_1")
        
        assert result1 is True
        assert result2 is False
        assert result3 is False
    
    def test_update_existing_key(self, cache):
        """Test that setting an existing key updates the value."""
        cache.set("auth_1", "12345", True)
        cache.set("auth_1", "12345", False)  # Update
        
        is_cached, result = cache.get("auth_1", "12345")
        
        assert is_cached is True
        assert result is False
    
    # Thread-safety tests
    
    def test_thread_safety_concurrent_sets(self):
        """Test that concurrent set operations are thread-safe."""
        cache = AuthorizationCache(ttl=60, maxsize=100)
        errors = []
        
        def set_value(idx):
            try:
                cache.set(f"auth_{idx}", f"naan_{idx}", idx % 2 == 0)
            except Exception as e:
                errors.append(str(e))
        
        threads = [threading.Thread(target=set_value, args=(i,)) for i in range(50)]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Errors: {errors}"
        assert cache.size() == 50
    
    def test_thread_safety_concurrent_gets(self):
        """Test that concurrent get operations are thread-safe."""
        cache = AuthorizationCache(ttl=60, maxsize=100)
        
        # Pre-populate cache
        for i in range(20):
            cache.set(f"auth_{i}", f"naan_{i}", True)
        
        errors = []
        results = []
        
        def get_value(idx):
            try:
                is_cached, result = cache.get(f"auth_{idx % 20}", f"naan_{idx % 20}")
                results.append((is_cached, result))
            except Exception as e:
                errors.append(str(e))
        
        threads = [threading.Thread(target=get_value, args=(i,)) for i in range(100)]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Errors: {errors}"
        assert all(is_cached and result for is_cached, result in results)
    
    def test_thread_safety_mixed_operations(self):
        """Test thread-safety with mixed get/set/clear operations."""
        cache = AuthorizationCache(ttl=60, maxsize=100)
        errors = []
        
        def worker(idx):
            try:
                operation = idx % 3
                if operation == 0:
                    cache.set(f"auth_{idx}", "naan", True)
                elif operation == 1:
                    cache.get(f"auth_{idx}", "naan")
                else:
                    cache.size()
            except Exception as e:
                errors.append(str(e))
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker, i) for i in range(100)]
            for f in as_completed(futures):
                f.result()  # Raises any exceptions
        
        assert len(errors) == 0, f"Errors: {errors}"
    
    def test_thread_safety_eviction_under_load(self):
        """Test that eviction doesn't cause race conditions."""
        cache = AuthorizationCache(ttl=60, maxsize=10)  # Small maxsize
        errors = []
        
        def rapid_sets(thread_id):
            try:
                for i in range(50):
                    cache.set(f"auth_{thread_id}_{i}", "naan", True)
            except Exception as e:
                errors.append(str(e))
        
        threads = [threading.Thread(target=rapid_sets, args=(t,)) for t in range(5)]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Errors: {errors}"
        # Should not exceed maxsize
        assert cache.size() <= 10


class TestGetAuthCache:
    """Tests for get_auth_cache function."""
    
    def test_returns_singleton(self):
        """Test that get_auth_cache returns the same instance."""
        # Reset global cache
        import app.utils.auth_cache as cache_module
        cache_module._auth_cache = None
        
        with patch.object(cache_module, 'get_settings') as mock_settings:
            mock_settings.return_value = MagicMock(
                auth_cache_ttl=60,
                auth_cache_maxsize=1000
            )
            
            cache1 = get_auth_cache()
            cache2 = get_auth_cache()
            
            assert cache1 is cache2


class TestCheckAuthorizationCached:
    """Tests for check_authorization_cached function."""
    
    @pytest.fixture
    def mock_orchestrator(self):
        """Create a mock orchestrator."""
        mock = MagicMock()
        mock.is_authorized_for_naan.return_value = True
        return mock
    
    @pytest.fixture(autouse=True)
    def reset_cache(self):
        """Reset global cache before each test."""
        import app.utils.auth_cache as cache_module
        cache_module._auth_cache = None
        yield
        cache_module._auth_cache = None
    
    def test_cache_miss_queries_blockchain(self, mock_orchestrator):
        """Test that cache miss queries the blockchain."""
        with patch('app.utils.auth_cache.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock(
                auth_cache_ttl=60,
                auth_cache_maxsize=1000
            )
            
            result = check_authorization_cached(
                mock_orchestrator, "auth_1", "12345"
            )
            
            assert result is True
            mock_orchestrator.is_authorized_for_naan.assert_called_once_with("auth_1", "12345")
    
    def test_cache_hit_skips_blockchain(self, mock_orchestrator):
        """Test that cache hit doesn't query blockchain."""
        with patch('app.utils.auth_cache.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock(
                auth_cache_ttl=60,
                auth_cache_maxsize=1000
            )
            
            # First call - cache miss
            check_authorization_cached(mock_orchestrator, "auth_1", "12345")
            
            # Second call - should be cached
            result = check_authorization_cached(mock_orchestrator, "auth_1", "12345")
            
            assert result is True
            # Should only be called once (first time)
            assert mock_orchestrator.is_authorized_for_naan.call_count == 1
    
    def test_blockchain_error_returns_false(self, mock_orchestrator):
        """Test that blockchain errors return False (not cached)."""
        mock_orchestrator.is_authorized_for_naan.side_effect = Exception("Connection error")
        
        with patch('app.utils.auth_cache.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock(
                auth_cache_ttl=60,
                auth_cache_maxsize=1000
            )
            
            result = check_authorization_cached(mock_orchestrator, "auth_1", "12345")
            
            assert result is False
    
    def test_negative_result_is_cached(self, mock_orchestrator):
        """Test that negative authorization results are cached."""
        mock_orchestrator.is_authorized_for_naan.return_value = False
        
        with patch('app.utils.auth_cache.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock(
                auth_cache_ttl=60,
                auth_cache_maxsize=1000
            )
            
            # First call
            result1 = check_authorization_cached(mock_orchestrator, "auth_1", "12345")
            
            # Second call - should be cached
            result2 = check_authorization_cached(mock_orchestrator, "auth_1", "12345")
            
            assert result1 is False
            assert result2 is False
            assert mock_orchestrator.is_authorized_for_naan.call_count == 1

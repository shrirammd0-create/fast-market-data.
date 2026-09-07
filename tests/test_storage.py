from src.storage.cache import JsonCache
from src.storage.text_store import append_block


def test_append_block_creates_and_separates(tmp_path):
    path = tmp_path / "market_updates.txt"
    append_block(path, "block one")
    append_block(path, "block two\n")
    content = path.read_text()
    assert content == "block one\n\nblock two\n"


def test_json_cache_roundtrip(tmp_path):
    cache = JsonCache(tmp_path)
    cache.set("last_close", 3646.1)
    assert cache.get("last_close") == 3646.1


def test_json_cache_ttl_expiry(tmp_path):
    cache = JsonCache(tmp_path)
    cache.set("k", 1)
    assert cache.get("k", max_age_seconds=3600) == 1
    assert cache.get("k", max_age_seconds=-1) is None


def test_json_cache_corrupt_file_is_a_miss(tmp_path):
    cache = JsonCache(tmp_path)
    cache.set("k", 1)
    cache._path("k").write_text("{not json")
    assert cache.get("k") is None

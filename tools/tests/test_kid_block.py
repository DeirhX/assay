"""Direct-buy-block registry: mark / query / persistence.

The suite's autouse ``_isolate_kid_block`` fixture points the backing file at a
throwaway path, so every test starts from an empty registry and never touches
the real ``data/cache/kid-blocked.json``.
"""

from __future__ import annotations

import kid_block


def test_empty_registry_blocks_nothing():
    assert kid_block.blocked_symbols() == set()
    assert kid_block.is_blocked("XSD") is False


def test_mark_then_query_and_persist():
    assert kid_block.mark_blocked("xsd", "confirmed KID reject") is True
    assert kid_block.is_blocked("XSD") is True
    assert kid_block.blocked_symbols() == {"XSD"}
    # Persisted to disk: a fresh read (new set) still sees it.
    assert "XSD" in kid_block.blocked_symbols()


def test_mark_is_case_insensitive_and_idempotent():
    assert kid_block.mark_blocked("SOXX") is True
    # Same name, different case -> already present, no second write.
    assert kid_block.mark_blocked("soxx") is False
    assert kid_block.blocked_symbols() == {"SOXX"}
    assert kid_block.is_blocked(" soxx ".strip()) is True


def test_blank_symbol_is_ignored():
    assert kid_block.mark_blocked("") is False
    assert kid_block.mark_blocked("   ") is False
    assert kid_block.is_blocked(None) is False
    assert kid_block.blocked_symbols() == set()

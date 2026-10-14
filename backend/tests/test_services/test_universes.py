"""Hygiene tests for scan universes (issue #48).

PR #54's first cut shipped a from-memory SP400 list: 355 entries, internal
duplicates, 190 non-constituents (several acquired/delisted). These tests
make that class of rot loud: constituent lists must be duplicate-free,
disjoint where claimed, and sized like the index they name.
"""

from app.services.universes import (
    SP400_FULL,
    SP500_FULL,
    SP500_SAMPLE,
    UNIVERSES,
    get_combined_universe,
    get_universe,
)


def test_sp400_sized_like_the_index():
    # 400 constituents minus Moog (MOG-A), whose membership is unconfirmed
    assert 395 <= len(SP400_FULL) <= 400


def test_sp500_sized_like_the_index():
    assert 495 <= len(SP500_FULL) <= 510


def test_no_duplicates_within_lists():
    assert len(SP400_FULL) == len(set(SP400_FULL))
    assert len(SP500_FULL) == len(set(SP500_FULL))


def test_sp400_disjoint_from_sp500():
    assert not set(SP400_FULL) & set(SP500_FULL)


def test_combined_is_exact_union():
    combined = get_combined_universe()
    assert set(combined) == set(SP500_FULL) | set(SP400_FULL)
    assert combined == sorted(combined)


def test_combined_is_a_scannable_universe():
    assert "combined" in UNIVERSES
    assert UNIVERSES["combined"] == get_combined_universe()


def test_every_universe_is_described():
    from app.services.universes import UNIVERSE_DESCRIPTIONS

    assert set(UNIVERSE_DESCRIPTIONS) == set(UNIVERSES)


def test_unknown_universe_returns_empty():
    # The scan route relies on this to 400 unknown names instead of
    # silently scanning a fallback list.
    assert get_universe("robinhood") == []
    assert get_universe("quick") == []


def test_symbols_are_fmp_compatible():
    # Uppercase A-Z0-9 with optional -class suffix (BRK-B style); no
    # whitespace or lowercase that would break the quote URLs.
    import re

    pattern = re.compile(r"^[A-Z][A-Z0-9]*(-[A-Z])?$")
    for sym in SP400_FULL + SP500_FULL + SP500_SAMPLE:
        assert pattern.match(sym), f"suspicious symbol: {sym!r}"


def test_no_static_symbol_uses_dot_spelling():
    # Our FMP plan answers 402 for BRK.B and serves BRK-B (#127). A dotted
    # symbol silently returns no data in every scan of that universe.
    for name, symbols in UNIVERSES.items():
        dotted = [s for s in symbols if "." in s]
        assert not dotted, f"{name} has dotted symbols: {dotted}"

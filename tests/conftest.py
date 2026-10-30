"""Keep all financial test data independent of real customer configuration."""
import pytest
import src.client_split as split

@pytest.fixture(autouse=True)
def fictional_clients(monkeypatch):
    groups = tuple((f"Example CMS {letter}", f"EXAMPLE_CMS_{letter}") for letter in "ABCDEFGH")
    monkeypatch.setattr(split, "EXISTING_CMS_GROUPS", groups)
    monkeypatch.setattr(split, "CMS_RATE_SOURCE_NAMES", {gid: name for name, gid in groups})
    aliases = {split.normalize_name(name): name for name, _ in groups}
    aliases.update({"example cms a legal": "Example CMS A", "example cms f legal": "Example CMS F"})
    monkeypatch.setattr(split, "CMS_PARTNER_ALIASES", aliases)

"""Admin-added aliases for banks, products and cities."""
import pytest

from app.rag import entities
from app.services import aliases
from app.services import banker_directory as bd


@pytest.fixture(autouse=True)
def clean():
    yield
    for a in aliases.listing():
        aliases.remove(a["kind"], a["alias"])


def test_aliases_are_used_by_every_matcher(monkeypatch):
    monkeypatch.setattr(bd, "rebuild_quietly", lambda: None)
    assert entities.find_banks("bhfl rm in pune") == []
    aliases.add("bank", "BHFL", "Bajaj Finserv", by="admin@example.com")
    assert entities.find_banks("bhfl rm in pune") == ["Bajaj Finserv"]
    assert "bhfl" in entities.bank_aliases("Bajaj Finserv")
    assert bd.canonical_lender("BHFL") == ("Bajaj Finserv", True)
    aliases.add("product", "Super Saver HL", "Home Loan")
    assert entities.find_products("super saver hl policy") == ["Home Loan"]
    assert bd.canonical_product("Super Saver HL") == "Home Loan"
    aliases.add("city", "Pimpri", "Pune")
    assert bd.canonical_city("PIMPRI") == "Pune"
    aliases.add("bank", "Zentrix", "Zentrix Housing Finance")                # a new lender
    assert entities.find_banks("zentrix housing finance payout") == ["Zentrix Housing Finance"]

    assert aliases.remove("bank", "bhfl") and entities.find_banks("bhfl rm in pune") == []
    assert bd.canonical_lender("BHFL")[1] is False


def test_an_alias_cannot_take_another_banks_name(monkeypatch):
    monkeypatch.setattr(bd, "rebuild_quietly", lambda: None)
    with pytest.raises(ValueError, match="already means HDFC Bank"):
        aliases.add("bank", "hdfc", "ICICI Bank")
    with pytest.raises(ValueError):
        aliases.add("lender", "x y", "Y")


def test_a_new_alias_marks_the_directory_out_of_date(monkeypatch):
    monkeypatch.setattr(bd, "rebuild_quietly", lambda: None)
    before = bd.fingerprint([])
    aliases.add("city", "Pimpri", "Pune")
    assert bd.fingerprint([]) != before

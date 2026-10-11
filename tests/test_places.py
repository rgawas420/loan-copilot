"""Lender branches for the map under customer-profile answers (OpenStreetMap / Nominatim, mocked here)."""
import pytest

from app.core.config import get_settings
from app.services import places

PUNE = {"name": "Pune", "lat": 18.52, "lon": 73.85, "boundingbox": ["18.36", "18.68", "73.69", "74.01"]}


def _row(name, lat, lon, typ="bank", road="FC Road", suburb="Shivajinagar"):
    return {"name": name, "lat": str(lat), "lon": str(lon), "category": "amenity", "type": typ, "osm_type": "node",
            "osm_id": 1, "address": {"road": road, "suburb": suburb}, "extratags": {}}


@pytest.fixture
def nominatim(monkeypatch, tmp_path):
    """Fake Nominatim: the city, then rows per lender query; records every query."""
    monkeypatch.setattr(get_settings(), "places_cache_db", tmp_path / "places.db")
    calls, data = [], {}

    def search(params):
        calls.append(params["q"])
        if params["q"].endswith(", India"):
            return [PUNE]
        return next((v for k, v in data.items() if k.lower() == params["q"].lower()), [])   # case-insensitive, like Nominatim
    monkeypatch.setattr(places, "_search", search)
    return calls, data


def test_branches_are_nearest_first_without_atms_or_other_banks(nominatim):
    _, data = nominatim
    data["Axis Bank"] = [_row("Axis Bank", 18.60, 73.80), _row("Axis Bank", 18.53, 73.85), _row("Axis Bank ATM", 18.52, 73.85, "atm")]
    data["ICICI Home Finance"] = [_row("ICICI Bank", 18.52, 73.85)]            # a different lender: not a match
    out = places.find(["AXIS BANK", "ICICI Home Finance"], "Pune")
    assert [p["name"] for p in out["places"]] == ["Axis Bank", "Axis Bank"]
    assert out["places"][0]["km"] < out["places"][1]["km"]
    assert out["places"][0]["directions"].startswith("https://www.google.com/maps/dir/?api=1&destination=18.53")
    assert out["places"][0]["address"] == "FC Road, Shivajinagar"
    assert [m["lender"] for m in out["missing"]] == ["ICICI Home Finance"]
    assert out["city"] == "Pune" and "OpenStreetMap" in out["attribution"]


def test_every_lender_gets_its_nearest_branch_before_second_ones(nominatim):
    _, data = nominatim
    data["Axis Bank"] = [_row("Axis Bank", 18.53, 73.85), _row("Axis Bank", 18.55, 73.85)]
    data["Central Bank of India"] = [_row("Central Bank of India", 18.54, 73.85)]
    out = places.find(["Axis Bank", "Central Bank of India"], "Pune")
    assert [p["lender"] for p in out["places"]] == ["Axis Bank", "Central Bank of India", "Axis Bank"]


def test_results_are_cached(nominatim):
    calls, data = nominatim
    data["Axis Bank"] = [_row("Axis Bank", 18.53, 73.85)]
    places.find(["Axis Bank"], "Pune")
    places.find(["Axis Bank"], "Pune")
    assert calls == ["Pune, India", "Axis Bank"]


def test_a_failed_search_is_not_cached(nominatim, monkeypatch):
    calls, _ = nominatim
    monkeypatch.setattr(places, "_search", lambda params: calls.append(params["q"]) or None)
    assert places.find(["Axis Bank"], "Pune") is None
    assert places.find(["Axis Bank"], "Pune") is None
    assert calls == ["Pune, India", "Pune, India"]


def test_lender_names_are_cleaned_for_the_search():
    assert places.clean_lender("Adani Housing Finance (Tyger)") == "Adani Housing Finance"
    assert places.clean_lender("Muthoot Homefin India ltd.") == "Muthoot Homefin India"


def test_disabled_or_no_city_shows_nothing(nominatim, monkeypatch):
    assert places.find(["Axis Bank"], "") is None
    monkeypatch.setattr(get_settings(), "places_enabled", False)
    assert places.find(["Axis Bank"], "Pune") is None

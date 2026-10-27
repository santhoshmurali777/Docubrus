import pytest
from datetime import date
from app.correlation.normalizers import *

@pytest.mark.parametrize("t", ["01/04/2026", "1 April 2026", "1st April 2026", "April 1, 2026", "2026-04-01", "01-04-26", "1-Apr-2026", "01.04.2026"])
def test_dates_equal(t): assert normalize_date(t) == date(2026, 4, 1)

@pytest.mark.parametrize("t", ["", "31/02/2026", "garbage", "13/13/2026"])
def test_bad_dates_none(t): assert normalize_date(t) is None

def test_day_first():  assert normalize_date("03/04/2026") == date(2026, 4, 3)

def test_numbers():
    assert normalize_number("Rs. 1,20,000.50") == 120000.5 and normalize_number("60 seats") == 60
    assert normalize_number("abc") is None

def test_areas():
    assert area_to_acres("250 cents") == 2.5 and area_to_acres("2.5 acres") == 2.5
    assert area_to_sqm("10,000 sq.ft") == pytest.approx(929.03, rel=1e-3)
    assert area_to_sqm("1 hectare") == 10000 and area_to_sqm("500") is None

def test_names():
    assert normalize_name("Shri A. Rao") == normalize_name("a rao")
    assert values_agree("name", "A. Rao", "Asha Rao") is True
    assert values_agree("name", "Asha Rao", "Asha Kumar") is False
    assert values_agree("name", "Rao Asha", "Asha Rao") is True
    assert values_agree("name", "B. Rao", "Asha Rao") is False

def test_survey_and_identifier():
    assert normalize_survey("S.No. 45 / 2") == normalize_survey("Survey No: 45/2") == "45/2"
    assert values_agree("survey", "45/2", "45/3") is False
    assert normalize_identifier("TN/SOC-12 345") == "TNSOC12345"

def test_address():
    assert values_agree("address", "12, Main Rd, Chromepet, Chennai", "12 Main Road Chromepet Chennai") is True
    assert values_agree("address", "12 Main Road Chromepet Chennai", "98 Hill Street Madurai") is False

def test_number_tolerance():
    assert values_agree("number", "60", "60", {"abs": 0}) is True
    assert values_agree("number", "60", "61", {"abs": 0}) is False
    assert values_agree("number", "1000", "1008", {"rel": 0.01}) is True
    assert values_agree("number", "1000", "1020", {"rel": 0.01}) is False
    assert values_agree("number", "2.50", "2.505", {"abs": 0.01}) is True

def test_unreadable_is_never_agreement():
    assert values_agree("number", "n/a", "n/a") is None
    assert values_agree("name", "", "x") is None


def test_indic_digits_compare_equal_to_ascii():
    from app.correlation.normalizers import ascii_digits, normalize_date
    assert ascii_digits("४५/२") == "45/2" and ascii_digits("௪௫/௨") == "45/2"
    assert normalize_date("०१/०४/२०२६") == normalize_date("01/04/2026")

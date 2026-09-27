import pandas as pd
from src.clean import clean_locations

def test_clean_locations_standardizes_province_and_city():
    jobs = pd.DataFrame({
        "job_state": ["Ontario"],
        "job_city": ["toronto"],
    })
    cleaned, issues = clean_locations(jobs)
    assert cleaned.loc[0, "job_province_clean"] == "ON"
    assert cleaned.loc[0, "job_city_clean"] == "Toronto"
    assert cleaned.loc[0, "market_area"] == "Toronto GTA"
    assert cleaned.loc[0, "location_status"] == "valid"
    assert issues.empty


def test_clean_locations_marks_unrecognized_province_as_invalid():
    jobs = pd.DataFrame({
        "job_state": ["Not a province"],
        "job_city": ["Somewhere"],
    })
    cleaned, issues = clean_locations(jobs)
    assert cleaned.loc[0, "location_status"] == "invalid"
    assert len(issues) == 1
    assert issues.loc[0, "review_reason"] == "invalid_or_unrecognized_province"


def test_clean_locations_marks_missing_values_partial_or_unknown():
    jobs = pd.DataFrame({
        "job_state": ["Ontario", None, None],
        "job_city": [None, "Toronto", None],
    })
    cleaned, _ = clean_locations(jobs)
    assert cleaned["location_status"].tolist() == ["partial", "partial", "unknown"]

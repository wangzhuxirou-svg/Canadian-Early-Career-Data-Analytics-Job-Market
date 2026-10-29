import pandas as pd
import pytest
from src.validate import validate_features


def make_features() -> pd.DataFrame:
    return pd.DataFrame({
        'job_id': ['job-1', 'job-2'],
        'classified_role_family': ['data_analyst', 'outside_scope'],
        'classified_role_subtype': [None, None],
        'classified_career_level': ['internship', 'unknown'],
        'technical_skills': [['python'], []],
        'soft_skills': [[], ['communication']],
        'accepted_majors': [['computer_science'], []],
        'language_requirements': [[], ['english']],
        'stated_experience_years': pd.Series([0, pd.NA], dtype='Int64'),
    })


def test_valid_features_pass_validation(caplog):
    validate_features(make_features())
    assert 'manual sampling' in caplog.text


def test_unknown_role_and_career_labels_fail_with_values():
    features = make_features()
    features.loc[0, 'classified_role_family'] = 'unexpected_role'
    features.loc[0, 'classified_career_level'] = 'unexpected_level'
    with pytest.raises(ValueError) as error:
        validate_features(features)
    assert 'unexpected_role' in str(error.value)
    assert 'unexpected_level' in str(error.value)


def test_invalid_list_value_and_negative_experience_fail():
    features = make_features()
    features.loc[0, 'technical_skills'] = 'python'
    features.loc[0, 'stated_experience_years'] = -1
    with pytest.raises(ValueError) as error:
        validate_features(features)
    assert 'technical_skills' in str(error.value)
    assert 'Negative experience' in str(error.value)


def test_duplicate_job_ids_fail():
    features = make_features()
    features.loc[1, 'job_id'] = 'job-1'
    with pytest.raises(ValueError, match='Duplicate job IDs'):
        validate_features(features)


def test_financial_risk_subtype_is_required_and_constrained():
    features = make_features()
    features.loc[0, 'classified_role_family'] = 'financial_risk_analyst'
    features.loc[0, 'classified_role_subtype'] = 'financial_analysis'
    validate_features(features)

    features.loc[0, 'classified_role_subtype'] = 'finance'
    with pytest.raises(ValueError, match='Invalid role subtypes'):
        validate_features(features)

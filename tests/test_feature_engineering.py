import pandas as pd
import pytest

from config.settings import CORE_ROLE_FAMILIES, ROLE_QUERIES, SEARCH_QUERIES
from src.feature_engineering import (
    classify_one_career_level,
    classify_career_levels,
    classify_roles,
    classify_role_title,
    classify_role_title_details,
    engineer_features,
)


def test_classifies_core_role():
    assert classify_role_title('Junior Data Analyst') == 'data_analyst'


def test_classifies_hybrid_and_review_roles():
    assert classify_role_title('Data Analyst and Data Scientist') == 'hybrid'
    assert classify_role_title('Product Analyst') == 'product_analyst'
    assert classify_role_title('Operations Analyst') == 'operations_analyst'
    assert classify_role_title('Marketing Analyst') == 'adjacent_hybrid'
    assert classify_role_title('Unmatched Job Title') == 'other'


POSITIVE_ROLE_TITLES = {
    'data_analyst': [
        'Data Analyst', 'Junior Data Analyst', 'Data Analytics Analyst',
        'Data Analyst Co-op', 'Healthcare Data Analyst',
    ],
    'business_analyst': [
        'Business Analyst', 'Junior Business Analyst', 'Business Systems Analyst',
        'Business Process Analyst', 'Technical Business Analyst',
        'Analyst, Product Management',
    ],
    'bi_analyst': [
        'BI Analyst', 'Business Intelligence Analyst', 'Reporting Analyst',
        'Analyst, Business Intelligence', 'Senior BI Analyst',
    ],
    'data_scientist': [
        'Data Scientist', 'Junior Data Scientist', 'Applied Data Scientist',
        'Data Science Associate', 'Machine Learning Data Scientist',
    ],
    'data_engineer': [
        'Data Engineer', 'Junior Data Engineer', 'Analytics Engineer',
        'ETL Developer', 'Data Platform Engineer',
    ],
    'product_analyst': [
        'Product Analyst', 'Junior Product Analyst', 'Product Insights Analyst',
        'Product Analytics Analyst', 'Product Analyst - Monetization',
    ],
    'operations_analyst': [
        'Operations Analyst', 'Junior Operations Analyst', 'Operational Analyst',
        'Operations Analytics Analyst', 'Senior Operations Analyst',
    ],
    'financial_risk_analyst': [
        'Financial Analyst', 'Finance Analyst', 'Financial Reporting Analyst',
        'Credit Risk Analyst', 'Market Risk Analyst',
    ],
}

NEGATIVE_ROLE_TITLES = {
    'data_analyst': [
        'Business Analyst', 'BI Analyst', 'Data Scientist',
        'Product Owner Data & Analytics Practice',
    ],
    'business_analyst': ['Business Development Analyst', 'Data Analyst', 'BI Analyst'],
    'bi_analyst': ['Business Analyst', 'Business Intelligence Developer', 'Data Scientist'],
    'data_scientist': ['Data Analyst', 'Data Engineer', 'Business Analyst'],
    'data_engineer': ['Data Scientist', 'Software Engineer', 'Business Analyst'],
    'product_analyst': [
        'Marketing Analyst', 'Business Analyst', 'Product Manager',
        'Analyst, Product Management',
    ],
    'operations_analyst': ['Business Analyst', 'Operations Manager', 'Operational Risk Analyst'],
    'financial_risk_analyst': ['Investment Banking Analyst', 'Credit Analyst', 'Data Analyst'],
}


@pytest.mark.parametrize(
    ('role_family', 'title'),
    [
        (role_family, title)
        for role_family, titles in POSITIVE_ROLE_TITLES.items()
        for title in titles
    ],
)
def test_classifies_core_role_examples(role_family, title):
    assert classify_role_title(title) == role_family


@pytest.mark.parametrize(
    ('role_family', 'title'),
    [
        (role_family, title)
        for role_family, titles in NEGATIVE_ROLE_TITLES.items()
        for title in titles
    ],
)
def test_rejects_confusable_role_examples(role_family, title):
    assert classify_role_title(title) != role_family


def test_role_query_matrix_covers_eight_families_and_three_levels():
    assert len(CORE_ROLE_FAMILIES) == 8
    assert len(SEARCH_QUERIES) == 24
    assert {query['role_family'] for query in SEARCH_QUERIES} == CORE_ROLE_FAMILIES
    assert {query['search_level'] for query in SEARCH_QUERIES} == set({
        'internship', 'new_grad', 'entry_level',
    })
    assert set(ROLE_QUERIES) == CORE_ROLE_FAMILIES


def test_financial_risk_subtypes_and_conflicts_are_retained():
    assert classify_role_title_details('Financial Analyst') == (
        'financial_risk_analyst', 'financial_analysis',
    )
    assert classify_role_title_details('Credit Risk Analyst') == (
        'financial_risk_analyst', 'risk_analysis',
    )
    assert classify_role_title('Data Science Risk Analyst') == 'hybrid'
    assert classify_role_title_details('Climate Risk Analytics & Reporting Analyst') == (
        'financial_risk_analyst', 'risk_analysis',
    )


def test_senior_and_cross_class_titles_are_sent_to_review():
    roles = classify_roles(pd.DataFrame({
        'job_title': ['Senior Data Analyst', 'Data Analyst and Data Scientist'],
    }))

    assert roles['classified_role_family'].tolist() == ['data_analyst', 'hybrid']
    assert roles['role_status'].tolist() == ['manual_review', 'manual_review']


def test_classifies_career_level_from_job_title_first():
    result = classify_one_career_level(
        'Data Analyst Intern',
        'Entry level position',
    )
    assert result == ('internship', 'intern', 'job_title', 'classified')


def test_classifies_career_level_from_description_or_marks_unknown():
    description_result = classify_one_career_level(
        'Data Analyst',
        'This is an entry level position.',
    )
    unknown_result = classify_one_career_level('Data Analyst', 'Responsibilities include analysis.')
    assert description_result == ('entry_level', 'entry level', 'job_description', 'classified')
    assert unknown_result[0] == 'unknown'
    assert unknown_result[2:] == ('unknown', 'manual_review')


def test_career_level_evidence_does_not_overwrite_provider_source():
    features = classify_career_levels(pd.DataFrame({
        'job_title': ['Data Analyst'],
        'job_description': ['A role for an internship student.'],
        'source': ['jsearch'],
        'is_senior': [False],
    }))
    assert features.loc[0, 'source'] == 'jsearch'
    assert features.loc[0, 'career_level_evidence_source'] == 'job_description'


def test_engineer_features_runs_validation():
    duplicate_jobs = pd.DataFrame({
        'job_id': ['duplicate-id', 'duplicate-id'],
        'job_title': ['Data Analyst', 'Data Analyst'],
        'job_description': ['Uses SQL.', 'Uses Python.'],
    })

    with pytest.raises(ValueError, match='Duplicate job IDs'):
        engineer_features(duplicate_jobs)

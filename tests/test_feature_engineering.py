from src.feature_engineering import classify_one_career_level, classify_role_title


def test_classifies_core_role():
    assert classify_role_title('Junior Data Analyst') == 'data_analyst'


def test_classifies_hybrid_and_review_roles():
    assert classify_role_title('Data Analyst and Data Scientist') == 'hybrid'
    assert classify_role_title('Product Analyst') == 'adjacent_hybrid'
    assert classify_role_title('Unmatched Job Title') == 'other'


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

import logging
import pandas as pd
from config.settings import (
    ALLOWED_CAREER_LEVELS,
    ALLOWED_ROLE_FAMILIES,
    CAREER_LEVEL_REVIEW_LABELS,
    LIST_COLUMNS,
    ROLE_REVIEW_LABELS,
)

LOGGER = logging.getLogger(__name__)


def _value_counts_text(values: pd.Series) -> str:
    counts = values.map(repr).value_counts(dropna=False)
    return ', '.join(f'{value} ({count})' for value, count in counts.items())


def validate_features(df: pd.DataFrame) -> None:
    '''Validate the structure and values of engineered features.'''
    required_columns = {
        'job_id',
        'classified_role_family',
        'classified_career_level',
        'stated_experience_years',
        *LIST_COLUMNS,
    }
    missing_columns = required_columns - set(df.columns)
    if missing_columns:
        raise ValueError(
            'Feature validation failed:\n- Missing required columns: '
            + ', '.join(sorted(missing_columns))
        )

    errors = []
    if df['job_id'].duplicated().any():
        duplicate_ids = df.loc[df['job_id'].duplicated(keep=False), 'job_id']
        errors.append(f'Duplicate job IDs found: {_value_counts_text(duplicate_ids)}.')

    invalid_roles = ~df['classified_role_family'].isin(ALLOWED_ROLE_FAMILIES)
    if invalid_roles.any():
        invalid_values = df.loc[invalid_roles, 'classified_role_family']
        errors.append(
            f'Invalid role classifications ({int(invalid_roles.sum())}): '
            f'{_value_counts_text(invalid_values)}.'
        )

    invalid_levels = ~df['classified_career_level'].isin(ALLOWED_CAREER_LEVELS)
    if invalid_levels.any():
        invalid_values = df.loc[invalid_levels, 'classified_career_level']
        errors.append(
            f'Invalid career levels ({int(invalid_levels.sum())}): '
            f'{_value_counts_text(invalid_values)}.'
        )

    for column, review_labels in (
        ('classified_role_family', ROLE_REVIEW_LABELS),
        ('classified_career_level', CAREER_LEVEL_REVIEW_LABELS),
    ):
        review_values = df.loc[df[column].isin(review_labels), column]
        if not review_values.empty:
            LOGGER.warning(
                '%s review labels need manual sampling: %s',
                column,
                _value_counts_text(review_values),
            )

    for column in LIST_COLUMNS:
        invalid_type = ~df[column].map(
            lambda value: isinstance(value, list)
            and all(isinstance(item, str) for item in value)
        )
        if invalid_type.any():
            invalid_values = df.loc[invalid_type, column]
            errors.append(
                f'Invalid values in {column} ({int(invalid_type.sum())}): '
                f'{_value_counts_text(invalid_values)}.'
            )

    experience = pd.to_numeric(df['stated_experience_years'], errors='coerce')
    invalid_experience_type = df['stated_experience_years'].notna() & experience.isna()
    if invalid_experience_type.any():
        errors.append(
            'Non-numeric experience requirements: '
            f'{_value_counts_text(df.loc[invalid_experience_type, 'stated_experience_years'])}.'
        )
    invalid_experience = experience < 0
    if invalid_experience.any():
        errors.append(
            'Negative experience requirements found: '
            f'{_value_counts_text(df.loc[invalid_experience, 'stated_experience_years'])}.'
        )
    if errors:
        raise ValueError('Feature validation failed:\n- ' + '\n- '.join(errors))


def calculate_feature_coverage(df: pd.DataFrame,) -> pd.DataFrame:
    '''Calculate the non-empty coverage of engineered features.'''
    feature_columns = [
        'technical_skills',
        'soft_skills',
        'accepted_majors',
        'language_requirements',
        'stated_experience_years',
    ]
    records = []
    for column in feature_columns:
        if column in LIST_COLUMNS:
            covered = df[column].map(
                lambda value: isinstance(value, list) and len(value) > 0
            )
        else:
            covered = df[column].notna()
        coverage = covered.mean() if len(df) else 0.0
        records.append({'feature': column, 'coverage': coverage})
    return pd.DataFrame(records)
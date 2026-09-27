import pandas as pd

def build_review_queue(df: pd.DataFrame) -> pd.DataFrame:
    '''Generate the records to be reviewed based on is_senior, manual_review for roles and career level, 
    is_missing_description or is_short_description, has_invalid_posting_date or is_future_posting.'''
    df = df.copy()
    already_excluded = (df['is_old_posting'].fillna(False) | df['role_status'].eq('outside_scope').fillna(False))
    df = df.loc[~already_excluded].copy()

    senior_job = df.loc[df['is_senior'].fillna(False)].copy()
    senior_job['review_reason'] = 'potential_senior'

    role_review = df.loc[df['role_status'].eq('manual_review')].copy()
    role_review['review_reason'] = 'role_unclear'

    career_review = df.loc[df['career_level_status'].eq('manual_review') & ~df['is_senior'].fillna(False)].copy()
    career_review['review_reason'] = 'career_level_unclear'

    description_review = df.loc[(df['is_short_description'] | df['is_missing_description']).fillna(False)].copy()
    description_review['review_reason'] = 'short_or_missing_description'

    date_review = df.loc[(df['is_future_posting'] | df['has_invalid_posting_date']).fillna(False)].copy()
    date_review['review_reason'] = 'invalid_or_future_date'

    country = (df['job_country'].astype('string').str.strip().str.upper().replace('', pd.NA))
    country_review = df.loc[~country.isin(['CA', 'CAN', 'CANADA'])].copy()
    country_review['review_reason'] = 'country_needs_check'

    location_review = df.loc[df['location_status'].eq('invalid')].copy()
    location_review['review_reason'] = 'unrecognized_province'

    reviews = pd.concat([
        senior_job,
        role_review,
        career_review,
        description_review,
        date_review,
        country_review,
        location_review
    ], ignore_index=True)
    reasons = reviews.groupby('job_id')['review_reason'].agg(';'.join)
    df['review_reason'] = df['job_id'].map(reasons)
    return df.loc[df['review_reason'].notna()].reset_index(drop=True)

import pandas as pd
from config.settings import PROVINCE_MAP, VALID_CANADIAN_PROVINCES, CITY_NAME_MAP, \
    MONTREAL_METRO_CITIES, TORONTO_GTA_CITIES, JOB_EMPLOYMENT, MIN_DESCRIPTION_LENGTH, \
    OLD_POSTING_DAYS


def clean_text(df: pd.DataFrame) -> pd.DataFrame:
    '''Strip surrounding whitespace and standardize blank values.'''
    columns = [
        'job_id',
        'job_title',
        'employer_name',
        'job_state',
        'job_country',
        'job_city',
        'job_employment_type',
        'job_description',
    ]
    for column in columns:
        df[column] = df[column].astype('string').str.strip().replace('', pd.NA)
    return df


def convert_data_types(df: pd.DataFrame) -> pd.DataFrame:
    '''Change the type of job_posted_at_datetime_utc and collected_at_utc in df to datetime.'''
    df['job_posted_at_datetime_utc'] = pd.to_datetime(df['job_posted_at_datetime_utc'], errors='coerce',
                                                      utc=True)
    df['collected_at_utc'] = pd.to_datetime(df['collected_at_utc'], errors='coerce', utc=True)
    return df


def missing_job_id(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    missing_id = df['job_id'].isna()
    id_issues = df.loc[missing_id].copy()
    id_issues['exclusion_reason'] = 'missing_job_id'
    df = df.loc[~missing_id].copy()
    return df, id_issues


def create_search_history(df: pd.DataFrame) -> pd.DataFrame:
    '''Preserve the search tasks that returned each job.'''
    tracking_columns = [
        'job_id',
        'role_family',
        'search_level',
        'role_query',
        'search_query',
        'page_number',
        'collected_at_utc',
    ]
    search_history = df[tracking_columns].drop_duplicates().reset_index(drop=True)
    return search_history


def deduplicated_jobs(df: pd.DataFrame) -> pd.DataFrame:
    '''Keep one record for each API job ID.'''
    df = df.sort_values('collected_at_utc', na_position='first', kind='stable').drop_duplicates(subset='job_id',
                                                                                                keep='last').reset_index(
        drop=True)
    return df


def clean_locations(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    '''Standardize the name of provinces and cities'''
    df['job_province_clean'] = df['job_state'].astype('string').str.strip().str.upper().replace(PROVINCE_MAP).replace(
        '', pd.NA)
    df['job_city_clean'] = df['job_city'].astype('string').str.strip().str.title().replace(CITY_NAME_MAP).replace('',
                                                                                                                  pd.NA)
    df['location_status'] = 'valid'
    province_used_as_city = df['job_city_clean'].str.upper().replace(PROVINCE_MAP).isin(VALID_CANADIAN_PROVINCES)
    df.loc[province_used_as_city, 'job_city_clean'] = pd.NA
    partial_location = df['job_province_clean'].isna() | df['job_city_clean'].isna()
    df.loc[partial_location, 'location_status'] = 'partial'
    unknown_location = df['job_province_clean'].isna() & df['job_city_clean'].isna()
    df.loc[unknown_location, 'location_status'] = 'unknown'
    invalid_province = df['job_province_clean'].notna() & ~df['job_province_clean'].isin(VALID_CANADIAN_PROVINCES)
    df.loc[invalid_province, 'location_status'] = 'invalid'
    location_issues = df.loc[invalid_province].copy()
    location_issues['review_reason'] = 'invalid_or_unrecognized_province'
    df['market_area'] = 'Other Canada'
    montreal_metro_cities = df['job_city_clean'].isin(MONTREAL_METRO_CITIES)
    df.loc[montreal_metro_cities, 'market_area'] = 'Montreal Metro'
    toronto_gta_cities = df['job_city_clean'].isin(TORONTO_GTA_CITIES)
    df.loc[toronto_gta_cities, 'market_area'] = 'Toronto GTA'
    return df, location_issues


def clean_employment_types(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    '''Remain the employment types that includes full-time, part-time, contract, internship.'''
    invalid_employment_type = df['job_employment_type'].notna() & ~df['job_employment_type'].isin(JOB_EMPLOYMENT)
    employment_type_issues = df.loc[invalid_employment_type].copy()
    employment_type_issues['exclusion_reason'] = 'outside_employment'
    df = df.loc[~invalid_employment_type].copy()
    return df, employment_type_issues


def combine_issues(id_issues: pd.DataFrame, employment_type_issues: pd.DataFrame, location_issues: pd.DataFrame) -> pd.DataFrame:
    """Combine cleaning issues into one table with a common type and reason."""
    issue_groups = [
        (id_issues, 'missing_job_id'),
        (employment_type_issues, 'invalid_employment_type'),
        (location_issues, 'invalid_location'),
    ]
    combined = []
    for issues, issue_type in issue_groups:
        if issues.empty:
            continue
        issue_rows = issues.copy()
        if 'exclusion_reason' in issue_rows:
            issue_rows['issue_reason'] = issue_rows['exclusion_reason']
        elif 'review_reason' in issue_rows:
            issue_rows['issue_reason'] = issue_rows['review_reason']
        else:
            issue_rows['issue_reason'] = issue_type
        issue_rows['issue_type'] = issue_type
        issue_rows = issue_rows.drop(columns=['exclusion_reason', 'review_reason'], errors='ignore')
        combined.append(issue_rows)
    if not combined:
        return pd.DataFrame(columns=['issue_type', 'issue_reason'])
    return pd.concat(combined, ignore_index=True, sort=False)


def flag_short_descriptions(df: pd.DataFrame) -> pd.DataFrame:
    '''Flag the short job descriptions in df.'''
    df['description_length'] = df['job_description'].astype('string').str.len()
    df['is_short_description'] = False
    df.loc[df['description_length'] < MIN_DESCRIPTION_LENGTH, 'is_short_description'] = True
    df['is_missing_description'] = df['job_description'].isna()
    return df


def flag_old_posting(df: pd.DataFrame) -> pd.DataFrame:
    '''Flag the old postings that posted older than 35 days in df.'''
    posting_days = (df['collected_at_utc'] - df['job_posted_at_datetime_utc']).dt.total_seconds() / 86400
    df['posting_age_days'] = posting_days
    df['is_old_posting'] = posting_days.gt(OLD_POSTING_DAYS)
    df['is_future_posting'] = posting_days.lt(0)
    df['has_invalid_posting_date'] = posting_days.isna()
    return df


def clean_jobs(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    '''Clean job records and preserve cleaning issues.'''
    df = df.copy()
    df = clean_text(df)
    df = convert_data_types(df)
    df, id_issues = missing_job_id(df)
    search_history = create_search_history(df)
    df = deduplicated_jobs(df)
    df, location_issues = clean_locations(df)
    df, employment_type_issues = clean_employment_types(df)
    df = flag_short_descriptions(df)
    df = flag_old_posting(df)
    df = df.reset_index(drop=True)
    return df, id_issues, search_history, employment_type_issues, location_issues

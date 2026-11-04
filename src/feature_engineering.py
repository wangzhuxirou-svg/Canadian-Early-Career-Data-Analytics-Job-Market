import pandas as pd
import re
from src.validate import validate_features
from config.settings import SENIORITY_EXCLUDE_KEYWORDS, ROLE_KEYWORDS, ADJACENT_ROLE_KEYWORDS, CAREER_LEVEL_KEYWORDS, \
    CAREER_LEVEL_DESCRIPTION_KEYWORDS, OUTSIDE_ROLE_KEYWORDS, TECHNICAL_SKILLS_KEYWORDS, SOFT_SKILL_KEYWORDS, \
    LANGUAGE_KEYWORDS, EDUCATION_CONTEXT_KEYWORDS, MAJOR_KEYWORDS, EXPERIENCE_YEARS_PATTERN, NO_EXPERIENCE_PATTERN, \
    NUMBER_WORDS, ROLE_KEYWORD_EXCLUSIONS, FINANCIAL_RISK_SUBTYPE_KEYWORDS


def normalized_matching_text(value: str) -> str:
    '''Normalized the text before keyword matching.'''
    if pd.isna(value):
        return ''
    normalized_text = str(value).lower().strip()
    normalized_text = re.sub(r'[\W_]+', ' ', normalized_text)
    normalized_text = re.sub(r'\s+', ' ', normalized_text).strip()
    return normalized_text


def contain_keyword(normalized_text: str, keyword: str) -> bool:
    keyword = normalized_matching_text(keyword)
    if not keyword:
        return False
    return f' {keyword} ' in f' {normalized_text} '


def flag_senior_jobs(df: pd.DataFrame) -> pd.DataFrame:
    '''Flag the senior jobs in df.'''
    senior_pattern = r'\b(?:' + '|'.join(re.escape(keyword) for keyword in SENIORITY_EXCLUDE_KEYWORDS) + r')\b'
    senior_mask = df['job_title'].str.contains(senior_pattern, case=False, na=False)
    df['is_senior'] = False
    df.loc[senior_mask, 'is_senior'] = True
    return df


def classify_role_title_details(job_title: str) -> tuple[str, str | None]:
    '''Classify a title and retain the financial/risk subtype when present.'''
    normalized_title = normalized_matching_text(job_title)
    target_matches = []
    for role_family, keywords in ROLE_KEYWORDS.items():
        if any(
            contain_keyword(normalized_title, keyword)
            for keyword in ROLE_KEYWORD_EXCLUSIONS.get(role_family, [])
        ):
            continue
        if any(contain_keyword(normalized_title, keyword)
               for keyword in keywords
               ):
            target_matches.append(role_family)
    if len(target_matches) == 1:
        role_family = target_matches[0]
        if role_family == 'financial_risk_analyst':
            for subtype, keywords in FINANCIAL_RISK_SUBTYPE_KEYWORDS.items():
                if any(contain_keyword(normalized_title, keyword) for keyword in keywords):
                    return role_family, subtype
        return role_family, None
    if len(target_matches) > 1:
        return 'hybrid', None

    if any(
        contain_keyword(normalized_title, keyword)
        for keywords in ADJACENT_ROLE_KEYWORDS.values()
        for keyword in keywords
    ):
        return 'adjacent_hybrid', None
    if any(contain_keyword(normalized_title, keyword)
            for keyword in OUTSIDE_ROLE_KEYWORDS):
        return 'outside_scope', None
    return 'other', None


def classify_role_title(job_title: str) -> str:
    '''Classify role title into a job family.'''
    return classify_role_title_details(job_title)[0]


def classify_roles(df: pd.DataFrame) -> pd.DataFrame:
    '''Classify the roles of jobs in df.'''
    role_results = df['job_title'].apply(classify_role_title_details)
    df['classified_role_family'] = role_results.map(lambda result: result[0])
    df['classified_role_subtype'] = role_results.map(lambda result: result[1])
    target_role_family = set(ROLE_KEYWORDS)
    df['role_status'] = 'manual_review'
    df.loc[df['classified_role_family'].isin(target_role_family), 'role_status'] = 'target'
    df.loc[df['classified_role_family'].eq('outside_scope'),'role_status'] = 'outside_scope'
    senior_pattern = r'\b(?:' + '|'.join(
        re.escape(keyword) for keyword in SENIORITY_EXCLUDE_KEYWORDS
    ) + r')\b'
    senior_mask = df['job_title'].str.contains(senior_pattern, case=False, na=False)
    review_senior = senior_mask & df['classified_role_family'].ne('outside_scope')
    df.loc[review_senior, 'role_status'] = 'manual_review'
    return df


def find_career_level_match(text: str, keyword_groups: dict):
    '''Return the first matching career level and keyword.'''
    normalized_text = normalized_matching_text(text)
    for career_level, keywords in keyword_groups.items():
        for keyword in keywords:
            if contain_keyword(normalized_text, keyword):
                return career_level, keyword
    return None, None


def classify_one_career_level(job_title: str, job_description: str):
    '''Classify one career level using job_title first, then job_description.'''
    career_level, keyword = find_career_level_match(job_title, CAREER_LEVEL_KEYWORDS)
    if career_level is not None:
        return career_level, keyword, 'job_title', 'classified'
    career_level, keyword = find_career_level_match(job_description, CAREER_LEVEL_DESCRIPTION_KEYWORDS)
    if career_level is not None:
        return career_level, keyword, 'job_description', 'classified'
    return 'unknown', pd.NA, 'unknown', 'manual_review'


def classify_career_levels(df: pd.DataFrame) -> pd.DataFrame:
    '''Classify the career levels of jobs in df.'''
    if df.empty:
        for column in ['classified_career_level',
                       'career_keyword',
                       'career_level_evidence_source',
                       'career_level_status',
                       ]:
            df[column] = pd.Series(index=df.index, dtype='string')
        return df
    results = df.apply(lambda row: classify_one_career_level(row['job_title'], row['job_description']), axis=1,
                       result_type='expand')
    results.columns = ['classified_career_level',
                       'career_keyword',
                       'career_level_evidence_source',
                       'career_level_status',
                       ]
    df[results.columns] = results
    senior_mask = df['is_senior']
    df.loc[senior_mask, 'classified_career_level'] = 'potential_senior'
    df.loc[senior_mask, 'career_keyword'] = 'seniority keyword'
    df.loc[senior_mask, 'career_level_evidence_source'] = 'job_title'
    df.loc[senior_mask, 'career_level_status'] = 'manual_review'
    return df


def extract_education_text(job_description: str) -> str:
    '''Extract sections that mention education requirements from job description.'''
    if pd.isna(job_description):
        return ''
    sections = re.split(r'[.\n;]+', job_description)
    education_sections = []
    for section in sections:
        normalized_section = normalized_matching_text(section)
        if any(contain_keyword(normalized_section, keyword) for keyword in EDUCATION_CONTEXT_KEYWORDS):
            education_sections.append(section)
    return ' '.join(education_sections)


def extract_majors(job_description: str) -> list[str]:
    '''Extract majors mentioned in the education requirements.'''
    education_text = extract_education_text(job_description)
    normalized_education_text = normalized_matching_text(education_text)
    majors_found = []
    for major, keywords in MAJOR_KEYWORDS.items():
        if any(contain_keyword(normalized_education_text, keyword) for keyword in keywords):
            majors_found.append(major)
    return majors_found


def add_majors_column(df: pd.DataFrame) -> pd.DataFrame:
    '''Add a column of majors extracted from job description.'''
    df = df.copy()
    df['accepted_majors'] = df['job_description'].apply(extract_majors)
    df['education_text'] = df['job_description'].apply(extract_education_text)
    return df


def extract_skills(job_description: str, keywords_dictionary: dict[str, list[str]]) -> list[str]:
    '''Extract skills from job description.'''
    normalized_description = normalized_matching_text(job_description)
    skills_found = []
    for skill, keywords in keywords_dictionary.items():
        if any(contain_keyword(normalized_description, keyword) for keyword in keywords):
            skills_found.append(skill)
    return skills_found


def add_skills_column(df: pd.DataFrame) -> pd.DataFrame:
    '''Add a column of skills extracted from job description.'''
    df = df.copy()
    df['technical_skills'] = df['job_description'].apply(lambda x: extract_skills(x, TECHNICAL_SKILLS_KEYWORDS))
    df['soft_skills'] = df['job_description'].apply(lambda x: extract_skills(x, SOFT_SKILL_KEYWORDS))
    df['language_requirements'] = df['job_description'].apply(lambda x: extract_skills(x, LANGUAGE_KEYWORDS))
    return df


EXPERIENCE_YEARS_REGEX = re.compile(EXPERIENCE_YEARS_PATTERN, re.IGNORECASE)
NO_EXPERIENCE_REGEX = re.compile(NO_EXPERIENCE_PATTERN, re.IGNORECASE)

def extract_experience_years(job_description: str):
    '''Extract stated minimum years of experience from job description.'''
    if pd.isna(job_description):
        return pd.NA
    description = str(job_description)
    if NO_EXPERIENCE_REGEX.search(description):
        return 0
    matches = EXPERIENCE_YEARS_REGEX.finditer(description)

    def to_years(value: str) -> int:
        return int(value) if value.isdigit() else NUMBER_WORDS[value.lower()]

    minimum_years_found = [
        to_years(match.group('min_years'))
        for match in matches
    ]
    if not minimum_years_found:
        return pd.NA
    return max(minimum_years_found)


def add_experience_features(df: pd.DataFrame) -> pd.DataFrame:
    '''Add stated experience years extracted from job description.'''
    df = df.copy()
    df['stated_experience_years'] = df['job_description'].apply(extract_experience_years).astype('Int64')
    return df


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    '''Create role and career-level features.'''
    df = df.copy()
    df = classify_roles(df)
    df = flag_senior_jobs(df)
    df = classify_career_levels(df)
    df = add_majors_column(df)
    df = add_skills_column(df)
    df = add_experience_features(df)
    validate_features(df)
    return df

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = PROJECT_ROOT / 'data' / 'raw'

ROLE_QUERIES = {
    'data_analyst': 'data analyst',
    'business_analyst': 'business analyst',
    'bi_analyst': '"bi analyst" OR "business intelligence analyst" OR "reporting analyst"',
    'data_scientist': 'data scientist',
    'data_engineer': '"data engineer" OR "etl developer"',
}

CAREER_LEVEL_QUERIES = {
    'internship': '"internship" OR "intern" OR "co-op"',
    'new_grad': '"new grad" OR "new graduate" OR "recent graduate"',
    'entry_level': '"entry level" OR "junior"',
}

SEARCH_QUERIES = [{
    'role_family': role_family,
    'search_level': career_level,
    'role_query': role_query,
    'query': f'({role_query}) AND ({level_query})',
}
    for role_family, role_query in ROLE_QUERIES.items()
    for career_level, level_query in CAREER_LEVEL_QUERIES.items()
]

COUNTRY = 'ca'
DATE_POST = 'month'

ROLE_KEYWORDS = {
    'data_analyst': ['data analyst',
                     'junior data analyst',
                     'data analytics',
                     ],
    'business_analyst': ['business analyst',
                         'junior business analyst',
                         'business system analyst',
                         'business systems analyst',
                         ],
    'bi_analyst': ['bi analyst',
                   'business intelligence analyst',
                   'reporting analyst',
                   'analyst business intelligence',
                   'analyste bi',
                   'analyste intelligence',
                   'business intelligence',
                   ],
    'data_scientist': ['data scientist',
                       'junior data scientist',
                       'applied data scientist',
                       'data science',
                       ],
    'data_engineer': ['data engineer',
                      'junior data engineer',
                      'analytics engineer',
                      'etl developer',
                      'data integration developer',
                      'data engineering developer',
                      'data engineering',
                      'data platform engineer',
                      ],
}


ADJACENT_ROLE_KEYWORDS = {
    'product_analyst': ['product analyst'],
    'operations_analyst': ['operations analyst'],
    'marketing_analyst': ['marketing analyst'],
    'insight_analyst': ['insight analyst'],
}

OUTSIDE_ROLE_KEYWORDS = [
    'data entry',
    'business development',
    'communication',
    'communications',
    'stagiaire en communications',
    'investment banking analyst',
    'credit analyst',
    'actuarial analyst',
    'threat intelligence analyst',
    'competitive intelligence analyst',
    'hvac',
    'analog ic layout',
    'manufacturing design engineer',
]

CORE_ROLE_FAMILIES = set(ROLE_KEYWORDS)
ROLE_REVIEW_LABELS = {
    'hybrid',
    'adjacent_hybrid',
    'other',
}
EXCLUDED_ROLE_LABELS = {'outside_scope'}
ALLOWED_ROLE_FAMILIES = (
    CORE_ROLE_FAMILIES | ROLE_REVIEW_LABELS | EXCLUDED_ROLE_LABELS
)

CAREER_LEVEL_KEYWORDS = {
    'internship': ['internship',
                   'intern',
                   'interns',
                   'co-op',
                   'coop',
                   'student position',
                   'student opportunity',
                   ],
    'new_grad': ['new grad',
                 'new graduate',
                 'recent graduate',
                 'graduate programme',
                 'campus hire',
                 'campus recruitment',
                 ],
    'entry_level': ['entry level',
                    'entry-level',
                    'junior',
                    ],
}

CORE_CAREER_LEVELS = set(CAREER_LEVEL_KEYWORDS)
CAREER_LEVEL_REVIEW_LABELS = {
    'unknown',
    'potential_senior',
}
ALLOWED_CAREER_LEVELS = CORE_CAREER_LEVELS | CAREER_LEVEL_REVIEW_LABELS
LIST_COLUMNS = [
    'technical_skills',
    'soft_skills',
    'accepted_majors',
    'language_requirements',
]

CAREER_LEVEL_DESCRIPTION_KEYWORDS = {
    'internship': [
        'intern',
        'interns',
        'internship',
        'internship position',
        'internship opportunity',
        'internship program',
        'summer intern',
        'student intern',
        'coop',
        'co-op',
        'co-op student',
        'student position',
        'currently enrolled',
        'returning to school',
        'stagiaire',
        'stage',
    ],
    'new_grad': [
        'new grad',
        'new grads',
        'new graduate',
        'new graduates',
        'recent graduate',
        'recent graduates',
        'graduate program',
        'graduate programme',
        'graduate role',
        'campus hire',
        'campus recruitment',
        'early career',
        'jeune diplômé',
    ],
    'entry_level': [
        'entry level',
        'entry level position',
        'entry level role',
        'entry level opportunity',
        'junior',
        'junior position',
        'junior role',
        'junior level',
        'early career',
        'niveau débutant',
    ],
}

API_FIELDS = [
    'job_id',
    'job_title',
    'employer_name',
    'job_state',
    'job_country',
    'job_city',
    'job_latitude',
    'job_longitude',
    'job_employment_type',
    'job_is_remote',
    'job_posted_at_datetime_utc',
    'job_description',
    'job_apply_link',
    'job_min_salary',
    'job_max_salary',
    'job_salary_currency',
    'job_salary_period',
]

TRACKING_FIELDS = [
    'role_family',
    'role_query',
    'search_level',
    'search_query',
    'page_number',
    'collected_at_utc'
]

OUTPUT = API_FIELDS + TRACKING_FIELDS

SENIORITY_EXCLUDE_KEYWORDS = [
    'senior',
    'sr.',
    'sr',
    'lead',
    'manager',
    'director',
    'principal',
    'staff',
    'head',
]

START_PAGE = 1
MAX_PAGES = 4
REQUEST_DELAY_SECONDS = 1

PROVINCE_MAP = {
    'ALBERTA': 'AB',
    'BRITISH COLUMBIA': 'BC',
    'MANITOBA': 'MB',
    'NEW BRUNSWICK': 'NB',
    'NEWFOUNDLAND AND LABRADOR': 'NL',
    'NOVA SCOTIA': 'NS',
    'NUNAVUT': 'NU',
    'NORTHWEST TERRITORIES': 'NT',
    'ONTARIO': 'ON',
    'PRINCE EDWARD ISLAND': 'PE',
    'QUEBEC': 'QC',
    'SASKATCHEWAN': 'SK',
    'YUKON': 'YT',
}

VALID_CANADIAN_PROVINCES = {
    'AB',
    'BC',
    'MB',
    'NB',
    'NL',
    'NU',
    'NS',
    'NT',
    'ON',
    'PE',
    'QC',
    'SK',
    'YT',
}

CITY_NAME_MAP = {
    'Montréal': 'Montreal',
    'Montréal-Ouest': 'Montreal-Ouest',
    'Toronto On': 'Toronto',
}

MONTREAL_METRO_CITIES = [
    'Montreal',
    'Montreal-Ouest',
    'Greater Montreal',
    'Mont-Royal',
]

TORONTO_GTA_CITIES = [
    'Toronto',
    'Mississauga',
    'Markham',
]

JOB_EMPLOYMENT = [
    'Full-time',
    'Internship',
    'Part-time',
    'Contract',
]

MIN_DESCRIPTION_LENGTH = 500

OLD_POSTING_DAYS = 35


TECHNICAL_SKILLS_KEYWORDS = {
    'python': ['python', 'py'],
    'r': ['r programming', 'r language'],
    'sql': ['sql', 'structured query language'],
    'excel': ['excel', 'microsoft excel'],
    'tableau': ['tableau'],
    'power_bi': ['power bi', 'microsoft power bi', 'powerbi'],
    'pandas': ['pandas'],
    'numpy': ['numpy'],
    'matplotlib': ['matplotlib'],
    'scikit_learn': ['scikit-learn', 'sklearn'],
    'tensorflow': ['tensorflow'],
    'pytorch': ['pytorch', 'torch'],
    'spark': ['apache spark', 'spark', 'pyspark'],
    'aws': ['aws', 'amazon web services'],
    'azure': ['azure', 'microsoft azure'],
    'gcp': ['google cloud platform', 'gcp'],
    'snowflake': ['snowflake'],
    'databricks': ['databricks'],
    'git': ['git', 'github', 'gitlab'],
}

SOFT_SKILL_KEYWORDS = {
    'communication': ['communication skills', 'communicate effectively', 'written communication',
                      'verbal communication', 'communication skill'],
    'teamwork': ['teamwork', 'collaboration', 'collaborative', 'team player', 'cross-functional team'],
    'problem_solving': ['problem solving', 'analytical thinking', 'critical thinking'],
    'attention_to_detail': ['attention to detail', 'detail-oriented', 'meticulous'],
    'time_management': ['time management', 'prioritization', ',manage multiple priorities'],
    'stakeholder_management': ['stakeholder management', 'stakeholder engagement', 'manage stakeholders',
                               'work with stakeholders'],
    'presentation': ['presentation skills', 'present effectively', 'public speaking', 'present findings',
                     'communicate insights'],
}

LANGUAGE_KEYWORDS = {
    'english': [
        'english',
        'anglais',
    ],
    'french': [
        'french',
        'français',
        'francais',
        'fran?ais',
        'français requis',
        'french required',
    ],
    'spanish': ['spanish', 'espagnol'],
    'mandarin': ['mandarin', 'chinese mandarin'],
    'cantonese': ['cantonese'],
    'punjabi': ['punjabi'],
    'hindi': ['hindi'],
    'arabic': ['arabic', 'arabe'],
    'japanese': ['japanese'],
    'korean': ['korean'],
    'portuguese': ['portuguese'],
    'italian': ['italian'],
    'german': ['german'],
    'russian': ['russian'],
    'ukrainian': ['ukrainian'],
    'latvian': ['latvian'],
    'odia': ['odia'],
    'bilingual': [
        'bilingual',
        'bilingualism',
        'fluently bilingual',
        'english and french',
        'french and english',
        'anglais et français',
        'français et anglais',
    ],
}

MAJOR_KEYWORDS = {
    'computer_science': [
        'computer science', 'computing science', 'computer sciences',
        'software engineering', 'information technology', 'information systems',
    ],
    'statistics': ['statistics', 'statistical science'],
    'mathematics': ['mathematics', 'applied mathematics'],
    'data_science': [
        'data science', 'data analytics', 'data-related', 'data related',
        'analytics', 'artificial intelligence', 'ai',
    ],
    'engineering': [
        'engineering', 'industrial engineering', 'computer engineering',
        'electrical engineering', 'systems engineering',
    ],
    'business': [
        'business', 'business administration', 'business management',
        'business analytics', 'accounting', 'commerce', 'mba',
    ],
    'management': [
        'management', 'management science', 'management studies',
        'management information systems',
    ],
    'economics': ['economics', 'econometrics'],
    'finance': [
        'finance', 'financial engineering', 'financial mathematics',
        'financial analytics',
    ],
}

EDUCATION_CONTEXT_KEYWORDS = [
    'degree in',
    'degree',
    'bachelor',
    'bachelor\'s',
    'undergraduate',
    'master',
    'master\'s',
    'phd',
    'college',
    'university',
    'post-secondary',
    'master',
    'field of study',
    'educational background',
    'academic background',
    'diploma in',
]

NUMBER_WORDS = {
    'zero': 0,
    'one': 1,
    'two': 2,
    'three': 3,
    'four': 4,
    'five': 5,
    'six': 6,
    'seven': 7,
    'eight': 8,
    'nine': 9,
    'ten': 10,
}

EXPERIENCE_YEARS_PATTERN = (
    r'\b(?:(?:a\s+)?minimum\s+of\s+|at\s+least\s+)?'
    r'(?P<min_years>\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)'
    r'(?:\s*(?:-|–|—|to)\s*'
    r'(?P<max_years>\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten))?'
    r'\s*\+?\s*(?:years?|yrs?)\b'
    r'(?:\s*(?:of\s+)?'
    r'(?:relevant\s+|professional\s+|work\s+|industry\s+|related\s+)?'
    r'experience\b|\s+(?:as|in)\b)'
)

NO_EXPERIENCE_PATTERN = (
    r'\b(?:no|zero)\s+'
    r'(?:(?:prior|previous|professional|work|relevant)\s+)*'
    r'experience\s+'
    r'(?:is\s+)?'
    r'(?:required|necessary|needed)\b'
    r'|'
    r'\b(?:prior|previous)\s+experience\s+'
    r'(?:is\s+)?not\s+'
    r'(?:required|necessary|needed)\b'
)

MAJOR_GROUPS = {
    'Computing and Data': {'computer_science', 'data_science'},
    'Mathematics and Statistics': {'mathematics', 'statistics'},
    'Engineering': {'engineering'},
    'Business and Management': {'business', 'management', 'finance', 'economics'}
}
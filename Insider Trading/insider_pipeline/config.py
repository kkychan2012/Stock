import os

# Date range
LOOKBACK_DAYS = 180

# Filtering thresholds
MIN_TRANSACTION_VALUE = 100_000
CLUSTER_WINDOW_DAYS = 7
CLUSTER_MIN_INSIDERS = 3

# SEC API
SEC_USER_AGENT = "kkychan2012 kkychan2012@gmail.com"
SEC_RATE_LIMIT = 10  # max requests per second
SEC_RETRY_ATTEMPTS = 3
SEC_RETRY_BACKOFF = 2  # seconds, doubles each retry

# Roles to include
ROLES_TO_TRACK = [
    "CEO", "CFO", "COO", "President",
    "Chairman", "Director",
    "Chief Executive Officer",
    "Chief Financial Officer",
    "Chief Operating Officer",
]

# Transaction codes
VALID_TRANSACTION_CODES = {"P"}  # open market purchase only
EXCLUDED_CODES = {"S", "A", "M", "F"}

# Output
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "insider_trading_report.xlsx")
LOG_FILE = os.path.join(os.path.dirname(__file__), "pipeline.log")

# SEC EDGAR endpoints
EDGAR_FULL_INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/{quarter}/company.idx"
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_FILING_BASE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/"

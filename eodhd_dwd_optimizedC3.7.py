#!/usr/bin/env python3
"""
EODHD Market Data Downloader - Optimized Version with Integrity Checks
========================================================================

High-performance downloader for EODHD intraday and EOD market data with:
- Always up-to-date CSV files (incremental updates)
- Comprehensive integrity checking
- Colored status grid dashboard
- API efficiency tracking
- Crash-safe resume mechanism

Author: System Optimizer
Date: 2025-11-13
Updated: 2025-11-14
"""

import os
import sys
import json
import time
import glob
import logging
import argparse
import shutil
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import numpy as np

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    """Configuration from environment variables with fallbacks."""

    API_KEY = os.getenv("EODHD_API_KEY", "6667190c39a0f6.19622280")
    DATA_DIR = os.getenv("EODHD_DATA_DIR", "data")
    INTERVALS = os.getenv("EODHD_INTERVALS", "1m,5m,1h,1d").split(",")
    LOG_LEVEL = os.getenv("EODHD_LOG_LEVEL", "INFO")
    BATCH_SIZE = int(os.getenv("EODHD_BATCH_SIZE", "50"))

    # API limits (EODHD documented values)
    MAX_PERIOD = {
        "1m": 120,    # days per call
        "5m": 600,
        "1h": 7200,
        "1d": 999999  # Full history in single call
    }

    # Earliest data availability (from EODHD docs)
    EARLIEST_DATES = {
        "1m": {
            "US": datetime(2004, 1, 1),
            "FOREX": datetime(2009, 1, 1),
            "CRYPTO": datetime(2009, 1, 1),
            "default": datetime(2020, 10, 1)
        },
        "5m": {"default": datetime(2020, 10, 1)},
        "1h": {"default": datetime(2020, 10, 1)},
        "1d": {"default": datetime(1990, 1, 1)}
    }

    @classmethod
    def get_earliest_date(cls, interval: str, ticker: str) -> datetime:
        """Get earliest available data date for interval/ticker."""
        exchange_zone = ticker.split(".")[-1] if "." in ticker else "default"
        interval_config = cls.EARLIEST_DATES.get(interval, {})

        for key in [exchange_zone, "default"]:
            if key in interval_config:
                return interval_config[key]

        return datetime(2020, 10, 1)


# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging():
    """Configure structured logging."""
    logging.basicConfig(
        level=getattr(logging, Config.LOG_LEVEL),
        format='[%(asctime)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# ============================================================================
# ANSI COLOR CODES
# ============================================================================

class Colors:
    """ANSI color codes for terminal output."""
    RESET = '\033[0m'
    GREY = '\033[90m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    ORANGE = '\033[33m'  # Yellow as orange substitute

    @staticmethod
    def colorize(text: str, color: str) -> str:
        """Wrap text in color codes."""
        return f"{color}{text}{Colors.RESET}"

# ============================================================================
# STATUS GRID FOR DASHBOARD
# ============================================================================

@dataclass
class StatusCell:
    """Represents the status of a single ticker/interval combination."""
    ticker: str
    interval: str
    state: str = "pending"  # pending, downloading, downloaded_new, updated, up_to_date, no_data, etc.
    earliest_ts: Optional[datetime] = None
    latest_ts: Optional[datetime] = None
    rows_count: int = 0
    api_calls: int = 0
    issues: List[str] = field(default_factory=list)

    def get_text(self) -> str:
        """Get display text for this cell."""
        if self.state in ["pending", "downloading"]:
            return "==========================="
        elif self.state in ["no_data", "delisted"]:
            return "---------------------------"
        elif self.state in ["not_allowed", "auth_error"]:
            return "xxxxxxxxxxxxxxxxxxxxxxxxxxx"
        elif self.earliest_ts and self.latest_ts:
            return format_compact_range(self.earliest_ts, self.latest_ts)
        else:
            return "???????????????????????????"

    def get_color(self) -> str:
        """Get ANSI color code for this cell's state."""
        color_map = {
            "pending": Colors.GREY,
            "downloading": Colors.YELLOW,
            "downloaded_new": Colors.GREEN,
            "updated": Colors.CYAN,
            "up_to_date": Colors.BLUE,
            "no_data": Colors.GREY,
            "not_allowed": Colors.ORANGE,
            "auth_error": Colors.ORANGE,
            "http_error": Colors.RED,
            "connection_error": Colors.RED,
            "delisted": Colors.MAGENTA,
            "unknown_error": Colors.GREY,
        }
        return color_map.get(self.state, Colors.RESET)


class StatusGrid:
    """Manages and renders the status grid for multiple tickers."""

    def __init__(self, intervals: List[str]):
        self.intervals = intervals
        self.grid: Dict[str, Dict[str, StatusCell]] = defaultdict(dict)
        self.ticker_order: List[str] = []

    def update_cell(self, ticker: str, interval: str, **kwargs):
        """Update or create a cell in the grid."""
        if ticker not in self.ticker_order:
            self.ticker_order.append(ticker)

        if interval not in self.grid:
            self.grid[interval] = {}

        if ticker not in self.grid[interval]:
            self.grid[interval][ticker] = StatusCell(ticker=ticker, interval=interval)

        cell = self.grid[interval][ticker]
        for key, value in kwargs.items():
            setattr(cell, key, value)

    def render(self, title: str = "Download Status"):
        """Render the status grid to terminal."""
        if not self.ticker_order:
            return

        terminal_width = shutil.get_terminal_size().columns
        cell_width = 27  # Width of date range string
        interval_col_width = 4

        # Calculate how many tickers can fit
        available_width = terminal_width - interval_col_width - 2
        tickers_per_row = max(1, available_width // (cell_width + 1))

        # Split tickers into chunks if needed
        ticker_chunks = [self.ticker_order[i:i + tickers_per_row]
                        for i in range(0, len(self.ticker_order), tickers_per_row)]

        print(f"\n{'='*terminal_width}")
        print(f"{title:^{terminal_width}}")
        print(f"{'='*terminal_width}\n")

        for chunk_idx, ticker_chunk in enumerate(ticker_chunks):
            if chunk_idx > 0:
                print()  # Blank line between chunks

            # Header row with ticker symbols
            header = " " * interval_col_width
            for ticker in ticker_chunk:
                short_ticker = ticker[:cell_width].ljust(cell_width)
                header += " " + short_ticker
            print(header)
            print("-" * len(header))

            # One row per interval
            for interval in self.intervals:
                row = interval.ljust(interval_col_width)
                for ticker in ticker_chunk:
                    cell = self.grid[interval].get(ticker)
                    if cell:
                        text = cell.get_text()
                        color = cell.get_color()
                        colored_text = Colors.colorize(text.ljust(cell_width), color)
                    else:
                        colored_text = " " * cell_width
                    row += " " + colored_text
                print(row)

        print()

# ============================================================================
# METRICS COLLECTOR WITH EFFICIENCY TRACKING
# ============================================================================

class MetricsCollector:
    """Track performance metrics, API usage, and efficiency."""

    def __init__(self):
        self.api_calls_by_interval = defaultdict(int)
        self.api_calls_by_endpoint = defaultdict(int)
        self.data_points_by_interval = defaultdict(int)
        self.empty_calls_by_interval = defaultdict(int)
        self.tickers_processed = 0
        self.tickers_skipped = 0
        self.intervals_completed = 0
        self.errors_by_type = defaultdict(int)
        self.start_time = time.time()

    def record_api_call(self, endpoint: str, interval: str, rows_returned: int = 0):
        """Record an API call with data points received."""
        self.api_calls_by_endpoint[endpoint] += 1
        self.api_calls_by_interval[interval] += 1
        self.data_points_by_interval[interval] += rows_returned
        if rows_returned == 0:
            self.empty_calls_by_interval[interval] += 1

    def record_error(self, error_type: str):
        """Record an error."""
        self.errors_by_type[error_type] += 1

    def get_efficiency_ratio(self, interval: str) -> float:
        """Calculate efficiency: data points per API call."""
        calls = self.api_calls_by_interval.get(interval, 0)
        points = self.data_points_by_interval.get(interval, 0)
        return points / calls if calls > 0 else 0.0

    def get_empty_call_percent(self, interval: str) -> float:
        """Calculate percentage of empty API calls."""
        calls = self.api_calls_by_interval.get(interval, 0)
        empty = self.empty_calls_by_interval.get(interval, 0)
        return (empty / calls * 100) if calls > 0 else 0.0

    def log_summary(self):
        """Log final summary metrics with efficiency."""
        elapsed = time.time() - self.start_time
        total_calls = sum(self.api_calls_by_endpoint.values())
        total_points = sum(self.data_points_by_interval.values())

        print("\n" + "=" * 70)
        print("DOWNLOAD SUMMARY".center(70))
        print("=" * 70)
        print(f"Elapsed time: {int(elapsed)}s ({elapsed/60:.1f} minutes)")
        print(f"Tickers processed: {self.tickers_processed}")
        print(f"Tickers skipped (complete): {self.tickers_skipped}")
        print(f"Intervals completed: {self.intervals_completed}")
        print(f"Total API calls: {total_calls}")
        print(f"Total data points: {total_points:,}")

        if total_calls > 0:
            print(f"\nOverall efficiency: {total_points/total_calls:.1f} points/call")

        if self.api_calls_by_interval:
            print("\nAPI Efficiency by Interval:")
            print("-" * 70)
            print(f"{'Interval':<10} {'Calls':<10} {'Points':<12} {'Pts/Call':<12} {'Empty%':<10}")
            print("-" * 70)
            for interval in sorted(self.api_calls_by_interval.keys()):
                calls = self.api_calls_by_interval[interval]
                points = self.data_points_by_interval[interval]
                efficiency = self.get_efficiency_ratio(interval)
                empty_pct = self.get_empty_call_percent(interval)
                print(f"{interval:<10} {calls:<10} {points:<12,} {efficiency:<12.1f} {empty_pct:<10.1f}%")

        if self.errors_by_type:
            print("\nErrors encountered:")
            for error_type, count in self.errors_by_type.items():
                print(f"  {error_type}: {count}")

        print("=" * 70 + "\n")

metrics = MetricsCollector()

# ============================================================================
# HELPER FUNCTIONS FOR TIMESTAMPS AND FILENAMES
# ============================================================================

def format_compact_range(earliest: datetime, latest: datetime) -> str:
    """Format datetime range as YYMMDD_HHMMSS-YYMMDD_HHMMSS."""
    e = earliest.strftime("%y%m%d_%H%M%S")
    l = latest.strftime("%y%m%d_%H%M%S")
    return f"{e}-{l}"

def parse_timestamp_from_filename_part(date_str: str, time_str: str) -> Optional[datetime]:
    """Parse YYYY-MM-DD and HH-MM-SS from filename into datetime."""
    try:
        dt_str = f"{date_str} {time_str.replace('-', ':')}"
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def parse_filename_timestamps(filename: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Extract earliest and latest timestamps from filename.

    Expected format: {ez}_{ex}_{code}_{iv}_{start_date}_{start_time}_{end_date}_{end_time}_{ccy}.csv
    """
    try:
        base = os.path.basename(filename)[:-4]  # Remove .csv
        parts = base.split("_")
        if len(parts) < 9:
            return None, None

        # Parts: 0=ez, 1=ex, 2=code, 3=iv, 4=start_date, 5=start_time, 6=end_date, 7=end_time, 8=ccy
        earliest = parse_timestamp_from_filename_part(parts[4], parts[5])
        latest = parse_timestamp_from_filename_part(parts[6], parts[7])
        return earliest, latest
    except Exception:
        return None, None

def ensure_datetime(dt_obj):
    """Convert various datetime-like objects to Python datetime."""
    if isinstance(dt_obj, datetime):
        return dt_obj
    elif isinstance(dt_obj, pd.Timestamp):
        return dt_obj.to_pydatetime()
    elif isinstance(dt_obj, np.datetime64):
        return pd.Timestamp(dt_obj).to_pydatetime()
    elif isinstance(dt_obj, (int, np.integer)):
        return datetime.utcfromtimestamp(int(dt_obj))
    elif isinstance(dt_obj, str):
        return pd.to_datetime(dt_obj).to_pydatetime()
    else:
        raise TypeError(f"Cannot convert {type(dt_obj)} to datetime")

def detect_timestamp_column(df: pd.DataFrame) -> Optional[str]:
    """Detect timestamp column in dataframe."""
    for col in ['timestamp', 't', 'date', 'datetime', 'time']:
        if col not in df.columns:
            continue

        if pd.api.types.is_datetime64_dtype(df[col]):
            return col

        try:
            if pd.api.types.is_numeric_dtype(df[col]):
                test = pd.to_datetime(df[col], unit='s', errors='coerce')
            else:
                test = pd.to_datetime(df[col], errors='coerce')

            if not test.isna().all():
                new_col = f'{col}_dt'
                df[new_col] = test
                return new_col
        except Exception:
            pass

    return None

def prepare_dataframe(data: list, interval: str) -> Optional[pd.DataFrame]:
    """Convert API response to sorted DataFrame."""
    if not data:
        return None

    df = pd.DataFrame(data)

    if 't' in df.columns:
        if pd.api.types.is_numeric_dtype(df['t']):
            df['timestamp'] = pd.to_datetime(df['t'], unit='s', utc=True)
        else:
            df['timestamp'] = pd.to_datetime(df['t'])
    elif 'date' in df.columns:
        if not pd.api.types.is_datetime64_dtype(df['date']):
            df['date'] = pd.to_datetime(df['date'])

    ts_col = 'timestamp' if 'timestamp' in df.columns else 'date'
    if ts_col in df.columns:
        df = df.sort_values(ts_col)

    return df

def generate_filename(exchangezone: str, exchange_name: str, code: str,
                      interval: str, start_date: datetime, end_date: datetime,
                      currency: str) -> str:
    """Generate standardized CSV filename."""
    start_str = start_date.strftime("%Y-%m-%d_%H-%M-%S")
    end_str = end_date.strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{exchangezone}_{exchange_name}_{code}_{interval}_{start_str}_{end_str}_{currency}.csv"
    return os.path.join(Config.DATA_DIR, filename)

# ============================================================================
# INTEGRITY CHECK FUNCTIONS
# ============================================================================

def check_chronological_order(df: pd.DataFrame, ts_col: str) -> List[str]:
    """Check if timestamps are in ascending order. Returns list of issues."""
    issues = []
    if df[ts_col].is_monotonic_increasing:
        return issues

    # Find out-of-order pairs
    out_of_order = []
    for i in range(len(df) - 1):
        if df.iloc[i][ts_col] > df.iloc[i+1][ts_col]:
            out_of_order.append(i)
            if len(out_of_order) >= 5:  # Limit examples
                break

    if out_of_order:
        issues.append(f"Out-of-order timestamps at rows: {out_of_order[:5]}")

    return issues

def detect_gaps_1d(df: pd.DataFrame, ts_col: str) -> List[str]:
    """Detect missing business days in 1d data."""
    issues = []

    try:
        dates = pd.to_datetime(df[ts_col]).dt.date
        min_date = dates.min()
        max_date = dates.max()

        expected_dates = pd.bdate_range(start=min_date, end=max_date)
        expected_set = set(expected_dates.date)
        actual_set = set(dates)

        missing = expected_set - actual_set
        if missing:
            missing_sorted = sorted(list(missing))[:10]  # Show first 10
            issues.append(f"Missing {len(missing)} business days (first 10): {missing_sorted}")
    except Exception as e:
        issues.append(f"Gap detection failed: {e}")

    return issues

def detect_gaps_intraday(df: pd.DataFrame, ts_col: str, interval: str) -> List[str]:
    """Detect large gaps in intraday data."""
    issues = []

    try:
        timestamps = pd.to_datetime(df[ts_col])
        diffs = timestamps.diff()

        # Define "large gap" threshold (e.g., > 1 day)
        large_gap_threshold = pd.Timedelta(days=1)
        large_gaps = diffs[diffs > large_gap_threshold]

        if not large_gaps.empty:
            gap_count = len(large_gaps)
            max_gap = large_gaps.max()
            issues.append(f"Found {gap_count} gaps > 1 day (max: {max_gap})")
    except Exception as e:
        issues.append(f"Gap detection failed: {e}")

    return issues

def perform_integrity_check(filename: str, interval: str, full_check: bool = False) -> Dict:
    """Perform integrity check on a CSV file.

    Returns dict with: ok (bool), issues (list), actual_earliest, actual_latest
    """
    result = {
        "ok": True,
        "issues": [],
        "actual_earliest": None,
        "actual_latest": None
    }

    if not os.path.exists(filename):
        result["ok"] = False
        result["issues"].append("File does not exist")
        return result

    try:
        df = pd.read_csv(filename, low_memory=False)
        if df.empty:
            result["issues"].append("Empty file")
            return result

        ts_col = detect_timestamp_column(df)
        if not ts_col:
            result["ok"] = False
            result["issues"].append("Cannot detect timestamp column")
            return result

        # Get actual timestamps from data
        actual_earliest = ensure_datetime(df[ts_col].min())
        actual_latest = ensure_datetime(df[ts_col].max())
        result["actual_earliest"] = actual_earliest
        result["actual_latest"] = actual_latest

        # Parse filename timestamps
        filename_earliest, filename_latest = parse_filename_timestamps(filename)

        # Check filename consistency
        if filename_earliest and filename_latest:
            if abs((actual_earliest - filename_earliest).total_seconds()) > 60:  # 1 minute tolerance
                result["ok"] = False
                result["issues"].append(
                    f"Filename earliest {filename_earliest} != actual {actual_earliest}"
                )
            if abs((actual_latest - filename_latest).total_seconds()) > 60:
                result["ok"] = False
                result["issues"].append(
                    f"Filename latest {filename_latest} != actual {actual_latest}"
                )

        # Full integrity check
        if full_check:
            # Check chronological order
            order_issues = check_chronological_order(df, ts_col)
            if order_issues:
                result["ok"] = False
                result["issues"].extend(order_issues)

            # Check for gaps
            if interval == "1d":
                gap_issues = detect_gaps_1d(df, ts_col)
            else:
                gap_issues = detect_gaps_intraday(df, ts_col, interval)

            if gap_issues:
                result["ok"] = False
                result["issues"].extend(gap_issues)

    except Exception as e:
        result["ok"] = False
        result["issues"].append(f"Check failed: {e}")

    return result

# ============================================================================
# HTTP SESSION MANAGER
# ============================================================================

class SessionManager:
    """Persistent HTTP session with retry logic and connection pooling."""

    def __init__(self):
        self.session = requests.Session()

        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20
        )

        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers["User-Agent"] = "eodhd-downloader/3.0"

        self.last_limit_check = 0
        self.calls_remaining = None
        self.daily_total = None

    def check_api_limit(self) -> int:
        """Check remaining API calls (cached for 5 minutes)."""
        now = time.time()

        if now - self.last_limit_check < 300 and self.calls_remaining is not None:
            return self.calls_remaining

        try:
            url = "https://eodhd.com/api/user"
            params = {"api_token": Config.API_KEY, "fmt": "json"}
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()

            data = resp.json()
            daily_limit = data.get("dailyRateLimit", 100000)
            used = data.get("apiRequests", 0)
            extra = data.get("extraLimit", 0)

            self.daily_total = daily_limit + extra
            self.calls_remaining = self.daily_total - used
            self.last_limit_check = now

            logger.info(f"API quota: {used}/{self.daily_total} used, {self.calls_remaining} remaining")

        except Exception as e:
            logger.warning(f"Failed to check API limit: {e}")
            self.calls_remaining = 100000

        return self.calls_remaining

    def get(self, url: str, params: dict, timeout: int = 15) -> Tuple[Optional[dict], str]:
        """Make GET request with error handling.

        Returns: (data, status_code_or_error_type)
        """
        try:
            resp = self.session.get(url, params=params, timeout=timeout)

            if resp.status_code == 429:
                wait_time = int(resp.headers.get("Retry-After", 60))
                logger.warning(f"Rate limited, waiting {wait_time}s")
                time.sleep(wait_time)
                return None, "rate_limited"

            if resp.status_code == 401:
                return None, "auth_error"

            if resp.status_code == 403:
                return None, "not_allowed"

            if resp.status_code >= 500:
                logger.warning(f"HTTP {resp.status_code}: {url}")
                return None, "http_error"

            if resp.status_code >= 400:
                logger.warning(f"HTTP {resp.status_code}: {url}")
                return None, f"http_{resp.status_code}"

            data = resp.json()
            return data if data else None, "success"

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout: {url}")
            metrics.record_error("timeout")
            return None, "timeout"

        except requests.exceptions.RequestException as e:
            logger.warning(f"Request failed: {e}")
            metrics.record_error("request_exception")
            return None, "connection_error"

        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            metrics.record_error("unexpected")
            return None, "unknown_error"

    def close(self):
        """Close session and release connections."""
        self.session.close()

session_manager = SessionManager()

# ============================================================================
# LEDGER MANAGER
# ============================================================================

class LedgerManager:
    """Manages completed tickers ledger with batched writes and health tracking."""

    def __init__(self, exchangezone: str):
        self.exchangezone = exchangezone
        self.filename = f"{exchangezone}_completed_tickers.json"
        self.completed = self.load_with_salvage()
        self.dirty_count = 0
        self.last_flush_time = time.time()

    def load_with_salvage(self) -> dict:
        """Load JSON with robust salvage on corruption."""
        if not os.path.exists(self.filename):
            return {}

        try:
            with open(self.filename, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON corrupted in {self.filename} ({e}), salvaging...")
            return self._salvage_json()

    def _salvage_json(self) -> dict:
        """Attempt to salvage corrupted JSON file."""
        with open(self.filename, 'r', errors='ignore') as f:
            raw = f.read()

        for _ in range(20):
            cut = raw.rfind("}", 0, len(raw) - 1)
            if cut == -1:
                break
            try:
                data = json.loads(raw[:cut + 1])
                logger.info(f"Salvaged {len(data)} tickers from truncated JSON")
                return data
            except Exception:
                raw = raw[:cut]

        logger.warning("JSON unsalvageable, scanning CSV files...")
        return self._scan_csv_files()

    def _scan_csv_files(self) -> dict:
        """Rebuild ledger by scanning existing CSV files."""
        data = {}
        pattern = f"{self.exchangezone}_*_*_*_*_*_*.csv"

        for filepath in glob.glob(os.path.join(Config.DATA_DIR, pattern)):
            try:
                parts = os.path.basename(filepath)[:-4].split("_")
                if len(parts) < 8:
                    continue

                _, exchange, code, interval = parts[:4]
                currency = parts[-1]
                ticker = f"{code}.{self.exchangezone}"

                data.setdefault(ticker, {})[interval] = {
                    "filename": filepath,
                    "currency": currency,
                    "exchange_name": exchange,
                    "status": "unknown",
                    "health": "ok"
                }

            except Exception:
                continue

        logger.info(f"Rebuilt ledger from {len(data)} CSV files")
        return data

    def update(self, ticker: str, interval: str, metadata: dict):
        """Update ledger entry with extended metadata."""
        self.completed.setdefault(ticker, {})[interval] = metadata
        self.dirty_count += 1

    def maybe_flush(self, force: bool = False):
        """Flush to disk if dirty threshold reached or forced."""
        now = time.time()
        should_flush = (
            force or
            self.dirty_count >= Config.BATCH_SIZE or
            (now - self.last_flush_time) >= 60
        )

        if should_flush and self.dirty_count > 0:
            self._atomic_write()
            logger.info(f"Flushed ledger: {self.dirty_count} updates, {len(self.completed)} total tickers")
            self.dirty_count = 0
            self.last_flush_time = now

    def _atomic_write(self):
        """Write JSON atomically (temp file + rename)."""
        temp_file = self.filename + ".tmp"

        with open(temp_file, 'w') as f:
            json.dump(self.completed, f, indent=2)

        os.replace(temp_file, self.filename)

    def get_entry(self, ticker: str, interval: str) -> Optional[dict]:
        """Get ledger entry for ticker/interval."""
        return self.completed.get(ticker, {}).get(interval)

# ============================================================================
# API REQUEST FUNCTIONS
# ============================================================================

def api_request_intraday(ticker: str, interval: str, start_ts: int, end_ts: int) -> Tuple[Optional[list], str]:
    """Request intraday data from EODHD.

    Returns: (data, status)
    """
    url = f"https://eodhd.com/api/intraday/{ticker}"
    params = {
        "api_token": Config.API_KEY,
        "interval": interval,
        "from": start_ts,
        "to": end_ts,
        "fmt": "json"
    }

    data, status = session_manager.get(url, params)
    rows = len(data) if data else 0
    metrics.record_api_call("intraday", interval, rows)
    return data, status

def api_request_eod_range(ticker: str, start_date: str, end_date: str) -> Tuple[Optional[list], str]:
    """Request EOD data for specific date range.

    Returns: (data, status)
    """
    url = f"https://eodhd.com/api/eod/{ticker}"
    params = {
        "api_token": Config.API_KEY,
        "from": start_date,
        "to": end_date,
        "fmt": "json"
    }

    data, status = session_manager.get(url, params)
    rows = len(data) if data else 0
    metrics.record_api_call("eod", "1d", rows)
    return data, status

def api_request_eod_full_history(ticker: str) -> Tuple[Optional[list], str]:
    """Request FULL EOD history in a single call.

    Returns: (data, status)
    """
    url = f"https://eodhd.com/api/eod/{ticker}"
    params = {
        "api_token": Config.API_KEY,
        "fmt": "json"
    }

    data, status = session_manager.get(url, params)
    rows = len(data) if data else 0
    metrics.record_api_call("eod", "1d", rows)
    return data, status

# ============================================================================
# DOWNLOAD LOGIC WITH INCREMENTAL UPDATES
# ============================================================================

def download_and_update(ticker: str, interval: str, exchangezone: str, code: str,
                        currency: str, exchange_name: str, ledger: LedgerManager,
                        status_grid: StatusGrid, integrity_level: str = "none") -> StatusCell:
    """Download or update data for a ticker/interval with incremental updates.

    Returns StatusCell with final state.
    """

    # Initialize cell
    status_grid.update_cell(ticker, interval, state="downloading")

    # Check for existing file
    ledger_entry = ledger.get_entry(ticker, interval)
    existing_file = ledger_entry.get("filename") if ledger_entry else None

    existing_df = None
    latest_ts = None
    earliest_ts = None

    if existing_file and os.path.exists(existing_file):
        try:
            existing_df = pd.read_csv(existing_file, low_memory=False)
            if not existing_df.empty:
                ts_col = detect_timestamp_column(existing_df)
                if ts_col:
                    latest_ts = ensure_datetime(existing_df[ts_col].max())
                    earliest_ts = ensure_datetime(existing_df[ts_col].min())
        except Exception as e:
            logger.warning(f"{ticker} {interval}: Error reading existing file: {e}")

    # Determine download strategy
    today = datetime.utcnow()

    if interval == "1d":
        # EOD: download from latest_ts+1 day to today, or full history if no existing data
        if latest_ts:
            start_date = (latest_ts + timedelta(days=1)).strftime("%Y-%m-%d")
            end_date = today.strftime("%Y-%m-%d")
            data, status = api_request_eod_range(ticker, start_date, end_date)
        else:
            data, status = api_request_eod_full_history(ticker)
    else:
        # Intraday: download from latest_ts+1 interval to now, or backward scan if no existing data
        if latest_ts:
            start_ts = int((latest_ts + timedelta(seconds=60)).timestamp())
            end_ts = int(today.timestamp())
            data, status = api_request_intraday(ticker, interval, start_ts, end_ts)
        else:
            # No existing data - do backward scan
            return download_intraday_backward_scan(
                ticker, interval, exchangezone, code, currency, exchange_name,
                ledger, status_grid, integrity_level
            )

    # Handle different response statuses
    if status == "auth_error":
        status_grid.update_cell(ticker, interval, state="auth_error")
        ledger.update(ticker, interval, {
            "status": "auth_error",
            "health": "error",
            "issues": ["Authentication error"]
        })
        return status_grid.grid[interval][ticker]

    if status == "not_allowed":
        status_grid.update_cell(ticker, interval, state="not_allowed")
        ledger.update(ticker, interval, {
            "status": "not_allowed",
            "health": "error",
            "issues": ["Data not allowed for this subscription"]
        })
        return status_grid.grid[interval][ticker]

    if status in ["http_error", "connection_error", "timeout"]:
        status_grid.update_cell(ticker, interval, state=status)
        ledger.update(ticker, interval, {
            "status": status,
            "health": "error",
            "issues": [f"Network error: {status}"]
        })
        return status_grid.grid[interval][ticker]

    # Process data
    if not data:
        if existing_df is not None:
            # Already have data, nothing new
            status_grid.update_cell(
                ticker, interval,
                state="up_to_date",
                earliest_ts=earliest_ts,
                latest_ts=latest_ts,
                rows_count=len(existing_df)
            )
            return status_grid.grid[interval][ticker]
        else:
            # No data at all
            status_grid.update_cell(ticker, interval, state="no_data")
            ledger.update(ticker, interval, {
                "status": "no_data",
                "health": "ok"
            })
            return status_grid.grid[interval][ticker]

    # Prepare new data
    new_df = prepare_dataframe(data, interval)
    if new_df is None or new_df.empty:
        if existing_df is not None:
            status_grid.update_cell(
                ticker, interval,
                state="up_to_date",
                earliest_ts=earliest_ts,
                latest_ts=latest_ts,
                rows_count=len(existing_df)
            )
            return status_grid.grid[interval][ticker]
        else:
            status_grid.update_cell(ticker, interval, state="no_data")
            ledger.update(ticker, interval, {"status": "no_data", "health": "ok"})
            return status_grid.grid[interval][ticker]

    # Merge with existing data
    if existing_df is not None:
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        final_state = "updated"
    else:
        combined = new_df
        final_state = "downloaded_new"

    # Deduplicate and sort
    ts_col = detect_timestamp_column(combined)
    if not ts_col:
        status_grid.update_cell(ticker, interval, state="unknown_error")
        return status_grid.grid[interval][ticker]

    combined = combined.drop_duplicates(subset=[ts_col]).sort_values(ts_col)
    final_earliest = ensure_datetime(combined[ts_col].min())
    final_latest = ensure_datetime(combined[ts_col].max())

    # Generate filename and save
    filename = generate_filename(
        exchangezone, exchange_name, code, interval,
        final_earliest, final_latest, currency
    )
    combined.to_csv(filename, index=False)

    # Delete old file if name changed
    if existing_file and existing_file != filename and os.path.exists(existing_file):
        try:
            os.remove(existing_file)
        except Exception:
            pass

    # Perform integrity check if requested
    issues = []
    health_status = "ok"
    if integrity_level in ["basic", "full"]:
        check_result = perform_integrity_check(filename, interval, full_check=(integrity_level == "full"))
        if not check_result["ok"]:
            health_status = "issues_found"
            issues = check_result["issues"]
            logger.warning(f"{ticker} {interval}: Integrity issues: {issues}")

    # Update ledger
    ledger.update(ticker, interval, {
        "earliest_ts": final_earliest.isoformat(),
        "latest_ts": final_latest.isoformat(),
        "filename": filename,
        "currency": currency,
        "exchange_name": exchange_name,
        "status": final_state,
        "health": health_status,
        "issues": issues,
        "rows": len(combined)
    })

    # Update status grid
    status_grid.update_cell(
        ticker, interval,
        state=final_state,
        earliest_ts=final_earliest,
        latest_ts=final_latest,
        rows_count=len(combined)
    )

    return status_grid.grid[interval][ticker]

def download_intraday_backward_scan(ticker: str, interval: str, exchangezone: str,
                                     code: str, currency: str, exchange_name: str,
                                     ledger: LedgerManager, status_grid: StatusGrid,
                                     integrity_level: str = "none") -> StatusCell:
    """Download intraday data with backward scanning (for new tickers)."""

    today = datetime.utcnow()
    chunk_days = Config.MAX_PERIOD[interval]
    earliest_possible = Config.get_earliest_date(interval, ticker)

    # Initial probe
    probe_start = int((today - timedelta(days=10)).timestamp())
    probe_end = int(today.timestamp())
    data, status = api_request_intraday(ticker, interval, probe_start, probe_end)

    # Handle errors
    if status in ["auth_error", "not_allowed", "http_error", "connection_error"]:
        status_grid.update_cell(ticker, interval, state=status)
        ledger.update(ticker, interval, {"status": status, "health": "error"})
        return status_grid.grid[interval][ticker]

    if not data:
        status_grid.update_cell(ticker, interval, state="no_data")
        ledger.update(ticker, interval, {"status": "no_data", "health": "ok"})
        return status_grid.grid[interval][ticker]

    # Start backward scan
    frames = [prepare_dataframe(data, interval)]
    current_end = today - timedelta(days=10)
    current_start = max(current_end - timedelta(days=chunk_days), earliest_possible)

    while current_start >= earliest_possible:
        start_ts = int(current_start.timestamp())
        end_ts = int(current_end.timestamp())

        data, status = api_request_intraday(ticker, interval, start_ts, end_ts)
        if not data:
            break

        df = prepare_dataframe(data, interval)
        if df is None or df.empty:
            break

        frames.append(df)
        current_end = current_start - timedelta(days=1)
        current_start = max(current_end - timedelta(days=chunk_days), earliest_possible)
        time.sleep(0.05)

    # Combine all frames
    frames.reverse()
    combined = pd.concat(frames, ignore_index=True)

    ts_col = detect_timestamp_column(combined)
    if not ts_col:
        status_grid.update_cell(ticker, interval, state="unknown_error")
        return status_grid.grid[interval][ticker]

    combined = combined.drop_duplicates(subset=[ts_col]).sort_values(ts_col)
    final_earliest = ensure_datetime(combined[ts_col].min())
    final_latest = ensure_datetime(combined[ts_col].max())

    # Save
    filename = generate_filename(
        exchangezone, exchange_name, code, interval,
        final_earliest, final_latest, currency
    )
    combined.to_csv(filename, index=False)

    # Integrity check
    issues = []
    health_status = "ok"
    if integrity_level in ["basic", "full"]:
        check_result = perform_integrity_check(filename, interval, full_check=(integrity_level == "full"))
        if not check_result["ok"]:
            health_status = "issues_found"
            issues = check_result["issues"]

    # Update ledger
    ledger.update(ticker, interval, {
        "earliest_ts": final_earliest.isoformat(),
        "latest_ts": final_latest.isoformat(),
        "filename": filename,
        "currency": currency,
        "exchange_name": exchange_name,
        "status": "downloaded_new",
        "health": health_status,
        "issues": issues,
        "rows": len(combined)
    })

    status_grid.update_cell(
        ticker, interval,
        state="downloaded_new",
        earliest_ts=final_earliest,
        latest_ts=final_latest,
        rows_count=len(combined)
    )

    return status_grid.grid[interval][ticker]

def process_ticker(ticker: str, exchangezone: str, code: str, currency: str,
                   exchange_name: str, ledger: LedgerManager, status_grid: StatusGrid,
                   integrity_level: str = "none"):
    """Process all intervals for a single ticker."""

    metrics.tickers_processed += 1

    for interval in Config.INTERVALS:
        download_and_update(
            ticker, interval, exchangezone, code, currency, exchange_name,
            ledger, status_grid, integrity_level
        )
        ledger.maybe_flush()

# ============================================================================
# TICKER FILE PARSING
# ============================================================================

def parse_ticker_file(filepath: str, target_exchange: Optional[str] = None) -> List[Tuple]:
    """Parse ticker file (JSON list format)."""
    exchangezone = os.path.basename(filepath).split("_")[0]

    with open(filepath, 'r') as f:
        content = f.read()

    try:
        items = json.loads(content)
    except Exception as e:
        logger.error(f"Failed to parse {filepath}: {e}")
        return []

    tickers = []
    for item in items:
        code = item.get("Code")
        if not code:
            continue

        exchange = item.get("Exchange", exchangezone)
        if target_exchange and exchange != target_exchange:
            continue

        currency = item.get("Currency", "USD")
        ticker = f"{code}.{exchangezone}"
        tickers.append((ticker, exchangezone, code, currency, exchange))

    return tickers

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="EODHD Market Data Downloader (Optimized)")
    parser.add_argument("--exchange", help="Filter by exchange (e.g., NASDAQ)")
    parser.add_argument("--validate-only", action="store_true", help="Only run validator")
    parser.add_argument("--integrity-check", action="store_true", help="Basic integrity check")
    parser.add_argument("--full-integrity-check", action="store_true", help="Full integrity check")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without API calls")
    parser.add_argument("--intervals", help="Comma-separated intervals (default: from config)")

    args = parser.parse_args()

    # Override intervals if specified
    if args.intervals:
        Config.INTERVALS = args.intervals.split(",")

    # Determine integrity check level
    integrity_level = "none"
    if args.full_integrity_check:
        integrity_level = "full"
    elif args.integrity_check:
        integrity_level = "basic"

    logger.info("=" * 70)
    logger.info("EODHD Market Data Downloader - Optimized v3.1")
    logger.info("=" * 70)
    logger.info(f"Data directory: {Config.DATA_DIR}")
    logger.info(f"Intervals: {', '.join(Config.INTERVALS)}")
    logger.info(f"Integrity check: {integrity_level}")
    logger.info("=" * 70)

    os.makedirs(Config.DATA_DIR, exist_ok=True)

    ticker_files = glob.glob("*_tickers.txt")
    if not ticker_files:
        logger.error("No *_tickers.txt files found")
        return 1

    logger.info(f"Found {len(ticker_files)} ticker file(s)")

    remaining = session_manager.check_api_limit()
    if remaining < 1000:
        logger.warning(f"Low API quota: {remaining} calls remaining")

    for ticker_file in ticker_files:
        exchangezone = os.path.basename(ticker_file).split("_")[0]
        logger.info(f"\nProcessing {exchangezone} from {ticker_file}")

        tickers = parse_ticker_file(ticker_file, args.exchange)
        logger.info(f"Loaded {len(tickers)} tickers")

        if not tickers:
            continue

        ledger = LedgerManager(exchangezone)
        logger.info(f"Ledger: {len(ledger.completed)} tickers tracked")

        # Create status grid
        status_grid = StatusGrid(Config.INTERVALS)

        # Process tickers in batches for display
        batch_size = min(10, len(tickers))
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i + batch_size]

            # Initialize grid for this batch
            for ticker, _, _, _, _ in batch:
                for interval in Config.INTERVALS:
                    status_grid.update_cell(ticker, interval, state="pending")

            # Process batch
            for ticker, ez, code, currency, exchange in batch:
                if args.dry_run:
                    logger.info(f"[DRY RUN] Would process {ticker}")
                    continue

                try:
                    process_ticker(ticker, ez, code, currency, exchange, ledger, status_grid, integrity_level)

                except KeyboardInterrupt:
                    logger.warning("Interrupted by user, flushing ledger...")
                    ledger.maybe_flush(force=True)
                    raise

                except Exception as e:
                    logger.error(f"Error processing {ticker}: {e}")
                    metrics.record_error(type(e).__name__)
                    continue

            # Render grid for this batch
            status_grid.render(f"Batch {i//batch_size + 1} Status")

            # Clear grid for next batch
            status_grid = StatusGrid(Config.INTERVALS)

        ledger.maybe_flush(force=True)
        logger.info(f"Completed {exchangezone}: {len(ledger.completed)} tickers in ledger")

    session_manager.close()
    metrics.log_summary()

    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("\nInterrupted by user")
        sys.exit(130)

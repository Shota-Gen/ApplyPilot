"""GitHub job-board discovery: scrape community-maintained listing repos.

A class of popular GitHub repos (SimplifyJobs/New-Grad-Positions,
vanshb03/Summer2027-Internships, ...) maintain curated job lists. Each publishes
a structured `listings.json` under `.github/scripts/` containing every posting
with a direct apply URL. We fetch that JSON, filter to currently-active &
visible postings, and store them like any other discovered job.

Because the listing's `url` is ALREADY the external application link (Greenhouse,
Lever, Workday, Rippling, ...), we pre-set `application_url` so the apply stage
has a valid target even if enrichment can't find an apply button on the page.

Repos are hardcoded below. To add another listing repo of the same shape, append
an entry to REPOS.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx

from applypilot import config
from applypilot.database import get_connection, init_db, store_jobs

log = logging.getLogger(__name__)

# Engine name recorded in the jobs.source column (used by `apply --sources`).
SOURCE = "github"

# Hardcoded listing repos. Each exposes the same `listings.json` schema:
#   {company_name, title, locations[], url, active, is_visible, sponsorship,
#    season, date_posted, ...}
# `site` is the granular per-repo label stored on each job. It is what
# distinguishes the repos for `profile.json` education_by_source overrides and
# keeps internship vs new-grad rows separate (no cross-repo dedup).
REPOS: list[dict] = [
    {
        "owner": "SimplifyJobs",
        "name": "New-Grad-Positions",
        "branch": "dev",
        "json_path": ".github/scripts/listings.json",
        "site": "github-newgrad-simplify",
    },
    {
        "owner": "vanshb03",
        "name": "Summer2027-Internships",
        "branch": "dev",
        "json_path": ".github/scripts/listings.json",
        "site": "github-intern-vansh",
    },
]

UA = "Mozilla/5.0 (compatible; ApplyPilot/1.0; +https://github.com/Pickle-Pixel/ApplyPilot)"
FETCH_TIMEOUT = 60.0


# -- URL normalization -------------------------------------------------------

def normalize_url(raw: str | None) -> str | None:
    """Strip query, fragment, and trailing slash so cosmetic link changes
    don't create duplicate rows (or look like a brand-new, unapplied job)."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if not parts.scheme:
        return raw
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


# -- Location filter (same semantics as the other discovery engines) ---------

def _load_location_config(search_cfg: dict) -> tuple[list[str], list[str]]:
    """Read the user's location accept/reject patterns from search config."""
    accept = search_cfg.get("location_accept", []) or []
    reject = search_cfg.get("location_reject_non_remote", []) or []
    return accept, reject


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check if a job location passes the user's location filter.

    Mirrors discovery.smartextract._location_ok: remote always passes; with no
    accept patterns configured everything passes (accept-all default)."""
    if not location:
        return True
    loc = location.lower()
    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        return True
    for r in reject:
        if r.lower() in loc:
            return False
    if not accept:
        return True
    for a in accept:
        if a.lower() in loc:
            return True
    return False


# -- Listing parsing ---------------------------------------------------------

def _raw_url(repo: dict) -> str:
    return (
        f"https://raw.githubusercontent.com/"
        f"{repo['owner']}/{repo['name']}/{repo['branch']}/{repo['json_path']}"
    )


def fetch_listings(repo: dict) -> list[dict]:
    """Fetch and JSON-parse a repo's listings.json. Returns the raw list."""
    url = _raw_url(repo)
    resp = httpx.get(url, headers={"User-Agent": UA}, timeout=FETCH_TIMEOUT,
                     follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"Unexpected listings.json shape for {repo['name']}: {type(data)}")
    return data


def _listing_to_job(listing: dict) -> dict | None:
    """Map a listings.json entry to a job dict, or None if unusable."""
    url = normalize_url(listing.get("url"))
    if not url:
        return None

    title = (listing.get("title") or "").strip() or None
    company = (listing.get("company_name") or "").strip()

    locations = listing.get("locations") or []
    if isinstance(locations, list):
        location = ", ".join(str(loc) for loc in locations if loc) or None
    else:
        location = str(locations) or None

    # Short description preserves the company + metadata the JSON gives us,
    # since there is no dedicated company column. The full JD is fetched later
    # by the enrichment stage from the application page.
    season = listing.get("season") or ""
    sponsorship = listing.get("sponsorship") or ""
    desc_bits = [b for b in (company, season and f"Season: {season}",
                             sponsorship and f"Sponsorship: {sponsorship}") if b]
    description = " | ".join(desc_bits) or None

    return {
        "url": url,
        "title": title,
        "salary": None,
        "description": description,
        "location": location,
        # The listing's url IS the apply link — pre-set so apply always has a
        # valid target even if enrichment fails to find an apply button.
        "application_url": url,
    }


# -- Per-repo discovery ------------------------------------------------------

def discover_repo(conn, repo: dict, accept_locs: list[str],
                  reject_locs: list[str]) -> dict:
    """Fetch, filter, and store one repo's listings. Returns a stat dict."""
    stat = {"site": repo["site"], "fetched": 0, "active": 0,
            "stored": 0, "duplicate": 0, "filtered": 0, "error": None}

    try:
        listings = fetch_listings(repo)
    except Exception as e:
        log.error("GitHub fetch failed for %s/%s: %s", repo["owner"], repo["name"], e)
        stat["error"] = str(e)
        return stat

    stat["fetched"] = len(listings)

    jobs: list[dict] = []
    for listing in listings:
        # Mandatory: only currently-open, visible postings. The repos retain
        # historical/closed rows (SimplifyJobs' file is ~12MB of them).
        if not (listing.get("active") and listing.get("is_visible")):
            continue
        stat["active"] += 1

        job = _listing_to_job(listing)
        if not job:
            continue

        if not _location_ok(job.get("location"), accept_locs, reject_locs):
            stat["filtered"] += 1
            continue

        jobs.append(job)

    new, existing = store_jobs(conn, jobs, site=repo["site"],
                               strategy="github_json", source=SOURCE)
    stat["stored"] = new
    stat["duplicate"] = existing
    return stat


def run_github_discovery(repos: list[dict] | None = None) -> dict:
    """Discover jobs from the hardcoded GitHub listing repos.

    Args:
        repos: Override the repo list (defaults to REPOS).

    Returns:
        {"repos": [stat dicts], "stored": int, "duplicate": int}
    """
    repos = repos if repos is not None else REPOS
    conn = init_db()

    search_cfg = config.load_search_config()
    accept_locs, reject_locs = _load_location_config(search_cfg)

    results: list[dict] = []
    total_new = 0
    total_dup = 0
    for repo in repos:
        log.info("GitHub listings: %s/%s (%s)", repo["owner"], repo["name"], repo["branch"])
        stat = discover_repo(conn, repo, accept_locs, reject_locs)
        results.append(stat)
        total_new += stat["stored"]
        total_dup += stat["duplicate"]
        if stat["error"]:
            log.warning("  %s: error — %s", repo["site"], stat["error"])
        else:
            log.info("  %s: %d active, %d new, %d dup, %d filtered",
                     stat["site"], stat["active"], stat["stored"],
                     stat["duplicate"], stat["filtered"])

    return {"repos": results, "stored": total_new, "duplicate": total_dup}

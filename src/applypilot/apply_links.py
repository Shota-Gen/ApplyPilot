"""Direct-link application workflow.

Lets you hand ApplyPilot one or more application URLs and have it apply for you,
reusing the same database as the general auto-applier so it never re-applies to
a job that's already been submitted.

Flow (one-shot):
    1. Ingest  — normalize each URL and INSERT into the shared `jobs` table with
                 source="manual". Dedup is automatic: a URL already in the DB
                 (discovered or applied) is skipped and reported.
    2. Prep    — for the provided links ONLY: enrich (scrape the JD), score (for
                 visibility — NOT used to gate), tailor a resume (or fall back to
                 the base resume if the JD can't be scraped), write a cover letter.
    3. Apply   — submit via the normal browser flow, scoped to source="manual"
                 with no score gate, so every link you provide gets applied to.

The shared DB + normalized-URL primary key + the apply_status gate in
acquire_job() are what guarantee no double-applying.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from applypilot import config
from applypilot.config import (
    RESUME_PATH, RESUME_PDF_PATH, TAILORED_DIR, COVER_LETTER_DIR, load_profile,
)
from applypilot.database import get_connection, init_db, store_jobs

log = logging.getLogger(__name__)
console = Console()

SOURCE = "manual"


# -- URL handling ------------------------------------------------------------

def normalize_url(raw: str | None) -> str | None:
    """Strip query/fragment/trailing slash so the same link dedups across runs."""
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


def _title_from_url(url: str) -> str:
    """Best-effort human-ish title from the URL (real role comes from the JD)."""
    parts = urlsplit(url)
    tail = [seg for seg in parts.path.split("/") if seg][-1:] or [""]
    return f"{parts.netloc} {tail[0]}".strip()[:80] or "Provided link"


def _prefix(url: str) -> str:
    """Stable, collision-free filename prefix per URL."""
    return f"manual_{hashlib.md5(url.encode()).hexdigest()[:8]}"


def read_links(urls: list[str] | None, file: str | None) -> list[str]:
    """Collect, normalize, and de-duplicate links from args and/or a file."""
    raw: list[str] = list(urls or [])
    if file:
        p = Path(file).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"--file not found: {p}")
        raw += [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]

    seen: set[str] = set()
    out: list[str] = []
    for r in raw:
        link = normalize_url(r)
        if link and link not in seen:
            seen.add(link)
            out.append(link)
    return out


# -- Ingest ------------------------------------------------------------------

def _is_applied(row) -> bool:
    return bool(row["applied_at"]) or (row["apply_status"] or "") == "applied"


def ingest(conn, links: list[str]) -> tuple[list[str], list[tuple], list[tuple]]:
    """Route links into the manual flow.

    Returns (new_urls, adopted[(url, prev_source)], skipped[(url, reason)]):
      - already applied            -> skipped (never re-applied)
      - already in DB, not applied -> ADOPTED: re-pointed to source=manual so this
                                       run preps (only what's missing) and applies it
      - not in DB                  -> inserted fresh as source=manual
    """
    new: list[str] = []
    adopted: list[tuple] = []
    skipped: list[tuple] = []
    for link in links:
        existing = conn.execute(
            "SELECT url, apply_status, applied_at, source FROM jobs "
            "WHERE url = ? OR application_url = ?",
            (link, link),
        ).fetchone()
        if existing:
            if _is_applied(existing):
                skipped.append((link, "already applied"))
                continue
            # Adopt an existing, un-applied job into the manual workflow.
            if existing["source"] != SOURCE:
                conn.execute(
                    "UPDATE jobs SET source = ? WHERE url = ?", (SOURCE, existing["url"])
                )
                conn.commit()
            adopted.append((link, existing["source"]))
            continue
        store_jobs(
            conn,
            [{"url": link, "title": _title_from_url(link), "application_url": link,
              "salary": None, "description": None, "location": None}],
            site="manual", strategy="manual_link", source=SOURCE,
        )
        new.append(link)
    return new, adopted, skipped


# -- Prep helpers ------------------------------------------------------------

def _save_resume(conn, job: dict, text: str) -> str:
    """Write a tailored (or base) resume + PDF sibling, point the job at it."""
    from applypilot.scoring.pdf import convert_to_pdf
    txt_path = TAILORED_DIR / f"{_prefix(job['url'])}.txt"
    txt_path.write_text(text, encoding="utf-8")
    try:
        convert_to_pdf(txt_path)
    except Exception:
        log.debug("Resume PDF generation failed for %s", txt_path, exc_info=True)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE jobs SET tailored_resume_path = ?, tailored_at = ? WHERE url = ?",
        (str(txt_path), now, job["url"]),
    )
    conn.commit()
    return str(txt_path)


def _save_cover(conn, job: dict, letter: str) -> None:
    from applypilot.scoring.pdf import convert_to_pdf
    cl_path = COVER_LETTER_DIR / f"{_prefix(job['url'])}_CL.txt"
    cl_path.write_text(letter, encoding="utf-8")
    try:
        convert_to_pdf(cl_path)
    except Exception:
        log.debug("Cover PDF generation failed for %s", cl_path, exc_info=True)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE jobs SET cover_letter_path = ?, cover_letter_at = ? WHERE url = ?",
        (str(cl_path), now, job["url"]),
    )
    conn.commit()


def _row_to_dict(row) -> dict:
    return dict(zip(row.keys(), row))


def prep(conn, validation_mode: str = "normal") -> list[dict]:
    """Enrich → score (visibility) → tailor/fallback → cover, for unapplied
    manual jobs. Returns per-job summary dicts."""
    from applypilot.scoring.scorer import score_job
    from applypilot.scoring.tailor import tailor_resume
    from applypilot.scoring.cover_letter import generate_cover_letter

    profile = load_profile()
    base_resume = RESUME_PATH.read_text(encoding="utf-8") if RESUME_PATH.exists() else ""

    rows = conn.execute(
        "SELECT * FROM jobs WHERE source = ? AND applied_at IS NULL", (SOURCE,)
    ).fetchall()
    jobs = [_row_to_dict(r) for r in rows]
    if not jobs:
        return []

    # 1. Enrich (only those without a description yet) — scrape the JD.
    pending = [(j["url"], j.get("title") or "") for j in jobs if not j.get("full_description")]
    if pending:
        console.print(f"  [cyan]Enriching {len(pending)} link(s)...[/cyan]")
        try:
            from applypilot.enrichment.detail import scrape_site_batch
            scrape_site_batch(conn, "manual", pending)
        except Exception as e:
            log.error("Enrichment failed: %s", e)

    # Re-read post-enrichment state.
    rows = conn.execute(
        "SELECT * FROM jobs WHERE source = ? AND applied_at IS NULL", (SOURCE,)
    ).fetchall()
    jobs = [_row_to_dict(r) for r in rows]

    summaries: list[dict] = []
    for job in jobs:
        url = job["url"]
        has_jd = bool(job.get("full_description"))

        # 2. Score — visibility only, never gates. Reuse an existing score.
        if job.get("fit_score") is None:
            try:
                sc = score_job(base_resume, job)
                score = sc.get("score", 0)
                conn.execute(
                    "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
                    (score, sc.get("reasoning", ""), datetime.now(timezone.utc).isoformat(), url),
                )
                conn.commit()
            except Exception as e:
                log.error("Scoring failed for %s: %s", url, e)
                score = 0
        else:
            score = job["fit_score"]

        # 3. Resume — keep an existing tailored resume; otherwise tailor. Even
        #    without a JD the tailor trims your master down to ONE page (targeting
        #    the title). The raw master is the trim SOURCE and is NEVER uploaded.
        #    If tailoring errors outright, skip the link rather than send the master.
        if job.get("tailored_resume_path"):
            resume_status = "kept"
            try:
                resume_text = Path(job["tailored_resume_path"]).read_text(encoding="utf-8")
            except Exception:
                resume_text = base_resume
        else:
            try:
                text, report = tailor_resume(base_resume, job, profile,
                                             validation_mode=validation_mode)
                _save_resume(conn, job, text)
                resume_text = text
                resume_status = report.get("status", "tailored") + ("" if has_jd else " (no JD)")
            except Exception as e:
                log.error("Tailoring failed for %s: %s — skipping link (master never uploaded)", url, e)
                summaries.append({"url": url, "score": score, "jd": has_jd,
                                  "resume": "FAILED — skipped", "cover": "-"})
                continue

        # 4. Cover letter — keep existing; else generate (best-effort).
        if job.get("cover_letter_path"):
            cover = "kept"
        else:
            try:
                letter = generate_cover_letter(resume_text, job, profile,
                                               validation_mode=validation_mode)
                _save_cover(conn, job, letter)
                cover = "ok"
            except Exception as e:
                log.error("Cover letter failed for %s: %s", url, e)
                cover = "skipped"

        summaries.append({"url": url, "score": score, "jd": has_jd,
                          "resume": resume_status, "cover": cover})
    return summaries


# -- Orchestrator ------------------------------------------------------------

def run_apply_links(
    urls: list[str] | None = None,
    file: str | None = None,
    no_apply: bool = False,
    dry_run: bool = False,
    headless: bool = False,
    model: str = "haiku",
    workers: int = 1,
    limit: int | None = None,
    validation_mode: str = "normal",
) -> dict:
    """Ingest provided links, prep them, and apply. Returns a stats dict."""
    config.ensure_dirs()
    conn = init_db()

    links = read_links(urls, file)
    if not links:
        console.print("[red]No links provided.[/red] Pass URLs as arguments or --file.")
        return {"new": 0, "skipped": 0, "applied": 0}

    console.print(f"\n[bold blue]Apply-Links[/bold blue] — {len(links)} link(s)")

    new, adopted, skipped = ingest(conn, links)
    for link, prev in adopted:
        console.print(f"  [cyan]adopt[/cyan] {link}  [dim](was source={prev}, applying manually)[/dim]")
    for link, reason in skipped:
        console.print(f"  [yellow]skip[/yellow] {link}  [dim]({reason})[/dim]")
    console.print(f"  [green]{len(new)} new[/green], {len(adopted)} adopted, {len(skipped)} skipped\n")

    # Prep everything still pending under source=manual (covers new + prior incomplete).
    summaries = prep(conn, validation_mode=validation_mode)

    if summaries:
        tbl = Table(title="Prepared", show_header=True, header_style="bold cyan")
        tbl.add_column("Score", justify="center")
        tbl.add_column("JD")
        tbl.add_column("Resume")
        tbl.add_column("Cover")
        tbl.add_column("URL")
        for s in sorted(summaries, key=lambda x: x["score"], reverse=True):
            tbl.add_row(str(s["score"]), "yes" if s["jd"] else "[yellow]no[/yellow]",
                        s["resume"], s["cover"], s["url"][:60])
        console.print(tbl)

    if no_apply:
        console.print("\n[dim]--no-apply set: prepared but not submitted. "
                      "Run `applypilot apply --sources manual` when ready.[/dim]")
        return {"new": len(new), "skipped": len(skipped), "prepared": len(summaries), "applied": 0}

    # Count manual jobs that are ready and not yet applied. We pass this as a
    # FINITE limit so apply drains exactly these and stops — limit=0 would put
    # the worker into continuous (forever-polling) mode.
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE source = ? "
        "AND tailored_resume_path IS NOT NULL AND applied_at IS NULL "
        "AND (apply_status IS NULL OR apply_status = 'failed')",
        (SOURCE,),
    ).fetchone()[0]
    if ready == 0:
        console.print("[yellow]No manual links ready to apply.[/yellow]")
        return {"new": len(new), "skipped": len(skipped),
                "prepared": len(summaries), "applied": 0}

    # Apply: scoped to source=manual, no score gate (min_score=0), dedup-safe.
    from applypilot.apply.launcher import main as apply_main
    effective_limit = limit if limit is not None else ready
    console.print(f"\n[bold]Applying to {ready} manual link(s)[/bold] (dry_run={dry_run})...")
    apply_main(
        limit=effective_limit,
        min_score=0,
        sources=[SOURCE],
        headless=headless,
        model=model,
        dry_run=dry_run,
        workers=workers,
    )
    return {"new": len(new), "skipped": len(skipped),
            "prepared": len(summaries), "applied": effective_limit}

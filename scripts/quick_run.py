#!/usr/bin/env python3
"""
Aipply quick run — scan LinkedIn, tailor materials with AI, track & report.

This is the lightweight entry point used by the dashboard and for quick
one-off runs. For the full pipeline (all keywords x locations), use
scripts/run_cycle.py.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import os

os.chdir(PROJECT_ROOT)

import yaml

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from src.linkedin_scanner import LinkedInScanner
from src.linkedin_applicant import LinkedInApplicant
from src.resume_tailor import ResumeTailor
from src.cover_letter_gen import CoverLetterGenerator
from src.resume_selector import ResumeSelector
from src.cv_optimizer import CVOptimizer
from src.tracker import ApplicationTracker
from src.deslop import clean_docx

STOP_FILE = PROJECT_ROOT / ".stop"


def _settings() -> dict:
    with open("config/settings.yaml") as f:
        return yaml.safe_load(f) or {}


def _profile() -> dict:
    with open("config/profile.yaml") as f:
        return yaml.safe_load(f) or {}


def scan_jobs(keyword="devops engineer", location="Remote", limit=5):
    """Scan LinkedIn and return jobs with full descriptions.

    Respects the emergency stop file (.stop) and skips already-applied jobs.
    """
    if STOP_FILE.exists():
        print("🛑 EMERGENCY STOP — .stop file detected")
        return []

    settings = _settings()
    max_age_days = int(settings.get("search", {}).get("max_age_days", 7))

    tracker = ApplicationTracker("output/tracker.json")

    scanner = LinkedInScanner(config=settings)
    scanner.connect_browser()

    cards = scanner.search_jobs(
        keywords=keyword, location=location, max_results=limit + 5, max_age_days=max_age_days
    )
    # Apply the same company, seniority, and target-role filters as the full
    # cycle before opening individual listings.
    cards = scanner.filter_results(cards)

    jobs = []
    for card in cards[:limit]:
        url = card.get("url", "")
        card_company = card.get("company", "").strip()
        card_title = card.get("title", "").split("\n")[0].strip()
        if not url or tracker.is_already_applied(job_url=url, company=card_company, position=card_title):
            print(f"  ⏭️  Skipping (already applied): {card_company} — {card_title}")
            continue
        try:
            details = scanner.get_job_details(url)
            if details.get("description"):
                # Card-level posted time as fallback if detail page lacks one
                if not details.get("posted_text"):
                    details["posted_text"] = card.get("posted_text", "")
                    details["posted_at"] = card.get("posted_at", "")
                jobs.append(details)
        except Exception as e:
            print(f"  ⚠️ Failed to get details for {card.get('company')}: {e}")

    scanner.close()
    return jobs


def save_application(job: dict, dry_run: bool = False):
    """Tailor materials for a job with AI, save them, optionally apply, track.

    Args:
        job: Job dict with url, title, company, location, description.
        dry_run: If True, generate materials but do not submit the application.

    Returns:
        Dict with paths and apply status, or None if skipped.
    """
    if STOP_FILE.exists():
        print("🛑 STOP — .stop file detected")
        return None

    settings = _settings()
    profile = _profile()
    candidate = profile.get("candidate", {})

    company = (job.get("company") or "Unknown").strip()
    title = (job.get("title") or "Role").split("\n")[0].strip()
    location = job.get("location", "")
    url = job.get("url", "")
    desc = job.get("description", "")

    # Dedup check — skip if already applied (by URL or company+title)
    tracker = ApplicationTracker("output/tracker.json")
    if tracker.is_already_applied(job_url=url, company=company, position=title):
        print(f"⏭️  Already applied: {company} — {title}")
        return None

    # Output directory for this application
    from src.utils import sanitize_filename

    dir_name = sanitize_filename(f"{company}_{title}_{datetime.now().strftime('%Y%m%d_%H%M')}")
    out_dir = Path("output") / "applications" / dir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Pick the right CV variant for this job ---
    selector = ResumeSelector(config=settings)
    selected = selector.select(job)
    base_resume = selected["template"]
    cv_variant = selected["key"]
    if not Path(base_resume).exists():
        print(f"  ⚠️ Variant template missing ({base_resume}), falling back to base_resume.docx")
        base_resume = "templates/base_resume.docx"
    print(f"  📄 CV variant: {selected['name']} (match score {selected['score']})")

    # Tailor resume (AI via OpenRouter, falls back to base clone without a key)
    tailor = ResumeTailor(config={**settings, "candidate_name": candidate.get("name", "")})
    resume_path = tailor.tailor_and_save(
        base_resume_path=base_resume,
        job_description=desc,
        company=company,
        role=title,
        output_dir=out_dir,
    )

    # --- AI CV score for the dashboard ---
    cv_score = None
    score_details = None
    try:
        optimizer = CVOptimizer(config=settings)
        score_result = optimizer.score_resume(resume_path, desc, role=title, company=company)
        cv_score = score_result.get("score")
        score_details = score_result
        print(f"  ⭐ CV score: {cv_score}/100")
    except Exception as e:
        print(f"  ⚠️ CV scoring failed: {e}")

    # Generate cover letter (AI via OpenAI, falls back to template without a key)
    cl_gen = CoverLetterGenerator(config=settings)
    cl_path = cl_gen.generate_and_save(
        job_description=desc,
        candidate_profile=candidate,
        company=company,
        role=title,
        output_dir=out_dir,
        example_letter_path="templates/base_cover_letter.docx",
    )

    # Save job description
    jd_path = out_dir / "job_description.txt"
    jd_path.write_text(
        f"Company: {company}\nTitle: {title}\nLocation: {location}\nURL: {url}"
        f"\nPosted: {job.get('posted_text', 'unknown')}\n\n{desc}"
    )

    # De-slop all generated docs (strip AI tells)
    if resume_path:
        clean_docx(resume_path)
    if cl_path:
        clean_docx(cl_path)

    # --- Easy Apply submission (external-site jobs are never automated) ---
    status = "materials_ready"
    apply_result = {}
    apply_method = ""
    saved_screenshots = []
    if not dry_run:
        try:
            applicant = LinkedInApplicant(config=settings, profile=profile)
            applicant.connect_browser()
            apply_result = applicant.apply_to_job(
                job={"url": url, "title": title, "company": company},
                resume_path=str(resume_path),
                cover_letter_path=str(cl_path) if cl_path else None,
            )
            applicant.close()

            # Copy screenshots into the application output directory
            for ss_path in apply_result.get("screenshots", []):
                if ss_path and Path(ss_path).exists():
                    dest = out_dir / Path(ss_path).name
                    import shutil

                    shutil.copy2(ss_path, dest)
                    saved_screenshots.append(str(dest.resolve()))

            apply_status = apply_result.get("status", "failed")
            apply_method = apply_result.get("apply_method", "")
            if apply_status == "applied":
                status = "applied"
            elif apply_status == "dry_run":
                status = "materials_ready"
            else:
                status = "manual_needed"
        except Exception as e:
            print(f"  ⚠️ Easy Apply failed: {e}")
            status = "manual_needed"
            apply_method = "unknown"

    # Build notes with screenshot paths
    apply_notes = apply_result.get("reason", "") if not dry_run else "dry run - not applied"
    if saved_screenshots:
        apply_notes += f" | screenshots: {','.join(saved_screenshots)}"
    if apply_method == "manual" or apply_result.get("reason") == "external_site_apply":
        apply_notes = (apply_notes + " | external site — apply manually via job link").strip(" |")

    # Track
    tracker.add_application(
        company=company,
        position=title,
        job_url=url,
        location=location,
        status=status,
        resume_path=str(resume_path),
        cover_letter_path=str(cl_path),
        jd_file_path=str(jd_path),
        job_description=desc[:500],
        notes=apply_notes.strip(),
        screenshots=saved_screenshots,
        cv_variant=cv_variant,
        cv_score=cv_score,
        posted_at=job.get("posted_at", "") or "",
        posted_text=job.get("posted_text", "") or "",
        apply_method=apply_method or ("easy_apply" if status == "applied" else ""),
    )

    # Regenerate static report
    tracker.generate_html_report("output/reports/applications_report.html")

    print(f"  ✅ {status}: {company} — {title}")
    return {
        "company": company,
        "title": title,
        "resume": str(resume_path),
        "cover_letter": str(cl_path),
        "job_description": str(jd_path),
        "screenshots": saved_screenshots,
        "apply_status": status,
    }


def main():
    parser = argparse.ArgumentParser(description="Aipply quick run")
    parser.add_argument("keyword", nargs="?", default=None, help="Search keyword")
    parser.add_argument("location", nargs="?", default=None, help="Search location")
    parser.add_argument("--limit", type=int, default=5, help="Max jobs to process")
    parser.add_argument("--dry-run", action="store_true", help="Generate materials only, don't apply")
    args = parser.parse_args()

    settings = _settings()
    search = settings.get("search", {})
    keyword = args.keyword or (search.get("keywords") or ["devops engineer"])[0]
    location = args.location or (search.get("locations") or ["Remote"])[0]

    jobs = scan_jobs(keyword=keyword, location=location, limit=args.limit)
    print(f"Found {len(jobs)} job(s) to process")

    results = []
    for i, job in enumerate(jobs, 1):
        title_lines = (job.get("title") or "").splitlines()
        title_display = title_lines[0] if title_lines else "(untitled)"
        print(f"[{i}/{len(jobs)}] {job.get('company')} — {title_display}")
        try:
            result = save_application(job, dry_run=args.dry_run)
            if result:
                results.append(result)
        except Exception as e:
            print(f"  ⚠️ Failed: {e}")

    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()

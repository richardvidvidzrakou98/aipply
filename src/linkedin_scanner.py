"""LinkedIn job scanner module.

Searches LinkedIn for job postings matching configured criteria
and filters results based on exclusion rules.
"""

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlencode

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)


def parse_posted_text(text: str) -> datetime | None:
    """Parse a LinkedIn posted-time string into a naive local datetime.

    Handles formats like '3 hours ago', '2 days ago', '1 week ago',
    '30 minutes ago', '2 mo ago'. Returns None when unparseable.
    Callers that persist the value should call ``.isoformat()`` on it.
    """
    if not text:
        return None
    t = text.strip().lower()
    match = re.search(r"(\d+)\s*(minute|hour|day|week|month|mo)s?\s*ago", t)
    if not match:
        # 'just now' / 'today'
        if "now" in t or "today" in t or "moments" in t:
            return datetime.now()
        return None
    value = int(match.group(1))
    unit = match.group(2)
    delta = {
        "minute": timedelta(minutes=value),
        "hour": timedelta(hours=value),
        "day": timedelta(days=value),
        "week": timedelta(weeks=value),
        "month": timedelta(days=value * 30),
        "mo": timedelta(days=value * 30),
    }[unit]
    return datetime.now() - delta


# Default persistent browser profile directory
DEFAULT_PROFILE_DIR = str(Path.home() / ".aipply" / "chrome-profile")

# Fallback CSS selectors for LinkedIn job cards (LinkedIn changes these frequently)
JOB_CARD_SELECTORS = [
    "[data-occludable-job-id]",  # virtualized list shells — stable even when not scrolled into view
    ".job-card-container",
    ".jobs-search-results__list-item",
    "li.ember-view.occludable-update",
]

TITLE_SELECTORS = [
    ".artdeco-entity-lockup__title a[aria-label]",  # current LinkedIn markup
    ".job-card-list__title",
    ".job-card-list__title--link",
    ".job-card-container__link",
    "a.job-card-list__title--link",
    ".artdeco-entity-lockup__title a",
    "a[data-control-name='jobPosting_jobCardTitle']",
]

COMPANY_SELECTORS = [
    ".artdeco-entity-lockup__subtitle span",  # current LinkedIn markup
    ".job-card-container__primary-description",
    ".job-card-container__company-name",
    ".job-card-list__company-name",
]

LOCATION_SELECTORS = [
    ".job-card-container__metadata-wrapper li",  # current LinkedIn markup
    ".job-card-container__metadata-item",
    ".artdeco-entity-lockup__caption span",
    ".job-card-list__location",
]

POSTED_SELECTORS = [
    ".job-card-container__listed-time",          # classic markup
    ".artdeco-entity-lockup__caption time",      # current markup
    ".job-card-list__listed-time time",
    "time[datetime]",
    ".job-details-jobs-unified-top-card__posted-date",
]


class LinkedInScanner:
    """Scans LinkedIn for job postings and filters results."""

    def __init__(self, config: dict[str, Any] | None = None):
        """Initialize scanner with search configuration.

        Args:
            config: Search settings from config/settings.yaml.
        """
        self.config = config or {}
        self.search_config = self.config.get("search", {})
        self.exclusions = self.config.get("exclusions", {})
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # ------------------------------------------------------------------
    # Browser management
    # ------------------------------------------------------------------

    def connect_browser(self, cdp_url: str | None = None) -> Page:
        """Connect to an existing Chrome via CDP or launch a persistent context.

        Args:
            cdp_url: Chrome DevTools Protocol URL (e.g. ``http://localhost:9222``).
                     If provided, connects over CDP.  Otherwise launches a
                     persistent Chromium context using a local user-data dir.

        Returns:
            A Playwright ``Page`` object ready for navigation.
        """
        self._playwright = sync_playwright().start()

        if cdp_url:
            logger.info("Connecting to Chrome via CDP at %s", cdp_url)
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
                contexts = self._browser.contexts
                if contexts:
                    self._context = contexts[0]
                    pages = self._context.pages
                    self._page = pages[0] if pages else self._context.new_page()
                else:
                    self._context = self._browser.new_context()
                    self._page = self._context.new_page()
                logger.info("Connected to existing Chrome instance via CDP")
                return self._page
            except Exception as exc:
                logger.warning("CDP connection failed (%s), falling back to persistent context", exc)

        # Fallback: persistent browser context
        profile_dir = (
            (self.config.get("browser") or {}).get("profile_dir") or DEFAULT_PROFILE_DIR
        )
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        logger.info("Launching persistent Chromium context at %s", profile_dir)

        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    # ------------------------------------------------------------------
    # Job search
    # ------------------------------------------------------------------

    def search_jobs(
        self,
        keywords: str | list[str],
        location: str,
        max_results: int = 25,
        max_age_days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search LinkedIn Jobs for postings matching *keywords* and *location*.

        Args:
            keywords: Search term(s).  If a list, joined with spaces.
            location: Target location string (e.g. ``"San Francisco Bay Area"``).
            max_results: Stop collecting after this many results (default 25).
            max_age_days: Only keep jobs posted within this many days.
                          Defaults to the configured ``search.max_age_days``
                          (1 week) or 7 when unset. Pass 0 to disable.

        Returns:
            List of dicts with keys: ``title``, ``company``, ``location``,
            ``url``, ``linkedin_job_id``, ``posted_at`` (ISO), ``posted_text``.
        """
        if self._page is None:
            raise RuntimeError("Browser not connected. Call connect_browser() first.")

        if isinstance(keywords, list):
            keywords = " ".join(keywords)

        if max_age_days is None:
            max_age_days = int(
                (self.search_config or {}).get("max_age_days", 7)
            )

        params = {
            "keywords": keywords,
            "location": location,
            "f_TPR": "r604800",  # LinkedIn server-side filter: past week
        }
        search_url = (
            f"https://www.linkedin.com/jobs/search/?{urlencode(params)}"
        )
        logger.info("Navigating to %s", search_url)
        self._page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
        # Wait until at least one job card is rendered (SPA render can take a while)
        try:
            self._page.wait_for_selector(
                JOB_CARD_SELECTORS[0], timeout=15_000
            )
        except Exception:
            # Fall back to a static wait, then continue — the card finder
            # below tries several selectors anyway.
            self._page.wait_for_timeout(5000)
        self._page.wait_for_timeout(2000)  # settle

        all_jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        max_pages = 5

        for page_num in range(max_pages):
            if len(all_jobs) >= max_results:
                break

            logger.info("Parsing results page %d (collected %d so far)", page_num + 1, len(all_jobs))
            cards = self._find_job_cards()

            for card in cards:
                if len(all_jobs) >= max_results:
                    break
                job = self._parse_job_card(card)
                if not job:
                    continue
                if job.get("linkedin_job_id") in seen_ids:
                    continue
                if max_age_days and not self._is_within_age(job.get("posted_text", ""), max_age_days):
                    logger.debug(
                        "Skipping old posting: %s (%s)",
                        job.get("title"), job.get("posted_text"),
                    )
                    continue
                seen_ids.add(job["linkedin_job_id"])
                all_jobs.append(job)

            # Stop paginating once we have enough parsed (not just rendered) jobs
            if len(all_jobs) >= max_results:
                break

            # Paginate: scroll down and wait for more results
            if not self._load_more_results():
                logger.info("No more results to load.")
                break

        logger.info("Collected %d job(s) total", len(all_jobs))
        return all_jobs[:max_results]

    def _find_job_cards(self):
        """Try multiple selectors to locate job card elements."""
        for selector in JOB_CARD_SELECTORS:
            cards = self._page.query_selector_all(selector)
            if cards:
                logger.debug("Found %d cards with selector '%s'", len(cards), selector)
                return cards
        logger.warning("No job cards found with any known selector")
        return []

    def _parse_job_card(self, card) -> dict[str, Any] | None:
        """Extract structured data from a single job card element."""
        try:
            title = self._extract_text(card, TITLE_SELECTORS) or ""
            company = self._extract_text(card, COMPANY_SELECTORS) or ""
            location = self._extract_text(card, LOCATION_SELECTORS) or ""

            # Try to get the job URL from an anchor tag
            url = ""
            linkedin_job_id = ""
            link_el = card.query_selector("a[href*='/jobs/view/']") or card.query_selector("a")
            if link_el:
                href = link_el.get_attribute("href") or ""
                if href.startswith("/"):
                    href = f"https://www.linkedin.com{href}"
                url = href.split("?")[0]  # strip query params

                # Extract job ID from URL like /jobs/view/1234567890/
                id_match = re.search(r"/jobs/view/(\d+)", url)
                if id_match:
                    linkedin_job_id = id_match.group(1)

            # Fallback: data attribute for job ID. The jobs list is a
            # VIRTUALIZED list — off-screen <li data-occludable-job-id=...>
            # shells render without inner anchors, so derive the URL from the
            # attribute instead of relying on an <a href> being present.
            if not linkedin_job_id:
                for attr in ("data-occludable-job-id", "data-job-id", "data-entity-urn"):
                    val = card.get_attribute(attr)
                    if val:
                        # URNs look like urn:li:jobPosting:1234567890
                        digits = re.search(r"(\d+)", val)
                        if digits:
                            linkedin_job_id = digits.group(1)
                            break

            # Posted time: LinkedIn renders e.g. "3 days ago", "2 weeks ago"
            posted_text = self._extract_text(card, POSTED_SELECTORS) or ""
            posted_text = posted_text.strip()

            # Card must have some identity: text content or a real job id
            if not title and not company and not linkedin_job_id:
                return None

            # Virtualized shells may have an id but no rendered anchor —
            # reconstruct the URL from the id.
            if linkedin_job_id and not url:
                url = f"https://www.linkedin.com/jobs/view/{linkedin_job_id}/"
            if not linkedin_job_id:
                linkedin_job_id = re.sub(r"\W", "", f"{title}_{company}")[:64]

            posted_dt = parse_posted_text(posted_text)
            return {
                "title": title.strip(),
                "company": company.strip(),
                "location": location.strip(),
                "url": url,
                "linkedin_job_id": linkedin_job_id,
                "posted_text": posted_text,
                "posted_at": posted_dt.isoformat() if posted_dt else "",
            }
        except Exception as exc:
            logger.debug("Failed to parse job card: %s", exc)
            return None

    def _is_within_age(self, posted_text: str, max_age_days: int) -> bool:
        """Return True if the posted-time text is within *max_age_days*."""
        if not posted_text:
            # No timestamp shown — LinkedIn's f_TPR filter already applied,
            # so trust the server-side filter and keep the job.
            return True
        posted_at = parse_posted_text(posted_text)
        if posted_at is None:
            return True
        cutoff = datetime.now() - timedelta(days=max_age_days)
        return posted_at >= cutoff

    def _extract_text(self, parent, selectors: list[str]) -> str | None:
        """Try each selector within *parent* and return inner text of the first match."""
        for sel in selectors:
            try:
                el = parent.query_selector(sel)
                if el:
                    text = el.inner_text()
                    if text and text.strip():
                        return text.strip()
            except Exception:
                continue
        return None

    def _load_more_results(self) -> bool:
        """Scroll the results panel to force-render more virtualized items."""
        try:
            # Scroll INSIDE the jobs results panel (the virtualized scroller),
            # not the window — window.scrollTo does nothing here.
            self._page.evaluate(
                """() => {
                    const scroller = document.querySelector('.jobs-search-results-list');
                    if (scroller) { scroller.scrollTop = scroller.scrollHeight; return; }
                    const ul = document.querySelector('ul.scaffold-layout__list-container');
                    if (ul) { ul.scrollTop = ul.scrollHeight; return; }
                    const last = document.querySelector('li[data-occludable-job-id]:last-of-type');
                    if (last) { last.scrollIntoView({block: 'end'}); }
                }"""
            )
            self._page.wait_for_timeout(2000)

            # Try clicking a "See more jobs" button if present
            try:
                see_more = self._page.query_selector(
                    "button.infinite-scroller__show-more-button, "
                    "button[aria-label='See more jobs'], "
                    "button.see-more-jobs"
                )
                if see_more and see_more.is_visible():
                    see_more.click()
                    self._page.wait_for_timeout(2000)
                    return True
            except Exception:
                pass

            # Check whether new job cards appeared after the scroll
            before = len(self._page.query_selector_all("li[data-occludable-job-id]"))
            self._page.wait_for_timeout(1500)
            after = len(self._page.query_selector_all("li[data-occludable-job-id]"))
            return after > before or before > 0  # assume scroll loaded more
        except Exception as exc:
            logger.debug("Load-more failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def filter_results(
        self,
        jobs: list[dict[str, Any]],
        exclusions: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Filter out excluded companies, off-profile titles, and duplicates.

        Args:
            jobs: Raw job results from :meth:`search_jobs`.
            exclusions: Override exclusion config. Falls back to
                        ``self.exclusions`` from the constructor config.

        Returns:
            Filtered (deduplicated) list of job dicts.
        """
        exclusions = exclusions or self.exclusions
        excluded_companies = {c.lower() for c in exclusions.get("companies", [])}
        excluded_keywords = {k.lower() for k in exclusions.get("keywords", [])}
        title_include_keywords = {
            k.lower()
            for k in self.search_config.get("title_include_keywords", [])
            if k
        }

        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        filtered: list[dict[str, Any]] = []

        for job in jobs:
            # Deduplicate
            jid = job.get("linkedin_job_id", "")
            url = job.get("url", "")
            if jid in seen_ids or (url and url in seen_urls):
                logger.debug("Skipping duplicate: %s", job.get("title"))
                continue

            # Company exclusion (case-insensitive substring match)
            company_lower = job.get("company", "").lower()
            if any(exc in company_lower for exc in excluded_companies):
                logger.info("Excluding '%s' at '%s' (excluded company)", job.get("title"), job.get("company"))
                continue

            # Keyword exclusion in title
            title_lower = job.get("title", "").lower()
            if excluded_keywords and any(kw in title_lower for kw in excluded_keywords):
                logger.info("Excluding '%s' (excluded keyword in title)", job.get("title"))
                continue

            # Search results are broad on LinkedIn.  When configured, only
            # retain titles that explicitly match a target role family.
            if title_include_keywords and not any(
                keyword in title_lower for keyword in title_include_keywords
            ):
                logger.info("Excluding '%s' (not a target role)", job.get("title"))
                continue

            seen_ids.add(jid)
            if url:
                seen_urls.add(url)
            filtered.append(job)

        logger.info("Filtered %d → %d jobs", len(jobs), len(filtered))
        return filtered

    # ------------------------------------------------------------------
    # Job detail extraction
    # ------------------------------------------------------------------

    def get_job_details(
        self,
        job_url: str,
        page: Page | None = None,
    ) -> dict[str, Any]:
        """Navigate to a job posting and extract the full description.

        Args:
            job_url: LinkedIn job posting URL.
            page: Optional Playwright Page to use (defaults to internal page).

        Returns:
            Dict with ``title``, ``company``, ``location``, ``description``,
            ``url``, ``linkedin_job_id``.
        """
        target_page = page or self._page
        if target_page is None:
            raise RuntimeError("Browser not connected. Call connect_browser() first.")

        logger.info("Fetching job details from %s", job_url)
        target_page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
        target_page.wait_for_timeout(2000)

        # Scroll down to trigger lazy-loading of description section
        for _ in range(4):
            target_page.evaluate("window.scrollBy(0, 600)")
            target_page.wait_for_timeout(400)
        target_page.wait_for_timeout(1000)

        # Try to click "Show more" / "See more" to expand the description
        try:
            show_more = target_page.query_selector(
                "button[aria-label='Show more'], "
                "button.jobs-description__footer-button, "
                "button[data-control-name='see_more']"
            )
            if show_more and show_more.is_visible():
                show_more.click()
                target_page.wait_for_timeout(1000)
        except Exception:
            pass

        # Extract title
        title = ""
        for sel in [
            ".job-details-jobs-unified-top-card__job-title h1",
            ".jobs-unified-top-card__job-title",
            ".t-24.t-bold",
            "h1",
        ]:
            el = target_page.query_selector(sel)
            if el:
                title = el.inner_text().strip()
                if title:
                    break

        # Extract company
        company = ""
        for sel in [
            ".job-details-jobs-unified-top-card__company-name a",
            ".jobs-unified-top-card__company-name a",
            ".jobs-unified-top-card__company-name",
        ]:
            el = target_page.query_selector(sel)
            if el:
                company = el.inner_text().strip()
                if company:
                    break

        # Fallback: the current LinkedIn job page has no <h1> and obfuscated
        # classes, but the <title> tag reliably reads "Role | Company | LinkedIn".
        if not title or not company:
            try:
                parts = [p.strip() for p in target_page.title().split("|")]
                if parts and parts[-1] == "LinkedIn" and len(parts) >= 2:
                    if not title:
                        title = parts[0]
                    if not company and len(parts) >= 3:
                        company = parts[1]
            except Exception:
                pass

        # Extract location
        location = ""
        for sel in [
            ".job-details-jobs-unified-top-card__bullet",
            ".jobs-unified-top-card__bullet",
            ".jobs-unified-top-card__workplace-type",
        ]:
            el = target_page.query_selector(sel)
            if el:
                location = el.inner_text().strip()
                if location:
                    break

        # Fallback: scan body text for a workplace-type line
        if not location:
            try:
                body_head = target_page.evaluate("() => document.body.innerText")[:2000]
                for line in body_head.split("\n"):
                    line_s = line.strip()
                    if any(w in line_s for w in ("Remote", "Hybrid", "On-site")) and len(line_s) < 80:
                        location = line_s.split("\u00b7")[0].strip()
                        break
            except Exception:
                pass

        # Extract description
        description = ""
        for sel in [
            ".jobs-description__content",
            ".jobs-description-content__text",
            "#job-details",
            ".jobs-box__html-content",
            "article",
        ]:
            el = target_page.query_selector(sel)
            if el:
                description = el.inner_text().strip()
                if description:
                    break

        # Fallback: extract "About the job" section from page body text
        if not description:
            try:
                body_text = target_page.evaluate("() => document.body.innerText")
                marker = "About the job"
                idx = body_text.find(marker)
                if idx != -1:
                    raw = body_text[idx + len(marker):].strip()
                    # Truncate at common footer markers (current + classic page)
                    for stop in [
                        "Show less",
                        "People you can reach out to",
                        "Similar jobs",
                        "More from this employer",
                        "Unlock hiring insights",
                        "About the company",
                        "Set alert for similar jobs",
                        "More jobs",
                    ]:
                        stop_idx = raw.find(stop)
                        if stop_idx != -1:
                            raw = raw[:stop_idx].strip()
                    if len(raw) > 100:
                        description = raw
                        logger.debug("Used body-text fallback for description (%d chars)", len(description))
            except Exception as exc:
                logger.debug("Body text fallback failed: %s", exc)

        # Extract job ID from URL
        linkedin_job_id = ""
        id_match = re.search(r"/jobs/view/(\d+)", job_url)
        if id_match:
            linkedin_job_id = id_match.group(1)

        # Extract posted time (top card usually shows e.g. "2 days ago")
        posted_text = ""
        for sel in POSTED_SELECTORS:
            try:
                el = target_page.query_selector(sel)
                if el:
                    posted_text = (el.inner_text() or "").strip()
                    if posted_text:
                        break
            except Exception:
                continue
        if not posted_text:
            try:
                body_head = target_page.evaluate("() => document.body.innerText")[:3000]
                match = re.search(r"\b\d+\s*(?:minutes?|hours?|days?|weeks?|months?)\s*ago\b", body_head)
                if match:
                    posted_text = match.group(0)
            except Exception:
                pass
        _posted_dt = parse_posted_text(posted_text)
        _posted_iso = _posted_dt.isoformat() if _posted_dt else ""

        return {
            "title": title,
            "company": company,
            "location": location,
            "description": description,
            "url": job_url,
            "linkedin_job_id": linkedin_job_id,
            "posted_text": posted_text,
            "posted_at": _posted_iso,
        }

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self):
        """Close browser and Playwright resources."""
        try:
            if self._context:
                self._context.close()
        except Exception as exc:
            logger.debug("Error closing context: %s", exc)
        try:
            if self._browser:
                self._browser.close()
        except Exception as exc:
            logger.debug("Error closing browser: %s", exc)
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception as exc:
            logger.debug("Error stopping playwright: %s", exc)
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None
        logger.info("Browser resources cleaned up")

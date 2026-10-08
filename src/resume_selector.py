"""CV variant selection: pick the right base resume for each job.

The candidate maintains three CV variants — Software Engineer, DevOps Engineer,
and Cloud Engineer. Selection is keyword-based (fast, deterministic, free), so
it runs for every job before any AI call. The LLM tailoring step then refines
the chosen variant's summary/skills for the specific job description.
"""

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"

# Default variants: (key, display name, template filename, keywords).
# Keywords are matched against job title + description, case-insensitive.
# Multi-word keywords use substring matching; single words use word boundaries.
DEFAULT_VARIANTS = [
    {
        "key": "devops",
        "name": "DevOps Engineer",
        "template": "devops_resume.docx",
        "keywords": [
            "devops", "sre", "site reliability", "platform engineer", "platform engineering",
            "infrastructure engineer", "build engineer", "release engineer",
            "ci/cd", "cicd", "kubernetes", "terraform", "ansible", "jenkins",
            "docker compose", "helm", "argocd", "github actions",
        ],
    },
    {
        "key": "cloud",
        "name": "Cloud Engineer",
        "template": "cloud_resume.docx",
        "keywords": [
            "cloud engineer", "cloud architect", "cloud infrastructure", "cloud operations",
            "cloud support", "aws engineer", "azure engineer", "gcp engineer",
            "serverless", "lambda", "cloudformation", "cloud native", "cloud-native",
            "infra as code", "iac",
        ],
    },
    {
        "key": "software_engineer",
        "name": "Software Engineer",
        "template": "software_engineer_resume.docx",
        "keywords": [
            "software engineer", "software developer", "backend developer",
            "backend engineer", "fullstack", "full-stack", "full stack",
            "frontend developer", "web developer", "node.js", "nodejs", "laravel",
            "react", "python developer", "application developer", "programmer",
        ],
    },
]


def _keyword_hit(keyword: str, text: str) -> bool:
    """Case-insensitive match: substring for phrases, word-boundary for words."""
    if " " in keyword or "/" in keyword or "-" in keyword or "." in keyword:
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


class ResumeSelector:
    """Selects the most relevant CV variant for a job posting."""

    def __init__(self, config: dict[str, Any] | None = None):
        """Initialize the selector.

        Args:
            config: Settings dict. Optional ``resume_variants`` list overrides
                    the built-in variant definitions.
        """
        self.config = config or {}
        self.variants = self.config.get("resume_variants") or DEFAULT_VARIANTS

    def list_variants(self) -> list[dict[str, Any]]:
        """Return all configured variants with resolved template paths."""
        result = []
        for variant in self.variants:
            entry = dict(variant)
            entry["path"] = str(TEMPLATES_DIR / variant.get("template", ""))
            result.append(entry)
        return result

    def _variant_path(self, variant: dict[str, Any]) -> Path:
        return TEMPLATES_DIR / variant.get("template", "")

    def score_job(self, job: dict[str, Any], variant: dict[str, Any]) -> int:
        """Score one variant against a job's title + description.

        Title hits are worth 3x — the posting's own title is the strongest signal.
        """
        title = (job.get("title") or "").lower()
        description = (job.get("description") or "").lower()
        score = 0
        for keyword in variant.get("keywords", []):
            kw = keyword.lower()
            if _keyword_hit(kw, description):
                score += 2
            if _keyword_hit(kw, title):
                score += 3
        return score

    def select(self, job: dict[str, Any]) -> dict[str, Any]:
        """Pick the best variant for a job.

        Returns:
            Dict with ``key``, ``name``, ``template`` (path), ``score``,
            ``all_scores``. Falls back to the software_engineer variant when
            nothing matches or scores tie.
        """
        scored: list[tuple[int, str, dict[str, Any]]] = [
            (self.score_job(job, variant), variant["key"], variant)
            for variant in self.variants
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))

        best_score, best_key, best_variant = scored[0]

        # Zero hits or a tie → prefer software_engineer as the generalist default
        tie_or_none = best_score == 0 or (
            len(scored) > 1 and scored[0][0] == scored[1][0]
        )
        if tie_or_none:
            generalist = next((v for _, k, v in scored if k == "software_engineer"), None)
            if generalist is not None:
                best_key, best_variant = "software_engineer", generalist

        result = {
            "key": best_key,
            "name": best_variant.get("name", best_key),
            "template": str(self._variant_path(best_variant)),
            "score": best_score,
            "all_scores": {k: s for s, k, _ in scored},
        }
        logger.info(
            "Selected CV variant '%s' for '%s' (scores: %s)",
            best_key, job.get("title", "?"), result["all_scores"],
        )
        return result

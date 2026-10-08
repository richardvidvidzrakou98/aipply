"""CV scoring and optimization via OpenRouter.

Given a job description and the selected base resume, the LLM produces:

- a 0-100 match score with a breakdown (skills, experience, title, education)
- strengths and gaps
- an optimized summary and competency list that can be written into the resume

All calls degrade gracefully: if no API key is configured or the call fails,
the module returns heuristic results so the dashboard keeps working.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

from src.llm import LLMClient
from src.utils import ensure_dir

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a senior technical recruiter and ATS expert. You evaluate resume-job "
    "fit honestly and return ONLY valid JSON. You never inflate scores."
)

PRIORITY_WEIGHTS = {"critical": 5, "required": 3, "preferred": 1}


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip().lower())


def _verified_evidence(evidence: list[str], resume_text: str) -> list[str]:
    """Keep only model-provided quotations actually present in the resume."""
    resume = _normalized(resume_text)
    return [item for item in evidence if _normalized(item) and _normalized(item) in resume]


def _clean_requirements(raw: Any, resume_text: str) -> list[dict[str, Any]]:
    """Validate AI criteria and make unsupported matches explicitly uncertain."""
    requirements = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        requirement = str(item.get("requirement") or "").strip()
        if not requirement:
            continue
        priority = str(item.get("priority") or "required").lower()
        priority = priority if priority in PRIORITY_WEIGHTS else "required"
        status = str(item.get("status") or "missing").lower()
        status = status if status in {"matched", "missing", "uncertain"} else "uncertain"
        evidence = _verified_evidence(
            [str(value) for value in (item.get("evidence") or [])], resume_text
        )
        if status == "matched" and not evidence:
            status = "uncertain"
        requirements.append({
            "requirement": requirement[:240],
            "priority": priority,
            "category": str(item.get("category") or "General")[:80],
            "status": status,
            "evidence": evidence[:3],
        })
    return requirements[:20]


def _apply_user_criteria(requirements: list[dict[str, Any]], criteria: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Make explicit user edits authoritative while retaining AI evidence checks."""
    if not criteria:
        return requirements
    generated = {_normalized(item["requirement"]): item for item in requirements}
    selected = []
    for item in criteria[:20]:
        requirement = str(item.get("requirement") or "").strip()
        if not requirement:
            continue
        priority = str(item.get("priority") or "required").lower()
        priority = priority if priority in PRIORITY_WEIGHTS else "required"
        generated_item = generated.get(_normalized(requirement), {})
        selected.append({
            "requirement": requirement[:240], "priority": priority,
            "category": str(item.get("category") or generated_item.get("category") or "User edited")[:80],
            "status": generated_item.get("status", "uncertain"),
            "evidence": generated_item.get("evidence", []),
        })
    return selected


def _score_requirements(requirements: list[dict[str, Any]]) -> dict[str, Any]:
    """Score verified requirements; the LLM never supplies the final score."""
    if not requirements:
        return {"score": 0, "breakdown": {}, "strengths": [], "gaps": [], "recommendation": "No requirement analysis was produced."}
    total = sum(PRIORITY_WEIGHTS[item["priority"]] for item in requirements)
    earned = sum(
        PRIORITY_WEIGHTS[item["priority"]] * {"matched": 1, "uncertain": 0.35, "missing": 0}[item["status"]]
        for item in requirements
    )
    categories: dict[str, list[dict[str, Any]]] = {}
    for item in requirements:
        categories.setdefault(item["category"], []).append(item)
    breakdown = {
        category: round(100 * sum(PRIORITY_WEIGHTS[item["priority"]] * {"matched": 1, "uncertain": .35, "missing": 0}[item["status"]] for item in items) / sum(PRIORITY_WEIGHTS[item["priority"]] for item in items))
        for category, items in categories.items()
    }
    missing = [item["requirement"] for item in requirements if item["status"] == "missing"]
    uncertain = [item["requirement"] for item in requirements if item["status"] == "uncertain"]
    matched = [item["requirement"] for item in requirements if item["status"] == "matched"]
    critical = [item["requirement"] for item in requirements if item["priority"] == "critical" and item["status"] != "matched"]
    recommendation = "Apply as a strong match." if not critical else "Not a strong match until you can truthfully demonstrate: " + ", ".join(critical[:3]) + "."
    return {
        "score": round(100 * earned / total), "breakdown": breakdown,
        "strengths": matched[:6], "gaps": (missing + [f"Uncertain evidence: {item}" for item in uncertain])[:10],
        "critical_missing": critical, "recommendation": recommendation,
    }


def _heuristic_score(job_description: str, resume_text: str) -> dict[str, Any]:
    """Keyword-overlap fallback when no LLM is available."""
    jd_words = set(re.findall(r"[a-z][a-z+#./-]{2,}", job_description.lower()))
    resume_words = set(re.findall(r"[a-z][a-z+#./-]{2,}", resume_text.lower()))
    stopwords = {
        "the", "and", "for", "with", "you", "our", "will", "are", "have", "this",
        "that", "from", "your", "job", "role", "team", "work", "who", "was", "has",
        "their", "they", "them", "his", "her", "its", "all", "any", "can", "not",
        "but", "out", "per", "via", "use", "using", "used", "including", "etc",
    }
    jd_words -= stopwords
    resume_words -= stopwords
    if not jd_words:
        return {"score": 0, "breakdown": {}, "strengths": [], "gaps": [], "optimized_summary": "", "optimized_competencies": [], "notes": "no job description"}
    overlap = jd_words & resume_words
    score = round(100 * len(overlap) / max(len(jd_words), 1))
    return {
        "score": min(score, 95),
        "breakdown": {},
        "strengths": sorted(overlap)[:8],
        "gaps": sorted(jd_words - resume_words)[:8],
        "optimized_summary": "",
        "optimized_competencies": [],
        "notes": "heuristic keyword overlap (no LLM key configured)",
    }


def _heuristic_optimization(job_description: str, resume_text: str) -> dict[str, Any]:
    """Create a conservative, offline optimization proposal.

    The fallback never invents experience.  It preserves the existing summary
    and competency wording and only brings lines containing job keywords to
    the front.  This keeps the CV popup useful when an OpenRouter free model
    is unavailable or returns an empty completion.
    """
    lines = [line.strip() for line in resume_text.splitlines() if line.strip()]
    section = ""
    summary_lines: list[str] = []
    competency_lines: list[str] = []
    for line in lines:
        normalized = line.lower().rstrip(":")
        if "summary" in normalized or normalized in {"profile", "objective"}:
            section = "summary"
            continue
        if "competenc" in normalized or normalized in {"skills", "technical skills"}:
            section = "competencies"
            continue
        # Common resume section headers terminate the selected section.
        if len(line) < 48 and normalized in {
            "experience", "professional experience", "education", "certifications",
            "projects", "work experience", "employment history",
        }:
            section = ""
            continue
        if section == "summary":
            summary_lines.append(line)
        elif section == "competencies":
            competency_lines.append(line)

    jd_terms = set(re.findall(r"[a-z][a-z+#./-]{2,}", job_description.lower()))
    # Stable sorting preserves the source order for equally relevant lines.
    competencies = sorted(
        competency_lines,
        key=lambda line: -len(set(re.findall(r"[a-z][a-z+#./-]{2,}", line.lower())) & jd_terms),
    )
    return {
        "summary": " ".join(summary_lines),
        "competencies": competencies,
        "changes": [
            "AI provider was unavailable, so original resume wording was preserved.",
            "Existing competency lines were ordered by overlap with the job description.",
        ],
        "model": "heuristic_fallback",
        "fallback": True,
    }
class CVOptimizer:
    """Scores and optimizes a resume against a specific job description."""

    def __init__(self, config: dict[str, Any] | None = None):
        """Initialize with settings dict (uses the ``llm`` section)."""
        self.config = config or {}
        self.llm = LLMClient(config=self.config)

    def read_resume(self, path: str | Path) -> str:
        """Read resume text from a .docx file (paragraph per line)."""
        from docx import Document

        resume_path = Path(path)
        if not resume_path.exists():
            raise FileNotFoundError(f"Resume not found: {resume_path}")
        doc = Document(str(resume_path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    def score_resume(
        self,
        resume_path: str | Path,
        job_description: str,
        role: str = "",
        company: str = "",
        criteria: list[dict[str, Any]] | None = None,
        instruction: str = "",
    ) -> dict[str, Any]:
        """Score a resume against a job description.

        Returns:
            Dict with ``score`` (0-100), ``breakdown`` (sub-scores),
            ``strengths``, ``gaps``, ``recommendation``, ``model``.
        """
        resume_text = self.read_resume(resume_path)
        if not self.llm.available():
            result = _heuristic_score(job_description, resume_text)
            result["recommendation"] = ""
            result["model"] = "heuristic"
            result.update({"requirements": [], "analysis_available": False, "fallback": True, "ai_error": self.llm.last_error})
            return result

        criteria_context = json.dumps(criteria, ensure_ascii=False) if criteria else "[]"
        user_prompt = f"""Analyze this resume against the job description. Do not assign an overall score.

JOB: {role} at {company}
JOB DESCRIPTION:
{job_description[:4000]}

RESUME:
{resume_text[:5000]}

USER-EDITED CRITERIA (authoritative when present):
{criteria_context}

REVISION INSTRUCTION:
{instruction[:1000] or "None"}

Return ONLY valid JSON:
{{
  "requirements": [
    {{"requirement": "specific JD requirement", "priority": "critical|required|preferred", "category": "Skills|Experience|Education|Domain|Other", "status": "matched|missing|uncertain", "evidence": ["exact quote copied from the resume"]}}
  ]
}}

Extract 5-15 role-specific requirements. Respect user-edited criteria when supplied.
Only mark matched when the evidence quote appears verbatim in the resume. Do not
infer skills from adjacent experience, and do not fabricate qualifications."""

        parsed = self.llm.chat_json(SYSTEM_PROMPT, user_prompt, temperature=0.2)
        if not parsed or "requirements" not in parsed:
            result = _heuristic_score(job_description, resume_text)
            result["recommendation"] = ""
            result["model"] = "heuristic_fallback"
            result.update({"requirements": [], "analysis_available": False, "fallback": True, "ai_error": self.llm.last_error})
            return result

        requirements = _apply_user_criteria(
            _clean_requirements(parsed.get("requirements"), resume_text), criteria
        )
        result = _score_requirements(requirements)
        result.update({"requirements": requirements, "analysis_available": True, "fallback": False, "model": self.llm.model})
        return result

    def optimize_resume(
        self,
        resume_path: str | Path,
        job_description: str,
        role: str = "",
        company: str = "",
        analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Produce an optimized summary + competencies for this job.

        The optimization only rewrites the Professional Summary and Core
        Competencies sections; all other content (employers, dates, titles)
        stays untouched, so nothing can be fabricated.

        Returns:
            Dict with ``summary``, ``competencies``, ``changes`` (human-readable
            list), ``model``. Empty summary/competencies when the LLM is
            unavailable.
        """
        resume_text = self.read_resume(resume_path)
        if not self.llm.available():
            return _heuristic_optimization(job_description, resume_text)

        user_prompt = f"""Optimize this resume for the job below.

JOB: {role} at {company}
JOB DESCRIPTION:
{job_description[:4000]}

RESUME:
{resume_text[:5000]}

VERIFIED REQUIREMENT ANALYSIS:
{json.dumps(analysis or {}, ensure_ascii=False)[:5000]}

Produce:
1. "summary": a rewritten Professional Summary (3-4 sentences) emphasizing the
   experience most relevant to this job.
2. "competencies": 5-8 job-relevant competency lines, most relevant first,
   drawn ONLY from the base resume. Keep the original grouping style: each may
   be a category with a short skill list, e.g.
   "Cloud & DevOps: AWS (EC2, S3, Lambda), Docker, Kubernetes".
3. "changes": 3-5 short strings describing what you changed and why.

RULES:
- NEVER fabricate experience, employers, dates, or certifications. Only use
  what is in the resume.
- Emphasize only requirements marked matched with verified evidence. Never
  imply that missing or uncertain requirements are present.
- Keep real job titles, companies, and dates unchanged.
- Do not use em-dashes. Use commas instead.
- Avoid buzzwords like "leverage", "utilize", "synergy".

Return ONLY valid JSON:
{{"summary": "...", "competencies": ["...", "..."], "changes": ["...", "..."]}}"""

        parsed = self.llm.chat_json(SYSTEM_PROMPT, user_prompt, temperature=0.4)
        if not parsed:
            fallback = _heuristic_optimization(job_description, resume_text)
            fallback["ai_error"] = self.llm.last_error
            return fallback

        return {
            "summary": str(parsed.get("summary") or ""),
            "competencies": [str(c) for c in (parsed.get("competencies") or [])][:10],
            "changes": [str(c) for c in (parsed.get("changes") or [])][:6],
            "model": self.llm.model,
            "fallback": False,
        }

    def review(self, resume_path: str | Path, job_description: str, role: str = "", company: str = "") -> dict:
        """Alias combining score + optimization notes for the dashboard review view."""
        score_result = self.score_resume(resume_path, job_description, role=role, company=company)
        return score_result

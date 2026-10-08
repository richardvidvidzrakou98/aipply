"""Resume tailoring module.

Uses OpenRouter (OpenAI-compatible) to tailor the selected base resume for each
job description, then produces a tailored .docx that preserves the base
resume's formatting (only the Professional Summary and Core Competencies
sections are replaced).

If no OpenRouter API key is configured, tailoring degrades gracefully: the base
resume is cloned unchanged so the pipeline can still produce materials.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from docx import Document

from src.llm import LLMClient, extract_json
from src.utils import ensure_dir, sanitize_filename

logger = logging.getLogger(__name__)


class ResumeTailor:
    """AI-powered resume tailoring using OpenRouter."""

    def __init__(self, config: dict[str, Any] | None = None):
        """Initialize tailor with configuration.

        Args:
            config: Settings dict containing an ``llm`` (or legacy ``openai``)
                    section with ``model`` and ``temperature`` keys.
        """
        self.config = config or {}
        self.llm = LLMClient(config=self.config)
        self.model = self.llm.model
        self.temperature = self.llm.temperature

    def _get_client(self):
        """Return the shared LLM client (None when no API key is set)."""
        if not self.llm.available():
            logger.warning(
                "OPENROUTER_API_KEY not set — resume tailoring will use the "
                "unmodified base resume."
            )
            return None
        return self.llm

    @staticmethod
    def read_base_resume(path: str | Path) -> str:
        """Read all text from a .docx resume.

        Args:
            path: Path to the resume .docx file.

        Returns:
            Resume text with one paragraph per line.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        resume_path = Path(path)
        if not resume_path.exists():
            raise FileNotFoundError(f"Base resume not found: {resume_path}")
        doc = Document(str(resume_path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    def tailor_resume(
        self,
        base_resume_path: str | Path,
        job_description: str,
        company: str = "",
        role: str = "",
    ) -> str:
        """Tailor a base resume for a job using OpenAI.

        Args:
            base_resume_path: Path to the base resume .docx.
            job_description: Full job description text.
            company: Company name (for the prompt).
            role: Job title (for the prompt).

        Returns:
            Raw model output — expected to be JSON with ``summary`` and
            ``competencies`` keys. Empty string if no API key is configured
            or the API call fails.
        """
        resume_text = self.read_base_resume(base_resume_path)

        client = self._get_client()
        if client is None:
            return ""

        candidate_name = (self.config.get("candidate_name") or "").strip()
        prompt = f"""You are tailoring a resume for a specific job application.

BASE RESUME:
{resume_text[:4000]}

JOB: {role} at {company}
JOB DESCRIPTION:
{job_description[:3000]}

TASK: Produce:
1. "summary": a rewritten Professional Summary (one paragraph, 3-4 sentences)
   that emphasizes the experience most relevant to this job.
2. "competencies": the 10 most job-relevant competencies/skills, reordered
   with the most relevant first, drawn ONLY from the base resume.

RULES:
- NEVER fabricate experience, employers, dates, or certifications. Only use
  what is in the base resume.
- Keep the candidate's real job titles, companies, and dates unchanged.
- Do not use em-dashes. Use commas instead.
- Avoid buzzwords like "leverage", "utilize", "synergy".
"""
        if candidate_name:
            prompt += f"- The candidate's name is {candidate_name}.\n"

        prompt += """
Return ONLY valid JSON, no markdown fences:
{"summary": "...", "competencies": ["...", "..."]}

Produce 5-8 competencies. Keep the resume's original grouping style: each
competency may be a category with a short skill list after it, e.g.
"Cloud & DevOps: AWS (EC2, S3, Lambda), Docker, Kubernetes".
"""

        try:
            raw = self.llm.chat(
                (
                    "You are a professional resume writer. You follow "
                    "instructions exactly and return only valid JSON. "
                    "You never fabricate experience."
                ),
                prompt,
            )
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
            return cleaned.strip()
        except Exception as exc:
            logger.warning("Resume tailoring API call failed: %s", exc)
            return ""

    @staticmethod
    def save_tailored_resume(content: str, output_path: str | Path) -> Path:
        """Save tailored resume text as a formatted .docx.

        Used as the fallback path when structured tailoring is unavailable.

        Args:
            content: Resume text (plain text, newlines separate paragraphs).
            output_path: Destination .docx path.

        Returns:
            The Path the resume was saved to.
        """
        output_path = Path(output_path)
        ensure_dir(output_path.parent)

        doc = Document()
        style = doc.styles["Normal"]
        style.font.name = "Calibri"
        style.font.size = None  # keep default size

        headers = {
            "summary",
            "professional summary",
            "experience",
            "professional experience",
            "skills",
            "skills & competencies",
            "core competencies",
            "education",
            "certifications",
            "projects",
            "project experience",
        }

        for line in content.split("\n"):
            text = line.strip()
            if not text:
                doc.add_paragraph("")
                continue
            if text.lower().rstrip(":") in headers:
                p = doc.add_paragraph()
                run = p.add_run(text.upper())
                run.bold = True
            else:
                doc.add_paragraph(text)

        doc.save(str(output_path))
        logger.info("Saved tailored resume to %s", output_path)
        return output_path

    @staticmethod
    def clone_and_tailor(
        base_path: str | Path,
        output_path: str | Path,
        new_summary: str | None = None,
        new_competencies: list[str] | None = None,
    ) -> Path:
        """Clone .docx and replace summary and/or competencies in place.

        Preserves ALL original formatting (fonts, sizes, bold, bullets,
        spacing). Only the text content of targeted paragraphs is changed.

        Args:
            base_path: Path to the base resume .docx.
            output_path: Destination .docx path.
            new_summary: Replacement Professional Summary text.
            new_competencies: Replacement competency lines (most relevant first).

        Returns:
            The Path the tailored resume was saved to.
        """
        output_path = Path(output_path)
        ensure_dir(output_path.parent)

        # Byte-for-byte copy first
        shutil.copy2(str(base_path), str(output_path))

        doc = Document(str(output_path))
        current_section = None
        comp_idx = 0

        for p in doc.paragraphs:
            text = p.text.strip()
            if not text:
                continue

            # Detect section headers
            lowered = text.lower()
            if "summary" in lowered and len(text) < 40:
                current_section = "summary"
                continue
            elif "competenc" in lowered and len(text) < 40:
                current_section = "competencies"
                comp_idx = 0
                continue
            elif any(
                h in lowered
                for h in ["experience", "education", "certification", "project"]
            ) and len(text) < 40:
                current_section = None
                continue

            # Replace summary
            if current_section == "summary" and new_summary:
                _replace_paragraph_text(p, new_summary)
                current_section = None

            # Replace competencies (bullet lines, in order)
            elif (
                current_section == "competencies"
                and new_competencies
                and (text[:1] in "•●‣▪·-" or p.style.name.startswith("List"))
            ):
                if comp_idx < len(new_competencies):
                    bullet = f"{text[:1]} " if text[:1] in "•●‣▪·" else ""
                    _replace_paragraph_text(p, f"{bullet}{new_competencies[comp_idx]}")
                    comp_idx += 1

        doc.save(str(output_path))
        logger.info("Saved tailored resume to %s", output_path)
        return output_path

    def tailor_and_save(
        self,
        base_resume_path: str | Path,
        job_description: str,
        company: str,
        role: str,
        output_dir: str | Path,
    ) -> Path:
        """Tailor a resume via OpenAI and save the resulting .docx.

        Attempts structured tailoring (summary + competencies replacement into
        a clone of the base resume, preserving formatting). Falls back to a
        plain-text render, then to an unmodified clone of the base resume.

        Args:
            base_resume_path: Path to the base resume .docx.
            job_description: Full job description text.
            company: Company name.
            role: Job title.
            output_dir: Directory for the tailored resume.

        Returns:
            Path to the tailored resume .docx.
        """
        output_dir = ensure_dir(output_dir)
        name = (
            self.config.get("candidate_name")
            or _guess_name_from_resume(base_resume_path)
            or "Candidate"
        )
        short_role = _short_role(role)
        filename = sanitize_filename(f"{name}_Resume_{short_role}") + ".docx"
        output_path = output_dir / filename

        tailored = self.tailor_resume(
            base_resume_path, job_description, company=company, role=role
        )

        if tailored:
            try:
                parsed = json.loads(tailored)
                summary = parsed.get("summary", "")
                competencies = parsed.get("competencies", [])
                if summary or competencies:
                    return self.clone_and_tailor(
                        base_resume_path, output_path,
                        new_summary=summary or None,
                        new_competencies=competencies or None,
                    )
            except (json.JSONDecodeError, AttributeError):
                logger.warning(
                    "Tailored resume output was not valid JSON; using plain-text render."
                )
            return self.save_tailored_resume(tailored, output_path)

        # No API key or API failure: clone base resume unchanged
        logger.info("Using unmodified base resume (no tailoring available).")
        return self.clone_and_tailor(base_resume_path, output_path)


def _guess_name_from_resume(base_path: str | Path) -> str:
    """Guess the candidate's name from the first line of the resume."""
    try:
        first_line = ResumeTailor.read_base_resume(base_path).split("\n", 1)[0]
        words = first_line.strip()
        # Sanity check: a name is short and has no punctuation
        if 0 < len(words) <= 50 and not any(c.isdigit() for c in words):
            return words
    except Exception:
        pass
    return ""


def _short_role(role: str) -> str:
    """Shorten a role title for use in a filename."""
    for sep in [",", "|", "—", "-", "/"]:
        if sep in role:
            role = role.split(sep)[0]
    return role.strip()[:30]


def _replace_paragraph_text(paragraph, new_text: str):
    """Replace paragraph text while preserving formatting of the first run."""
    if not paragraph.runs:
        paragraph.text = new_text
        return
    first_run = paragraph.runs[0]
    for run in paragraph.runs[1:]:
        run.text = ""
    first_run.text = new_text

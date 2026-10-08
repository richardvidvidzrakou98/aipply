"""Cover letter generation module.

Uses OpenRouter (OpenAI-compatible) to generate a targeted cover letter for
each job, and saves it as a professionally formatted .docx. If no OpenRouter
API key is configured, a clean template letter is produced instead so the
pipeline still works.
"""

import logging
from pathlib import Path
from typing import Any

from docx import Document
from docx.shared import Pt, Inches

from src.llm import LLMClient
from src.utils import ensure_dir, sanitize_filename

logger = logging.getLogger(__name__)


class CoverLetterGenerator:
    """Generates a cover letter via OpenRouter and saves it as .docx."""

    def __init__(self, config: dict[str, Any] | None = None):
        """Initialize generator with configuration.

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
                "OPENROUTER_API_KEY not set — a template cover letter will be "
                "generated instead of an AI-tailored one."
            )
            return None
        return self.llm

    @staticmethod
    def read_example(path: str | Path = "templates/base_cover_letter.docx") -> str:
        """Read the example cover letter for tone/style reference.

        Args:
            path: Path to the example cover letter .docx.

        Returns:
            Letter text with one paragraph per line, or "" if missing.
        """
        letter_path = Path(path)
        if not letter_path.exists():
            return ""
        try:
            doc = Document(str(letter_path))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as exc:
            logger.debug("Could not read example cover letter: %s", exc)
            return ""

    def generate(
        self,
        job_description: str,
        candidate_profile: dict,
        company: str = "",
        role: str = "",
        example_letter_path: str | Path | None = None,
    ) -> str:
        """Generate cover letter text via OpenAI.

        Args:
            job_description: Full job description text.
            candidate_profile: Candidate dict (name, email, summary, strengths, target_roles).
            company: Company name.
            role: Job title.
            example_letter_path: Optional path to an example letter whose tone
                                 the generated letter should match.

        Returns:
            Cover letter text. Falls back to a template letter when no API key
            is configured or the API call fails.
        """
        client = self._get_client()
        if client is None:
            return self._template_letter(candidate_profile, company, role)

        name = candidate_profile.get("name", "")
        summary = candidate_profile.get("summary", "")
        strengths = ", ".join(candidate_profile.get("strengths", []))
        target_roles = ", ".join(candidate_profile.get("target_roles", []))

        example_text = ""
        if example_letter_path:
            example_text = self.read_example(example_letter_path)

        prompt = f"""Write a job application cover letter.

CANDIDATE:
Name: {name}
Profile summary: {summary}
Key strengths: {strengths}
Target roles: {target_roles}

JOB: {role} at {company}
JOB DESCRIPTION:
{job_description[:3000]}
"""
        if example_text:
            prompt += f"""
EXAMPLE COVER LETTER (match this tone, structure, and length):
{example_text[:2000]}
"""
        prompt += """
RULES:
- 3 to 4 short paragraphs. Professional, direct, no fluff.
- Reference only real experience from the candidate profile above. NEVER
  fabricate employers, dates, or achievements.
- Do not use em-dashes. Use commas instead.
- Avoid clichés like "I'm excited to", "I'm thrilled to", "leverage", "utilize".
- Do not include the candidate's address or contact block; the signature is
  added separately.

Return ONLY the letter text (no subject line, no markdown).
"""

        try:
            letter = self.llm.chat(
                (
                    "You are a professional cover letter writer. You write "
                    "concise, specific letters and never fabricate experience."
                ),
                prompt,
            )
            if letter:
                return letter
            logger.warning("Empty cover letter from API; using template letter.")
        except Exception as exc:
            logger.warning("Cover letter API call failed: %s", exc)

        return self._template_letter(candidate_profile, company, role)

    def _template_letter(
        self, candidate_profile: dict, company: str = "", role: str = ""
    ) -> str:
        """Produce a clean template letter when AI generation is unavailable."""
        name = candidate_profile.get("name", "")
        strengths = candidate_profile.get("strengths", [])
        summary = candidate_profile.get("summary", "")

        strengths_line = ""
        if strengths:
            strengths_line = (
                "My background includes " + ", ".join(strengths[:3]).lower() + "."
            )

        return (
            f"Dear Hiring Team,\n\n"
            f"I am writing to apply for the {role or 'advertised'} position"
            f"{' at ' + company if company else ''}. {summary} {strengths_line}\n\n"
            f"My experience aligns closely with the requirements outlined in the "
            f"job description, and I would welcome the opportunity to discuss how "
            f"my background can contribute to your team.\n\n"
            f"Thank you for your time and consideration.\n\n"
            f"Best regards,\n{name}"
        ).strip()

    @staticmethod
    def save_cover_letter(text: str, output_path: str | Path) -> Path:
        """Save cover letter text as a professionally formatted .docx.

        Args:
            text: Cover letter text (newlines separate paragraphs).
            output_path: Destination .docx path.

        Returns:
            The Path the cover letter was saved to.
        """
        output_path = Path(output_path)
        ensure_dir(output_path.parent)

        doc = Document()

        style = doc.styles["Normal"]
        style.font.name = "Calibri"
        style.font.size = Pt(11)

        for section in doc.sections:
            section.top_margin = Inches(1)
            section.bottom_margin = Inches(1)
            section.left_margin = Inches(1)
            section.right_margin = Inches(1)

        for para_text in [p.strip() for p in text.split("\n") if p.strip()]:
            p = doc.add_paragraph(para_text)
            p.space_after = Pt(6)

        doc.save(str(output_path))
        logger.info("Saved cover letter to %s", output_path)
        return output_path

    def generate_and_save(
        self,
        job_description: str,
        candidate_profile: dict,
        company: str,
        role: str,
        output_dir: str | Path | None = None,
        example_letter_path: str | Path | None = None,
    ) -> Path:
        """Generate a cover letter and save it as a .docx.

        Args:
            job_description: Full job description text.
            candidate_profile: Candidate dict from config/profile.yaml.
            company: Company name.
            role: Job title.
            output_dir: Directory for the cover letter. Defaults to
                        ``output/applications/<company>_<role>/``.
            example_letter_path: Optional example letter for tone matching.

        Returns:
            Path to the saved cover letter .docx.
        """
        if output_dir is None:
            output_dir = (
                Path("output")
                / "applications"
                / sanitize_filename(f"{company}_{role}")
            )
        output_dir = ensure_dir(output_dir)
        name = candidate_profile.get("name") or "Candidate"
        short_role = _short_role(role)
        filename = sanitize_filename(f"{name}_Cover_Letter_{short_role}") + ".docx"
        output_path = output_dir / filename

        letter_text = self.generate(
            job_description,
            candidate_profile,
            company=company,
            role=role,
            example_letter_path=example_letter_path,
        )
        return self.save_cover_letter(letter_text, output_path)


def _short_role(role: str) -> str:
    """Shorten a role title for use in a filename."""
    for sep in [",", "|", "—", "-", "/"]:
        if sep in role:
            role = role.split(sep)[0]
    return role.strip()[:30]

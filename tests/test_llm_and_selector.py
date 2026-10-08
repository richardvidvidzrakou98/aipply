"""Tests for the OpenRouter LLM client, CV selector, and CV optimizer."""
import os
from unittest.mock import patch, MagicMock

import pytest
from docx import Document

from src.llm import LLMClient, extract_json
from src.resume_selector import ResumeSelector


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_json_in_fences(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_in_prose(self):
        assert extract_json('Here is the result: {"a": 1} hope it helps') == {"a": 1}

    def test_invalid(self):
        assert extract_json("no json here") is None


class TestLLMClientConfig:
    def test_defaults(self):
        client = LLMClient()
        assert client.model
        assert client.temperature == 0.7

    def test_llm_section(self):
        client = LLMClient({"llm": {"model": "custom/model", "temperature": 0.3}})
        assert client.model == "custom/model"
        assert client.temperature == 0.3

    def test_legacy_openai_section(self):
        client = LLMClient({"openai": {"model": "legacy/model"}})
        assert client.model == "legacy/model"

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False)
    def test_available_with_key(self):
        client = LLMClient()
        assert client.available() is True

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "OPENAI_API_KEY": ""}, clear=False)
    def test_unavailable_without_key(self):
        client = LLMClient()
        assert client.available() is False

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False)
    def test_chat_returns_content(self):
        client = LLMClient()
        mock_openai = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "hello world"
        mock_openai.chat.completions.create.return_value = mock_resp
        client._client = mock_openai
        assert client.chat("sys", "user") == "hello world"

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False)
    def test_chat_falls_back_on_error(self):
        client = LLMClient({"llm": {"model": "m1", "fallback_models": ["m2"]}})
        mock_openai = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "from m2"
        mock_openai.chat.completions.create.side_effect = [
            Exception("rate limited"), mock_resp,
        ]
        client._client = mock_openai
        assert client.chat("sys", "user") == "from m2"

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False)
    def test_chat_retries_empty_content_before_fallback(self):
        client = LLMClient({"llm": {"model": "m1", "fallback_models": [], "empty_retries": 1}})
        mock_openai = MagicMock()
        empty = MagicMock()
        empty.choices = [MagicMock()]
        empty.choices[0].message.content = ""
        empty.choices[0].finish_reason = "length"
        answer = MagicMock()
        answer.choices = [MagicMock()]
        answer.choices[0].message.content = "recovered"
        mock_openai.chat.completions.create.side_effect = [empty, answer]
        client._client = mock_openai

        assert client.chat("sys", "user") == "recovered"
        assert mock_openai.chat.completions.create.call_count == 2

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False)
    def test_chat_json_parses(self):
        client = LLMClient()
        mock_openai = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = '{"score": 42}'
        mock_openai.chat.completions.create.return_value = mock_resp
        client._client = mock_openai
        assert client.chat_json("sys", "user") == {"score": 42}


# ---------------------------------------------------------------------------
# ResumeSelector
# ---------------------------------------------------------------------------

@pytest.fixture
def selector():
    return ResumeSelector()


class TestResumeSelector:
    def test_devops_job_selects_devops(self, selector):
        job = {
            "title": "DevOps Engineer",
            "description": "Manage Kubernetes clusters, Terraform IaC, CI/CD pipelines with Jenkins.",
        }
        assert selector.select(job)["key"] == "devops"

    def test_cloud_job_selects_cloud(self, selector):
        job = {
            "title": "Cloud Engineer",
            "description": "AWS serverless architecture with Lambda and CloudFormation.",
        }
        assert selector.select(job)["key"] == "cloud"

    def test_backend_job_selects_software_engineer(self, selector):
        job = {
            "title": "Backend Developer",
            "description": "Build REST APIs with Node.js, Laravel and PostgreSQL.",
        }
        assert selector.select(job)["key"] == "software_engineer"

    def test_no_match_defaults_to_software_engineer(self, selector):
        job = {"title": "Generic Role", "description": "Misc duties as assigned."}
        assert selector.select(job)["key"] == "software_engineer"

    def test_title_match_beats_description_match(self, selector):
        job = {
            "title": "Site Reliability Engineer",
            "description": "The word kubernetes appears but so does react and node.js web development.",
        }
        assert selector.select(job)["key"] == "devops"

    def test_select_returns_template_path(self, selector):
        job = {"title": "DevOps Engineer", "description": "kubernetes terraform"}
        result = selector.select(job)
        assert result["template"].endswith("devops_resume.docx")
        assert result["score"] > 0
        assert set(result["all_scores"].keys()) == {"devops", "cloud", "software_engineer"}

    def test_list_variants(self, selector):
        variants = selector.list_variants()
        keys = {v["key"] for v in variants}
        assert keys == {"devops", "cloud", "software_engineer"}

    def test_score_job_title_weighted_higher(self, selector):
        devops = selector.variants[0]
        title_job = {"title": "DevOps Engineer", "description": ""}
        desc_job = {"title": "", "description": "devops engineer role"}
        assert selector.score_job(title_job, devops) > selector.score_job(desc_job, devops)

"""Tests for dashboard status synchronization endpoints."""

from src.tracker import ApplicationTracker
from scripts import dashboard


def _seed_tracker(path, status="materials_ready"):
    tracker = ApplicationTracker(str(path))
    tracker.add_application(
        company="Acme",
        position="Platform Engineer",
        job_url="https://www.linkedin.com/jobs/view/123",
        status=status,
        resume_path="/tmp/resume.docx",
    )


def test_manual_application_can_be_marked_applied(tmp_path, monkeypatch):
    tracker_path = tmp_path / "tracker.json"
    _seed_tracker(tracker_path)
    monkeypatch.setattr(dashboard, "TRACKER", tracker_path)

    client = dashboard.app.test_client()
    response = client.post("/api/mark-applied", json={"id": 1})

    assert response.status_code == 200
    assert response.get_json()["status"] == "applied"
    saved = ApplicationTracker(str(tracker_path)).applications[0]
    assert saved["status"] == "applied"
    assert "manually marked applied from dashboard" in saved["notes"]


def test_application_endpoint_returns_persisted_status(tmp_path, monkeypatch):
    tracker_path = tmp_path / "tracker.json"
    _seed_tracker(tracker_path, status="applying")
    monkeypatch.setattr(dashboard, "TRACKER", tracker_path)

    response = dashboard.app.test_client().get("/api/application/1")

    assert response.status_code == 200
    assert response.get_json()["status"] == "applying"


def test_cv_review_revisions_are_versioned(tmp_path, monkeypatch):
    tracker_path = tmp_path / "tracker.json"
    resume_path = tmp_path / "resume.docx"
    resume_path.write_bytes(b"placeholder")
    tracker = ApplicationTracker(str(tracker_path))
    tracker.add_application(
        company="Acme", position="Engineer", job_url="u", resume_path=str(resume_path),
        job_description="Build reliable systems.",
    )
    monkeypatch.setattr(dashboard, "TRACKER", tracker_path)

    def fake_score(self, *args, **kwargs):
        requirement = (kwargs.get("criteria") or [{"requirement": "Systems design", "priority": "required"}])[0]
        return {
            "score": 50, "breakdown": {"Skills": 50}, "strengths": [], "gaps": [requirement["requirement"]],
            "recommendation": "Needs evidence.", "requirements": [{**requirement, "category": "Skills", "status": "missing", "evidence": []}],
            "analysis_available": True, "fallback": False, "model": "test-model",
        }

    monkeypatch.setattr("src.cv_optimizer.CVOptimizer.score_resume", fake_score)
    client = dashboard.app.test_client()
    first = client.post("/api/cv/review", json={"id": 1})
    second = client.post("/api/cv/review/revise", json={"id": 1, "requirements": [{"requirement": "Observability", "priority": "preferred"}], "instruction": "Focus on operations."})

    assert first.status_code == 200
    assert first.get_json()["version"] == 1
    assert second.status_code == 200
    assert second.get_json()["version"] == 2
    assert len(ApplicationTracker(str(tracker_path)).applications[0]["cv_review_history"]) == 2

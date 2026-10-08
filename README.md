# Aipply

Aipply is a local, AI-assisted LinkedIn job-search workspace. It finds jobs, prepares role-specific application materials, submits LinkedIn Easy Apply forms when enabled, and keeps a local dashboard and application history.

Your LinkedIn session, CVs, contact details, job descriptions, and generated documents stay in your local project directory or browser profile. Review every generated document and application before relying on it.

## What it does

- Searches LinkedIn using your keywords, locations, freshness rules, and exclusions.
- Selects the most relevant DOCX CV variant, then creates a tailored resume and cover letter.
- Submits LinkedIn **Easy Apply** applications when configured; external career-site jobs are recorded as **Manual Needed** and are never auto-submitted.
- Provides a local dashboard for application status, documents, screenshots, CV scoring, optimization, and manual status updates.
- Uses OpenRouter for optional AI features. Without an AI key, scanning and tracking continue, but document and CV-analysis features use limited local fallbacks.

## Requirements

- Python 3.10 or newer
- Google Chrome or Chromium
- A LinkedIn account that you can log into manually
- An OpenRouter API key for AI tailoring, cover letters, and requirement-level CV analysis

Use automation responsibly and ensure your use complies with LinkedIn's terms, the job site's rules, and your local laws. Complete CAPTCHA, 2FA, and any required authorization yourself.

## Install

```bash
git clone https://github.com/richardvidvidzrakou98/aipply.git
cd aipply

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Windows PowerShell activation:

```powershell
.venv\Scripts\Activate.ps1
```

## Configure your workspace

### 1. Create private configuration files

```bash
cp config/settings.example.yaml config/settings.yaml
cp config/profile.example.yaml config/profile.yaml
```

`config/settings.yaml` controls job searches and application behavior. At minimum, set `search.keywords`, `search.locations`, exclusions, and the application limit.

`config/profile.yaml` contains the candidate information used for application questions and document generation. Do not commit it.

### 2. Add an OpenRouter key (recommended)

Create a `.env` file in the project root:

```bash
OPENROUTER_API_KEY=sk-or-v1-your-key
```

The current settings use `openrouter/free` with fallback models. Free providers can be intermittent; the dashboard labels any local fallback so it is not mistaken for AI analysis. To use a specific OpenRouter model, configure the `llm` section in `config/settings.yaml`:

```yaml
llm:
  model: "provider/model-name"
  fallback_models: []
  temperature: 0.4
  max_tokens: 2000
```

Never commit `.env`, `config/profile.yaml`, CVs, or generated application files.

### 3. Add your CV templates

Place DOCX templates in `templates/`. The built-in selector expects:

```text
templates/
├── devops_resume.docx
├── cloud_resume.docx
└── software_engineer_resume.docx
```

You may define your own CV variants with `resume_variants` in `settings.yaml`. Each entry needs a display name, template filename, and matching keywords. A `templates/base_cover_letter.docx` file is optional and is used as a tone/style reference.

### 4. Log into LinkedIn once

```bash
source .venv/bin/activate
python scripts/login.py
```

Complete login, CAPTCHA, and 2FA in the browser window. The persistent browser profile is saved under `~/.aipply/chrome-profile` and reused by later runs. Run the command again if your LinkedIn session expires.

## Start using Aipply

### Dashboard-first workflow

Start the dashboard:

```bash
source .venv/bin/activate
python scripts/dashboard.py
```

Open <http://127.0.0.1:8092>.

The dashboard lets you:

- Start a scan-and-apply cycle.
- Open job links and generated resume, cover-letter, and JD files.
- Apply a prepared Easy Apply job, then see its status update when submission completes.
- Use **Mark applied** after applying yourself on LinkedIn or an external site.
- Use **CV** to score, review, revise, optimize, and update a role-specific CV.
- Stop the current cycle immediately and resume later.

### Command-line workflow

Generate application materials for a small search without submitting:

```bash
python scripts/quick_run.py "devops engineer" "Remote" --limit 5 --dry-run
```

Run the full configured search:

```bash
python scripts/run_cycle.py
```

Useful options:

```bash
python scripts/run_cycle.py --dry-run        # scan and prepare, do not submit
python scripts/run_cycle.py --limit 5        # cap one cycle
python scripts/run_cycle.py --report-only    # regenerate a report from tracker data
python scripts/run_cycle.py --verbose        # detailed logs
```

## CV Studio: how analysis works

The dashboard’s **CV** panel performs a role-specific review:

1. The AI extracts requirements from the actual job description and classifies them as critical, required, or preferred.
2. It provides exact CV quotations for any claimed match.
3. Aipply verifies those quotations against the saved CV. Unsupported matches become **uncertain**.
4. The final score is calculated locally from the verified requirement matrix, not accepted directly from the model.

Use **Full review** to save an analysis version. In the matrix, edit the requirement wording or priority and add a natural-language instruction (for example, “treat Kubernetes as preferred”), then choose **Recalculate with edits**. Each revision is saved alongside earlier versions in `output/tracker.json`.

If the provider returns no usable response, the UI shows a clearly labeled keyword-overlap fallback. It is not a requirement-level AI review.

## Application states

| Status               | Meaning                                                                  |
| -------------------- | ------------------------------------------------------------------------ |
| Materials Ready      | Resume and supporting materials exist; ready for Easy Apply.             |
| Applying             | The dashboard is submitting an Easy Apply form.                          |
| Applied              | Submitted by the automation or manually confirmed with **Mark applied**. |
| Manual Needed        | External site, CAPTCHA, or another step requires you to act.             |
| Apply Failed         | Submission could not finish; inspect notes and screenshots.              |
| Interview / Rejected | Follow-up states that can be recorded in the tracker.                    |

## Output and data

```text
output/
├── applications/      # Per-job resumes, cover letters, JD text, screenshots
├── reports/           # Static HTML reports
└── tracker.json       # Dashboard source of truth and CV review history
```

The dashboard reads `tracker.json` on each request. Do not edit it while a cycle is running. Back it up if you want to preserve your application history.

## Safety controls

- Click **Stop** in the dashboard, or create `.stop` in the project root, to halt work before the next application.
- Remove `.stop` or use **Resume** to allow a future cycle.
- Keep `application.max_applications_per_cycle` conservative while testing.
- Begin with `--dry-run` and inspect generated documents before enabling submissions.

## Troubleshooting

### Dashboard will not start

Activate the project environment and confirm dependencies are installed:

```bash
source .venv/bin/activate
pip install -r requirements.txt
python scripts/dashboard.py
```

Pass a port as the first argument if `8092` is occupied:

```bash
python scripts/dashboard.py 8093
```

### LinkedIn login or scanning fails

Run `python scripts/login.py` again, complete any LinkedIn prompt manually, and retry. LinkedIn page changes, CAPTCHAs, and expired sessions can prevent browser automation from continuing.

### CV Studio shows a fallback notice

Check that `.env` contains a valid `OPENROUTER_API_KEY`, restart the dashboard, and retry. Free models may return empty or unavailable responses; the popup includes a provider detail when all retry and fallback attempts fail.

### A job was already applied to manually

Open the job’s dashboard row and select **Mark applied**. LinkedIn does not send the local dashboard a manual-application event.

## Tests

```bash
source .venv/bin/activate
pytest -q
```

## License

MIT — see [LICENSE](LICENSE) when present.

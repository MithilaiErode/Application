# Mithilai Kinetic Migration Assessor

A standalone web app that assesses Epicor **Classic** customisations and converts them for
**Kinetic**. It is one central Mithilai tool that serves every client: nothing is installed in any
client's Epicor.

**Input:** Classic customisation exports (XML, a zip of exports, or `.cs` scripts), plus a
client profile and requirements. Optionally, 2–3 Kinetic sample exports so generated files match
the client's format.

**Output, for each customisation:**

| File | Contents |
|---|---|
| `ANALYSIS.md` | What the customisation does, where each part goes in Kinetic, bucket, confidence, estimate, risks and client questions |
| `bpm/*.cs`, `function/*.cs` | Complete, paste-ready BPM and Epicor Function code |
| `layer/*.json` or `layer/LAYER_STEPS.md` | An Application Studio layer file when Kinetic samples were uploaded; otherwise click-by-click steps |
| `design/*` | Design document and code skeleton for rebuild items |
| `TEST_STEPS.md` | Import steps and test cases for the TEST environment |
| `original_script.cs` | The extracted Classic code, for side-by-side review |

**Output, for the whole job:** `inventory.xlsx` (inventory, plus a summary with hours and cost
formulas) and `client_report.docx`. Everything downloads as one zip.

> Everything generated is a **draft for consultant review**. Import into a TEST environment only.
> Promote to production through Solution Workbench after testing.

## How it works

```
Upload → parser.py (finds each customisation and its C# script, decoding embedded XML/base64/gzip)
       → analyzer.py (Claude, with the Mithilai rules + client profile + Kinetic samples, returning structured JSON)
       → outputs.py (per-customisation folders, inventory.xlsx, client_report.docx, zip)
```

- **The "Skill":** [`app/rules/mithilai_rules.md`](app/rules/mithilai_rules.md) holds the
  Mithilai conversion rules: decision rules, coding standards, estimation bands and confidence
  guidance. Edit this file to change how the tool decides. Every client uses the same rules.
- **Per client:** client profile, requirements and Kinetic samples are entered with each job.
- **Cost:** the rules and client profile are prompt-cached, so every customisation after the
  first in a job reuses them at a lower price.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env          # then fill in ANTHROPIC_API_KEY and APP_PASSWORD
.venv\Scripts\python run_local.py
```

Open http://127.0.0.1:8010. Test with the files in `samples/classic/`. These are **synthetic**
examples; replace them with real exports.

Run the offline tests (the Claude call is stubbed, so they're free):

```bash
.venv\Scripts\python -m pytest -q
```

## Deploy on Render

1. Push this folder to a GitHub repository (e.g. under `Mithilai-Solutions-Pty-Ltd`).
2. In Render, choose **New → Blueprint** and select the repository. `render.yaml` sets up the service.
3. Set the secret environment variables: `ANTHROPIC_API_KEY` and `APP_PASSWORD`.
4. Open the service URL and sign in with `APP_USERNAME` / `APP_PASSWORD`.

Use the **Starter** plan or higher. The free plan sleeps when idle and has limited memory, which
can interrupt long analyses.

## Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Claude API key (required) |
| `APP_USERNAME` / `APP_PASSWORD` | – | Login for the app. If `APP_PASSWORD` is empty the app is open to anyone with the link |
| `CLAUDE_MODEL` | `claude-opus-5` | Model used for analysis |
| `CLAUDE_EFFORT` | `high` | `low` / `medium` / `high` / `xhigh` / `max` |
| `CLAUDE_MAX_TOKENS` | `48000` | Output limit per customisation |
| `MAX_PARALLEL` | `3` | Customisations analysed at the same time |
| `MAX_UPLOAD_MB` | `25` | Upload limit per job |
| `MAX_ITEMS_PER_JOB` | `300` | Customisation limit per job |
| `JOB_TTL_HOURS` | `72` | How long results are kept |

## Stage 1 limitations

- **Jobs:** kept in memory and on the service's temporary disk. A restart or redeploy clears
  them, so download the zip to keep results. Stage 2 adds a database (e.g. Supabase).
- **Login:** a single shared login (HTTP Basic). Stage 2 adds per-user accounts and per-client access.
- **Export formats:** the parser has been tested on synthetic samples only. Validate it against
  real exports from your Epicor versions first.
- **Epicor connection:** none. Consultants import the generated files into TEST by hand
  (Level 1). Direct import through the Epicor REST API is a later stage.

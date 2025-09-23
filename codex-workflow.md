Awesome—let’s wire up a clean **Codex ↔ Codex** workflow that uses GitHub as the single source of truth and automation backbone. The goal: you draft/approve in **Codex Web**, generate/execute in **Codex CLI**, and ship via **GitHub PRs** with guardrails, tests, and deploy hooks.

Here’s a streamlined, repeatable playbook.

# 1) Source of truth (repo contract)

* **Main branches**

  * `main` (protected): always deployable.
  * `dev` (protected): integration branch for approved work.
* **Working branches**

  * `feat/<slug>`, `fix/<slug>`, `chore/<slug>`, `refactor/<slug>`.
* **Conventional Commits** for clear change logs (`feat:`, `fix:`…).
* **Repo files Codex relies on**

  * `instructions.md` (you already have it): definitive spec the Codex Web agent reads to plan.
  * `ops/litellm/config.yaml`, `ops/mcp/servers.yaml`, `src/.../schemas.py`, `storage/models.sql`: contracts.
  * `/.github/ISSUE_TEMPLATE/*.yml` and `/.github/PULL_REQUEST_TEMPLATE.md`: prompts/forms for Codex to use.
  * `/docs/adr/` (Architecture Decision Records): short “why” notes when choices change.

# 2) Roles for the two Codex surfaces

* **Codex Web (planner/editor)**

  * Reads `instructions.md` and issues.
  * Writes/edits: prompts, YAML configs, SQL schemas, README/docs.
  * Opens/curates PRs; reviews diffs; discusses tradeoffs.
* **Codex CLI (executor/generator)**

  * Generates scaffolds, stubs, boilerplate across tree.
  * Applies multi-file refactors, code moves, and rote changes.
  * Runs scripted tasks (migrations, ETL dry-runs, test suites) locally before pushing.

# 3) Tight feedback loop using GitHub Issues + PRs

## 3.1 Issue types (templates)

Create three issue templates so either Codex can start work with the right context:

* **Design/Plan** (`design_plan.yml`)

  * Fields: Objective, Background, Constraints, Acceptance criteria, Affected modules.
  * Label auto-apply: `codex:plan`.
* **Generate/Implement** (`generate_impl.yml`)

  * Fields: Spec (files/dirs to create/edit), Interfaces (schemas/contracts), Test plan, Done definition.
  * Label auto-apply: `codex:generate`.
* **Refactor/Maintain** (`refactor_maint.yml`)

  * Fields: Scope, Safety checks, Expected no-op behavior, Benchmark (if any).
  * Label: `codex:refactor`.

Codex Web can open *Design/Plan* issues; Codex CLI can pick up *Generate/Implement* issues.

## 3.2 PR template (checklist Codex must complete)

`/.github/PULL_REQUEST_TEMPLATE.md`:

* [ ] Linked issue(s)
* [ ] Changes summarized (bullet list)
* [ ] Contracts touched (schemas, prompts, YAML)
* [ ] Tests added/updated and passing locally
* [ ] Backwards compatibility (yes/no; notes)
* [ ] Security review (keys/configs unaffected)
* [ ] Manual steps (if any)

# 4) Branch & sync commands (minimal muscle memory)

```bash
# Codex CLI – start work
git checkout -b feat/planner-json-contract
codex apply --spec instructions.md  # or your CLI command for generation

# run locally
make migrate
make ingest
pytest -q

# commit + push
git add -A
git commit -m "feat(planner): emit strict JSON Plan + pydantic validation"
git push -u origin feat/planner-json-contract

# open PR (CLI or GitHub UI)
gh pr create --fill
```

Codex Web then reviews, requests edits, or merges.

# 5) Labels that drive automation

Use labels to branch behavior in CI:

* `codex:plan` → run **docs/prompt validation** only.
* `codex:generate` → run **full test matrix**, linters, type checks.
* `codex:infra` → validate YAML/JSON schemas (`ops/litellm/config.yaml`, `ops/mcp/servers.yaml`).
* `safe-to-merge` → auto-merge if CI green.
* `needs-human-approval` → holds until you approve.

# 6) GitHub Actions (guardrails + speed)

Create a few focused workflows in `/.github/workflows`:

## 6.1 `ci.yml` (push/PR)

* **Matrix**: Python 3.11/3.12.
* Steps:

  * `pip install -r requirements.txt`
  * `ruff` (lint) + `mypy` (types)
  * `pytest -q` (unit tests)
  * Validate **YAML/JSON**: `ops/litellm/config.yaml`, `ops/mcp/servers.yaml` against a small JSONSchema you keep in `ops/schemas/`.
  * Smoke-validate SQL (`sqlfluff lint` optional).
  * If label `codex:plan` only, skip tests to speed review.

## 6.2 `build-docker.yml` (on tag/release)

* Build images for `api`, `etl`, optional `mcp-sidecars`.
* Push to GHCR.

## 6.3 `quality-gates.yml`

* Enforce **protected branch**: require green CI + at least one non-author approval (you or Codex Web).

## 6.4 `secrets-scan.yml`

* Use GitHub Advanced Security secret scanning or `gitleaks` to catch key leaks.

# 7) Pre-commit & local quality

Add `.pre-commit-config.yaml`:

* `ruff`, `isort`, `black` (or just `ruff format`), `detect-secrets`, `yamllint`, `sqlfluff`.
  Then:

```bash
pre-commit install
```

Codex CLI runs pre-commit before committing to avoid noisy CI fails.

# 8) Config & prompt versioning (so Codex doesn’t drift)

* Treat **prompts** as code: `src/.../planner/prompts/*.md`, `response/prompts/*.md`.
* Add **prompt unit tests** (golden files): tiny tests that feed the system prompts and expect JSON structure compliance.
* Version configs:

  * `ops/litellm/config.yaml` → add `x-version: "2025-09-23"` and a comment block of changes.
  * `ops/mcp/servers.yaml` → same.
* Maintain **ADR notes** in `/docs/adr/NNN-title.md` (one paragraph each: context, decision, consequences).

# 9) Environment & secrets

* **Local**: `.env` + `.env.example` (no secrets in git).
* **GitHub**: environments `dev` and `prod` with secrets:

  * `OPENAI_KEY_*`, `ANTHROPIC_KEY`, `GOOGLE_API_KEY`, `COHERE_KEY`, `DATABASE_URL`.
* GitHub Actions uses **environment protection rules**: merges to `main` require manual approval.

# 10) Codex ↔ Codex “handoff” pattern

**Plan → Implement → Review → Merge** loop:

1. **Codex Web**

   * Opens a **Design/Plan** issue with acceptance criteria.
   * Creates/updates `instructions.md` and prompts.
   * Labels: `codex:plan`.

2. **Codex CLI**

   * Creates branch; runs generators/scaffolds; updates code & tests.
   * Opens **Generate/Implement** PR.
   * Labels: `codex:generate`.

3. **Codex Web**

   * Reviews PR: architectural consistency, prompt quality, config diffs.
   * If bigger changes needed: pushes commits to the same branch (Web is great for YAML/prompt diffs).

4. **You** (human)

   * Final approval if label `needs-human-approval`.
   * Merge to `dev` (or `main` when ready).
   * Tag release if desired (triggers docker build).

5. **Bot hygiene**

   * A small **merge bot** can auto-label `safe-to-merge` when CI green + approvals met.

# 11) Repo automation niceties

* **CODEOWNERS**:

  * `/ops/litellm/**` → you + Codex Web
  * `/src/**` → you + Codex CLI
* **Templates to speed new features**:

  * `scripts/new-module.sh` creates `src/...` stubs + test file + import wiring.
* **Backlog board**: GitHub Projects with columns: *Plan*, *Implement*, *Review*, *Blocked*, *Done*. Labels map issues to columns automatically.

# 12) Special handling for our stack (what’s unique)

* **Schemas as contracts**: `schemas.py` changes should **fail CI** unless tests prove backward compatibility or migrations are in the PR.
* **YAML schemas** for configs:

  * Keep a small `ops/schemas/litellm.schema.json` and `mcp_servers.schema.json`. Validate in CI.
* **Migrations**: `ops/scripts/migrate.py` must run in CI (dry-run or against ephemeral Postgres) to ensure SQL stays valid.
* **Prompt lint**: optional, but you can add a simple script that checks JSON-schema snippets referenced in prompts still validate.

# 13) Example GitHub Action (CI)

```yaml
name: CI
on:
  pull_request:
    branches: [dev, main]
  push:
    branches: [dev]
jobs:
  build-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r requirements.txt
      - run: pip install ruff mypy sqlfluff jsonschema pre-commit
      - run: pre-commit run --all-files
      - name: Validate configs
        run: |
          python - << 'PY'
          import json, yaml, jsonschema, sys
          from jsonschema import validate
          with open('ops/litellm/config.yaml') as f: cfg=yaml.safe_load(f)
          with open('ops/schemas/litellm.schema.json') as f: sch=json.load(f)
          validate(cfg, sch)
          with open('ops/mcp/servers.yaml') as f: cfg=yaml.safe_load(f)
          with open('ops/schemas/mcp_servers.schema.json') as f: sch=json.load(f)
          validate(cfg, sch)
          PY
      - name: Run tests
        run: pytest -q
```

# 14) Day-to-day flow (cheat sheet)

* **Start a feature**: open *Design/Plan* issue (Codex Web), then `feat/` branch (Codex CLI).
* **Generate & run**: Codex CLI scaffolds → `make migrate && pytest`.
* **Open PR**: auto-assign reviewers (CODEOWNERS), CI runs, labels guide gates.
* **Review**: Codex Web edits YAML/prompts/docs; you approve; merge.
* **Release**: tag for image build; optional deploy step hooks.

---

If you want, I can spit out the **issue/PR templates**, **CODEOWNERS**, **pre-commit config**, and the **CI YAML** files tailored to this repo so you can drop them in and have the workflow live in one pass.

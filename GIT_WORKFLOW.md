# Git Workflow

## Branching
- **Main Branch**: `main` (protected; represents stable state)
- **Feature Branches**: Created from `main` for each feature or task; naming: `feature/<short-description>`
- **Release Branches**: Not used in Phase 0; may be introduced later for release candidates.

## Commit Messages
- **Format**: `<type>: <short-description>`
  - `feat`: New feature
  - `fix`: Bug fix
  - `docs`: Documentation changes
  - `style`: Formatting, missing semicolons, etc.
  - `refactor`: Code refactoring
  - `test`: Adding or modifying tests
  - `chore`: Chore tasks (build, tooling, etc.)
- **Examples**:
  - `feat: add research plan data model`
  - `fix: handle null tool response`
  - `docs: update architecture diagram`
  - `chore: initialize pre-commit hooks`

## Pull Requests
- **Required**: All changes to `main` must come via pull request.
- **Review**: At least one approving review required.
- **Checks**: All CI checks must pass (to be set up).
- **Squash**: Squash and merge is preferred to keep history clean.

## Pre-commit Hooks
- Local hooks remain optional. The authoritative validation commands are pytest, Ruff, and strict MyPy.
- GitHub Actions runs all three commands on pushes and pull requests to `main`.

## Ignored Files
- See `.gitignore` for intentionally untracked files.
- Never commit: `.env`, virtual environments, cache directories, secrets.

## Tagging
- Use semantic versioning for releases (e.g., `v0.1.0`).
- Tags created from `main` after release validation.

## Remote
- `origin` is the GitHub repository documented in `pyproject.toml`.
- Release completion requires local `HEAD` and `origin/main` to match with a clean working tree.

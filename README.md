# r2t: Repo to Try

**Found a cool repo? Try it locally.**

r2t inspects an unfamiliar repository, explains what it needs, writes a Docker launch bundle you can read before anything runs, and checks that an HTTP endpoint answers. Every detection cites the file (and key or line) that supports it. When something is ambiguous, r2t says so and names the exact flag that resolves it instead of guessing.

No accounts, no backend, no LLM calls, no telemetry.

## Example

Excerpt from a real run on Linux with Docker 29.9.2 (2026-11-03), using the `vite-app` fixture in this repo. Paths are shortened and the build log is omitted.

```console
$ r2t inspect vite-app
r2t inspect vite-app
Status: supported
Source: …/vite-app
Snapshot digest: sha256:e1555b10e596c1c21094e52a2ef6d7fde89b3dd9baaab2d98176e2e94c59ba05
Evidence paths are relative to the app root.

Stack            Node.js / vite (package.json#scripts.dev, package.json#devDependencies.vite)
Runtime image    node:22-bookworm-slim  constraints: .nvmrc: 22 (.nvmrc)
Package manager  npm (package-lock.json)
Lock status      package-lock.json, locked (package-lock.json)
Install          npm ci
Start            npm run dev -- --host 0.0.0.0 --port 5173 --strictPort (package.json#scripts.dev)
Container port   5173 (readiness path /) (r2t default)

$ r2t plan vite-app -o vite-bundle
$ r2t run vite-bundle --yes --host-port 15173
r2t will run:
  R2T_HOST_PORT=15173 docker compose -p r2t-vite-app-55946518 -f …/vite-bundle/compose.yaml build
  R2T_HOST_PORT=15173 docker compose -p r2t-vite-app-55946518 -f …/vite-bundle/compose.yaml up -d
Then check: http://127.0.0.1:15173/

The build installs dependencies from the network, and starting the container runs the repository's code.
Docker is an isolation boundary, not a guarantee that arbitrary code is safe.

Build: ok
Container: started
Readiness: an HTTP endpoint responded with status 200 at http://127.0.0.1:15173/
This only shows that something answered; r2t does not check that the app works.
```

## Install

Not on PyPI. Install from source (Python 3.11+, Linux or macOS; Docker with Compose v2.24+ is needed for `run`):

```bash
git clone https://github.com/Lameda12/r2t && cd r2t && pip install -e .
```

## Workflow

```bash
# 1. Look first. Reads files only; nothing from the repo is executed.
r2t inspect https://github.com/OWNER/REPO

# 2. Write a bundle you can review.
r2t plan https://github.com/OWNER/REPO --output ./try-repo

# 3. Review ./try-repo/REPORT.md, Dockerfile and compose.yaml.
#    If the app needs environment variables, create the real file yourself:
cp ./try-repo/.env.template ./try-repo/.env   # then fill in values

# 4. Build, start, and check that an HTTP endpoint answers.
r2t run ./try-repo

# Later
r2t logs ./try-repo --follow
r2t stop ./try-repo
```

`inspect` and `plan` accept `--subdir PATH`, `--ref REF` (remote only), `--package-manager {npm,pnpm}`, `--entry MODULE:ATTR` (FastAPI), `--runtime-version VERSION`, and `--json`. `run` accepts `--yes`, `--timeout SECONDS` (default 180) and `--host-port N`.

When a target is `needs_input`, the output tells you which flag to add. For example, a monorepo root lists `--subdir apps/web`, two lockfiles ask for `--package-manager`, two FastAPI apps list each `--entry`.

The bundle:

```
try-repo/
  source/          filtered snapshot of the app (no .git, node_modules, .env, keys, .npmrc ...)
  Dockerfile
  .dockerignore    only source/ and the Dockerfile may enter the image
  compose.yaml     127.0.0.1-only port, no mounts, cap_drop ALL, no-new-privileges, non-root
  plan.json        machine-readable plan: evidence, commands, hashes, source identity
  REPORT.md        decisions, assumptions, blockers, sources of variation
  .env.template    variable names found in the repo's example env files, values empty
```

## What is supported

| Target | Detected from | Launched as |
|---|---|---|
| Vite (npm or pnpm) | `scripts.dev` is a simple `vite` invocation | `npm run dev -- --host 0.0.0.0 --port P --strictPort` or the pnpm equivalent |
| Next.js (npm or pnpm) | `scripts.dev` is a simple `next dev` invocation | `... run dev -H 0.0.0.0 -p P` |
| FastAPI | `fastapi` and `uvicorn` declared, a module-level `app = FastAPI(...)` found by parsing (never importing) | `uvicorn MODULE:ATTR --host 0.0.0.0 --port 8000` |

Limitations, stated plainly:

- Dev servers only. Vite/Next run in development mode; this is a way to look at a repo, not to deploy it.
- Not supported (reported as `unsupported`, with the reason): Yarn, Bun, workspaces and monorepo members, Poetry, uv, Pipenv, `dev` scripts that chain commands or use `concurrently`/`cross-env`, other frameworks, and apps that need a database or other services you have not provided.
- r2t lists services and variables an app may need (for example `DATABASE_URL`, Prisma, Redis) but does not start them.
- Existing Dockerfiles, Compose files and devcontainers are listed as evidence and otherwise ignored.
- Private packages that need a registry token will fail to install: `.npmrc`, `.pypirc` and `.netrc` are deliberately excluded from the snapshot.
- r2t never calls output reproducible. `REPORT.md` lists what can vary between builds (base image tags, unpinned package manager versions, unlocked dependencies, network fetches at build time, dev-server mode) and what is pinned.
- Verified in this repo's integration tests on Linux only. macOS is expected to work with Docker Desktop but has not been tested. Windows hosts are out of scope.

## Security model

- `inspect` and `plan` never execute target code: no npm, pnpm, pip, node or python is run against the repo, no module is imported, no script or README command is run. They read files, parse ASTs and run hardened Git commands.
- Git is run with hooks disabled, `fsmonitor` off, the `file` protocol blocked, no submodules, no prompts, and LFS smudging skipped. Local repositories are read with read-only commands.
- Inputs are validated: only `https://github.com/OWNER/REPO` URLs, refs that cannot be mistaken for options, and subdirectories that stay inside the repository.
- The snapshot never follows symlinks. Links that are absolute, escape the root or dangle are not copied and are listed in the report. `.git`, `node_modules`, virtualenvs, caches, build output, `.env` and `.env.*` (except `.env.example`-style files), private keys, cloud credentials and registry configs are excluded.
- Environment variable names are read from example files. Values are never printed or written into the report, `plan.json` or `.env.template`.
- Containers run unprivileged as a non-root user, with all capabilities dropped and `no-new-privileges`, publish only on `127.0.0.1`, and mount nothing. `run` re-validates the resolved Compose config before every run, so an edited bundle that is privileged, mounts the Docker socket or a host path, uses host namespaces, or publishes on a non-loopback address is refused (exit 4).
- Your `.env` is outside the Docker build context, so it is passed to the container at run time and never enters the image.

**Docker is an isolation boundary, not a guarantee that arbitrary code is safe.** Building runs dependency installation (including install scripts), and starting the container runs the repository's code. r2t asks for confirmation before doing either. Read `REPORT.md` and only try repositories you are willing to run.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Unexpected internal error (`R2T_DEBUG=1` prints a traceback) |
| 2 | Usage or input error: bad URL, ref, path, existing output directory, missing confirmation without a TTY, busy port, declined prompt |
| 3 | `plan`: target is `needs_input` or `unsupported` (the report is still written) |
| 4 | Docker or Compose unavailable or too old, or the bundle is invalid or unsafe |
| 5 | Image build failed |
| 6 | Container exited, or readiness timed out |
| 130 | Interrupted |

`inspect` exits 0 whenever inspection completes; the status is in the output. For `run`, any HTTP response (including 404 or 5xx) counts as "an endpoint responded" and exits 0; 5xx is called out in the output.

## Troubleshooting

- **"Docker daemon is not reachable"** (exit 4): start Docker Desktop, or `sudo systemctl start docker` on Linux.
- **"Docker Compose ... too old"** (exit 4): bundles use `env_file` with `required: false`, which needs Compose 2.24.0 or newer.
- **Port already in use** (exit 2): `r2t run DIR --host-port 8080`.
- **Readiness timed out** (exit 6): Next.js compiles on the first request, so slow machines may need `--timeout 400`. The last log lines are printed; the container is left running.
- **Look at logs**: `r2t logs DIR --follow`.
- **Stop it**: `r2t stop DIR` (removes this bundle's containers and network only; never volumes or images).
- **Remove the image**: `docker image rm r2t/<project>:latest` (the exact command is printed by `r2t stop`).
- **Build behind a corporate proxy that re-signs TLS**: the base images will not trust your proxy's CA. Edit the generated Dockerfile (it is yours to change) to add your CA; r2t warns that the bundle was modified and continues.
- **Docker Hub rate limits** (HTTP 429 on image pulls): `docker login` or wait and retry.

## Contributing

```bash
pip install -e ".[dev]"
ruff check
pytest                          # unit tests: no network, no Docker
R2T_DOCKER_TESTS=1 pytest -m docker   # real builds for the Vite, Next.js and FastAPI fixtures
scripts/demo.sh                 # inspect/plan every fixture, then run and stop one
```

Layout: `cli.py` (Typer and Rich only), `source.py` (acquisition and validation), `snapshot.py`, `detect/` (Node, Python, shared helpers), `analyze.py`, `render.py`, `docker.py`, `readiness.py`. Every subprocess call goes through one injectable `Runner`; the HTTP probe, clock and sleep are injectable too, so tests need no monkeypatching. Design decisions and the facts checked while building are in [docs/DECISIONS.md](docs/DECISIONS.md).

MIT licensed. See [LICENSE](LICENSE).

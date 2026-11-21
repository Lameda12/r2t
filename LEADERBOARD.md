# Fresh local checkout to first HTTP response

How long does a public Vite starter take to serve HTTP, starting from a fresh local checkout? Measured with `r2t bench` on one Docker runner, three rounds, five apps. Every number below was observed in the runs listed here; nothing is estimated.

**What the timer covers.** Fetching the repository from GitHub is *outside* the timer. Each app was shallow-cloned once beforehand and pinned to a commit. For each trial `r2t bench` then made a new local checkout of that commit, and the clock ran from just before that checkout until r2t's existing HTTP readiness check passed. That includes planning, bundle validation, the image build (`docker compose build --no-cache`, so the dependency install is inside the timer), container start and the wait. It excludes the review prompt and cleanup. The readiness check is unchanged: it probes `/` and accepts any HTTP status without following redirects, so the status column matters.

## Ranking

Rule: an app is ranked only if it returned the expected successful response (HTTP 200 for its documented route) in **all three** trials. Ranked apps are ordered by median elapsed time.

| Rank | App | Commit | Elapsed per trial (HTTP status) | Median | Range |
|---|---|---|---|---|---|
| 1 | [dan5py/react-vite-shadcn-ui](https://github.com/dan5py/react-vite-shadcn-ui) | `6be1e78987b5cc63706e40fd1a38d02bf6491ae3` | 16.0s (200), 16.1s (200), 15.8s (200) | **16.0s** | 15.8s to 16.1s |

Only one of the five apps qualified. The other four are listed in full below.

## Not ranked

| App | Commit | Elapsed per trial (HTTP status) | Median | Range | Observed failure |
|---|---|---|---|---|---|
| [justboil/admin-one-vue-tailwind](https://github.com/justboil/admin-one-vue-tailwind) | `8a1c411b62c2fd01b7bd81feed7f4e36c46047e7` | 15.2s (302), 12.6s (302), 12.9s (302) | 12.9s (all three runs answered, but with 302) | 12.6s to 15.2s | `/` answered **302**, not 200, in all three trials. Its `vite.config.js` sets `base: "/admin-one-vue-tailwind/"`; a one-off request to the running container (outside the timed runs) showed `GET /` returns `302 Found` with `Location: /admin-one-vue-tailwind/`, and that path returns 200. The README documents only `npm run dev`, no URL, so the expected route is the default `/`. Not ranked. |
| [antfu-collective/vitesse](https://github.com/antfu-collective/vitesse) | `a8785264f83c2ebe1b3cf689296775a4949957c8` | no trial ran (n/a) | n/a | n/a | **Unsupported** in all three rounds, before any build: `pnpm-workspace.yaml found: workspaces are not supported in the MVP`. `r2t bench` exited 3. |
| [kirklin/boot-vue](https://github.com/kirklin/boot-vue) | `6c82e21359770392086c1181fb1a4ec9d8eb4ad5` | no trial ran (n/a) | n/a | n/a | **Unsupported** in all three rounds, same reason: `pnpm-workspace.yaml found: workspaces are not supported in the MVP`. Exit 3. |
| [alvarosabu/vitesome](https://github.com/alvarosabu/vitesome) | `0041fd8a697712d54bdecd385c6ff425dfadae25` | failed at build after 4.5s, 2.4s, 2.5s (no HTTP status) | n/a (no successful run) | n/a | **Build failed** in all three trials, stage `build`: `pnpm install --frozen-lockfile` exited 1 with `ERR_PNPM_LOCKFILE_BREAKING_CHANGE  Lockfile /app/pnpm-lock.yaml not compatible with current pnpm`. The repo does not pin a pnpm version, so r2t installed its default `pnpm@10.34.9`. Exit 5. |

The elapsed times for vitesome are time-to-failure, not time-to-HTTP. justboil's median and range are shown only for completeness.

## How the five apps were chosen

The rule was fixed before any trial and used only static facts from the repositories (no r2t result). Starting from GitHub's `topic:vite-template` search sorted by stars (non-archived), take the first five whose root `package.json` has a `dev` script beginning with `vite`, which have a root lockfile, and whose README documents a command that starts the dev server. Candidates passed over, in star order:

| Repo | Why not chosen |
|---|---|
| vbenjs/vue-vben-admin | `dev` is `turbo-run dev` |
| buqiyuan/vue3-antdv-admin | `dev` is `vp dev` |
| vue-zone/vue3-vant-mobile | `dev` is `cross-env MOCK_SERVER_PORT=8086 vite` |

Chosen, with the startup instruction each README documents:

| Repo | Documented startup | Documented route |
|---|---|---|
| antfu-collective/vitesse | `pnpm dev` | "visit http://localhost:3333" (`/`) |
| justboil/admin-one-vue-tailwind | `npm run dev` | none stated |
| dan5py/react-vite-shadcn-ui | `pnpm dev` ("Starts the development server") | none stated; Vite's default `/` |
| kirklin/boot-vue | `pnpm dev` | `http://localhost:8888` (`/`) |
| alvarosabu/vitesome | `pnpm run dev` | `http://localhost:3260` (`/`) |

Ports documented in READMEs differ from the port in the container: r2t always passes `--host 0.0.0.0 --port 5173 --strictPort` (or the script's own port) to Vite. Only the route `/` is compared.

## All 15 runs

Rounds rotate the starting app (A B C D E, then B C D E A, then C D E A B), so each app is visited once per round and no app always runs first. One `r2t bench <app> --runs 1 --yes` invocation per app per round, sequentially, 05:12 to 05:14 UTC on 2026-11-21.

| Round | Order | App | Result | Elapsed | HTTP |
|---|---|---|---|---|---|
| 1 | 1 | vitesse | unsupported (exit 3) | n/a | n/a |
| 1 | 2 | admin-one-vue-tailwind | ok | 15.2s | 302 |
| 1 | 3 | react-vite-shadcn-ui | ok | 16.0s | 200 |
| 1 | 4 | boot-vue | unsupported (exit 3) | n/a | n/a |
| 1 | 5 | vitesome | failed at build (exit 5) | 4.5s | n/a |
| 2 | 1 | admin-one-vue-tailwind | ok | 12.6s | 302 |
| 2 | 2 | react-vite-shadcn-ui | ok | 16.1s | 200 |
| 2 | 3 | boot-vue | unsupported (exit 3) | n/a | n/a |
| 2 | 4 | vitesome | failed at build (exit 5) | 2.4s | n/a |
| 2 | 5 | vitesse | unsupported (exit 3) | n/a | n/a |
| 3 | 1 | react-vite-shadcn-ui | ok | 15.8s | 200 |
| 3 | 2 | boot-vue | unsupported (exit 3) | n/a | n/a |
| 3 | 3 | vitesome | failed at build (exit 5) | 2.5s | n/a |
| 3 | 4 | vitesse | unsupported (exit 3) | n/a | n/a |
| 3 | 5 | admin-one-vue-tailwind | ok | 12.9s | 302 |

No trial was blocked by Docker or registry access: every build that started either completed or failed on its own error as recorded above. No cleanup warnings were reported, and afterwards there were no `r2t` containers, no `r2t/*` images and no `r2t-bench-*` temporary directories left.

## Runner

All 15 runs used the same machine and Docker daemon (identical in every report):

- r2t 0.1.0 (commit `a9841a8` of this repository), Python 3.13.18
- Host: Linux 6.18.44 (x86_64, glibc 2.39), 4 CPUs
- Docker Engine 29.9.2 (4 CPUs, 15.7 GiB visible to Docker), Docker Compose 5.6.1
- Base images used: `node:26-bookworm-slim` (image ID `d09cb410cf5e`) for admin-one-vue-tailwind and react-vite-shadcn-ui; `node:24-bookworm-slim` (`818adb4ab1d2`) for vitesome. These are local copies of the official images with this sandbox's TLS-inspecting proxy CA layered in and re-tagged under the same names, because the sandbox's egress proxy re-signs HTTPS and the stock images do not trust it. Same images, same daemon, every trial.
- Network: through the sandbox's egress proxy. No Docker Hub rate-limit or registry error appeared in any of the 15 logs (base images were already local).

## Cache policy

- Fresh per trial: new temporary checkout, new random Compose project and image name, `docker compose build --no-cache`. No project layer or dependency install (`npm ci` / `pnpm install`) is reused from another trial or an earlier build. In the build log the only cached step was the base-image `FROM`.
- Not controlled: base images were pre-pulled and are not cleared per trial; registry, proxy or CDN caches, Docker's cache for base layers, the OS page cache and network speed all remain. The first run of each app may therefore differ from later ones (admin-one-vue-tailwind took 15.2s first, 12.6s and 12.9s after).
- After each trial r2t removed only that trial's Compose project, image and temp directory.

## Reproduce

```bash
git clone https://github.com/Lameda12/r2t && cd r2t && git checkout a9841a8 && pip install -e .

# 1. Fetch and pin each app (outside the timer). The checked-out commit is the pin.
mkdir -p repos && cd repos
pin() {  # pin <owner/repo> <full sha>
  d=${1//\//__}; git init -q "$d" && git -C "$d" remote add origin "https://github.com/$1" &&
  git -C "$d" fetch -q --depth 1 origin "$2" && git -C "$d" checkout -q --detach FETCH_HEAD
}
pin antfu-collective/vitesse           a8785264f83c2ebe1b3cf689296775a4949957c8
pin justboil/admin-one-vue-tailwind    8a1c411b62c2fd01b7bd81feed7f4e36c46047e7
pin dan5py/react-vite-shadcn-ui        6be1e78987b5cc63706e40fd1a38d02bf6491ae3
pin kirklin/boot-vue                   6c82e21359770392086c1181fb1a4ec9d8eb4ad5
pin alvarosabu/vitesome                0041fd8a697712d54bdecd385c6ff425dfadae25
cd ..

# 2. Three rotating rounds, one trial per app per round.
APPS=(antfu-collective__vitesse justboil__admin-one-vue-tailwind dan5py__react-vite-shadcn-ui \
      kirklin__boot-vue alvarosabu__vitesome)
mkdir -p out
for round in 1 2 3; do
  for i in 0 1 2 3 4; do
    app=${APPS[$(( (i + round - 1) % 5 ))]}
    r2t bench "repos/$app" --runs 1 --yes > "out/r$round-$app.out" 2> "out/r$round-$app.err"
    echo "$?" > "out/r$round-$app.exit"
  done
done

# 3. Each out/*.out starts with the review text and ends with the leaderboard row,
#    failures and a per-run table. Medians and ranges above are computed from those rows.
grep -h "^| 1 |\|^- Run" out/*.out
```

`r2t bench` asks for confirmation before building and running repository code; `--yes` answers it, which is appropriate only for repositories you are willing to run. Docker is an isolation boundary, not a guarantee that arbitrary code is safe.

## Caveats

- One machine, one daemon, three trials per app. Ranges are small here but three samples say little about variance elsewhere.
- Pass/fail reflects r2t's MVP, not the apps alone: two apps are excluded because r2t does not support `pnpm-workspace.yaml`, and vitesome failed because r2t's default pnpm rejected its old lockfile. Both could behave differently with other tooling.
- The ranked board has one entry, so it says nothing yet about relative speed between apps. justboil's three times (12.6s to 15.2s) are real observations but are not ranked because `/` answered 302.
- Elapsed time for the one ranked app includes installing dependencies with `pnpm@10.34.9`, which r2t chose because the repo does not pin a pnpm version.

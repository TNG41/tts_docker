# Docker / CI-CD setup

## Files

- `Dockerfile` — builds the bot image (Python 3.11-slim + ffmpeg + deps).
- `docker-compose.yml` — base config, pulls the image CI publishes to GHCR.
- `docker-compose.override.yml` — local-dev only; auto-merged by `docker compose up`,
  builds from source instead of pulling. Delete it on your deploy server (or always
  run with `-f docker-compose.yml` there, as the CI workflow does).
- `.env.example` — copy to `.env` and fill in your token/settings. Never commit `.env`.
- `.github/workflows/deploy.yml` — builds the image, pushes to GHCR, then (optionally)
  deploys over SSH.

## Local development

```bash
cp .env.example .env   # fill in DISCORD_BOT_TOKEN, etc.
docker compose up --build
```

## GHCR image name

Edit `docker-compose.yml` and replace `ghcr.io/<your-org>/<your-repo>` with your
actual repo path (GitHub Actions publishes to
`ghcr.io/<github-username-or-org>/<repo-name>` by default).

By default, GHCR packages built from a repo are private. Either make the package
public, or have your deploy host `docker login ghcr.io` with a token that has
`read:packages` scope (the workflow already does this on the deploy side).

## Enabling auto-deploy over SSH

The `deploy` job only runs if the repo variable `DEPLOY_ENABLED` is set to `true`
(Settings → Secrets and variables → Actions → Variables). If you'd rather deploy
manually, leave it unset/false and just run this on your server after a build:

```bash
docker compose -f docker-compose.yml pull
docker compose -f docker-compose.yml up -d
```

To enable auto-deploy, set these repository secrets:

| Secret            | Value                                              |
|-------------------|-----------------------------------------------------|
| `DEPLOY_HOST`     | Server IP/hostname                                  |
| `DEPLOY_USER`     | SSH user                                            |
| `DEPLOY_SSH_KEY`  | Private key for that user (add the matching public key to the server's `~/.ssh/authorized_keys`) |
| `DEPLOY_PATH`     | Absolute path on the server containing `docker-compose.yml` and `.env` |

The server needs Docker + Compose installed, and a `.env` file already in place
(the workflow never uploads secrets — it only pulls the image and restarts).

## Persisted data

`data/` (settings, custom TTS names) and `tts_audio/` are named Docker volumes,
so they survive container restarts and image updates.

## "Please sign in" errors playing YouTube

YouTube increasingly challenges datacenter/VPS IPs (AWS, Hetzner, DigitalOcean,
etc.) regardless of the video — this shows up as an extraction error demanding
sign-in. Fix: export cookies from a logged-in browser session (e.g. the
"Get cookies.txt LOCALLY" extension) into a `cookies.txt` file, put it next to
`docker-compose.yml`, then uncomment the `COOKIES_FILE` env var and the
`cookies.txt` volume line in `docker-compose.yml`. Re-export periodically —
the session cookies do expire.

The mount must stay writable (no `:ro`) — yt-dlp updates the cookie jar in
the file after each use, and the container runs as uid 1000 (non-root), so
make sure the host file is writable by that uid too:
`chmod 666 cookies.txt` is the simplest fix if you hit a permissions error.

`yt-dlp` itself also breaks against YouTube frequently as sites change; if
playback errors don't mention signing in, first try bumping the pinned
version in `requirements.txt` to whatever's latest and rebuilding.

## "Requested format is not available" / signature or "n challenge" solving failed

YouTube requires solving JS-based signature/player challenges for most
clients now (see yt-dlp's
[EJS setup guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS)). Two things
have to both be present:

1. A JS runtime — the image installs **Deno**, yt-dlp's recommended and
   default-enabled runtime (no extra flags needed, unlike Node/Bun which
   require `--js-runtimes`).
2. The actual challenge-solver scripts — `requirements.txt` installs
   `yt-dlp[default]`, which bundles the `yt-dlp-ejs` package. A JS runtime
   alone isn't enough without this.

Also note: if you're using a `cookies.txt` (see above), yt-dlp will skip
the `android`/`ios` player clients entirely, since those clients don't
support cookies — it falls through to `web`, which is exactly the path
that needs Deno + `yt-dlp-ejs` working.

This is a moving target — YouTube and yt-dlp are in an active cat-and-mouse
fight, so this exact fix may stop working at some point. If it recurs, check
the [yt-dlp issue tracker](https://github.com/yt-dlp/yt-dlp/issues) and the
EJS wiki page for whatever's currently recommended, and bump both `yt-dlp`
and the Deno version in the Dockerfile.

## Voice fails to connect / retries forever ("Failed to connect to voice")

Since March 2026, Discord requires end-to-end encrypted (DAVE protocol) voice
connections on non-stage channels; a client without DAVE support gets its
connection closed and retries in a loop, which looks like a Docker/network
problem but isn't. This is fixed by pinning `discord.py[voice]>=2.6` and
including `dave.py` (its encryption binding) in `requirements.txt` — both are
already set correctly here. If you still hit this after rebuilding, check
that the image actually picked up the new `requirements.txt` (rebuild without
cache: `docker compose build --no-cache`).

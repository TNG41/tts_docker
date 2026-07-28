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

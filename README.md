# restic-ui

A simple Flask-based web UI for browsing and restoring [restic](https://restic.net/) backups.

This app:

- Lists snapshots from a restic repository.
- Lets you browse snapshot contents (lazy-loaded tree).
- Allows restoring selected files/directories from a snapshot.

## Requirements

- Docker
- Docker Compose
- A working restic repository (local or remote, e.g. Backblaze B2/S3).

The app expects the usual `RESTIC_*` environment variables to be set so the `restic` CLI can access your repository.

## Running with Docker Compose

The repository includes a `docker-compose.yml` that builds and runs the app.

### 1. Configure restic access

Edit `docker-compose.yml` and set the appropriate environment variables under the `restic-ui` service:


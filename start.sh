#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
env_file="$project_root/deploy/.env"
workspace=""
no_chat=false
build_option=--build
while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-file|--workspace)
            if [[ $# -lt 2 || -z "$2" ]]; then printf 'Missing value for %s\n' "$1" >&2; exit 2; fi
            if [[ "$1" == --env-file ]]; then env_file="$2"; else workspace="$2"; fi
            shift 2 ;;
        --no-chat) no_chat=true; shift ;;
        --no-build) build_option=--no-build; shift ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done
command -v docker >/dev/null || { printf 'Install Docker first.\n' >&2; exit 1; }
docker info --format '{{.ServerVersion}}' >/dev/null
docker compose version >/dev/null
[[ "$env_file" == /* ]] || env_file="$project_root/$env_file"
if [[ ! -f "$env_file" ]]; then
    if [[ "$env_file" == "$project_root/deploy/.env" ]]; then
        cp "$project_root/deploy/.env.example" "$env_file"
        printf 'Created %s. Set your API key and workspace, then run again.\n' "$env_file"
        exit 1
    fi
    printf 'Configuration file does not exist: %s\n' "$env_file" >&2; exit 1
fi
export LOOPSMITH_ENV_FILE="$env_file"
if [[ -n "$workspace" ]]; then
    [[ "$workspace" == /* ]] || workspace="$project_root/$workspace"
    export LOOPSMITH_WORKSPACE="$(cd -- "$workspace" && pwd)"
fi
compose=(docker compose --project-directory "$project_root" --env-file "$env_file" -f "$project_root/compose.yaml")
"${compose[@]}" config --quiet
if ! "${compose[@]}" up --detach "$build_option" --wait --wait-timeout 120 agent; then
    printf 'Startup failed. Check Core logs, credentials, paths, proxy and file permissions.\n' >&2
    exit 1
fi
printf 'Core is ready. Session data is stored in the agent-data volume.\n'
if [[ "$no_chat" == false ]]; then "${compose[@]}" exec agent loopsmith-tui; fi

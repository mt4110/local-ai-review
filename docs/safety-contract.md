# Safety Contract

## Non-Negotiable Rules

The privileged GitHub Actions path must never:

- use `actions/checkout`;
- run PR-controlled code;
- run `pip install`, `npm install`, build, test, shell scripts, or package hooks from the PR;
- pass repository secrets into the job;
- treat diff text as instructions;
- call cloud LLM APIs by default.

It may:

- use `pull_request_target` only for label-gated PR commenting;
- fetch PR metadata and diff through the GitHub API;
- call a trusted `llreview` binary already installed on the self-hosted runner;
- send diff text to local Ollama;
- write or update one marker-based PR comment;
- persist encrypted evidence locally.

## Design Philosophy

This tool operates as a local review immune system. It assumes all PR input is untrusted and must be kept strictly separated from execution environments and secrets.

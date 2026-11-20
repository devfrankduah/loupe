# Use loupe in CI

loupe is also a GitHub Action. It installs loupe from this repository at the ref you name, scans your pages, uploads the JSON results as an artifact, and fails the step according to `fail-on`. It runs on Linux and macOS runners.

## Scan your site after each deploy

This workflow runs whenever a deployment succeeds, for hosts such as Netlify and Vercel that report their deployments to GitHub. It scans the deployed site's pages and fails if any of them hides text worded like an instruction to an AI.

```yaml
name: Scan the deployed site

on:
  deployment_status:

permissions:
  contents: read

jobs:
  loupe:
    if: github.event.deployment_status.state == 'success'
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      # Pin to a commit SHA so the scan only changes when you update it.
      - uses: devfrankduah/loupe@main
        with:
          urls: |
            https://example.com/
            https://example.com/pricing/
            https://example.com/docs/getting-started/
          render: true
```

Replace the URLs with your own pages. A host that deploys previews to their own address puts it in `github.event.deployment_status.environment_url`, which can stand in for the first URL.

When the site is deployed by a workflow in the same repository, as GitHub Pages sites built with Actions are, start the scan when that workflow finishes instead. Deployments made with the workflow's own token don't start other workflows, so `deployment_status` may never fire:

```yaml
on:
  workflow_run:
    workflows: [Deploy]  # the name of your deploy workflow
    types: [completed]

jobs:
  loupe:
    if: github.event.workflow_run.conclusion == 'success'
```

The steps are the same as above. A site with neither can run the scan on a `schedule`.

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `urls` | (required) | The pages to scan, separated by spaces or new lines. |
| `render` | `false` | `true` loads each page in Chromium and checks what actually shows. It adds about a minute to install the browser. |
| `fail-on` | `instructions` | Which findings fail the step: `instructions` (hidden text worded like an instruction to an AI), `hidden` (any hidden text) or `never`. A page that couldn't be scanned always fails it. |
| `backend` | `auto` | `local`, `brightdata`, or `auto`, which uses Bright Data when its credentials are set and the runner otherwise. |
| `artifact-name` | `loupe-results` | The name the JSON results are uploaded under. Give each loupe step in a job its own. |

## Outputs

| Output | Meaning |
| --- | --- |
| `exit-code` | loupe's exit code: 0 clean, 1 when a finding `fail-on` counts, 2 when a page couldn't be scanned in full. Empty when loupe didn't run, such as for an input the action doesn't accept. |
| `results` | The path of the JSON results on the runner, in the format [json.md](json.md) describes. |

To keep the job going when loupe fails the step, so a later step can read the results, add `continue-on-error: true` and an `id`, then check `steps.<id>.outputs.exit-code`.

## Sites that block bots

Sites behind bot protection turn the runner away, and those pages exit 2 as blocked. The Bright Data backend can reach them. Store the token as a repository secret and pass it in the step's `env`, never in `with`:

```yaml
      - uses: devfrankduah/loupe@main
        env:
          BRIGHTDATA_API_TOKEN: ${{ secrets.BRIGHTDATA_API_TOKEN }}
        with:
          urls: https://example.com/
          backend: brightdata
```

loupe never prints or saves the token, and never prints any part of a Browser API endpoint.

## What the action does

1. Checks the inputs, and stops with an error for a value it doesn't accept.
2. Sets up Python 3.13 without changing the job's own Python or `PATH`, and installs loupe into its own virtual environment from the action's checkout, so it's the ref the workflow names. With `render: true` it also installs Chromium.
3. Runs `loupe --urls-file` on the URLs with `--json-output`, printing the usual report and summary table to the log. Text quoted from the pages can't act as a workflow command, because the runner stops reading commands while loupe runs.
4. Uploads the results file as the artifact, even when the scan failed the step.

Inputs reach the action's scripts as environment variables and never as part of the script, so nothing in a URL can run as a shell command. This repository's CI runs the action against the test pages, served inside the job, on every push, including a URL built to run a command if that ever stopped being true.

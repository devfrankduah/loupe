# Releasing

loupe is published to PyPI as `loupe-scan` by [`.github/workflows/release.yml`](../.github/workflows/release.yml), and only when a GitHub release is published. The workflow uses PyPI's [trusted publishing](https://docs.pypi.org/trusted-publishers/): PyPI trusts that one workflow, running in this repository's `pypi` environment, to upload the project, so no API token is ever created or stored. It builds the sdist and the wheel, checks them with `twine check --strict`, refuses a release whose tag isn't `v` followed by the package's version, and uploads them.

## One-time setup

Only the project's owner can do these, on pypi.org and on GitHub, and nothing is published until the first release.

1. On PyPI, add a pending publisher, which lets the first release create the project. Sign in, open your account's Publishing settings, and add a GitHub publisher with:
   - PyPI project name: `loupe-scan`
   - Owner: `devfrankduah`
   - Repository name: `loupe`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   PyPI's guide is [Creating a PyPI project with a Trusted Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/). A pending publisher doesn't reserve the name: if someone else registers `loupe-scan` before the first release, the pending publisher stops working. Once the project exists, its publishers are managed from the project's own settings, as in [Adding a Trusted Publisher to an existing PyPI project](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).
2. On GitHub, in the repository's Settings, under Environments, create an environment named `pypi`, as GitHub's [Managing environments for deployment](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-deployments/managing-environments-for-deployment) describes. Adding yourself as a required reviewer makes every publish wait for your approval, and limiting its deployments to tags matching `v*` keeps anything else from using it. PyPI calls the environment optional but strongly recommends it. With the GitHub CLI signed in as the repository's owner, these two commands create it with you as its required reviewer, `56163058` being your GitHub user id, and deployments limited to tags matching `v*`:

   ```
   gh api --method PUT repos/devfrankduah/loupe/environments/pypi --input - <<'EOF'
   {"reviewers": [{"type": "User", "id": 56163058}], "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
   EOF
   gh api --method POST repos/devfrankduah/loupe/environments/pypi/deployment-branch-policies -f name='v*' -f type=tag
   ```

   `gh api repos/devfrankduah/loupe/environments/pypi` shows the result.

There's no token or secret to add anywhere.

## Each release

1. Make sure `main` is green and that `src/loupe_scan/_version.py` holds the version to release.
2. In `CHANGELOG.md`, replace "(unreleased)" after the version with the date, and check that `CITATION.cff` has the same version.
3. On GitHub, draft a release with a new tag, `v` followed by the version, such as `v0.2.0`, on `main`, and publish it. A pre-release publishes too, so use one only for a version PyPI should treat as a pre-release, such as `0.3.0rc1`.
4. The Release workflow runs. If the `pypi` environment has a required reviewer, approve the publish job when it asks.
5. Check the project at [pypi.org/project/loupe-scan](https://pypi.org/project/loupe-scan/), and install it from PyPI in a fresh virtual environment to try it.
6. Only then switch the install instructions to PyPI and remove the test that blocks them until the package is there.

PyPA's guide to this kind of workflow is [Publishing package distribution releases using GitHub Actions](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/), and [Using a Trusted Publisher](https://docs.pypi.org/trusted-publishers/using-a-publisher/) covers the workflow side.

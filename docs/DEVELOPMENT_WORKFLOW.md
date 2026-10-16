# Work across computers without losing changes

Use this public repository as the shared source for future public-app work.
Keep each running installation in its own private directory outside the checkout.
Never place databases, statements, keys, `.env` files or backups in a Git branch.

## Start on another computer

```sh
git clone https://github.com/samuel-zhang01/im-i-broke.git
cd im-i-broke
git config pull.ff only
git config fetch.prune true
git switch main
git pull --ff-only
git switch -c fix/describe-the-change
```

Follow [Contributing](../CONTRIBUTING.md) for dependencies and synthetic checks.
Commit and push before changing devices. On the next device, fetch and switch
to that same branch. Avoid editing the same branch on two devices at once.

```sh
git status
git add path/to/changed-file
git diff --cached
git commit -m "Describe the change"
git push -u origin HEAD
```

Use a pull request to review and merge work into `main`. After it merges, switch
back to `main` and pull with `--ff-only` before starting another branch.
Inspect staged files before every push, including synthetic screenshots and logs.
The root `.gitattributes` keeps text in LF form; `.editorconfig` keeps common
editor settings consistent. Run shell helpers through Bash on Windows/WSL.

## When Git refuses a pull

Run `git status` and preserve local work in a commit or stash first. Fetch, then
inspect `git log --oneline --graph --decorate --all` to understand both sides.
On your own unpublished feature branch, `git rebase origin/main` can incorporate
updates; resolve each conflict and run the relevant checks before pushing.
For a branch already shared between devices or contributors, merge `origin/main`
instead, so existing commits keep their identities. Never force-push shared
`main` or use `reset --hard` to make a deployment checkout match.

## Keep private predecessor history separate

The public repository deliberately has a new root commit. Do not add the private
predecessor as a public remote, merge its branches, push its tags, use `--mirror`,
or upload its archives. A branch in that old repository would still carry its
earlier history.

For future work, develop public-safe changes here first. If a separate private
codebase needs the same fix, transfer only a reviewed patch or cherry-pick the
public commit **into the private codebase**. Bringing a private commit into this
repository also brings its message, author metadata and complete diff; recreate
the public-safe change here and review it instead. Keep any recurring transfer
procedure explicit so it cannot silently add old private objects.

## Coding and deployment are separate

A Git push runs synthetic CI; it does not update a user's running installation.
For installations created by `scripts/setup.py`, follow
[release and upgrade](DEPLOYMENT.md#release-and-upgrade). The root
[`deploy.sh`](../deploy.sh) is only for an existing root-Compose checkout and
requires a clean, fast-forwardable deployment branch; see
[automatic deployment](AUTOMATIC_DEPLOYMENT.md).

Do not change an existing private deployment's remote to this repository to
solve the unrelated histories. Keep its current storage and tested deployment
process until you plan and rehearse a deliberate migration.

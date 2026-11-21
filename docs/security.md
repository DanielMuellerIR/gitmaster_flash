# Git transfers and safety

[English](security.md) · [Deutsch](security.de.md) · [Overview](../README.md)

Fetch, pull and push use checked target identities and explicit refs. The safeguards apply to actions started through Gitmaster Flash.

## Fetch and scan state

The initial scan reads local state and remote-tracking refs. `R` refreshes
safe remotes in the background and `U` refreshes one remote. Gitmaster Flash
fetches the advertised commit of the current branch with tags and submodule
recursion disabled, then updates only the corresponding tracking ref. The
ref update leaves symbolic refs undereferenced. Fetch and content-comparison
errors stay visible as measurement failures, with the last successful fetch
time where available.

## Commits and stashes

The commit helper uses a temporary index for the approved working-tree paths.
It verifies the branch and the committed tree before updating the real
index for just those paths. Stash actions preview the affected entries and
files. Apply and pop require a clean index and working tree, and delete asks
for separate confirmation. See the [interface guide](interface.md) for
scope, conflicts and what a partial batch can leave behind.

## Pull and push

`P` is intentionally limited to a non-public sync remote. It fetches first,
requires a clean working tree and rejects behind or divergent history. Fetch and push
URLs must identify one identical credential-free host/repository target; multiple
or differing push URLs are blocked. The mix that is common at hosting services —
fetch over HTTPS, push over SSH through the virtual `git` user — counts as the
same target: `git@host:org/repo` and `https://host/org/repo` mean the same
repository. Immediately before a confirmed mutation the
branch, HEAD, index, worktree, remote identity and target OID are checked again.
Push sends the approved commit OID through an explicit refspec. An exact target-OID lease prevents a
remote deletion or concurrent move from turning it into an unreviewed update;
tags and submodule commits are never sent. The one push also disables repository
hooks, so a pre-push hook cannot publish unreviewed tags or additional refs. The transfer uses the exact URL that was
checked, not a remote name that concurrent configuration could redirect. The way
to that address counts as well: for that one call an SSH wrapper from the
environment or from the repository's config is ignored, and three settings in
the repository's *own* config block the transfer outright — `core.gitProxy`,
`http.proxy` (including its address-bound form) and `http.sslVerify = false`.
Each of them can send a transfer somewhere other than the checked address, and
none can be switched off for a single call: the first is a multi-valued key, and
forcing the second empty would also disable a real corporate proxy. In your
global Git config all three stay allowed: there you wrote them yourself, and a
repository cannot slip them in. Its fetch
refspec must map `refs/heads/<branch>` to that remote's matching tracking ref.

Press `L` to choose a pull remote. “Fetch all remotes” first refreshes every safe
remote of this repository, then opens the source chooser again. Only the
**remote branch with the same name** is integrated into the current local branch;
the existing upstream stays unchanged. gmf previews incoming commits and file names
before **Y ⏎** confirmation. Only fast-forward with a clean working tree is allowed;
divergence, conflicts and active Git operations stop the pull. Local ignored files
are preserved if incoming files use the same paths; branch squash settings cannot
change the fast-forward. Immediately before
applying, gmf rechecks the approved state and integrates the fixed commit ID without
hooks, autostash or recursive submodules. Do not edit or switch this repository in
another program meanwhile: branch, index and working tree cannot be bound atomically
against a concurrent checkout. Merge and rebase remain terminal special cases.

GitHub uses the separate `G` path. It works only when the same branch already
exists on exactly one GitHub remote and the histories are related. Before
publishing it shows every outgoing commit and changed file name. The question
sits below that list as a white-on-black bar with the `Y ⏎ = yes` part in red, a
blank line above it and the terminal cursor visible behind it, so it still stands
out after a hundred file names; it is answered with `Y` + ⏎ (any other input, an
empty ⏎ or Esc cancels):

```text
 GitHub push preview · app → github/master
 …
 M	src/search.py
 A	docs/search.md

 Publish this branch to github? Y ⏎ = yes, anything else cancels: ▮      ← bar
 ↑/↓ scroll · Esc cancel · line 41-72 / 72
```

The `P` question for the private sync remote uses the same bar, one line above
the footer of the repo list. The final command still sends only the current
branch: approved source OID, exact target lease, no tags, no new branch. A remote
with multiple or differing fetch/push targets is blocked entirely, even if both
targets are on GitHub. Complex cases stay terminal-only.

Git never asks for credentials here. Every Git call runs with terminal prompts and
askpass disabled and in its own session, because Git writes such a question
(`Username for 'https://github.com':`) straight to the terminal rather than to the
captured output — inside the curses screen that destroys the display and then waits
for input that never arrives. A remote that needs a login therefore fails right away
with `<remote> needs a login (no credential helper or SSH key).` instead of asking.
Store HTTPS credentials in a credential helper (in a logged-in macOS GUI session:
`git config --global credential.helper osxkeychain`) or use SSH with a key in the
agent; both work without any prompt.

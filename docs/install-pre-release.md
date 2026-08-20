# Install (pre-release / early-access testers)

> Install instructions for non-public pre-release builds hosted on TestPyPI.
> Deliberately not linked from the documentation index: it is handed out
> directly to testers, not part of normal navigation.

## What this is

You're testing a pre-release build of **Sentience Governor**. It's hosted
on TestPyPI, which is Python's staging server. Crescere Labs publishes
builds there before they go live on the real Python package index.

The install is one command. It takes about 30 seconds.

## Before you start

You need:

- A Mac or Linux terminal (Windows works too via PowerShell)
- Python 3.10 or newer (check by running `python3 --version`)
- An internet connection

You do **not** need:

- A GitHub account
- To download any file manually
- To click any link

## The install command

Replace `<VERSION>` with the version Crescere Labs gave you (for example, `0.2.5.1`).

**If you have `pipx` installed** (recommended on macOS and Linux — see "First-time setup" below if you don't):

```bash
pipx install --index-url https://test.pypi.org/simple/ \
    --pip-args='--extra-index-url https://pypi.org/simple/' \
    sentience-governor==<VERSION>
```

**If you're inside an active virtualenv** (or on Windows where `pipx` is less common):

```bash
pip install --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ \
    sentience-governor==<VERSION>
```

Either command will fetch the package, fetch its dependencies, and install everything. You'll see a few lines of output ending in `Successfully installed sentience-governor-<VERSION> ...`.

### First-time setup (only if you don't have pipx)

On macOS and most Linux systems, raw `pip install` fails with an `externally-managed-environment` error because modern Pythons block global pip installs by design. `pipx` is the standard fix.

**macOS:**

```bash
brew install pipx
pipx ensurepath
```

**Linux / WSL:**

```bash
python3 -m pip install --user pipx
pipx ensurepath
```

After `pipx ensurepath`, restart your terminal (or run `source ~/.zshrc` / `source ~/.bashrc`) so the new `$PATH` takes effect.

## Verify it worked

Run these:

```bash
sentience --help
sentience-cli --help
```

Each should print a usage screen. If both print usage, you're installed and ready to go.

## What to do next

Now that you're installed, follow the public quickstart:

**[https://getsentience.ai/docs/quickstart/](https://getsentience.ai/docs/quickstart/)**

**Skip the "Install" section there.** You've already done it (with the
TestPyPI command above, which is different from the public one). Every
other section applies exactly as written: wiring the Claude Code hook,
running `sentience` commands, and viewing traces.

If the public quickstart does not match your installed version, email
**operators@crescerelabs.com** with the mismatch.

## Known wrinkle: macOS + Python from python.org

If a network call (for example the first-run launch-list email submit) fails with `SSL: CERTIFICATE_VERIFY_FAILED`, your Python install is missing CA roots. This is a known macOS-Python.org packaging quirk, not a Sentience bug. One-time fix:

```bash
/Applications/Python\ 3.13/Install\ Certificates.command
```

(Adjust `3.13` to match your installed Python version.)

## If something went wrong

### "No matching distribution found for sentience-governor"

You probably ran `pip install sentience-governor` (or `pipx install sentience-governor`) without the `--index-url` flags. The package is only on the staging server right now, not the main one. Re-run the full command from [The install command](#the-install-command) above.

If you already tried the wrong command once, `pip` or `pipx` may have cached the failure. Clear the cache and retry:

```bash
pip cache purge
pipx uninstall sentience-governor 2>/dev/null  # ok if "not installed"
```

### "externally-managed-environment" error

You ran `pip install sentience-governor` (or with the `--index-url` flags) directly against your system Python. Modern macOS and Linux Pythons block this by design. Use `pipx` instead — see [First-time setup](#first-time-setup-only-if-you-dont-have-pipx) above.

### "pipx ensurepip" failure

If `pipx install` fails with an `ensurepip` exit status 1 error, the Python pipx defaulted to has a broken `ensurepip` module (sometimes happens with very new Python versions on macOS). Tell pipx to use a known-good Python:

```bash
rm -rf ~/.local/pipx/shared
PIPX_DEFAULT_PYTHON=python3.12 pipx install --index-url https://test.pypi.org/simple/ \
    --pip-args='--extra-index-url https://pypi.org/simple/' \
    sentience-governor==<VERSION>
```

### "command not found: sentience"

The install worked, but the directory `pip` put the commands into isn't
on your `$PATH`. Quick fix on macOS:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Add that line to `~/.zshrc` to make it permanent.

### "ERROR: This package requires Python >=3.10"

Your default `python3` is older than 3.10. Install a newer one (Homebrew:
`brew install python@3.12`) and re-run the command using `python3.12 -m
pip install ...` instead of `pip install ...`.

### Anything else: get help

Email **operators@crescerelabs.com** with a screenshot of the full
terminal output (the error *and* the command that produced it). Don't
truncate. That reaches a human directly, usually replied to within a
few hours.

## What I should not do

- **Do not download files manually.** If you saw a webpage with two file
  links (one ending `.whl`, one ending `.tar.gz`), ignore it. Those
  links are for `pip` to read, not for you to click. Clicking them
  shows binary garbage in the browser. That's expected, and not how
  installation works.
- **Do not click "Cmd+Click" workarounds on those links.** You don't
  need the files at all.
- **Do not run `pip install sentience-governor`** (without flags). That
  hits the wrong server and fails.

## When the test is over

To uninstall:

```bash
pip uninstall sentience-governor
```

Removes the package and its command-line entry points. Local trace files at
`~/.sentience/` stay until you delete them yourself.

---

## One inbox for everything

**operators@crescerelabs.com** is the address for all of it:

- **Stuck on install or anything else.** Send a screenshot.
- **Found a bug.** Describe what you did and what happened.
- **Have feedback on the product.** What's confusing, what's missing, what feels wrong, what feels right. All useful.
- **Have a feature suggestion.** Send it.

Crescere Labs replies within a few hours. There's no separate channel
and no form to fill out. It's just an email to a human.

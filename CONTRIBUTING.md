# Contributing to bgpcanary

Thanks for helping! Bug reports, ideas and fixes are all welcome.

## Reporting a bug or asking for a feature

[Open an issue](https://github.com/gregsowell/bgpcanary/issues). For bugs, include:
- your bgpcanary version (bottom of every page, or `bgpcanary-admin version`);
- your OS;
- what happened versus what you expected;
- the relevant lines from `journalctl -u bgpcanary`.

**Remove passwords, API keys, webhook URLs and anything private first.** Security problems go privately
instead; see [SECURITY.md](SECURITY.md).

## Sending a change

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Fork the repository and make your change on a branch.
3. Keep to the existing style: small, readable functions; comments that explain *why*; no new dependencies without discussion.
4. Test on a Rocky / AlmaLinux 9 or 10 machine or VM. `deploy/push.sh user@host` copies your working tree to a test server and runs the installer there.
5. Add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md).
6. Open a pull request describing what changed and how you tested it.

**Database changes** must be additive (new tables or columns) and applied automatically at startup in
`bgpcanary/db.py`, so upgrades never need manual steps.

## Licensing of contributions

bgpcanary is released under the [Functional Source License](LICENSE.md), and the author may also offer it
under separate commercial licenses. By submitting a contribution you confirm that you wrote it (or have the
right to submit it), and you agree that it may be distributed under the project's license and under any
license the author offers, including commercial licenses. If that isn't acceptable to you, please say so in
your pull request before it's merged.

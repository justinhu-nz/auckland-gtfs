# Auckland GTFS

[![Update and archive GTFS](https://github.com/justinhu-nz/auckland-gtfs/actions/workflows/update-gtfs.yml/badge.svg)](https://github.com/justinhu-nz/auckland-gtfs/actions/workflows/update-gtfs.yml)

A mirror of Auckland Transport's static public transport schedules. `gtfs.zip`
contains the original download; `gtfs/` contains its extracted CSV files.

Source: [AT's GTFS page](https://at.govt.nz/about-us/at-data-sources/general-transit-feed-specification)
and [direct ZIP download](https://gtfs.at.govt.nz/gtfs.zip).
Data is published by Auckland Transport under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

## Automatic updates and archives

GitHub Actions checks daily at 17:23 UTC (05:23 NZST / 06:23 NZDT).
The schedule is best effort. It downloads into temporary storage, checks ZIP
integrity, required files and columns, CSV structure, and feed validity dates,
then archives snapshots before replacing the current files. These are structural
checks, not a complete GTFS semantic validation.

Each distinct feed is stored in [GitHub Releases](https://github.com/justinhu-nz/auckland-gtfs/releases)
with two assets:

- `gtfs.zip`: the original ZIP captured for that feed.
- `metadata.json`: source, archive date, feed version, validity dates, row counts,
  and SHA-256 checksums of the ZIP and each extracted file.

Tags use `gtfs-<content-sha256>`. The hash covers filenames and uncompressed file
contents, so changes to ZIP timestamps or compression do not create duplicate
archives or commits. CSV row reordering counts as a content change. The first run
also archives the existing repository ZIP; its original download date is unknown.
Snapshots remain in Releases until someone deletes them. Older feeds from before
this automation cannot be recovered automatically.

Uploads start as draft releases and are published once both assets are present.
A rerun can complete interrupted uploads. Download, validation, or archive failures
leave the current feed unchanged. Obsolete extracted files are removed when a new
feed is installed. Only changed data is committed, and workflow runs are serialized.
Releases identify data by their ZIP assets and checksums; the release's automatic
source-code archive/tag points to the checkout used by the updater, not the later
data update commit.

The current ZIP remains in Git for compatibility, so Git history will still grow
as it changes. Historical ZIP copies are stored in Releases, not dated repo folders.

## Enable on GitHub

1. Push these files to the repository's default branch.
2. Enable GitHub Actions if disabled. The workflow requests `contents: write`
   using the built-in `GITHUB_TOKEN`; no personal token or AT API key is needed.
   Repository/organization policy and branch protection must allow the bot to
   commit to the default branch and create releases.
3. Open **Actions → Update and archive GTFS → Run workflow** on the default branch
   for the first refresh. Inspect the run summary and Releases to confirm that AT
   accepts requests from the GitHub runner.

GitHub can disable scheduled workflows in public repositories after 60 days of
repository inactivity. Check Actions if updates stop; re-enable the workflow when
needed. Failed runs appear in Actions and use your GitHub notification settings.

## Local use

Python 3.11+ is required; no third-party Python packages are needed.

```sh
# Download and validate without modifying tracked files or publishing releases:
python3 scripts/update_gtfs.py --dry-run

# Refresh local data without publishing archives:
python3 scripts/update_gtfs.py

# Run offline tests:
python3 -m unittest discover -s tests -v
```

`--archive` is intended for the GitHub workflow and requires `GH_TOKEN`,
`GITHUB_REPOSITORY` (`owner/repo`), and `GITHUB_SHA` (the checkout commit).
The downloaded feed must not be expired; future-starting feeds are accepted.

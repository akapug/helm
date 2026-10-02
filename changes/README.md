# Change notes

A lane writes its change note here, as `changes/<lane>.md`: one or more
markdown bullets, written like a CHANGELOG.md bullet. A lane never edits
CHANGELOG.md, and no two lanes write one file, so their notes never conflict
when a train merges them. At a cut, `scripts/release/release.py <version>
--fold` moves every note here but this README into CHANGELOG.md.

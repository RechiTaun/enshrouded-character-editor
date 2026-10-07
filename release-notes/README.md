# Writing release notes

Every release must have curated notes committed as `vMAJOR.MINOR.PATCH.md`
in this directory before dispatching **Release from main**. Determine the
next version using `python scripts\release_version.py --bump patch` (or
`minor` / `major`) after fetching tags.

Use the existing versioned notes as examples. Include a versioned title,
a clear summary and populated sections named:

- **Highlights**: actual user-facing changes, not just commit subjects.
- **Get started**: download links and concise launch/update guidance.
- **Compatibility & safety**: supported systems, limitations and backup warnings.
- **Verification**: measured test/build evidence; never claim unverified results.
- **Learn more**: version-specific documentation and comparison links.

Emoji in headings are optional. Keep all claims accurate for that version.
Run `python scripts\check_release_notes.py --version MAJOR.MINOR.PATCH`
to validate the title and required sections.

The workflow checks notes before building and uses the committed text for the
release body instead of automatically generated commit lists. Missing or
incomplete notes stop the run. Commit the corrected notes before retrying.
The published body can be improved later without changing its tag or assets.

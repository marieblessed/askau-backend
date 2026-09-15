# Documentation

## Tracked here

[`architecture/`](architecture/) — the design, in Markdown, reviewable in a diff. Start at
[architecture/README.md](architecture/README.md). Every requirement it cites carries its
FR/NFR identifier, so it reads without the source documents to hand.

### Guides — read these to do something

| | For whom | |
|---|---|---|
| [local-setup.md](local-setup.md) | A new developer | What to install, the services, the web client, and the handful of things that otherwise cost an afternoon |
| [azure-setup.md](azure-setup.md) | Whoever wires up identity and storage | Entra tenant, app registrations, a Blob container and the configuration that joins them — end to end, with the traps that fail silently |
| [configuration.md](configuration.md) | Anyone deploying or debugging | Every environment variable: what it is for, where the value comes from, and what breaks without it |

### Reference

| | |
|---|---|
| [data-models.md](data-models.md) | Every table **as built** — what it stores and whether anything uses it yet. The counterpart to `architecture/03-database-schema.md`, which is the design; this one is the state. |

### Point-in-time records

These describe a moment, not the current system. Kept because the reasoning is worth
having and the decisions they record are cited elsewhere — but **do not read them as
current documentation.**

| | |
|---|---|
| [frontend-alignment.md](frontend-alignment.md) | The backend↔client audit of 2026-09-02: what did not line up, what was fixed here, and what was sent back to the client team |
| [frontend-settings-response.md](frontend-settings-response.md) | The build / defer / remove answer on the settings modal's six sections |

## Not tracked

The requirements as the African Union Commission supplied them:

| File | Size | What it is |
|---|---|---|
| `Ask AU Project .pdf` | 37 MB | Project brief. 9 pages, 56 embedded images |
| `AskAU_SRS_v1.0.docx` | 1.2 MB | Software Requirements Specification — the FR/NFR numbering everything else cites |
| `Ask_AU_Project_Work_Plan.pdf` | 492 KB | Nine-month work plan |

They are `.gitignore`d. Git stores binaries whole and forever; 39 MB of mostly-images
would be paid by every clone in perpetuity, and the only way to remove them afterwards is
a history rewrite that breaks everyone who has already cloned.

**To work with them:** ask the programme team for the originals and drop them in this
directory. Nothing in the build needs them — they are read by people, not by code — so a
fresh clone is fully functional without them.

**If you add a source document,** it is ignored automatically by extension. Do not
`git add -f` it. If a document genuinely must be versioned alongside the code, raise it
as a decision — Git LFS is the mechanism, and it needs platform-team support before the
first file is committed, not after.

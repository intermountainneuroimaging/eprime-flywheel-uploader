# Flywheel Event Log Upload Tool

Sorts, validates, and uploads raw event-log exports (E-Prime `.csv`/`.xlsx` exports,
and optionally raw E-Prime `.txt` logs) to Flywheel, then marks each session's
`COMPLETENESS.Stimulus Complete` flag once all its `func-bold*` acquisitions have
an event log.

Two equivalent versions are included:
- [`flywheel_event_upload.ipynb`](flywheel_event_upload.ipynb) — interactive, step-by-step notebook.
- [`flywheel_event_upload.py`](flywheel_event_upload.py) — command-line script, for repeated/scripted runs.

## 1. Set up a Python environment

Install [Miniconda](https://docs.conda.io/en/latest/miniconda.html) if you don't already have conda, then:

```bash
conda create -n flywheel-upload python=3.10 -y
conda activate flywheel-upload
pip install -r requirements.txt
```

You'll need to `conda activate flywheel-upload` again in any new terminal session before running the script.

### CU Boulder HPC (Blanca / PetaLibrary)

If you're running this on CU Boulder's HPC, use the shared conda environment instead of creating
your own. Follow the INC documentation's instructions for
[setting up conda environments](https://inc-documentation.readthedocs.io/en/latest/pl_and_blanca_basics.html#setting-up-conda-environments),
then:

```bash
module load anaconda
conda activate incenv
pip install -r requirements.txt
```

You'll need to run `module load anaconda` and `conda activate incenv` again in any new job/login
session before running the script.

## 2. Clone the convert-eprime repo (only needed for `--convert-eprime`)

If you have raw E-Prime `.txt` logs (rather than already-exported `.csv`/`.xlsx` files), clone
[tsalo/convert-eprime](https://github.com/tsalo/convert-eprime) as a **sibling directory** to
your source data folder — the script looks for it at `<source_dir's parent>/convert-eprime`:

```bash
cd /path/to/your/project
git clone https://github.com/tsalo/convert-eprime.git
```

For example, if your raw data lives in `/path/to/your/project/raw-data`, the repo should end up at
`/path/to/your/project/convert-eprime`.

## 3. Authenticate with Flywheel

The script uses the Flywheel CLI's cached login rather than a hardcoded API key. Install the
[Flywheel CLI](https://docs.flywheel.io/hc/en-us/articles/360008162214-Installing-the-Flywheel-CLI)
and log in once:

```bash
fw login <your-instance-api-key>
```

(Find your API key in the Flywheel web UI under your profile menu → "Your Profile".) This caches a
session that `flywheel.Client('')` in the script picks up automatically — you shouldn't need to
touch API keys again after this.

## 4. Organize your input data

Put all your raw, unsorted export files in a single flat source directory — no subfolders needed,
the script builds those itself. Filenames need to follow the naming convention it expects:

| File type | Filename pattern | Example |
|---|---|---|
| YEARS task | `V{1\|2}_<run>_<subject>_<date>.csv` | `V1_4_1278_06.09.25.csv` |
| MID task | `MID_Short_Flywheel_<subject>_<date>.csv` | `MID_Short_Flywheel_1587_06.28.26.csv` |
| Raw E-Prime log (optional, needs `--convert-eprime`) | E-Prime's native `.txt` naming | `V1_1_YEARS-1909-1.txt`, `MID_short-1909-1.txt` |

Each file must contain a `Subject` column matching the subject id encoded in its filename — the
script validates this before organizing anything, and flags (rather than silently processes) any
mismatch.

Example layout before running:

```
raw-data/
  V1_1_1278_06.09.25.csv
  V1_2_1278_06.09.25.csv
  MID_Short_Flywheel_1278_06.09.25.csv
  V1_1_YEARS-1909-1.txt        # optional: raw E-Prime log, needs --convert-eprime
```

## 5. Run the script

Basic usage:

```bash
python3 flywheel_event_upload.py <source_dir> [options]
```

**Always test with `--dry-run` first** — it previews every Flywheel upload and metadata write
without actually performing them. Without `--dry-run`, the script performs real uploads and
writes to Flywheel immediately.

| Flag | Description |
|---|---|
| `source_dir` (required) | Path to your unstructured folder of raw exports |
| `--dry-run` | Preview uploads/writes instead of performing them |
| `--convert-eprime` | Convert raw E-Prime `.txt` logs to `.csv` first (step 0) |
| `--cleanup-after-upload` | Delete local organized copies once confirmed on Flywheel (no-op under `--dry-run`) |
| `--project YEARS` | Flywheel project label to operate on (default: `YEARS`) |
| `--subject <id>` | Limit processing to a single subject id (default: all subjects found) |

The script infers two paths from `source_dir` (as siblings of it):
- Organized tree: `<source_dir>_organized`
- convert-eprime repo: `<source_dir's parent>/convert-eprime`

### Example workflow

```bash
conda activate flywheel-upload

# 1. Preview everything first — no files uploaded, no Flywheel metadata changed
python3 flywheel_event_upload.py raw-data --dry-run --convert-eprime

# 2. Review the printed output, then run for real once it looks right
python3 flywheel_event_upload.py raw-data --convert-eprime

# Optional: scope a run to a single subject
python3 flywheel_event_upload.py raw-data --convert-eprime --subject 1909

# Optional: clean up local organized-tree copies once uploads are confirmed
python3 flywheel_event_upload.py raw-data --cleanup-after-upload
```

## Troubleshooting

- **"unrecognized filename pattern"** — the filename doesn't match either naming convention above.
- **"Subject in file does not match subject in filename"** — the file's own `Subject` column
  disagrees with the subject encoded in its filename; both are printed so you can tell which is wrong.
- **"N subjects match ... ambiguous"** — more than one Subject container on Flywheel shares that
  label; the script won't guess which one is correct, so resolve the duplicate in the Flywheel UI first.
- **"subject/session/acquisition not found on Flywheel"** — the corresponding container doesn't
  exist yet; the script won't auto-create it.

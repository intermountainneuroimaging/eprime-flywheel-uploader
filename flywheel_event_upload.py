#!/usr/bin/env python3
"""
Sort, validate, and upload raw event-log exports to Flywheel.

Takes an unstructured folder of raw event-log exports (e.g. `V1_4_1278_06.09.25.csv`,
`MID_Short_Flywheel_1587_06.28.26.csv`, and optionally raw E-Prime `.txt` logs),
validates the `Subject` column inside each file against the subject encoded in its
filename, copies the validated files into an organized subject/session/acquisition
tree, uploads them to the matching Flywheel acquisition (never overwriting an
existing file), and -- once every func-bold* acquisition on a session has an event
log (excluding SBRef acquisitions) -- marks
session.info['COMPLETENESS']['Stimulus Complete'] = True, preserving every other
key already in that object.

See flywheel_event_upload.ipynb for the interactive, step-by-step version this
script was ported from.
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import flywheel
import pandas as pd


# --------------------------------------------------------------------------
# Filename <-> subject/session/acquisition
# --------------------------------------------------------------------------

def parse_filename(filename):
    """Return (subject, session_label, acquisition_label) or None if unrecognized."""
    session_label = 'S1'

    if filename.startswith('MID_Short_Flywheel_'):
        # MID(1)_Short(2)_Flywheel(3)_<subject>(4)_<date>(5...)
        parts = filename.split('_')
        subject = parts[3]
        acquisition = 'func-bold_task-mid_dir-ap_run-01'
        return subject, session_label, acquisition

    if filename.startswith(('V1_', 'V2_')):
        # V{1|2}(1)_<run>(2)_<subject>(3)_<date>(4...) -- subject is the 3rd underscore-delimited field
        parts = filename.split('_')
        run_number = parts[1]
        subject = parts[2]
        run = f'{int(run_number):02d}'
        acquisition = f'func-bold_task-years_dir-ap_run-{run}'
        return subject, session_label, acquisition

    return None


def normalize_id(value):
    """Normalize a subject id so '1587', '1587.0', and 1587 all compare equal."""
    s = str(value).strip()
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except ValueError:
        pass
    return s


# --------------------------------------------------------------------------
# Step 0 (optional): convert raw E-Prime .txt logs to .csv
# --------------------------------------------------------------------------

def get_value_by_key(text, key):
    for line in text.splitlines():
        if line.startswith(f'{key}:'):
            return line.split(':', 1)[1].strip()
    return None


def get_session_date(df, txt_path):
    """Pull the session date from the file's own SessionDate field (MM-DD-YYYY),
    reformatted to the pipeline's MM.DD.YY convention. Falls back to today's date
    if SessionDate is missing/unparseable."""
    if 'SessionDate' in df.columns:
        raw_dates = df['SessionDate'].dropna().unique()
        if len(raw_dates) > 0:
            try:
                return datetime.strptime(str(raw_dates[0]), '%m-%d-%Y').strftime('%m.%d.%y')
            except ValueError:
                print(f'  could not parse SessionDate "{raw_dates[0]}" for {txt_path.name}, using today\'s date')

    print(f'  no usable SessionDate found for {txt_path.name}, using today\'s date')
    return datetime.now().strftime('%m.%d.%y')


def build_converted_filename(txt_path, subject, date_str):
    """Match the naming convention parse_filename() expects
    (V{1|2}_<run>_<subject>_<date>.csv or MID_Short_Flywheel_<subject>_<date>.csv)
    instead of E-Prime's native .txt filename, tagged with _autoconverted so
    these are visibly distinguishable from manual exports."""
    lower_stem = txt_path.stem.lower()

    if lower_stem.startswith(('v1_', 'v2_')):
        parts = txt_path.stem.split('_')
        version, run = parts[0], parts[1]
        base = f'{version}_{run}_{subject}'
    elif lower_stem.startswith('mid'):
        base = f'MID_Short_Flywheel_{subject}'
    else:
        base = txt_path.stem  # unrecognized native naming -- leave as-is rather than guess

    return f'{base}_{date_str}_autoconverted.csv'


def convert_eprime_txt_files(source_dir, convert_eprime_repo_path, subject_filter=None):
    sys.path.insert(0, str(convert_eprime_repo_path))
    from convert_eprime.convert import text_to_csv

    processed_txt_dir = source_dir / 'processed'
    processed_txt_dir.mkdir(parents=True, exist_ok=True)

    txt_files = sorted(source_dir.glob('*.txt'))
    print(f'found {len(txt_files)} eprime .txt file(s) to convert')

    for txt_path in txt_files:
        tmp_csv_path = txt_path.with_suffix('.tmp.csv')

        with open(txt_path, 'r', encoding='utf-16') as f:
            text = f.read()

        text_to_csv(str(txt_path), str(tmp_csv_path))

        df = pd.read_csv(tmp_csv_path)
        df.insert(loc=0, column='ExperimentName', value=get_value_by_key(text, 'Experiment'))

        if 'YEARS' in txt_path.name:
            df = df.rename(columns={'Procedure': 'Procedure[Trial]'})

        if 'Subject' not in df.columns or df['Subject'].dropna().empty:
            print(f'no Subject column found in conversion of {txt_path.name}, leaving .txt in place for review')
            tmp_csv_path.unlink(missing_ok=True)
            continue

        subject = normalize_id(df['Subject'].dropna().iloc[0])

        if subject_filter and subject != subject_filter:
            print(f'skip (subject {subject} does not match --subject {subject_filter}): {txt_path.name}')
            tmp_csv_path.unlink(missing_ok=True)
            continue

        date_str = get_session_date(df, txt_path)
        csv_path = txt_path.parent / build_converted_filename(txt_path, subject, date_str)

        if csv_path.exists():
            print(f'skip (csv already exists): {csv_path.name}')
            tmp_csv_path.unlink(missing_ok=True)
        else:
            df.to_csv(csv_path, index=None)
            tmp_csv_path.unlink(missing_ok=True)
            print(f'converted: {txt_path.name} -> {csv_path.name}')

        processed_txt_path = processed_txt_dir / txt_path.name
        shutil.move(str(txt_path), str(processed_txt_path))
        print(f'  moved {txt_path.name} -> processed/{processed_txt_path.name}')


# --------------------------------------------------------------------------
# Step 2: validate Subject column against filename, copy into organized tree
# --------------------------------------------------------------------------

def find_csv_header_row(path, key_column, encoding='utf-8-sig'):
    """Some exports (e.g. convert_eprime with mismatched LogFrame counts) prepend a
    placeholder header + metadata line before the real header row. Scan for the row
    that actually contains key_column rather than assuming row 0."""
    with open(path, 'r', encoding=encoding, errors='replace') as f:
        for i, line in enumerate(f):
            fields = [c.strip() for c in line.rstrip('\n').split(',')]
            if key_column in fields:
                return i
    return None


def load_subject_values(path):
    key_column = 'Subject'

    if path.suffix.lower() == '.csv':
        header_row = find_csv_header_row(path, key_column)
        if header_row is None:
            raise ValueError(f"no '{key_column}' column found")
        df = pd.read_csv(path, skiprows=header_row, encoding='utf-8-sig')
    else:
        raw = pd.read_excel(path, header=None)
        header_row = next(
            (i for i, row in raw.iterrows() if key_column in row.astype(str).str.strip().values),
            None,
        )
        if header_row is None:
            raise ValueError(f"no '{key_column}' column found")
        df = pd.read_excel(path, header=header_row)

    if key_column not in df.columns:
        raise ValueError(f"no '{key_column}' column found")
    return sorted({normalize_id(v) for v in df[key_column].dropna()})


def validate_and_organize(source_dir, organized_dir, subject_filter=None):
    organized = []
    flagged = []

    for path in sorted(source_dir.iterdir()):
        if not path.is_file() or path.name.startswith('.'):
            continue

        parsed = parse_filename(path.name)
        if parsed is None:
            flagged.append({'filename': path.name, 'reason': 'unrecognized filename pattern'})
            continue

        subject_from_name, session_label, acquisition_label = parsed

        if subject_filter and normalize_id(subject_from_name) != subject_filter:
            continue

        try:
            subject_values = load_subject_values(path)
        except Exception as e:
            flagged.append({'filename': path.name, 'reason': f'could not read Subject column: {e}'})
            continue

        if len(subject_values) != 1:
            flagged.append({
                'filename': path.name,
                'reason': f'expected exactly one Subject value in file, found {subject_values}',
            })
            continue

        subject_in_file = subject_values[0]
        if subject_in_file != normalize_id(subject_from_name):
            flagged.append({
                'filename': path.name,
                'reason': f'Subject in file ("{subject_in_file}") does not match subject in filename ("{subject_from_name}")',
            })
            continue

        dest_dir = organized_dir / subject_from_name / session_label / acquisition_label
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / path.name
        shutil.copy2(str(path), str(dest_path))

        organized.append({
            'local_path': dest_path,
            'subject': subject_from_name,
            'session': session_label,
            'acquisition': acquisition_label,
            'filename': path.name,
        })

    print(f'organized {len(organized)} file(s), flagged {len(flagged)} file(s) for review')
    for item in flagged:
        print(f"  FLAGGED: {item['filename']} - {item['reason']}")

    return organized, flagged


# --------------------------------------------------------------------------
# Step 3: upload organized files to Flywheel (never overwrite)
# --------------------------------------------------------------------------

def find_subjects(project, subject_id):
    """Query subjects by label. Numeric-looking labels (e.g. '1587') must be
    quoted in the filter string or Flywheel's query parser treats them as a
    number/regex and silently matches nothing against the string field."""
    subject_id = str(subject_id).strip()
    matches = {s.id: s for s in project.subjects.find(f'label="{subject_id}"')}
    return list(matches.values())


def iter_organized_files(organized_dir, subject_filter=None):
    """Walk ORGANIZED_DIR/<subject>/<session>/<acquisition>/<file>, regardless of
    whether the file was copied there by this run or a previous one."""
    for path in sorted(organized_dir.glob('*/*/*/*')):
        if not path.is_file():
            continue
        subject = path.parent.parent.parent.name
        if subject_filter and normalize_id(subject) != subject_filter:
            continue
        yield {
            'local_path': path,
            'subject': subject,
            'session': path.parent.parent.name,
            'acquisition': path.parent.name,
            'filename': path.name,
        }


def upload_organized_files(project, organized_dir, dry_run, subject_filter=None):
    upload_candidates = list(iter_organized_files(organized_dir, subject_filter))
    print(f'found {len(upload_candidates)} organized file(s) to check against Flywheel')

    uploaded = []
    upload_skipped = []
    upload_errors = []

    for item in upload_candidates:
        subject_matches = find_subjects(project, item['subject'])

        if len(subject_matches) == 0:
            upload_errors.append({**item, 'reason': 'subject not found on Flywheel'})
            continue
        if len(subject_matches) > 1:
            ids = [s.id for s in subject_matches]
            upload_errors.append({
                **item,
                'reason': f'{len(subject_matches)} subjects match "{item["subject"]}" (ids: {ids}) - ambiguous, needs manual resolution',
            })
            continue

        subject = subject_matches[0]

        session = subject.sessions.find_one(f"label={item['session']}")
        if session is None:
            upload_errors.append({**item, 'reason': 'session not found on Flywheel'})
            continue

        acquisition = session.acquisitions.find_one(f"label={item['acquisition']}")
        if acquisition is None:
            upload_errors.append({**item, 'reason': 'acquisition not found on Flywheel'})
            continue

        acquisition = acquisition.reload()
        existing_names = {f.name for f in acquisition.files}

        label_path = f"{item['subject']}/{item['session']}/{item['acquisition']}/{item['filename']}"

        if item['filename'] in existing_names:
            upload_skipped.append(item)
            print(f'skip (already exists): {label_path}')
            continue

        if dry_run:
            print(f'[dry run] would upload: {label_path}')
        else:
            acquisition.upload_file(str(item['local_path']))
            print(f'uploaded: {label_path}')

        uploaded.append(item)

    print(f"\n{len(uploaded)} file(s) uploaded/would-upload, {len(upload_skipped)} skipped (already present), "
          f"{len(upload_errors)} error(s)")

    return upload_candidates, uploaded, upload_skipped, upload_errors


# --------------------------------------------------------------------------
# Step 3b (optional): clean up local organized copies after successful upload
# --------------------------------------------------------------------------

def cleanup_after_upload(organized_dir, uploaded, upload_skipped, dry_run, cleanup_enabled):
    cleaned_up = []

    if cleanup_enabled and dry_run:
        print('--cleanup-after-upload was set but --dry-run is also set -- skipping cleanup '
              '(nothing was actually confirmed uploaded this run)')
    elif cleanup_enabled:
        for item in uploaded + upload_skipped:
            local_path = item['local_path']
            local_path.unlink(missing_ok=True)
            cleaned_up.append(item)

            # prune now-empty parent directories, but never remove organized_dir itself
            parent = local_path.parent
            while parent != organized_dir and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent

        print(f'removed {len(cleaned_up)} local file(s) from {organized_dir} (confirmed present on Flywheel)')
    else:
        print('--cleanup-after-upload not set, leaving organized copies in place')

    return cleaned_up


# --------------------------------------------------------------------------
# Step 4: mark Stimulus Complete once every func-bold acquisition has an event log
# --------------------------------------------------------------------------

def has_event_log(acquisition):
    for f in acquisition.files:
        name_lower = f.name.lower()
        if 'event' in name_lower or name_lower.startswith(('mid_short_flywheel', 'v1_', 'v2_')):
            return True
    return False


def get_func_bold_acquisitions(session):
    return [
        a.reload() for a in session.acquisitions.iter()
        if a.label.startswith('func-bold') and not a.label.endswith('SBRef')
    ]


def update_completeness(project, upload_candidates, dry_run):
    touched_sessions = sorted({(item['subject'], item['session']) for item in upload_candidates})
    completeness_updated = []
    completeness_already_complete = []
    completeness_errors = []

    for subject_label, session_label in touched_sessions:
        subject_matches = find_subjects(project, subject_label)
        if len(subject_matches) != 1:
            completeness_errors.append((subject_label, session_label, f'{len(subject_matches)} matching subject(s)'))
            continue
        subject = subject_matches[0]

        session = subject.sessions.find_one(f'label={session_label}')
        if session is None:
            continue

        session = session.reload()
        func_bold_acqs = get_func_bold_acquisitions(session)

        if not func_bold_acqs or not all(has_event_log(a) for a in func_bold_acqs):
            continue

        completeness = dict(session.info.get('COMPLETENESS', {}))
        if completeness.get('Stimulus Complete') is True:
            print(f'already marked Stimulus Complete, nothing to do: {subject_label}/{session_label}')
            completeness_already_complete.append((subject_label, session_label))
            continue

        completeness['Stimulus Complete'] = True

        if dry_run:
            print(f'[dry run] would mark Stimulus Complete: {subject_label}/{session_label}')
        else:
            session.update_info({'COMPLETENESS': completeness})
            print(f'marked Stimulus Complete: {subject_label}/{session_label}')

        completeness_updated.append((subject_label, session_label))

    print(f'\n{len(completeness_updated)} session(s) marked Stimulus Complete (or would be, in dry run).')
    print(f'{len(completeness_already_complete)} session(s) already had Stimulus Complete set.')
    if completeness_errors:
        print('\nSkipped due to ambiguous/missing subject:')
        for subject_label, session_label, reason in completeness_errors:
            print(f'  {subject_label}/{session_label}: {reason}')

    return completeness_updated, completeness_already_complete, completeness_errors


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description='Sort, validate, and upload raw event-log exports to Flywheel.',
    )
    parser.add_argument(
        'source_dir', type=Path,
        help='Path to the unstructured source directory of raw event-log exports.',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Preview the Flywheel upload, COMPLETENESS write, and cleanup without actually performing them.',
    )
    parser.add_argument(
        '--convert-eprime', action='store_true',
        help='First convert raw E-Prime .txt logs found in source-dir to .csv (see step 0).',
    )
    parser.add_argument(
        '--cleanup-after-upload', action='store_true',
        help='Delete local organized-dir copies once confirmed present on Flywheel. No-op under --dry-run.',
    )
    parser.add_argument(
        '--project', default='YEARS',
        help='Flywheel project label to operate on (default: YEARS).',
    )
    parser.add_argument(
        '--subject', default=None,
        help='Limit processing to a single subject id (default: process all subjects found).',
    )
    return parser.parse_args()


def main():
    args = parse_args()

    source_dir = args.source_dir.resolve()
    organized_dir = source_dir.parent / f'{source_dir.name}_organized'
    convert_eprime_repo_path = source_dir.parent / 'convert-eprime'
    subject_filter = normalize_id(args.subject) if args.subject else None

    print(f'source_dir:             {source_dir}')
    print(f'organized_dir:          {organized_dir}')
    print(f'convert_eprime_repo:    {convert_eprime_repo_path}')
    print(f'dry_run:                {args.dry_run}')
    print(f'convert_eprime:         {args.convert_eprime}')
    print(f'cleanup_after_upload:   {args.cleanup_after_upload}')
    print(f'project:                {args.project}')
    print(f'subject filter:         {subject_filter or "(all subjects)"}')
    print()

    fw = flywheel.Client('')
    project = fw.projects.find_one(f'label={args.project}')
    if project is None:
        print(f'ERROR: no project found with label "{args.project}"')
        sys.exit(1)
    print('project found:', project.label)

    if args.convert_eprime:
        print('\n=== Step 0: convert raw E-Prime .txt logs ===')
        convert_eprime_txt_files(source_dir, convert_eprime_repo_path, subject_filter)

    print('\n=== Step 2: validate Subject column, organize locally ===')
    organized, flagged = validate_and_organize(source_dir, organized_dir, subject_filter)

    print('\n=== Step 3: upload organized files to Flywheel ===')
    upload_candidates, uploaded, upload_skipped, upload_errors = upload_organized_files(
        project, organized_dir, args.dry_run, subject_filter,
    )

    print('\n=== Step 3b: clean up local organized copies ===')
    cleaned_up = cleanup_after_upload(
        organized_dir, uploaded, upload_skipped, args.dry_run, args.cleanup_after_upload,
    )

    print('\n=== Step 4: mark Stimulus Complete where applicable ===')
    completeness_updated, completeness_already_complete, completeness_errors = update_completeness(
        project, upload_candidates, args.dry_run,
    )

    print('\n=== Summary ===')
    print(f'Organized (copied this run):        {len(organized)}')
    print(f'Flagged (validation issues):        {len(flagged)}')
    print(f'Uploaded (or dry-run preview):      {len(uploaded)}')
    print(f'Skipped (already present):          {len(upload_skipped)}')
    print(f'Upload errors (not on Flywheel):    {len(upload_errors)}')
    print(f'Local copies cleaned up:            {len(cleaned_up)}')
    print(f'Sessions marked Stimulus Complete:  {len(completeness_updated)}')
    print(f'Sessions already Stimulus Complete: {len(completeness_already_complete)}')

    if flagged:
        print('\nFiles needing manual review:')
        for item in flagged:
            print(f"  {item['filename']}: {item['reason']}")

    if upload_errors:
        print('\nUpload errors:')
        for item in upload_errors:
            print(f"  {item['subject']}/{item['session']}/{item['acquisition']}/{item['filename']}: {item['reason']}")


if __name__ == '__main__':
    main()

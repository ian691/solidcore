#!/usr/bin/env python3
"""Export authenticated, bounded supplemental-run evidence. Never build or run tests.

SPEC_JSON pins completed run, control, candidate, exact base, artifact and digests.
Run from the exact-base checkout, using only its trusted baseline verifier.
"""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from collections import Counter
from datetime import datetime, timezone
import zipfile

REPO = 'ian691/solidcore'
CHECKS = ['fmt', 'clippy', 'clippy-cpp', 'wasm', 'kernel', 'workspace',
          'runner-cpp', 'sweep', 'perturb']
CPP = 'c9c5979f04cea8c38213004d4f04df0dcfb40d05'
MAX_ZIP = 256 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_digest(blob, expected):
    require('sha256:' + sha(blob) == expected, 'ZIP digest mismatch')


def api(path, binary=False):
    data = subprocess.check_output(['gh', 'api', '--hostname', 'github.com',
                                    '--method', 'GET', path], timeout=180)
    return data if binary else json.loads(data)


def safe_members(blob):
    """No extraction to disk; all paths, sizes, duplicates and file kinds checked."""
    require(len(blob) <= MAX_ZIP, 'candidate ZIP too large')
    result = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        members = archive.infolist()
        require(len(members) <= 10000, 'too many ZIP members')
        require(sum(i.file_size for i in members) <= MAX_EXPANDED, 'ZIP expansion limit')
        seen = set()
        for item in members:
            p = PurePosixPath(item.filename)
            require(item.filename not in seen, 'duplicate ZIP member')
            seen.add(item.filename)
            require(not p.is_absolute() and '..' not in p.parts and '\\' not in item.filename,
                    'unsafe ZIP path')
            require(not stat.S_ISLNK(item.external_attr >> 16), 'symlink ZIP member')
            require(item.file_size <= 64 * 1024 * 1024, 'ZIP member too large')
            if item.is_dir():
                continue
            require(stat.S_IFMT(item.external_attr >> 16) in (0, stat.S_IFREG),
                    'special ZIP member')
            result[item.filename] = archive.read(item)
    return result


def text(files, path):
    require(path in files, 'missing evidence: ' + path)
    return files[path].decode('utf-8')


def report_map(files, prefix, verifier):
    result = {}
    for path, data in files.items():
        if path.startswith(prefix) and path.endswith('.json'):
            unit = path[len(prefix):-5]
            require('/' not in unit, 'nested report path')
            result[unit] = json.loads(data, object_pairs_hook=verifier.unique_object)
    return result


def log_audit(files, output, spec=None):
    statuses = [line.split('\t') for line in text(files, 'exit-codes.tsv').splitlines()]
    require([p[0] for p in statuses] == CHECKS, 'missing, duplicate or reordered command status')
    checks = {}
    pattern = re.compile(r'test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; '
                         r'(\d+) ignored; (\d+) measured; (\d+) filtered out;')
    focus = re.compile(r'warning|error|FAILED|panicked|test result:|^difftest:|'
                       r'plane.*(?:recogn|origin|nurb)|full.turn|'
                       r'issue_1054|interval_knot_fix_is_opt_in|'
                       r'degree_one_interval_keeps_interior_extremum|interval_end_knots_are_covered|'
                       r'ellipse_face_heal_cache_compares_values|'
                       r'vertex_fixtures_compare_actual_caps_not_only_presence', re.I)
    for label, code in statuses:
        data = text(files, 'logs/' + label + '.log')
        sections = pattern.findall(data)
        counts = dict(zip(['passed', 'failed', 'ignored', 'measured', 'filtered'],
                          [sum(int(s[i]) for s in sections) for i in range(5)]))
        lines = [line for line in data.splitlines() if focus.search(line)]
        checks[label] = {'exit_code': int(code), 'sha256': sha(data.encode()),
                         'test_sections': len(sections), 'test_counts': counts,
                         'raw_evidence_lines': lines}
        print('RAW_LOG_BEGIN ' + label)
        for line in lines:
            print(line)
        print('RAW_LOG_END ' + label)
        if int(code) != 0:
            # Preserve assertion values and formatting diffs, not just FAILED headings.
            # The complete original artifact stays retained and is SHA256 identified.
            limit = 1024 * 1024
            encoded = data.encode()
            print('FAILED_COMMAND_LOG_BEGIN ' + label)
            if len(encoded) <= limit:
                print(data)
            else:
                print(encoded[:limit // 2].decode('utf-8', errors='replace'))
                print('EXPLICIT_TRUNCATION: middle omitted; complete artifact/log SHA256=' + sha(encoded))
                print(encoded[-limit // 2:].decode('utf-8', errors='replace'))
            print('FAILED_COMMAND_LOG_END ' + label)
    output['checks'] = checks
    output['all_nine_passed'] = all(c['exit_code'] == 0 for c in checks.values())
    if spec is not None:
        focused = {}
        for label, names in spec['required_tests_by_log'].items():
            require(label in CHECKS and names, 'invalid focused test log selector')
            data = text(files, 'logs/' + label + '.log')
            for name in names:
                pattern = re.compile(r'^test (?:\S*::)?' + re.escape(name) + r' \.\.\. ok$', re.M)
                lines = pattern.findall(data)
                focused[label + ':' + name] = lines
                for line in lines:
                    print('FOCUSED_PASS ' + label + ' ' + line)
        output['focused_pass_lines'] = focused
        if output['all_nine_passed']:
            require(all(focused.values()), 'expected focused test did not print a passing line')


def native_audit(files, baseline_files, verifier, expected, output):
    baseline = report_map(files, 'baseline/reports/', verifier)
    current = report_map(files, 'candidate-reports/', verifier)
    verifier.validate_set(baseline, expected)
    validated = verifier.validate_set(current, expected)
    require(all(r['floor_ok'] for r in validated.values()), 'candidate value-share floor')
    for unit in expected:
        name = unit + '.json'
        require(files['baseline/reports/' + name] == baseline_files[name],
                'embedded baseline differs from authentic ZIP: ' + unit)
    violations = verifier.ratchet_reports(baseline, current)
    counts = Counter()
    outcomes = Counter()
    statuses = Counter()
    differences = []
    exceptions = []
    units = {}
    signatures = hashlib.sha256()
    for unit in sorted(expected):
        old, new = baseline[unit], current[unit]
        statuses[new['status']] += 1
        units[unit] = {key: value for key, value in new.items() if key != 'cases'}
        units[unit]['substantive_passes'] = sum(
            c['outcome'] == 'pass' and verifier.case_has_values(new, c) for c in new['cases'])
        for key, value in new.items():
            if key.startswith('cases_') and type(value) is int:
                counts[key] += value
        settings = ['seeds', 'rel_tol', 'abs_floors', 'schema_version']
        for key in settings:
            if old.get(key) != new.get(key):
                differences.append([unit, None, key, old.get(key), new.get(key)])
        before = {c['name']: c for c in old['cases']}
        now = {c['name']: c for c in new['cases']}
        if set(before) != set(now):
            differences.append([unit, None, 'case_set_changed'])
        keys = ['name', 'source', 'outcome', 'pass', 'values_compared', 'exact_compared',
                'both_errored', 'vacuous', 'only_zeros']
        for name in sorted(now):
            case = now[name]
            outcomes[case['outcome']] += 1
            signature = {key: case.get(key) for key in keys}
            signatures.update(json.dumps([unit, signature], sort_keys=True).encode() + b'\n')
            if name in before:
                for key in keys:
                    if before[name].get(key) != case.get(key):
                        differences.append([unit, name, key, before[name].get(key), case.get(key)])
            if case['outcome'] not in ('pass', 'rejected'):
                exceptions.append({'unit': unit, 'case': name, 'outcome': case['outcome'],
                                   'mismatches': case.get('mismatches')})
    perturb = re.findall(r'^SENSITIVE (\S+) perturbed=(\d+) detected=(\d+) '
                         r'unperturbed=(\d+) deferred=(\d+)',
                         text(files, 'logs/perturb.log'), re.M)
    require(len(perturb) == len(expected) and {r[0] for r in perturb} == set(expected),
            'missing or duplicate perturb units')
    perturb_counts = dict(zip(['perturbed', 'detected', 'unperturbed', 'deferred'],
                             [sum(int(r[i]) for r in perturb) for i in range(1, 5)]))
    require(all(int(r[1]) == int(r[2]) for r in perturb), 'undetected perturbation')
    output['native'] = {'units': len(expected), 'statuses': dict(statuses),
                        'case_totals': dict(counts), 'outcomes': dict(outcomes),
                        'ratchet_violations': violations, 'baseline_signature_differences': differences,
                        'candidate_case_signature_sha256': signatures.hexdigest(),
                        'exceptions': exceptions, 'perturb': perturb_counts,
                        'unit_summaries': units,
                        'embedded_baseline_byte_match': True,
                        'all_value_share_floors_pass': True}
    require(not violations, 'exact-base coverage ratchet failed')


def audit(spec, verifier):
    for key in ['control', 'candidate', 'base']:
        require(re.fullmatch('[0-9a-f]{40}', spec[key]), 'full SHA required: ' + key)
    run = api(f'repos/{REPO}/actions/runs/{int(spec["run"])}')
    require(run['repository']['full_name'] == REPO and run['head_repository']['full_name'] == REPO,
            'candidate run repository mismatch')
    require(run['head_sha'] == spec['control'] and run['head_branch'] == spec['branch']
            and run['event'] == 'push' and run['run_attempt'] == spec['attempt']
            and run['path'] == spec['workflow'] and run['status'] == 'completed',
            'run/control/branch/workflow/attempt incomplete or mismatched')
    workflow = api(f'repos/{REPO}/contents/{spec["workflow"]}?ref={spec["control"]}')
    workflow_bytes = base64.b64decode(workflow['content'])
    require(sha(workflow_bytes) == spec['workflow_sha256'], 'reviewed source workflow hash mismatch')
    require(CPP.encode() in workflow_bytes and spec['candidate'].encode() in workflow_bytes,
            'source workflow reference/candidate pins missing')
    artifacts = api(f'repos/{REPO}/actions/runs/{run["id"]}/artifacts?per_page=100')['artifacts']
    choices = [a for a in artifacts if a['name'] == spec['artifact_name']]
    require(len(choices) == 1, 'ambiguous candidate artifact')
    artifact = api(f'repos/{REPO}/actions/artifacts/{int(spec["artifact"])}')
    require(choices[0]['id'] == artifact['id'] and artifact['digest'] == spec['digest']
            and not artifact['expired'] and artifact['size_in_bytes'] <= MAX_ZIP,
            'candidate artifact identity/digest/size/expiry')
    require(datetime.fromisoformat(artifact['expires_at'].replace('Z', '+00:00')) > datetime.now(timezone.utc),
            'candidate artifact expired')
    owner = artifact['workflow_run']
    require(owner['id'] == run['id'] and owner['head_sha'] == spec['control']
            and owner['head_branch'] == spec['branch']
            and owner['repository_id'] == run['repository']['id']
            and owner['head_repository_id'] == run['head_repository']['id'],
            'artifact run ownership mismatch')
    require(datetime.fromisoformat(artifact['created_at'].replace('Z', '+00:00')) >=
            datetime.fromisoformat(run['run_started_at'].replace('Z', '+00:00')),
            'artifact predates run attempt')
    blob = api(f'repos/{REPO}/actions/artifacts/{artifact["id"]}/zip', binary=True)
    verify_digest(blob, spec['digest'])
    files = safe_members(blob)
    after_download = api(f'repos/{REPO}/actions/runs/{run["id"]}')
    require(after_download['run_attempt'] == spec['attempt']
            and after_download['head_sha'] == spec['control'], 'run identity changed during download')
    require(text(files, 'head.txt').strip() == spec['candidate'], 'candidate source mismatch')
    require(text(files, 'control-checkout-head.txt').strip() == spec['control'], 'control checkout mismatch')
    require(not text(files, 'status.txt').strip() and files['candidate.patch'] == b'',
            'candidate working tree dirty')
    require(text(files, 'candidate-patch.sha256').split()[0] == sha(b''),
            'candidate empty-patch hash receipt mismatch')
    receipt = dict(line.split('=', 1) for line in text(files, 'control-receipt.txt').splitlines())
    require(receipt == {'control_sha': spec['control'], 'control_ref': 'refs/heads/' + spec['branch'],
                        'run_id': str(run['id']), 'run_attempt': str(spec['attempt'])}, 'control receipt mismatch')
    tool_lines = text(files, 'validation-tools.sha256').splitlines()
    tools = dict((line.split()[1].rsplit('/', 1)[-1], line.split()[0])
                 for line in tool_lines)
    require(len(tool_lines) == len(tools) == len(spec['helper_hashes']),
            'duplicate or missing helper hash receipts')
    require(tools == spec['helper_hashes'], 'reviewed helper hashes mismatch')
    require('Linux' in text(files, 'platform.txt') and 'x86_64' in text(files, 'platform.txt'),
            'unsupported candidate platform')
    baseline_run, baseline_artifact = verifier.fetch_exact(verifier.GitHub(), spec['base'], 0)
    require(baseline_run['id'] == spec['baseline_run']
            and baseline_artifact['id'] == spec['baseline_artifact']
            and baseline_artifact['digest'] == spec['baseline_digest'], 'baseline identity drift')
    baseline_detail = api(f'repos/{verifier.REPOSITORY}/actions/artifacts/{baseline_artifact["id"]}')
    verifier.select_artifact([baseline_detail], baseline_run, spec['base'], datetime.now(timezone.utc))
    require(baseline_detail['id'] == baseline_artifact['id']
            and baseline_detail['digest'] == baseline_artifact['digest'],
            'baseline artifact identity changed before download')
    baseline_blob = api(f'repos/{verifier.REPOSITORY}/actions/artifacts/{baseline_artifact["id"]}/zip', binary=True)
    current_baseline_run = verifier.select_run(
        [api(f'repos/{verifier.REPOSITORY}/actions/runs/{baseline_run["id"]}')],
        spec['base'], verifier.REPOSITORY, verifier.WORKFLOW_ID)
    require(current_baseline_run.get('run_attempt', 1) == baseline_run.get('run_attempt', 1),
            'baseline run attempt changed during download')
    provenance = json.loads(text(files, 'baseline/provenance.json'))
    baseline_receipt = json.loads(text(files, 'baseline-receipt.json'))
    require(all(provenance.get(key) == value for key, value in baseline_receipt.items()),
            'baseline receipt/provenance disagree')
    require(provenance['base_sha'] == spec['base'] and provenance['source_base_sha'] == spec['base']
            and provenance['run_id'] == baseline_run['id'] and provenance['artifact_id'] == baseline_artifact['id']
            and provenance['digest'] == spec['baseline_digest']
            and provenance['repository'] == verifier.REPOSITORY
            and provenance['repository_id'] == verifier.REPOSITORY_ID
            and provenance['workflow_id'] == verifier.WORKFLOW_ID
            and provenance['workflow_path'] == verifier.WORKFLOW_PATH
            and provenance['run_event'] == 'push' and provenance['run_branch'] == 'main'
            and provenance['run_head_sha'] == spec['base'], 'embedded baseline provenance mismatch')
    expected = verifier.base_units(spec['base'])
    out = Path('audit-output') / str(run['id'])
    out.mkdir(parents=True)
    verifier.install_zip(baseline_blob, out / 'authenticated-baseline', expected, provenance)
    with zipfile.ZipFile(io.BytesIO(baseline_blob)) as archive:
        baseline_files = {name: archive.read(name) for name in archive.namelist() if name.endswith('.json')}
    output = {'spec': spec, 'run_conclusion': run['conclusion'], 'artifact_digest_verified': True,
              'baseline_digest_verified': True, 'baseline_provenance': provenance,
              'source_receipts': {p: text(files, p) for p in ['head.txt', 'status.txt',
                  'candidate-patch.sha256', 'control-receipt.txt', 'platform.txt', 'rustc.txt',
                  'cpp-libraries.sha256', 'validation-tools.sha256']},
              'all_file_sha256': {p: sha(v) for p, v in sorted(files.items())}}
    cpp_setup = text(files, 'cpp-setup.log')
    require(CPP in cpp_setup, 'exact C++ reference fetch absent from setup log')
    print('CPP_SETUP_RAW_BEGIN')
    print(cpp_setup)
    print('CPP_SETUP_RAW_END')
    try:
        log_audit(files, output, spec)
        native_audit(files, baseline_files, verifier, expected, output)
    except Exception as error:
        output['audit_error'] = str(error)
        raise
    finally:
        summary = json.dumps(output, sort_keys=True)
        (out / 'audit.json').write_text(summary + '\n')
        print('AUDIT_JSON_BEGIN')
        print(summary)
        print('AUDIT_JSON_END')
    require(output['all_nine_passed'], 'one or more commands failed; evidence retained')
    require(run['conclusion'] == 'success', 'candidate run was not successful')


def main():
    spec = json.loads(os.environ['SPEC_JSON'])
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    require(head == spec['base'], 'audit must import exact-base verifier')
    path = Path('scripts/ci/difftest_baseline.py')
    require(sha(path.read_bytes()) == spec['verifier_sha256'], 'trusted verifier bytes differ')
    module = importlib.util.spec_from_file_location('trusted_baseline', path)
    verifier = importlib.util.module_from_spec(module)
    module.loader.exec_module(verifier)
    audit(spec, verifier)


if __name__ == '__main__':
    main()

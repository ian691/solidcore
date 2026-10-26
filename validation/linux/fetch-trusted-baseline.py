#!/usr/bin/env python3
"""Fetch an independently validated baseline for local/fork Linux validation.
No synthetic GitHub event: this is supplemental validation, not upstream CI.
Run from an exact-base candidate checkout; output must not already exist.
"""
import importlib.util
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

BASE = '002754f4ea652fb864527029a67ee9dcfeac01c6'
root = pathlib.Path.cwd()
subprocess.run(['git', 'merge-base', '--is-ancestor', BASE, 'HEAD'], check=True)
module_path = root / 'scripts/ci/difftest_baseline.py'
# Import only the trusted exact-base implementation, not candidate modifications.
expected = subprocess.check_output(['git', 'show', BASE + ':scripts/ci/difftest_baseline.py'])
if module_path.read_bytes() != expected:
    raise SystemExit('baseline verifier differs from trusted base')
spec = importlib.util.spec_from_file_location('trusted_baseline', module_path)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
api = b.GitHub()
run, artifact = b.fetch_exact(api, BASE, 0)
units = b.base_units(BASE)
b.validate_current_selection(units, root)
provenance = dict(repository=b.REPOSITORY, repository_id=b.REPOSITORY_ID,
    workflow_id=b.WORKFLOW_ID, workflow_path=b.WORKFLOW_PATH,
    source_event='supplemental-linux-validation', source_base_sha=BASE,
    run_event=run['event'], run_branch=run['head_branch'], run_head_sha=run['head_sha'],
    base_sha=BASE, run_id=run['id'], run_attempt=run.get('run_attempt', 1),
    artifact_id=artifact['id'], digest=artifact['digest'],
    selection_policy='exact-base green/partial numeric units; audit.recursion excluded only when declared by exact-base selector')
if len(sys.argv) == 2 and sys.argv[1] == '--metadata-only':
    print(json.dumps(provenance, indent=2))
    raise SystemExit(0)
if len(sys.argv) != 2:
    raise SystemExit('usage: fetch-trusted-baseline.py NEW_OUTPUT_DIRECTORY | --metadata-only')
detail = api.json(f"repos/{b.REPOSITORY}/actions/artifacts/{artifact['id']}")
b.select_artifact([detail], run, BASE, datetime.now(timezone.utc))
if detail.get('id') != artifact['id'] or detail.get('digest') != artifact['digest']:
    raise SystemExit('artifact identity changed')
blob = api.raw(f"repos/{b.REPOSITORY}/actions/artifacts/{artifact['id']}/zip")
current = b.select_run([api.json(f"repos/{b.REPOSITORY}/actions/runs/{run['id']}")], BASE, b.REPOSITORY, b.WORKFLOW_ID)
if current.get('run_attempt', 1) != run.get('run_attempt', 1):
    raise SystemExit('run attempt changed')
b.install_zip(blob, sys.argv[1], units, provenance)
print(json.dumps(provenance, indent=2))

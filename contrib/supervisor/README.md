# Optional supervised recovery

This Linux companion packages the durable orchestration used around a long-running
GNHF campaign. It is outer automation, separate from the native iteration loop.
Project hooks own acceptance criteria, source preservation and verification. It
does not review, merge, push, or open PRs.

```text
main GNHF attempt
  healthy -> keep running; repair stays dormant
  complete -> project completion hook -> completed
  failed/aborted -> isolated repair -> regression + frozen project verification
    verified -> journaled installation -> next GNHF attempt
    rejected -> bounded repair retry -> recovery_blocked
  operator stop / token exhaustion -> stop without repair
```

The native fixes in this PR send Codex prompts through stdin and add
`GNHF_CONFIG_DIR`. The companion needs Python 3.10+, Linux `/proc`, a working
`bubblewrap` installation with network namespaces, Node 20+, GNHF and Codex. It
adds no runtime dependency to native GNHF and does not change its platform support.

## Prepare a project

From a source checkout, run:

```sh
python3 contrib/supervisor/supervisor.py scaffold /absolute/path/to/new-run-adapter
```

Setup is offline. It creates a new directory and refuses to overwrite one. Read
and adapt `config.json` before starting. Select the intended project directory,
native `.gnhf/runs` directory, objective, repairable source files, synthetic
fixtures, and trusted verification scripts. Run GNHF on a prepared task branch or
worktree. Leave the active production controller and its worktree intact when
testing a new setup.

Set `prepared` to `true` only after configuring the adapter; `watch` refuses the
unprepared scaffold before admitting any model call. The generated `verify.py`
and `complete.py` deliberately refuse to pass until
you implement the project's checks. Verification must run the native validator,
check exact source identities and retained content, and check all completed
artifacts. Model-written tests supplement these frozen checks. Include only
explicitly reviewed synthetic fixtures in `fixture_files`; no automatic redactor
can prove that a real document or model response is safe to share.

`repair_files` maps paths inside the sanitized candidate to existing project
files. `test_files` identifies the regression tests. `trusted_files` are copied
once, before the first main run, into the private controller directory. Neither
trusted scripts nor fixtures can be installed by repair. Commands are argv lists,
never shell strings; `{project}`, `{state_dir}`, `{trusted}`, `{candidate}` and
`{max_tokens}` are the only substitutions. Use absolute executable paths where
needed. Keep the lexical path to a virtualenv interpreter; resolving its final
symlink can silently leave that environment.

The default profiles match the campaign: Luna medium for GNHF, Sol 6.1 high for
repair, 500 million total tokens and a cumulative 50 million repair reserve.
Coding and project jobs share the other 450 million. Defaults reserve 10 million
per main attempt and 5 million per repair, leaving room for concurrent jobs.
These are explicit allocations, not a promise that a provider call can be cut
off at an exact token. Usage is charged after each reported turn; an indivisible
turn can exceed its reservation, in which case further work blocks. Counts use
Codex `input_tokens + output_tokens`, including cached input exactly once.

```sh
python3 contrib/supervisor/supervisor.py watch /absolute/path/to/config.json
python3 contrib/supervisor/supervisor.py status /absolute/path/to/config.json
python3 contrib/supervisor/supervisor.py stop /absolute/path/to/config.json
```

Run `watch` under your service manager or terminal multiplexer. The controller
checkpoints its lifecycle and holds a single-owner lock. Restart `watch` with
the same config and state directory after a controller crash. Detached workers
retain their exact admission identity and durable result. Linux boot identity
and process start ticks prevent attaching to an unrelated reused PID. An
ambiguous admission or vanished worker blocks for reconciliation instead of
starting another model call. An operator stop persists across restart.

Native aborts take precedence over exit code zero. Each attempt filters native
events by their embedded timestamp; appending to a resumed log never revives an
old abort. An abort followed by `orchestrator:end` quiesces the owned child group
even if the TUI remains alive. A clean exit without current terminal evidence,
or without passing project completion, is a failure. Healthy polling never
admits repair and there is no model-based preflight.

## Record and reconcile expensive work

`storage.WorkQueue` binds immutable requests to a source/protocol identifier,
claims a stable execution identity, and records delivery idempotently. Reopen
the queue to refresh its frontier before every iteration. `jobs.run_job` combines
that queue with the shared budget and detached execution recorder. It saves the
exact request and effective command before invocation and returns a private
result receipt. Calling it again reconciles that result without repeating work.
The project must validate the delivered result before accepting an artifact.

```sh
python3 contrib/supervisor/jobs.py /absolute/path/to/config.json task-001 request.json \
  --max-tokens 100000 -- codex exec --model gpt-6-luna --json -
```

The command receives the request JSON through stdin. A platform-specific history
recorder can wrap this invocation boundary; this companion does not embed a
particular experiment store. The optional `bridge.py` is installed into the
generated GNHF config as a Codex wrapper. It refreshes the queue frontier,
streams token events, and rejects a claimed task success when no delivery
completed. It caps prepared prompts at 900,000 characters by default. Partition
oversized work while preserving all source identities and evidence; it never
truncates inputs. Project adapters own lossless packing and domain repair rules.

## Recovery boundaries

Repair receives an allowlist of source files, synthetic fixtures and a
host-authored failure category. Raw native logs, private config and model
responses are not copied into its workspace. The generated Codex command uses
`workspace-write` and disables web search. This is a workspace policy, not a
secret-isolating container; use your normal agent credential and sandbox policy.

Candidate tests and frozen verification run in a separate process with a
read-only filesystem and no network. They cannot mutate the controller's Python
globals, budget or private state. Verification must produce both an implementation
change and a regression-test change. An atomic-file installation journal and
backups let restart finish the same verified installation; concurrent project
edits block installation and preserve both versions. No git reset or cleanup is
used. Three rejected candidates per incident or three installed repairs without
project progress block by default. Configure `progress_command` to emit a stable
queue/artifact frontier; unchanged output does not reset that bound.

Everything under `state_dir` is private operational data. Keep it outside tracked
source or ignored. Do not publish request files, raw responses, logs, credentials,
connection details or private manifests. Do not delete state or increase limits
to bypass a blocked run. Reconcile the saved operation first. Import previous
usage into `Budget` as explicit settled reservations before migrating an existing
campaign; a new ledger must not reset an old campaign's spending.

## Verification and scope

```sh
python3 -m unittest discover -s contrib/supervisor -p 'test_*.py'
```

Tests use synthetic subprocesses and never call a model. The integration tests
exercise abort, repair, verification, installation, resume, controller death
during repair, protected assets, healthy completion and operator stop. They need
working bubblewrap and report a skip when the host cannot provide isolation.
The dedicated Linux CI job requires isolation rather than accepting those skips.

The source of this companion is the private campaign runner, adapted to remove
its filesystem layout, documents, taxonomy and experiment-store dependencies.
The existing live campaign continues using its tested deployment. This PR does
not migrate it or claim that the new generic adapter has run that campaign.

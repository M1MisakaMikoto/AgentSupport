"""EvalStore SQLAlchemy persistence round trip (sqlite-backed)."""

from uuid import uuid4

from agentsupport.adapters.persistence.sqlalchemy.eval_store import SqlAlchemyEvalStore
from agentsupport.evaluation.domain import (
    CaseRunOutcome,
    EvalCase,
    EvalCaseResult,
    EvalDataset,
    EvalRun,
    Verdict,
    VerifierConfig,
)


def _dataset() -> EvalDataset:
    return EvalDataset(
        name="persisted",
        description="round trip",
        workspace_id=uuid4(),
        baseline_version="version-1",
        labels={"project_id": "eval-p"},
        cases=[
            EvalCase(
                dataset_id=uuid4(),  # replaced below
                task="task-a",
                tags=["t1"],
                verifiers=[
                    VerifierConfig(type="terminal_state", params={"expect": "COMPLETED"})
                ],
            )
        ],
    )


def test_eval_store_round_trip(tmp_path):
    url = f"sqlite:///{tmp_path / 'eval.db'}"
    store = SqlAlchemyEvalStore(url, create_schema=True)

    dataset = _dataset()
    case = dataset.cases[0]
    case.dataset_id = dataset.id
    store.create_dataset(dataset)

    # add a second case after creation
    extra = EvalCase(dataset_id=dataset.id, task="task-b")
    store.add_case(extra)

    outcome = CaseRunOutcome(
        conversation_id=uuid4(),
        terminal_state="COMPLETED",
        events=[{"type": "run.completed", "payload": {"result": {"status": "completed"}}}],
        final_result={"status": "completed"},
        duration_seconds=0.5,
        workspace_id=uuid4(),
        workspace_path="/tmp/ws",
    )
    run = EvalRun(
        dataset_id=dataset.id,
        status="running",
        runner_fingerprint={"mode": "deterministic", "version": "0.1.0"},
        results=[
            EvalCaseResult(
                case_id=case.id,
                task="task-a",
                verdict="PASS",
                score=1.0,
                verifier_results=[
                    Verdict(
                        verifier_id="terminal_state",
                        status="PASS",
                        score=1.0,
                        reason="ok",
                    )
                ],
                outcome=outcome,
                usage={"input_tokens": 10},
            )
        ],
    )
    store.save_run(run)
    run.status = "completed"
    run.summary = {"passed": 1}
    store.save_run(run)

    # a fresh store instance sees everything (restart equivalence)
    reloaded_store = SqlAlchemyEvalStore(url, create_schema=False)
    datasets = reloaded_store.list_datasets()
    assert [item.name for item in datasets] == ["persisted"]
    persisted = reloaded_store.get_dataset(dataset.id)
    assert persisted is not None
    assert [item.task for item in persisted.cases] == ["task-a", "task-b"]
    assert persisted.baseline_version == "version-1"
    assert persisted.labels == {"project_id": "eval-p"}

    persisted_run = reloaded_store.get_run(run.id)
    assert persisted_run is not None
    assert persisted_run.status == "completed"
    assert persisted_run.summary == {"passed": 1}
    assert persisted_run.results[0].verdict == "PASS"
    assert persisted_run.results[0].usage == {"input_tokens": 10}
    assert persisted_run.results[0].outcome is not None
    assert persisted_run.results[0].outcome.terminal_state == "COMPLETED"

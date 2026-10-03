"""fae — a Framework for Agentic-authoring Evaluations: the engine.

    cli         the operator's verbs (`fae …`; `cli.py` at a root is a shim)
    experiment  the experiment: its definition, its workspace, its verbs
    driver      the orchestrator: conduct, supervise, state, zombies, score, check
    cell        one cell's life: prepare, the loop, the verify boundary, its ledger
    scoring     the scoreboard and the per-cell scorer
    mutex       THE filesystem mutex

The experiment it runs is loaded by path from the root's `experiment/`
(fae.experiment); the engine imports nothing from it by name.
"""

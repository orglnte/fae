"""fae — a Framework for Agentic-authoring Evaluations: the engine.

    cli        the operator's verbs (`fae …`; `cli.py` at a root is a shim)
    driver     the orchestrator: conduct, supervise, state, zombies, rig, score
    cell       one cell's life: prepare, the loop, the verify boundary
    scoring    the scoreboard and the per-cell scorer
    mutex      THE filesystem mutex; ledger — THE ledger parser

The experiment it runs is loaded by path from the root's `experiment/`
(fae.cell.experiment); the engine imports nothing from it by name.
"""

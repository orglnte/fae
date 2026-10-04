"""The experiment: its definition, its workspace, and the verbs that act on
it as a whole.

    exp()        the experiment this process runs; its `definition` and
                 `workspace` are public, and every caller reaches them
                 through it: fae.experiment.exp().workspace.queues
    Gate         what an experiment declares its gate with
    cell_id, parse_cell_id, matches, is_cell_selector_blanket   the cell-id grammar

Nothing else is exposed: the classes live in the private _experiment.

    Definition   the experiment as declared, as the engine reads it
    Workspace    the cells' folders of a root, and the scheduling plane beside them
    Experiment   the definition and the workspace together, and the operator's
                 verbs on the experiment: prepare, init, smoke, verb, reset_trace

THE DEFINITION

One root runs one experiment: the directory `EXPERIMENT_DIR` names (config,
default `<root>/experiment`) is a Python package the engine loads BY PATH and
registers as `experiment`, so the definition's own modules import each other
relatively and the frozen bring-ups reach it as `-m experiment.variants`
whatever directory it lives in. Loading a second definition into the same
process is refused; tests that need one call `unload()` first.

The experiment's variants are files, one per variant:
`<experiment>/variants/<id>.toml` (fae/cell/variants/files.py). A variant
is one complete set of what the agent is given and how its work is judged;
the set of files is the set of variants.

The definition's `__init__.py` declares, all optional:

    NAME                 a short name
    GATE                 a Gate (default: one arrangement)
    verifier_class()     -> the Verifier subclass (fae/cell/verify.py: one
                            verify(ctx) -> Verdict, EXCLUSIVE — the lock the
                            engine holds around every run — and FILES; what it
                            owes is on the base class; a function: `verifier`
                            is the package)
    verbs()              -> {name: callable} hooks the engine's own verbs call
                            ("reference_cell" for smoke, "selftest")
    commands()           -> {name: callable(argv) -> exit code} the
                            experiment's own operator commands, run as
                            `cli.py experiment verb NAME [ARGS...]`; each
                            parses its own arguments
    taint_rules          rules(cell, workspace, metrics, it_text, v_text,
                            rc_text, verdict) -> (taints, warns, fields):
                            what a rig fault looks like in this experiment's
                            evidence, read through the Cell; `workspace` (a
                            Workspace) holds its sibling cells
                            (fae/experiment/scoring/validate.py runs the engine's own
                            rules beside it)
    report_text(cell)    -> the verifier's per-attempt reports, concatenated,
                            for the taint rules ("" by default)
    POOLED_MODELS        {model id: scoreboard row label} for the results table
    report_summary(cells, metrics_of, delta, metrics) -> {key: value} the
                            experiment adds to the aggregate's summary (its
                            own gaps between variants, its reading notes); the
                            engine hands it every scored cell, its per-group
                            metric function, its None-safe delta and the
                            metric names
    CONFIG               {KEY: (toml section, name, default, "path"|"str")}
                            — machine-local settings the experiment needs,
                            read from fae.toml / the environment into
                            the config and exported to every child; a path
                            default may name "{experiment}"
    fingerprint_trees(conf)  -> directories whose *.py are hashed into the
                            verify fingerprint beside the experiment tree
    (agents' images: the base is the experiment root's Dockerfile.agent-base,
    else the engine's; each variant's layer over it is its [authoring] tools
    directory — fae/cell/image.py)
"""

from ._experiment import Gate, cell_id, exp, is_cell_selector_blanket, matches, parse_cell_id  # noqa: F401

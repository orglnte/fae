"""The experiment's variants, read from `<experiment>/variants/<id>.toml`.

A variant is one complete set of what the agent is given and how its work
is judged. Each file holds:

    label, retired, factors                  top level
    [authoring]  template, surface, tools, access_infra, [authoring.inputs]
    [verify]     image_dir, reference, [verify.run]
    [infra]      class, lock, lock_slots, params

Paths are relative to the experiment directory. A key this module does not
know is refused: a typo would otherwise drop a declaration silently.
"""
from __future__ import annotations

import importlib
import re

try:
    import tomllib
except ModuleNotFoundError:     # pragma: no cover - the engine runs on 3.11+
    import tomli as tomllib
from pathlib import Path

DIR = "variants"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")

KEYS = {
    "": {"label", "retired", "factors", "authoring", "verify", "infra", "substrate"},
    "authoring": {"template", "surface", "tools", "access_infra", "inputs"},
    "authoring.surface": {"files", "prefixes"},
    "verify": {"image_dir", "reference", "run"},
    "verify.run": {"command", "serves", "image", "image_dir", "build"},
    "infra": {"class", "lock", "lock_slots", "params"},
    "substrate": {"kind", "lifecycle", "access", "images", "expose_ports",
                  "cpus", "memory", "dind_image", "cluster_prefix",
                  "overlay", "class", "lock", "lock_slots", "params"},
}


class VariantFileError(ValueError):
    """A variant file the engine cannot read as a variant."""


def _refuse_unknown(path, section, table):
    unknown = sorted(set(table) - KEYS[section])
    if unknown:
        where = f"[{section}]" if section else "the top level"
        raise VariantFileError(f"{path}: unknown key(s) in {where}: {', '.join(unknown)} "
                               f"(known: {', '.join(sorted(KEYS[section]))})")


def _import_class(path, label, spec, expected_base):
    """`module:Class` under the experiment package, subclass of `expected_base`."""
    module, _, name = str(spec).partition(":")
    if not module or not name:
        raise VariantFileError(f"{path}: {label} must be 'module:Class', got {spec!r}")
    try:
        cls = getattr(importlib.import_module(f"experiment.{module}"), name)
    except (ImportError, AttributeError) as e:
        raise VariantFileError(f"{path}: {label} {spec!r} cannot be imported: {e}") from e
    if not (isinstance(cls, type) and issubclass(cls, expected_base)):
        raise VariantFileError(
            f"{path}: {label} {spec!r} is not a {expected_base.__name__} subclass"
        )
    return cls


def _infra_class(path, spec):
    """`module:Class` under the experiment package, an Infra subclass."""
    from fae.cell.infra.base import Infra
    module, _, name = str(spec).partition(":")
    if not module or not name:
        raise VariantFileError(f"{path}: [infra] class must be 'module:Class', got {spec!r}")
    try:
        cls = getattr(importlib.import_module(f"experiment.{module}"), name)
    except (ImportError, AttributeError) as e:
        raise VariantFileError(f"{path}: [infra] class {spec!r} cannot be imported: {e}") from e
    if not (isinstance(cls, type) and issubclass(cls, Infra)):
        raise VariantFileError(f"{path}: [infra] class {spec!r} is not an Infra subclass")
    return cls


def _parse_images(path, raw, exp_dir):
    from fae.cell.substrate.base import ImageSpec
    out = []
    for entry in (raw or ()):
        if isinstance(entry, str):
            out.append(ImageSpec(source=entry))
        elif isinstance(entry, dict):
            unknown = sorted(set(entry) - {"dir", "as_tag", "name"})
            if unknown:
                raise VariantFileError(
                    f"{path}: [substrate] image entry has unknown key(s): {', '.join(unknown)}"
                )
            if not entry.get("dir"):
                raise VariantFileError(f"{path}: [substrate] image table requires 'dir'")
            out.append(ImageSpec(
                dir=exp_dir / entry["dir"],
                as_tag=str(entry.get("as_tag") or ""),
                name=str(entry.get("name") or ""),
            ))
        else:
            raise VariantFileError(f"{path}: [substrate] image must be string or table, got {entry!r}")
    return tuple(out)


def _parse_substrate(path, sub, authoring, exp_dir, vid):
    from fae.cell.infra.base import Infra
    from fae.cell.substrate import KINDS, LIFECYCLES, DomainOverlay, SubstrateSpec, build_infra_class
    kind = str(sub.get("kind", "none"))
    if kind not in KINDS:
        raise VariantFileError(
            f"{path}: [substrate] kind {kind!r} not in {sorted(KINDS)}"
        )
    lifecycle = str(sub.get("lifecycle", "cell"))
    if lifecycle not in LIFECYCLES:
        raise VariantFileError(
            f"{path}: [substrate] lifecycle {lifecycle!r} not in {sorted(LIFECYCLES)}"
        )
    access = bool(sub["access"]) if "access" in sub else bool(authoring.get("access_infra", False))
    images = _parse_images(path, sub.get("images"), exp_dir)
    raw_ports = sub.get("expose_ports") or ()
    expose_ports = tuple(int(p) for p in (raw_ports.values() if isinstance(raw_ports, dict) else raw_ports))
    params = dict(sub.get("params") or {})
    if kind == "custom":
        if not sub.get("class"):
            raise VariantFileError(f"{path}: [substrate] kind='custom' requires 'class'")
        infra_cls = _import_class(path, "[substrate] class", sub["class"], Infra)
        spec = SubstrateSpec(
            kind="custom", lifecycle=lifecycle, access=access, images=images,
            expose_ports=expose_ports, params=tuple(sorted(params.items())),
            lock=sub.get("lock") or None, lock_slots=int(sub.get("lock_slots", 1)),
        )
        return spec, infra_cls
    if sub.get("class"):
        raise VariantFileError(f"{path}: [substrate] 'class' is only valid when kind='custom'")
    overlay = _import_class(path, "[substrate] overlay", sub["overlay"], DomainOverlay) if sub.get("overlay") else None
    default_mem = "4g" if kind == "k8s" else "1g"
    spec = SubstrateSpec(
        kind=kind,
        lifecycle=lifecycle,
        access=access,
        images=images,
        expose_ports=expose_ports,
        cpus=float(sub.get("cpus", 1.0)),
        memory=str(sub.get("memory", default_mem)),
        dind_image=str(sub.get("dind_image", "docker:27-dind")),
        cluster_prefix=str(sub.get("cluster_prefix", "fae-k8s-")),
        overlay=overlay,
        params=tuple(sorted(params.items())),
        lock=sub.get("lock") or None,
        lock_slots=int(sub.get("lock_slots", 1)),
    )
    return spec, build_infra_class(spec, name=_camel(vid))


def _camel(vid):
    return "".join(p.capitalize() for p in vid.split("_")) or "Variant"


def read(path, exp_dir):
    """The variant class `path` declares."""
    from .base import Variant
    path, exp_dir = Path(path), Path(exp_dir)
    vid = path.stem
    if not ID_RE.match(vid):
        raise VariantFileError(f"{path}: a variant id is lower case letters, digits and _")
    try:
        doc = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise VariantFileError(f"{path}: {e}") from e
    _refuse_unknown(path, "", doc)
    authoring = doc.get("authoring") or {}
    verify = doc.get("verify") or {}
    infra = doc.get("infra") or {}
    substrate = doc.get("substrate") or {}
    if infra and substrate:
        raise VariantFileError(f"{path}: declare either [substrate] or [infra], not both")
    for section, table in (("authoring", authoring), ("verify", verify),
                           ("infra", infra), ("substrate", substrate)):
        if not isinstance(table, dict):
            raise VariantFileError(f"{path}: [{section}] must be a table")
        _refuse_unknown(path, section, table)
    surface = authoring.get("surface") or {}
    run = verify.get("run") or {}
    _refuse_unknown(path, "authoring.surface", surface)
    _refuse_unknown(path, "verify.run", run)

    def at(rel):
        return (exp_dir / rel) if rel else None

    attrs = {
        "ID": vid,
        "SOURCE": path,
        "LABEL": str(doc.get("label") or vid),
        "RETIRED": bool(doc.get("retired", False)),
        "FACTORS": dict(doc.get("factors") or {}),
        "TEMPLATE": tuple(at(p) for p in authoring.get("template") or ()),
        "AUTHORING_SURFACE": (tuple(surface.get("files") or ()),
                              tuple(surface.get("prefixes") or ())),
        "AGENT_IMAGE_DIR": at(authoring.get("tools")),
        "ACCESS_INFRA": bool(authoring.get("access_infra", False)),
        "INPUTS": {str(k): at(v) for k, v in (authoring.get("inputs") or {}).items()},
        "IMAGE_DIR": at(verify.get("image_dir")),
        "REFERENCE": at(verify.get("reference")),
        "RUN": {**run, "image_dir": at(run.get("image_dir"))} if run else {},
        "LOCK": infra.get("lock") or None,
        "LOCK_SLOTS": int(infra.get("lock_slots", 1)),
        "PARAMS": dict(infra.get("params") or {}),
    }
    if not surface:
        attrs["AUTHORING_SURFACE"] = None
    if substrate:
        spec, infra_cls = _parse_substrate(path, substrate, authoring, exp_dir, vid)
        attrs["SUBSTRATE"] = spec
        attrs["INFRA"] = infra_cls
        attrs["ACCESS_INFRA"] = spec.access
        attrs["LOCK"] = spec.lock
        attrs["LOCK_SLOTS"] = spec.lock_slots
        attrs["PARAMS"] = spec.params_dict()
    elif infra.get("class"):
        attrs["INFRA"] = _infra_class(path, infra["class"])
    return type(_camel(vid), (Variant,), attrs)


def load(exp_dir):
    """{id: class} for every variant file of the experiment, in id order."""
    d = Path(exp_dir) / DIR
    return {p.stem: read(p, exp_dir) for p in sorted(d.glob("*.toml"))}


def template_files(cls):
    """{workspace path: source file} the template directories provide, the
    later directory winning on a shared path."""
    out = {}
    for d in cls.TEMPLATE:
        if d is None or not Path(d).is_dir():
            continue
        for p in sorted(Path(d).rglob("*")):
            if p.is_file():
                out[p.relative_to(d).as_posix()] = p
    return out


def problems(cls):
    """What keeps this variant from seeding a workspace: a template directory
    or an input file that is missing, an input path the template also
    provides. Empty when it seeds."""
    out = []
    for d in cls.TEMPLATE:
        if not Path(d).is_dir():
            out.append(f"template directory {d} is missing")
    for rel, src in cls.INPUTS.items():
        if src is None or not Path(src).is_file():
            out.append(f"input {rel}: {src} is not a file")
    clash = sorted(set(cls.INPUTS) & set(template_files(cls)))
    if clash:
        out.append(f"input path(s) the template also provides: {', '.join(clash)}")
    if cls.REFERENCE is not None and not Path(cls.REFERENCE).is_dir():
        out.append(f"reference {cls.REFERENCE} is not a directory")
    return out

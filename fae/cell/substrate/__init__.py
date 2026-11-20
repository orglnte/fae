"""Declarative substrate subsystem (`[substrate]` in variant TOMLs).

Provides typed runtime contexts (`NoneContext`, `DockerContext`, `K8sContext`),
`DomainOverlay` for experiment-specific layers on top of a FAE substrate, and
`build_infra_class(spec)` which compiles a `SubstrateSpec` into a concrete
`SubstrateInfra` subclass."""
from __future__ import annotations

from .base import (  # noqa: F401
    KINDS,
    LIFECYCLES,
    DomainOverlay,
    ImageSpec,
    NoneContext,
    NoneSubstrate,
    SubstrateInfra,
    SubstrateSpec,
    _merge_prefixes,
)
from .docker import DockerContext, DockerSubstrate  # noqa: F401
from .k8s import K8sContext, K8sSubstrate, cluster_name_for  # noqa: F401


def build_infra_class(spec: SubstrateSpec, name="BoundSubstrate"):
    """Compile a declarative `SubstrateSpec` into a concrete `SubstrateInfra`
    subclass whose class attributes (`PREFIXES`, `ENV_FILE`, `VERIFY_ADOPTS`,
    `RUN_DIR`) merge substrate and overlay metadata."""
    base_cls = {
        "none": NoneSubstrate,
        "docker": DockerSubstrate,
        "k8s": K8sSubstrate,
    }.get(spec.kind)
    if base_cls is None:
        raise ValueError(f"unsupported substrate kind {spec.kind!r}")
    base_prefixes = dict(base_cls.PREFIXES)
    if spec.kind == "k8s":
        base_prefixes = {"cluster": spec.cluster_prefix}
    overlay = spec.overlay
    attrs = {
        "SPEC": spec,
        "PREFIXES": _merge_prefixes(base_prefixes, overlay.PREFIXES if overlay else {}),
        "ENV_FILE": overlay.ENV_FILE if overlay else None,
        "VERIFY_ADOPTS": overlay.VERIFY_ADOPTS if overlay else None,
        "RUN_DIR": overlay.RUN_DIR if overlay else None,
    }
    return type(f"{name}{base_cls.__name__}", (base_cls,), attrs)

"""A variant: one complete set of what the agent is given and how its work
is judged, as its file declares it (fae/experiment/variants/files.py builds a
subclass per file). Data only; what exists around its program is its
infra class's (fae/cell/infra/base.py)."""
from __future__ import annotations

from fae import paths as _paths

from fae.cell.infra.base import DefaultInfra

HARNESS = _paths.ENGINE
ROOT = _paths.ROOT


class Variant:
    ID = ""
    SOURCE = None           # the variant file
    LABEL = ""              # what reports show; the id when the file names none
    RETIRED = False         # keeps its cells, never scheduled again
    FACTORS = {}            # the experimental factors it is a level of
    # [authoring]: what the agent gets
    TEMPLATE = ()           # directories merged into the workspace, in order
    INPUTS = {}             # workspace path -> source file the agent reads
    # (exact relpaths, directory prefixes) the agent may write; every other
    # seeded file is fixed and healed before a verdict. Required: a cell of
    # a variant that leaves it None is refused.
    AUTHORING_SURFACE = None
    AGENT_IMAGE_DIR = None  # the agent's image layer (a Dockerfile `FROM $BASE`)
    ACCESS_INFRA = False    # the agent's container is connected to the cell's infra
    # [verify]: how the work is judged
    IMAGE_DIR = None        # the verify container's layer, FROM the verifier's image
    REFERENCE = None        # the known answer, laid over the template for smoke
    RUN = {}                # how the verifier runs the artifacts (secrunner.for_variant)
    # [infra]
    INFRA = DefaultInfra    # the infra class, instantiated per cell with the variant
    LOCK = None             # the lock a cell holds setup to teardown; None: none
    LOCK_SLOTS = 1          # its default cap when the config names none
    PARAMS = {}             # the infra class's own settings

    @classmethod
    def runs_own_image(cls):
        """Whether its program runs in an image of its own ([verify.run]
        image or image_dir) rather than the verify image."""
        return bool(cls.RUN.get("image") or cls.RUN.get("image_dir"))

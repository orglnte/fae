from fae.cell.variants.base import Variant


class Beta(Variant):
    ARM = "beta"
    TECH = "beta"
    LOCK = None
    LOCK_SLOTS = 2
    CONDITIONS = ("apidocs", "onlysrc")
    AUTHORING_SURFACE = (("declaration.toml",), ("app/",))

    def substrate_alive(self):
        return True

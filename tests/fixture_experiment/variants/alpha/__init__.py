from fae.cell.variants.base import Variant


class Alpha(Variant):
    ARM = "alpha"
    TECH = "alpha"
    LOCK = 'alpha'
    LOCK_SLOTS = 2
    CONDITIONS = ("apidocs", "howto", "openbook", "onlysrc")
    AUTHORING_SURFACE = (("answer.txt",), ("app/", "k8s/"))
    SUBSTRATE_PREFIXES = {"container": "fae-dind-", "cluster": "fx-cluster-"}

    @classmethod
    def substrate_identities(cls, cid):
        return [("container", f"fae-dind-{cid}")]

    def substrate_alive(self):
        return True

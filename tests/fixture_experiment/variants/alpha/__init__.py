from fae.cell.variants.base import Variant


class Alpha(Variant):
    ARM = "alpha"
    TECH = "alpha"
    LOCK = 'alpha'
    LOCK_SLOTS = 2
    CONDITIONS = ("apidocs", "howto", "openbook", "onlysrc")
    AUTHORING_SURFACE = (("answer.txt",), ("app/", "k8s/"))
    INFRA_PREFIXES = {"container": "fae-dind-", "cluster": "fx-cluster-"}

    @classmethod
    def infra_identities(cls, cid):
        return [("container", f"fae-dind-{cid}")]

    def infra_alive(self):
        return True

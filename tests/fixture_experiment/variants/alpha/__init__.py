from fae.cell.variants.base import Variant


class Alpha(Variant):
    INFRA_PREFIXES = {"container": "fae-dind-", "cluster": "fx-cluster-"}

    @classmethod
    def infra_identities(cls, cid):
        return [("container", f"fae-dind-{cid}")]

    def infra_alive(self):
        return True

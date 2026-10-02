from fae.cell.infra.base import Infra


class Alpha(Infra):
    PREFIXES = {"container": "fae-dind-", "cluster": "fx-cluster-"}

    @classmethod
    def identities(cls, cid):
        return [("container", f"fae-dind-{cid}")]

    def alive(self):
        return True

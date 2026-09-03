"""The registration manifest, and the mechanism that makes re-applying it work.

`kubectl apply` neither prunes nor re-runs a Completed Job. So the manifest has
to name a Job that does not exist yet — every time — and the question of whether
that Job has anything to do is answered in the cluster, not here. These tests pin
both halves: the name is unique per rendering, and the revision that decides the
answer reaches both the Job and the release.
"""

from typing import Dict

import pytest
import yaml

from gpustack.k8s.bootstrap import BOOTSTRAP_NAME, RELEASE_NAME, render_bootstrap
from gpustack.k8s.manifest_template import TemplateConfig
from gpustack.schemas.clusters import K8sOptions
from gpustack_runtime.detector import ManufacturerEnum


def config(**kwargs) -> TemplateConfig:
    defaults = {
        "token": "tok",
        "server_url": "http://gpustack.example.com:30080",
        "image": "gpustack/gpustack:dev",
        "env": {"GPUSTACK_TOKEN": "tok"},
        "args": [],
        "cluster_owner_principal_identifier": "1",
    }
    defaults.update(kwargs)
    return TemplateConfig(**defaults)


def objects(**kwargs) -> Dict[str, dict]:
    docs = [d for d in yaml.safe_load_all(render_bootstrap(config(**kwargs))) if d]
    return {f"{d['kind']}/{d['metadata']['name']}": d for d in docs}


def named(rendered: Dict[str, dict], kind: str) -> str:
    for doc in rendered.values():
        if doc["kind"] == kind:
            return doc["metadata"]["name"]
    pytest.fail(f"no {kind} was rendered")


def job_env(rendered: Dict[str, dict]) -> Dict[str, str]:
    job = rendered[f"Job/{named(rendered, 'Job')}"]
    container = job["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e["value"] for e in container["env"]}


def chart_values(rendered: Dict[str, dict]) -> dict:
    return yaml.safe_load(
        rendered[f"ConfigMap/{BOOTSTRAP_NAME}"]["data"]["values.yaml"]
    )


class TestJobNaming:
    def test_every_rendering_names_a_new_job(self):
        # The only way an apply can make something happen. A name derived from
        # the configuration would skip a revert to an earlier one, whose Job is
        # already Completed: A -> B -> A would leave the cluster on B.
        names = {named(objects(), "Job") for _ in range(5)}
        assert len(names) == 5, names

    def test_nothing_else_is_named_per_rendering(self):
        # `kubectl apply` does not prune and only a Job has a TTL, so a name
        # that moved would leave an orphan behind on every fetch.
        one, two = objects(), objects()
        for kind in ("ConfigMap", "ServiceAccount", "ClusterRoleBinding"):
            assert named(one, kind) == named(two, kind) == BOOTSTRAP_NAME

    def test_the_job_reads_the_configmap_rather_than_a_baked_copy(self):
        rendered = objects()
        pod = rendered[f"Job/{named(rendered, 'Job')}"]["spec"]["template"]["spec"]
        assert pod["volumes"][0]["configMap"]["name"] == BOOTSTRAP_NAME
        env = job_env(rendered)
        assert env["VALUES_FILE"].startswith("/bootstrap/")
        assert env["RELEASE"] == RELEASE_NAME


class TestAppliedRevision:
    def test_the_job_and_the_values_agree(self):
        # The Job compares one against the other in the cluster; if they were
        # computed differently it would either reinstall forever or never.
        rendered = objects()
        assert (
            job_env(rendered)["DESIRED_REVISION"]
            == chart_values(rendered)["appliedRevision"]
        )

    def test_the_revision_tracks_the_configuration(self):
        one = chart_values(objects(runtimes=[ManufacturerEnum.NVIDIA]))
        two = chart_values(objects(runtimes=[ManufacturerEnum.ASCEND]))
        assert one["appliedRevision"] != two["appliedRevision"]

    def test_the_revision_is_stable_for_the_same_configuration(self):
        # Unlike the Job name. A revision that moved per rendering would make
        # every apply a real `helm upgrade` and fill `helm history` with no-ops.
        one = chart_values(objects(k8s_options=K8sOptions(node_selector={"a": "b"})))
        two = chart_values(objects(k8s_options=K8sOptions(node_selector={"a": "b"})))
        assert one["appliedRevision"] == two["appliedRevision"]

    def test_a_reverted_configuration_returns_to_its_revision(self):
        # A -> B -> A. The Job names differ, so all three apply; the revision
        # returning to A's value is what tells the third Job it has work to do,
        # because the release still records B's.
        a_first = chart_values(objects(runtimes=[ManufacturerEnum.NVIDIA]))
        b = chart_values(objects(runtimes=[ManufacturerEnum.ASCEND]))
        a_again = chart_values(objects(runtimes=[ManufacturerEnum.NVIDIA]))
        assert a_first["appliedRevision"] == a_again["appliedRevision"]
        assert b["appliedRevision"] != a_first["appliedRevision"]


class TestOwnership:
    def test_the_token_secret_survives_a_helm_prune(self):
        # A cluster whose chart used to create this Secret has it in the previous
        # release manifest and not in the new one, which is what Helm prunes.
        # Losing it leaves running workers alive and every new pod unable to
        # start: it is mounted with `optional: false`.
        secret = objects()["Secret/registration-token"]
        assert secret["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"

    def test_both_namespaces_are_created(self):
        rendered = objects()
        namespaces = {
            doc["metadata"]["name"]
            for doc in rendered.values()
            if doc["kind"] == "Namespace"
        }
        assert namespaces == {"gpustack-system", "gpustack-1"}

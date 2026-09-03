"""The generated values have to be values the chart actually reads.

Asserting the dict's shape alone would pass for a key the chart ignores, which
is the failure this is most exposed to: a worker-only install that renders
cleanly and then deploys the wrong thing. So the important cases render the real
chart with the generated values and assert on the objects that come out.

Rendering is skipped when helm or the packaged chart is absent; the pure mapping
tests always run.
"""

import json
import pathlib
import shutil
import subprocess
from typing import Any, Dict, List

import pytest
import yaml

from gpustack.k8s.chart import chart_available
from gpustack.k8s.manifest_template import TemplateConfig
from gpustack.k8s.values import build_chart_values, split_image_reference
from gpustack.schemas.clusters import (
    ImageCredential,
    K8sOptions,
    K8sVolumeMount,
    VolumeSource,
)
from gpustack_runtime.detector import ManufacturerEnum

HELM = shutil.which("helm")
CHART = "charts/gpustack-chart"


def config(**kwargs) -> TemplateConfig:
    defaults: Dict[str, Any] = {
        "token": "tok",
        "server_url": "http://gpustack.example.com:30080",
        "image": "docker.io/gpustack/gpustack:dev",
        "env": {},
        "args": [],
    }
    defaults.update(kwargs)
    return TemplateConfig(**defaults)


def render(values: Dict[str, Any], namespace: str = "gpustack-system") -> List[dict]:
    if HELM is None or not chart_available():
        pytest.skip("helm or the packaged chart is unavailable")
    with_values = pathlib.Path(
        subprocess.run(
            ["mktemp", "-t", "values"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    with_values.write_text(yaml.safe_dump(values))
    result = subprocess.run(
        [
            HELM,
            "template",
            "gpustack",
            CHART,
            "--namespace",
            namespace,
            "-f",
            str(with_values),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def worker_daemonset(docs: List[dict]) -> dict:
    """The gpustack worker DaemonSet, by name.

    The operator's tree contributes DaemonSets of its own — device managers, NFD,
    the CSI node plugins — so picking "the first DaemonSet" silently asserts
    against whichever one helm happened to emit first.
    """
    for doc in docs:
        if doc["kind"] == "DaemonSet" and doc["metadata"]["name"] == "gpustack-worker":
            return doc
    pytest.fail("the gpustack-worker DaemonSet was not rendered")


class TestSplitImageReference:
    @pytest.mark.parametrize(
        "reference,expected",
        [
            # A leading segment is a registry only when it carries a "." or ":",
            # which is the only thing telling these two apart.
            (
                "docker.io/gpustack/gpustack:dev",
                ("docker.io", "gpustack/gpustack", "dev"),
            ),
            ("gpustack/gpustack:dev", (None, "gpustack/gpustack", "dev")),
            (
                "localhost:5000/gpustack/gpustack:v1",
                ("localhost:5000", "gpustack/gpustack", "v1"),
            ),
            ("localhost/gpustack:v1", ("localhost", "gpustack", "v1")),
            ("gpustack/gpustack", (None, "gpustack/gpustack", "")),
        ],
    )
    def test_splits(self, reference, expected):
        assert split_image_reference(reference) == expected

    def test_refuses_a_digest(self):
        # The chart composes `repository:tag`; dropping the digest would deploy
        # a different image than the one named.
        with pytest.raises(ValueError, match="digest"):
            split_image_reference("gpustack/gpustack@sha256:" + "0" * 64)


class TestWorkerOnlyValues:
    def test_renders_workers_and_the_operator_but_no_server(self):
        docs = render(build_chart_values(config(runtimes=[ManufacturerEnum.NVIDIA])))
        kinds = {doc["kind"] for doc in docs}
        names = {doc["metadata"]["name"] for doc in docs}
        assert "StatefulSet" not in kinds
        assert {"gpustack-worker", "gpustack-worker-nvidia"} <= names
        assert "gpustack-operator-worker" in names

    def test_workers_are_told_where_the_server_is(self):
        docs = render(build_chart_values(config()))
        # By name: the operator's tree brings DaemonSets of its own (device
        # managers, NFD, the CSI node plugins), so "the first DaemonSet" is not
        # the worker.
        daemonset = worker_daemonset(docs)
        env = {
            e["name"]: e.get("value")
            for e in daemonset["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["GPUSTACK_SERVER_URL"] == "http://gpustack.example.com:30080"

    def test_the_chart_does_not_create_the_token_secret(self):
        # The bootstrap manifest owns it, so a re-render must not be able to
        # rotate or delete the token.
        docs = render(build_chart_values(config()))
        assert not [
            d
            for d in docs
            if d["kind"] == "Secret" and d["metadata"]["name"] == "registration-token"
        ]

    def test_the_chart_does_not_create_pull_secrets_but_references_them(self):
        values = build_chart_values(
            config(
                k8s_options=K8sOptions(
                    image_credentials=[
                        ImageCredential(
                            registry="reg.example.com", username="u", password="p"
                        )
                    ]
                )
            )
        )
        docs = render(values)
        assert not [
            d
            for d in docs
            if d["kind"] == "Secret" and "image-pull-secret" in d["metadata"]["name"]
        ]
        referenced = {
            ref["name"]
            for doc in docs
            if doc["kind"] in ("DaemonSet", "Deployment")
            for ref in doc["spec"]["template"]["spec"].get("imagePullSecrets") or []
        }
        assert referenced == {"gpustack-image-pull-secret-0"}

    def test_no_credentials_means_no_dangling_reference(self):
        # The chart's default references the Secret it would otherwise create;
        # with create=false that would point every pod at a Secret nobody made.
        docs = render(build_chart_values(config()))
        for doc in docs:
            if doc["kind"] not in ("DaemonSet", "Deployment", "Job"):
                continue
            assert not doc["spec"]["template"]["spec"].get("imagePullSecrets")

    def test_registry_reaches_both_subtrees(self):
        values = build_chart_values(
            config(image="mirror.example.com/gpustack/gpustack:dev")
        )
        docs = render(values)
        images = set()
        for doc in docs:
            if doc["kind"] not in ("Deployment", "DaemonSet", "StatefulSet", "Job"):
                continue
            pod = doc["spec"]["template"]["spec"]
            for container in (pod.get("containers") or []) + (
                pod.get("initContainers") or []
            ):
                images.add(container["image"])
        strays = sorted(i for i in images if not i.startswith("mirror.example.com/"))
        assert not strays, f"images not pointing at the mirror: {strays}"

    def test_data_dir_and_extra_mounts_reach_the_daemonset(self):
        options = K8sOptions(
            volume_mounts=[
                K8sVolumeMount(
                    name="gpustack-data-dir",
                    mountPath="/var/lib/gpustack",
                    volumeSource=VolumeSource.model_validate(
                        {
                            "hostPath": {
                                "path": "/data/gpustack",
                                "type": "DirectoryOrCreate",
                            }
                        }
                    ),
                ),
                K8sVolumeMount(
                    name="extra-lib",
                    mountPath="/opt/lib",
                    readOnly=True,
                    volumeSource=VolumeSource.model_validate(
                        {"hostPath": {"path": "/opt/lib", "type": "Directory"}}
                    ),
                ),
            ]
        )
        docs = render(build_chart_values(config(k8s_options=options)))
        pod = worker_daemonset(docs)["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in pod["volumes"]}
        assert volumes["gpustack-data-dir"]["hostPath"]["path"] == "/data/gpustack"
        assert volumes["extra-lib"]["hostPath"]["path"] == "/opt/lib"
        mounts = {m["name"]: m for m in pod["containers"][0]["volumeMounts"]}
        assert mounts["extra-lib"]["mountPath"] == "/opt/lib"
        assert mounts["extra-lib"]["readOnly"] is True


class TestHelmValues:
    """Passing the chart's own values through, which is what #6011 asked for."""

    def values(self, overlay) -> K8sOptions:
        return K8sOptions.model_validate({"helmValues": overlay})

    def test_a_component_the_cluster_already_runs_is_skipped(self):
        # The issue's case: a cluster with its own Kueue must not get a second.
        docs = render(
            build_chart_values(
                config(
                    k8s_options=self.values(
                        {"gpustack-operator": {"kueue": {"enabled": False}}}
                    )
                )
            )
        )
        names = [doc["metadata"]["name"] for doc in docs]
        assert not [n for n in names if "kueue" in n]
        # Only what was asked for: the rest of the release is untouched.
        assert "gpustack-operator-worker" in names
        assert [n for n in names if "node-feature-discovery" in n]

    def test_arbitrary_chart_values_reach_the_release(self):
        # The point of a passthrough: this needs no field of its own, and no
        # release of ours to become available.
        overlay = {"worker": {"tolerations": [{"key": "gpu", "operator": "Exists"}]}}
        values = build_chart_values(config(k8s_options=self.values(overlay)))
        assert values["worker"]["tolerations"] == overlay["worker"]["tolerations"]

    def test_merging_is_per_key_not_wholesale(self):
        # Setting one key under `worker` must not drop the derived siblings that
        # tell the workers where to register.
        values = build_chart_values(
            config(k8s_options=self.values({"worker": {"port": 20150}}))
        )
        assert values["worker"]["port"] == 20150
        assert values["worker"]["serverURL"] == "http://gpustack.example.com:30080"
        assert values["worker"]["enabled"] is True

    @pytest.mark.parametrize(
        "overlay",
        [
            {"worker": {"serverURL": "http://elsewhere"}},
            {"server": {"enabled": True}},
            {"image": {"tag": "someone-elses"}},
            {"registrationTokenSecretName": "mine"},
        ],
    )
    def test_server_owned_paths_are_refused(self, overlay):
        # They decide what the deployment is, not how it is configured: pointing
        # the workers at another server, adding a second control plane, or
        # breaking the pairing between the worker image and these templates.
        with pytest.raises(ValueError, match="cannot be set here"):
            self.values(overlay)

    def test_server_owned_paths_survive_a_row_written_around_the_api(self):
        # The validator above guards the API; this guards the merge, for a
        # cluster row edited directly. A release that quietly does not match the
        # cluster it was issued for is worth two defences.
        options = K8sOptions.model_construct(
            helm_values={"worker": {"serverURL": "http://elsewhere"}}
        )
        values = build_chart_values(config(k8s_options=options))
        assert values["worker"]["serverURL"] == "http://gpustack.example.com:30080"


class TestOperatorValues:
    def test_operator_env_is_passed_through(self):
        options = K8sOptions.model_validate(
            {"operator": {"env": {"GPUSTACK_LOG_LEVEL": "debug"}}}
        )
        docs = render(build_chart_values(config(k8s_options=options)))
        deployment = next(
            d
            for d in docs
            if d["kind"] == "Deployment"
            and d["metadata"]["name"] == "gpustack-operator-worker"
        )
        env = {
            e["name"]: e.get("value")
            for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["GPUSTACK_LOG_LEVEL"] == "debug"

    def test_gpu_instance_knobs_are_rendered_as_strings(self):
        # Tri-state: an explicit false has to arrive as the string "false", not
        # be dropped as falsy, or the operator's own default takes over.
        options = K8sOptions.model_validate(
            {"gpuInstanceOptions": {"gpuInstanceTypeDerivedFromNode": False}}
        )
        values = build_chart_values(config(k8s_options=options))
        env = values["gpustack-operator"]["worker"]["env"]
        assert env["GPUSTACK_INSTANCE_TYPE_DERIVED_FROM_NODE"] == "false"

    def test_values_dump_without_yaml_anchors(self):
        # The same dict reached two keys and PyYAML emitted an anchor plus an
        # alias for it. Helm reads that, but the generated anchor name moves with
        # key order — and the bootstrap Job is named after a hash of this dump,
        # so it would change the Job's name without changing the configuration.
        import yaml as yaml_module

        options = K8sOptions(node_selector={"disktype": "ssd"})
        dumped = yaml_module.safe_dump(
            build_chart_values(config(k8s_options=options)), sort_keys=True
        )
        assert "&id" not in dumped and "*id" not in dumped, dumped

    def test_values_are_json_serialisable(self):
        # They travel to the cluster as a ConfigMap, so anything that cannot be
        # dumped is a manifest that fails at apply time rather than here.
        json.dumps(build_chart_values(config()))

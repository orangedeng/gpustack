"""Rendering of the registration manifest for a Kubernetes-provider cluster.

The manifest carries no workload objects. It delivers what the chart cannot
supply itself — namespaces, the registration token, pull credentials, the values
— and a Job that installs the chart from this server.

Keeping the Job's inputs in a ConfigMap rather than in its own spec is what makes
re-applying the manifest behave: the Job's name is a hash of those inputs, so
identical inputs resolve to the same name and the apply is a no-op, while changed
inputs resolve to a new Job that runs. A fixed name would leave a Completed Job
in place and the change unapplied until its TTL expired.
"""

import base64
from datetime import datetime, timezone

import jinja2
import yaml

from gpustack.k8s.chart import chart_download_url
from gpustack.k8s.manifest_template import TemplateConfig
from gpustack.k8s.values import (
    REGISTRATION_TOKEN_SECRET_NAME,
    applied_revision,
    build_chart_values,
)
from gpustack.utils.compat_importlib import pkg_resources

# The chart derives every object name from the release name, and this one is what
# makes them match the manifest that registered clusters before the chart —
# `<release>-worker` against the old `gpustack-worker`. That is what lets a
# running cluster be adopted instead of gaining a second, unmanaged copy of every
# workload. bootstrap.sh refuses to touch a release of this name that turns out
# to be a server install.
RELEASE_NAME = "gpustack"

# Fixed for the ConfigMap, the ServiceAccount and its binding: `kubectl apply`
# updates them in place and never prunes, so a name that moved with the
# configuration would leave an orphan behind on every change — and unlike the
# Job, none of them has a TTL to clean it up.
BOOTSTRAP_NAME = "gpustack-bootstrap"
# Enough of the digest to make a collision irrelevant, short enough to leave room
# under the 63-character limit for object names.
_NAME_DIGEST_LENGTH = 10


def _job_name() -> str:
    """A Job name unique to this rendering of the manifest.

    `kubectl apply` neither prunes nor re-runs a Completed Job, so a manifest can
    only make something happen by naming a Job that does not exist yet — which
    means the name has to be new on every fetch. A name derived from the
    configuration cannot be: going from A to B and back to A resolves to the Job
    that already installed A, so the apply would be a no-op and the cluster would
    stay on B, with the values it now carries claiming otherwise.

    Whether that Job then has anything to do is decided in the cluster, by
    comparing the revision it carries against the one the release recorded. That
    is the right place for it: the server knows what it handed out, not what was
    applied. So a redundant apply costs a short-lived pod, not a Helm revision.

    A *saved* manifest keeps its name and stays a no-op to re-apply, which is
    what a GitOps loop re-syncing the same file relies on.
    """
    # Microseconds, not seconds: two fetches within the same second would
    # otherwise share a name, and the second one would silently not apply — the
    # very failure this name exists to avoid, just on a shorter timescale.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"{BOOTSTRAP_NAME}-{stamp}"


def _read_asset(name: str) -> str:
    with pkg_resources.path("gpustack.k8s", name) as path:
        with path.open(encoding="utf-8") as handle:
            return handle.read()


def render_bootstrap(config: TemplateConfig) -> str:
    """The manifest a user applies to register a Kubernetes cluster."""
    values = build_chart_values(config)
    script = _read_asset("bootstrap.sh")
    chart_url = chart_download_url(config.server_url)
    # Computed before it is added to the values, and covering the script and the
    # chart URL as well: a server upgrade that rewrites either has to reinstall,
    # and neither is a value.
    revision = applied_revision(values, script, chart_url)
    values["appliedRevision"] = revision
    # sort_keys, so an unchanged configuration renders byte-identical values and
    # the recorded revision stays stable.
    values_yaml = yaml.safe_dump(values, sort_keys=True, default_flow_style=False)

    def b64encode(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("utf-8")

    def to_yaml(value) -> str:
        return yaml.safe_dump(value, default_flow_style=False).strip()

    env = jinja2.Environment(keep_trailing_newline=True)
    env.filters["b64encode"] = b64encode
    env.filters["to_yaml"] = to_yaml

    return env.from_string(_read_asset("bootstrap.jinja")).render(
        config=config,
        chart_values=values_yaml,
        bootstrap_script=script,
        chart_url=chart_url,
        release_name=RELEASE_NAME,
        registration_token_secret_name=REGISTRATION_TOKEN_SECRET_NAME,
        bootstrap_name=BOOTSTRAP_NAME,
        job_name=_job_name(),
        applied_revision=revision,
    )

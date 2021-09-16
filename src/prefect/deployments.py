import pathlib
import runpy
from contextlib import contextmanager
from contextvars import ContextVar
from os.path import abspath
from typing import Any, Set, AnyStr
from uuid import UUID
from tempfile import NamedTemporaryFile

import yaml
from pydantic import root_validator, validator

from prefect.client import OrionClient, inject_client
from prefect.exceptions import FlowScriptError, MissingFlowError, UnspecifiedFlowError
from prefect.flows import Flow
from prefect.orion import schemas
from prefect.orion.schemas.data import DataDocument
from prefect.orion.schemas.schedules import SCHEDULE_TYPES
from prefect.orion.utilities.schemas import PrefectBaseModel
from prefect.utilities.asyncio import sync_compatible
from prefect.utilities.collections import extract_instances, listrepr
from prefect.utilities.filesystem import tmpchdir


class DeploymentSpec(PrefectBaseModel):
    """
    Specification for a flow deployment. Used to create or update deployments.

    The flow object or flow location must be provided. If a flow object is not provided,
    `load_flow` must be called to load the flow from the given flow location.
    """

    name: str
    flow: Flow = None
    flow_name: str = None
    flow_location: str = None
    schedule: SCHEDULE_TYPES = None

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        # After initialization; register this deployment. See `_register_new_specs`
        _register_spec(self)

    def load_flow(self):
        if self.flow_location and not self.flow:
            self.flow = load_flow_from_script(self.flow_location, self.flow_name)
            if not self.flow_name:
                self.flow_name = self.flow.name

    @root_validator
    def infer_location_from_flow(cls, values):
        if values.get("flow") and not values.get("flow_location"):
            flow_file = values["flow"].fn.__globals__.get("__file__")
            if flow_file:
                values["flow_location"] = abspath(str(flow_file))
        return values

    @validator("flow_location", pre=True)
    def ensure_flow_location_is_str(cls, value):
        return str(value)

    @validator("flow_location", pre=True)
    def ensure_flow_location_is_absolute(cls, value):
        return str(pathlib.Path(value).absolute())

    @root_validator
    def infer_flow_name_from_flow(cls, values):
        if values.get("flow") and not values.get("flow_name"):
            values["flow_name"] = values["flow"].name
        return values

    @root_validator
    def ensure_flow_name_matches_flow_object(cls, values):
        flow, flow_name = values.get("flow"), values.get("flow_name")
        if flow and flow_name and flow.name != flow_name:
            raise ValueError(
                "`flow.name` and `flow_name` must match. "
                f"Got {flow.name!r} and {flow_name!r}."
            )
        return values

    class Config:
        arbitrary_types_allowed = True

    def __hash__(self) -> int:
        # Deployments are unique on name / flow name pair
        return hash((self.name, self.flow_name))


def load_flow_from_script(script_path: str, flow_name: str = None) -> Flow:
    """
    Extract a flow object from a script by running all of the code in the file

    If the script has multiple flows in it, a flow name must be provided to specify
    the flow to return.

    Raises:
        - MissingFlowError: If no flows exist in the script
        - MissingFlowError: If a flow name is provided and that flow does not exist
        - UnspecifiedFlowError: If multiple flows exist but no flow name was provided
    """
    try:
        variables = runpy.run_path(script_path)
    except Exception as exc:
        raise FlowScriptError(
            user_exc=exc,
            script_path=script_path,
        ) from exc

    flows = {f.name: f for f in extract_instances(variables.values(), types=Flow)}

    if not flows:
        raise MissingFlowError(f"No flows found at path {script_path!r}")

    elif flow_name and flow_name not in flows:
        raise MissingFlowError(
            f"Flow {flow_name!r} not found at path {script_path!r}. "
            f"Found the following flows: {listrepr(flows.keys())}"
        )

    elif not flow_name and len(flows) > 1:
        raise UnspecifiedFlowError(
            f"Found {len(flows)} flows at {script_path!r}: {listrepr(flows.keys())}. "
            "Specify a flow name to select a flow to deploy.",
        )

    if flow_name:
        return flows[flow_name]
    else:
        return list(flows.values())[0]


@sync_compatible
@inject_client
async def create_deployment_from_spec(
    spec: DeploymentSpec, client: OrionClient
) -> UUID:
    """
    Create a deployment from a specification
    """
    spec.load_flow()
    flow_id = await client.create_flow(spec.flow)
    flow_data = DataDocument(encoding="file", blob=spec.flow_location.encode())
    deployment_id = await client.create_deployment(
        flow_id=flow_id,
        name=spec.name,
        schedule=spec.schedule,
        flow_data=flow_data,
    )

    return deployment_id


def deployment_specs_from_script(script_path: str) -> Set[DeploymentSpec]:
    """
    Load deployment specifications from a python script
    """
    with _register_new_specs() as specs:
        runpy.run_path(script_path)

    return specs


def deployment_specs_from_yaml(path: str) -> Set[DeploymentSpec]:
    """
    Load deployment specifications from a yaml file
    """
    with open(path, "r") as f:
        contents = yaml.safe_load(f.read())

    # Load deployments relative to the yaml file's directory
    with tmpchdir(path):
        if isinstance(contents, list):
            specs = {DeploymentSpec.parse_obj(spec) for spec in contents}
        else:
            specs = {DeploymentSpec.parse_obj(contents)}

    return specs


# Global for `DeploymentSpec` collection; see `_register_new_specs`
_DeploymentSpecContextVar = ContextVar("_DeploymentSpecContext")


@contextmanager
def _register_new_specs():
    """
    Collect any `DeploymentSpec` objects created during this context into a set.
    If multiple specs with the same name are created, the last will be used an the
    earlier will be used.

    This is convenient for `deployment_specs_from_script` which can collect deployment
    declarations without requiring them to be assigned to a global variable
    """
    specs = set()
    token = _DeploymentSpecContextVar.set(specs)
    yield specs
    _DeploymentSpecContextVar.reset(token)


def _register_spec(spec: DeploymentSpec) -> None:
    """
    See `_register_new_specs`
    """
    specs = _DeploymentSpecContextVar.get(None)

    if specs is None:
        return

    # Replace the existing spec with the new one if they collide
    specs.discard(spec)
    specs.add(spec)


def load_flow_from_text(script_contents: AnyStr, flow_name: str):
    with NamedTemporaryFile(
        mode="wt" if isinstance(script_contents, str) else "wb",
        prefix=f"flow-script-{flow_name}",
        suffix=".py",
    ) as tmpfile:
        tmpfile.write(script_contents)
        tmpfile.flush()
        flow = load_flow_from_script(tmpfile.name, flow_name=flow_name)
    return flow


@inject_client
async def load_flow_from_deployment(
    deployment: schemas.core.Deployment, client: OrionClient
):
    flow_model = await client.read_flow(deployment.flow_id)

    if deployment.flow_data.encoding == "file":
        flow_script_contents = deployment.flow_data.decode()
        flow = load_flow_from_text(flow_script_contents, flow_model.name)
    elif deployment.flow_data.encoding == "cloudpickle":
        flow = deployment.flow_data.decode()
    else:
        raise ValueError(
            f"Unknown flow data encoding {deployment.flow_data.encoding!r}"
        )

    return flow

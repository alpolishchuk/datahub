import logging
import time
import warnings
from abc import ABC
from typing import Dict, Iterable, Optional, Tuple

from pydantic.fields import Field

from datahub.configuration.common import ConfigModel
from datahub.emitter.mce_builder import make_tag_urn
from datahub.ingestion.api.common import PipelineContext
from datahub.ingestion.api.decorators import (
    SourceCapability,
    SupportStatus,
    capability,
    config_class,
    platform_name,
    support_status,
)
from datahub.ingestion.api.source import Source, SourceReport
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.ingestion.source.openapi_parser import (
    SchemaMetadataExtractor,
    clean_url,
    compose_url_attr,
    extract_fields,
    get_endpoints,
    get_swag_json,
    get_tok,
    request_call,
    set_metadata,
    try_guessing,
)
from datahub.metadata.com.linkedin.pegasus2avro.metadata.snapshot import DatasetSnapshot
from datahub.metadata.com.linkedin.pegasus2avro.mxe import MetadataChangeEvent
from datahub.metadata.schema_classes import (
    AuditStampClass,
    DatasetPropertiesClass,
    GlobalTagsClass,
    InstitutionalMemoryClass,
    InstitutionalMemoryMetadataClass,
    TagAssociationClass,
)

logger: logging.Logger = logging.getLogger(__name__)


class OpenApiConfig(ConfigModel):
    name: str = Field(description="")
    url: str = Field(description="")
    swagger_file: str = Field(description="")
    ignore_endpoints: list = Field(default=[], description="")
    username: str = Field(default="", description="")
    password: str = Field(default="", description="")
    forced_examples: dict = Field(default={}, description="")
    token: Optional[str] = Field(default=None, description="")
    get_token: dict = Field(default={}, description="")

    def get_swagger(self) -> Dict:
        if self.get_token or self.token is not None:
            if self.token is not None:
                ...
            else:
                assert (
                    "url_complement" in self.get_token.keys()
                ), "When 'request_type' is set to 'get', an url_complement is needed for the request."
                if self.get_token["request_type"] == "get":
                    assert (
                        "{username}" in self.get_token["url_complement"]
                    ), "we expect the keyword {username} to be present in the url"
                    assert (
                        "{password}" in self.get_token["url_complement"]
                    ), "we expect the keyword {password} to be present in the url"
                    url4req = self.get_token["url_complement"].replace(
                        "{username}", self.username
                    )
                    url4req = url4req.replace("{password}", self.password)
                elif self.get_token["request_type"] == "post":
                    url4req = self.get_token["url_complement"]
                else:
                    raise KeyError(
                        "This tool accepts only 'get' and 'post' as method for getting tokens"
                    )
                self.token = get_tok(
                    url=self.url,
                    username=self.username,
                    password=self.password,
                    tok_url=url4req,
                    method=self.get_token["request_type"],
                )
            sw_dict = get_swag_json(
                self.url, token=self.token, swagger_file=self.swagger_file
            )  # load the swagger file

        else:  # using basic auth for accessing endpoints
            sw_dict = get_swag_json(
                self.url,
                username=self.username,
                password=self.password,
                swagger_file=self.swagger_file,
            )
        return sw_dict


class ApiWorkUnit(MetadataWorkUnit):
    pass


@platform_name("OpenAPI", id="openapi")
@config_class(OpenApiConfig)
@support_status(SupportStatus.CERTIFIED)
@capability(SourceCapability.PLATFORM_INSTANCE, supported=False, description="")
class APISource(Source, ABC):
    """

    This plugin is meant to gather dataset-like information about OpenApi Endpoints.

    As example, if by calling GET at the endpoint at `https://test_endpoint.com/api/users/` you obtain as result:
    ```JSON
    [{"user": "albert_physics",
      "name": "Albert Einstein",
      "job": "nature declutterer",
      "is_active": true},
      {"user": "phytagoras",
      "name": "Phytagoras of Kroton",
      "job": "Phylosopher on steroids",
      "is_active": true}
    ]
    ```

    in Datahub you will see a dataset called `test_endpoint/users` which contains as fields `user`, `name` and `job`.

    """

    def __init__(self, config: OpenApiConfig, ctx: PipelineContext, platform: str):
        super().__init__(ctx)
        self.config = config
        self.platform = platform
        self.report = SourceReport()
        self.url_basepath = ""

    def report_bad_response(self, status_code: int, key: str) -> None:
        codes_mapping = {
            400: "Unknown error for reaching endpoint",
            403: "Not authorised to get endpoint",
            404: "Unable to find an example for endpoint. Please add it to the list of forced examples.",
            500: "Server error for reaching endpoint",
            504: "Timeout for reaching endpoint",
        }

        reason = codes_mapping.get(status_code)
        if reason is None:
            raise Exception(
                f"Unable to retrieve endpoint, response code {status_code}, key {key}"
            )

    def init_dataset(
        self, endpoint_k: str, endpoint_dets: dict
    ) -> Tuple[DatasetSnapshot, str]:
        config = self.config

        dataset_name = endpoint_k[1:].replace("/", ".")

        if len(dataset_name) > 0:
            if dataset_name[-1] == ".":
                dataset_name = dataset_name[:-1]
        else:
            dataset_name = "root"

        dataset_snapshot = DatasetSnapshot(
            urn=f"urn:li:dataset:(urn:li:dataPlatform:{self.platform},{config.name}.{dataset_name},PROD)",
            aspects=[],
        )

        # adding description
        dataset_properties = DatasetPropertiesClass(
            description=endpoint_dets["description"], customProperties={}
        )
        dataset_snapshot.aspects.append(dataset_properties)

        # adding tags
        tags_str = (make_tag_urn(t) for t in endpoint_dets["tags"])
        tags_tac = [TagAssociationClass(t) for t in tags_str]
        gtc = GlobalTagsClass(tags_tac)
        dataset_snapshot.aspects.append(gtc)

        # the link will appear in the "documentation"
        link_url = clean_url(f"{config.url}{self.url_basepath}{endpoint_k}")
        link_description = "Link to call for the dataset."
        creation = AuditStampClass(
            time=int(time.time()), actor="urn:li:corpuser:etl", impersonator=None
        )
        link_metadata = InstitutionalMemoryMetadataClass(
            url=link_url, description=link_description, createStamp=creation
        )
        inst_memory = InstitutionalMemoryClass([link_metadata])
        dataset_snapshot.aspects.append(inst_memory)

        return dataset_snapshot, dataset_name

    def build_wu(
        self, dataset_snapshot: DatasetSnapshot, dataset_name: str
    ) -> ApiWorkUnit:
        mce = MetadataChangeEvent(proposedSnapshot=dataset_snapshot)
        return ApiWorkUnit(id=dataset_name, mce=mce)

    def get_workunits_internal(self) -> Iterable[ApiWorkUnit]:  # noqa: C901
        config = self.config

        specification = self.config.get_swagger()

        self.url_basepath = specification.get("basePath", "")

        # Getting all the URLs accepting the "GET" method
        with warnings.catch_warnings(record=True) as warn_c:
            url_endpoints = get_endpoints(specification)

            for w in warn_c:
                w_msg = w.message
                w_spl_reason, w_spl_key, *_ = w_msg.args[0].split(" --- ")  # type: ignore
                self.report.report_warning(key=w_spl_key, reason=w_spl_reason)

        # here we put a sample from the "listing endpoint". To be used for later guessing of comosed endpoints.
        root_dataset_samples = {}

        # looping on all the urls
        for endpoint_k, endpoint_dets in url_endpoints.items():
            if endpoint_k in config.ignore_endpoints:
                continue

            dataset_snapshot, dataset_name = self.init_dataset(
                endpoint_k, endpoint_dets
            )

            # adding dataset fields
            if (
                endpoint_dets.get("schema")
                and endpoint_dets.get("schema", {}).get("AnyValue") is None
            ):
                metadata_extractor = SchemaMetadataExtractor(
                    dataset_name,
                    endpoint_dets["schema"],
                    specification,
                )
                schema_metadata = metadata_extractor.extract_metadata()
                if schema_metadata:
                    schema_metadata = set_metadata(dataset_name, endpoint_dets["data"])
                    dataset_snapshot.aspects.append(schema_metadata)
                    yield self.build_wu(dataset_snapshot, dataset_name)
                    continue

            if endpoint_dets.get("data", {}):
                # we are lucky! data is defined in the swagger for this endpoint
                schema_metadata = set_metadata(dataset_name, endpoint_dets["data"])
                dataset_snapshot.aspects.append(schema_metadata)
                yield self.build_wu(dataset_snapshot, dataset_name)
            elif (
                "{" not in endpoint_k
            ):  # if the API does not explicitly require parameters
                tot_url = clean_url(f"{config.url}{self.url_basepath}{endpoint_k}")

                if config.token:
                    response = request_call(tot_url, token=config.token)
                else:
                    response = request_call(
                        tot_url, username=config.username, password=config.password
                    )
                if response.status_code == 200:
                    fields2add, root_dataset_samples[dataset_name] = extract_fields(
                        response, dataset_name
                    )
                    if not fields2add:
                        self.report.report_warning(key=endpoint_k, reason="No Fields")
                    schema_metadata = set_metadata(dataset_name, fields2add)
                    dataset_snapshot.aspects.append(schema_metadata)

                    yield self.build_wu(dataset_snapshot, dataset_name)
                else:
                    self.report_bad_response(response.status_code, key=endpoint_k)
            else:
                if endpoint_k not in config.forced_examples.keys():
                    # start guessing...
                    url_guess = try_guessing(endpoint_k, root_dataset_samples)
                    tot_url = clean_url(f"{config.url}{self.url_basepath}{url_guess}")
                    if config.token:
                        response = request_call(tot_url, token=config.token)
                    else:
                        response = request_call(
                            tot_url, username=config.username, password=config.password
                        )
                    if response.status_code == 200:
                        fields2add, _ = extract_fields(response, dataset_name)
                        if not fields2add:
                            self.report.report_warning(
                                key=endpoint_k, reason="No Fields"
                            )
                        schema_metadata = set_metadata(dataset_name, fields2add)
                        dataset_snapshot.aspects.append(schema_metadata)

                        yield self.build_wu(dataset_snapshot, dataset_name)
                    else:
                        self.report_bad_response(response.status_code, key=endpoint_k)
                else:
                    composed_url = compose_url_attr(
                        raw_url=endpoint_k, attr_list=config.forced_examples[endpoint_k]
                    )
                    tot_url = clean_url(
                        f"{config.url}{self.url_basepath}{composed_url}"
                    )
                    if config.token:
                        response = request_call(tot_url, token=config.token)
                    else:
                        response = request_call(
                            tot_url, username=config.username, password=config.password
                        )
                    if response.status_code == 200:
                        fields2add, _ = extract_fields(response, dataset_name)
                        if not fields2add:
                            self.report.report_warning(
                                key=endpoint_k, reason="No Fields"
                            )
                        schema_metadata = set_metadata(dataset_name, fields2add)
                        dataset_snapshot.aspects.append(schema_metadata)

                        yield self.build_wu(dataset_snapshot, dataset_name)
                    else:
                        self.report_bad_response(response.status_code, key=endpoint_k)

    def get_report(self):
        return self.report


class OpenApiSource(APISource):
    def __init__(self, config: OpenApiConfig, ctx: PipelineContext):
        super().__init__(config, ctx, "OpenApi")

    @classmethod
    def create(cls, config_dict, ctx):
        config = OpenApiConfig.parse_obj(config_dict)
        return cls(config, ctx)

import json
import logging
from collections.abc import Mapping
from typing import Any, cast

from httpx import get

from core.entities.provider_entities import ProviderConfig
from core.model_runtime.utils.encoders import jsonable_encoder
from core.tools.__base.tool_runtime import ToolRuntime
from core.tools.custom_tool.provider import ApiToolProviderController
from core.tools.entities.api_entities import ToolApiEntity, ToolProviderApiEntity
from core.tools.entities.common_entities import I18nObject
from core.tools.entities.tool_bundle import ApiToolBundle
from core.tools.entities.tool_entities import (
    ApiProviderAuthType,
    ApiProviderSchemaType,
)
from core.tools.tool_label_manager import ToolLabelManager
from core.tools.tool_manager import ToolManager
from core.tools.utils.encryption import create_tool_provider_encrypter
from core.tools.utils.parser import ApiBasedToolSchemaParser
from extensions.ext_database import db
from models.tools import ApiToolProvider
from services.tools.tools_transform_service import ToolTransformService

logger = logging.getLogger(__name__)


class ApiToolManageService:
    @staticmethod
    def parser_api_schema(schema: str) -> Mapping[str, Any]:
        """
        parse api schema to tool bundle
        """
        try:
            warnings: dict[str, str] = {}
            try:
                tool_bundles, schema_type = ApiBasedToolSchemaParser.auto_parse_to_tool_bundle(schema, warning=warnings)
            except Exception as e:
                raise ValueError(f"invalid schema: {str(e)}")

            credentials_schema = [
                ProviderConfig(
                    name="auth_type",
                    type=ProviderConfig.Type.SELECT,
                    required=True,
                    default="none",
                    options=[
                        ProviderConfig.Option(value="none", label=I18nObject(en_US="None", zh_Hans="无")),
                        ProviderConfig.Option(value="api_key", label=I18nObject(en_US="Api Key", zh_Hans="Api Key")),
                    ],
                    placeholder=I18nObject(en_US="Select auth type", zh_Hans="选择认证方式"),
                ),
                ProviderConfig(
                    name="api_key_header",
                    type=ProviderConfig.Type.TEXT_INPUT,
                    required=False,
                    placeholder=I18nObject(en_US="Enter api key header", zh_Hans="输入 api key header，如：X-API-KEY"),
                    default="api_key",
                    help=I18nObject(en_US="HTTP header name for api key", zh_Hans="HTTP 头部字段名，用于传递 api key"),
                ),
                ProviderConfig(
                    name="api_key_value",
                    type=ProviderConfig.Type.TEXT_INPUT,
                    required=False,
                    placeholder=I18nObject(en_US="Enter api key", zh_Hans="输入 api key"),
                    default="",
                ),
            ]

            return cast(
                Mapping,
                jsonable_encoder(
                    {
                        "schema_type": schema_type,
                        "parameters_schema": tool_bundles,
                        "credentials_schema": credentials_schema,
                        "warning": warnings,
                    }
                ),
            )
        except Exception as e:
            raise ValueError(f"invalid schema: {str(e)}")

    @staticmethod
    def convert_schema_to_tool_bundles(schema: str, extra_info: dict | None = None) -> tuple[list[ApiToolBundle], str]:
        """
        convert schema to tool bundles

        :return: the list of tool bundles, description
        """
        try:
            return ApiBasedToolSchemaParser.auto_parse_to_tool_bundle(schema, extra_info=extra_info)
        except Exception as e:
            raise ValueError(f"invalid schema: {str(e)}")

    @staticmethod
    def create_api_tool_provider(
        user_id: str,
        tenant_id: str,
        provider_name: str,
        icon: dict,
        credentials: dict,
        schema_type: str,
        schema: str,
        privacy_policy: str,
        custom_disclaimer: str,
        labels: list[str],
    ):
        """
        create api tool provider
        """
        if schema_type not in [member.value for member in ApiProviderSchemaType]:
            raise ValueError(f"invalid schema type {schema}")

        provider_name = provider_name.strip()

        # check if the provider exists
        provider = (
            db.session.query(ApiToolProvider)
            .where(
                ApiToolProvider.tenant_id == tenant_id,
                ApiToolProvider.name == provider_name,
            )
            .first()
        )

        if provider is not None:
            raise ValueError(f"provider {provider_name} already exists")

        # parse openapi to tool bundle
        extra_info: dict[str, str] = {}
        # extra info like description will be set here
        tool_bundles, schema_type = ApiToolManageService.convert_schema_to_tool_bundles(schema, extra_info)

        if len(tool_bundles) > 100:
            raise ValueError("the number of apis should be less than 100")

        # create db provider
        db_provider = ApiToolProvider(
            tenant_id=tenant_id,
            user_id=user_id,
            name=provider_name,
            icon=json.dumps(icon),
            schema=schema,
            description=extra_info.get("description", ""),
            schema_type_str=schema_type,
            tools_str=json.dumps(jsonable_encoder(tool_bundles)),
            credentials_str={},
            privacy_policy=privacy_policy,
            custom_disclaimer=custom_disclaimer,
        )

        if "auth_type" not in credentials:
            raise ValueError("auth_type is required")

        # get auth type, none or api key
        auth_type = ApiProviderAuthType.value_of(credentials["auth_type"])

        # create provider entity
        provider_controller = ApiToolProviderController.from_db(db_provider, auth_type)
        # load tools into provider entity
        provider_controller.load_bundled_tools(tool_bundles)

        # encrypt credentials
        encrypter, _ = create_tool_provider_encrypter(
            tenant_id=tenant_id,
            controller=provider_controller,
        )
        db_provider.credentials_str = json.dumps(encrypter.encrypt(credentials))

        db.session.add(db_provider)
        db.session.commit()

        # update labels
        ToolLabelManager.update_tool_labels(provider_controller, labels)

        return {"result": "success"}

    @staticmethod
    def get_api_tool_provider_remote_schema(user_id: str, tenant_id: str, url: str):
        """
        get api tool provider remote schema
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
            " Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
            "Accept": "*/*",
        }

        try:
            response = get(url, headers=headers, timeout=10)
            if response.status_code != 200:
                raise ValueError(f"Got status code {response.status_code}")
            schema = response.text

            # try to parse schema, avoid SSRF attack
            ApiToolManageService.parser_api_schema(schema)
        except Exception:
            logger.exception("parse api schema error")
            raise ValueError("invalid schema, please check the url you provided")

        return {"schema": schema}

    @staticmethod
    def list_api_tool_provider_tools(user_id: str, tenant_id: str, provider_name: str) -> list[ToolApiEntity]:
        """
        list api tool provider tools
        """
        provider: ApiToolProvider | None = (
            db.session.query(ApiToolProvider)
            .where(
                ApiToolProvider.tenant_id == tenant_id,
                ApiToolProvider.name == provider_name,
            )
            .first()
        )

        if provider is None:
            raise ValueError(f"you have not added provider {provider_name}")

        controller = ToolTransformService.api_provider_to_controller(db_provider=provider)
        labels = ToolLabelManager.get_tool_labels(controller)

        return [
            ToolTransformService.convert_tool_entity_to_api_entity(
                tool_bundle,
                tenant_id=tenant_id,
                labels=labels,
            )
            for tool_bundle in provider.tools
        ]

    @staticmethod
    def update_api_tool_provider(
        user_id: str,
        tenant_id: str,
        provider_name: str,
        original_provider: str,
        icon: dict,
        credentials: dict,
        schema_type: str,
        schema: str,
        privacy_policy: str,
        custom_disclaimer: str,
        labels: list[str],
    ):
        """
        update api tool provider
        """
        if schema_type not in [member.value for member in ApiProviderSchemaType]:
            raise ValueError(f"invalid schema type {schema}")

        provider_name = provider_name.strip()

        # check if the provider exists
        provider = (
            db.session.query(ApiToolProvider)
            .where(
                ApiToolProvider.tenant_id == tenant_id,
                ApiToolProvider.name == original_provider,
            )
            .first()
        )

        if provider is None:
            raise ValueError(f"api provider {provider_name} does not exists")
        # parse openapi to tool bundle
        extra_info: dict[str, str] = {}
        # extra info like description will be set here
        tool_bundles, schema_type = ApiToolManageService.convert_schema_to_tool_bundles(schema, extra_info)

        # update db provider
        provider.name = provider_name
        provider.icon = json.dumps(icon)
        provider.schema = schema
        provider.description = extra_info.get("description", "")
        provider.schema_type_str = ApiProviderSchemaType.OPENAPI.value
        provider.tools_str = json.dumps(jsonable_encoder(tool_bundles))
        provider.privacy_policy = privacy_policy
        provider.custom_disclaimer = custom_disclaimer

        if "auth_type" not in credentials:
            raise ValueError("auth_type is required")

        # get auth type, none or api key
        auth_type = ApiProviderAuthType.value_of(credentials["auth_type"])

        # create provider entity
        provider_controller = ApiToolProviderController.from_db(provider, auth_type)
        # load tools into provider entity
        provider_controller.load_bundled_tools(tool_bundles)

        # get original credentials if exists
        encrypter, cache = create_tool_provider_encrypter(
            tenant_id=tenant_id,
            controller=provider_controller,
        )

        original_credentials = encrypter.decrypt(provider.credentials)
        masked_credentials = encrypter.mask_tool_credentials(original_credentials)
        # check if the credential has changed, save the original credential
        for name, value in credentials.items():
            if name in masked_credentials and value == masked_credentials[name]:
                credentials[name] = original_credentials[name]

        credentials = encrypter.encrypt(credentials)
        provider.credentials_str = json.dumps(credentials)

        db.session.add(provider)
        db.session.commit()

        # delete cache
        cache.delete()

        # update labels
        ToolLabelManager.update_tool_labels(provider_controller, labels)

        return {"result": "success"}

    @staticmethod
    def delete_api_tool_provider(user_id: str, tenant_id: str, provider_name: str):
        """
        delete tool provider
        """
        provider = (
            db.session.query(ApiToolProvider)
            .where(
                ApiToolProvider.tenant_id == tenant_id,
                ApiToolProvider.name == provider_name,
            )
            .first()
        )

        if provider is None:
            raise ValueError(f"you have not added provider {provider_name}")

        db.session.delete(provider)
        db.session.commit()

        return {"result": "success"}

    @staticmethod
    def get_api_tool_provider(user_id: str, tenant_id: str, provider: str):
        """
        get api tool provider
        """
        return ToolManager.user_get_api_provider(provider=provider, tenant_id=tenant_id)

    @staticmethod
    def test_api_tool_preview(
        tenant_id: str,
        provider_name: str,
        tool_name: str,
        credentials: dict,
        parameters: dict,
        schema_type: str,
        schema: str,
    ):
        """
        test api tool before adding api tool provider
        """
        if schema_type not in [member.value for member in ApiProviderSchemaType]:
            raise ValueError(f"invalid schema type {schema_type}")

        try:
            tool_bundles, _ = ApiBasedToolSchemaParser.auto_parse_to_tool_bundle(schema)
        except Exception:
            raise ValueError("invalid schema")

        # get tool bundle
        tool_bundle = next(filter(lambda tb: tb.operation_id == tool_name, tool_bundles), None)
        if tool_bundle is None:
            raise ValueError(f"invalid tool name {tool_name}")

        db_provider = (
            db.session.query(ApiToolProvider)
            .where(
                ApiToolProvider.tenant_id == tenant_id,
                ApiToolProvider.name == provider_name,
            )
            .first()
        )

        if not db_provider:
            # create a fake db provider
            db_provider = ApiToolProvider(
                tenant_id="",
                user_id="",
                name="",
                icon="",
                schema=schema,
                description="",
                schema_type_str=ApiProviderSchemaType.OPENAPI.value,
                tools_str=json.dumps(jsonable_encoder(tool_bundles)),
                credentials_str=json.dumps(credentials),
            )

        if "auth_type" not in credentials:
            raise ValueError("auth_type is required")

        # get auth type, none or api key
        auth_type = ApiProviderAuthType.value_of(credentials["auth_type"])

        # create provider entity
        provider_controller = ApiToolProviderController.from_db(db_provider, auth_type)
        # load tools into provider entity
        provider_controller.load_bundled_tools(tool_bundles)

        # decrypt credentials
        if db_provider.id:
            encrypter, _ = create_tool_provider_encrypter(
                tenant_id=tenant_id,
                controller=provider_controller,
            )
            decrypted_credentials = encrypter.decrypt(credentials)
            # check if the credential has changed, save the original credential
            masked_credentials = encrypter.mask_tool_credentials(decrypted_credentials)
            for name, value in credentials.items():
                if name in masked_credentials and value == masked_credentials[name]:
                    credentials[name] = decrypted_credentials[name]

        try:
            provider_controller.validate_credentials_format(credentials)
            # get tool
            tool = provider_controller.get_tool(tool_name)
            tool = tool.fork_tool_runtime(
                runtime=ToolRuntime(
                    credentials=credentials,
                    tenant_id=tenant_id,
                )
            )
            result = tool.validate_credentials(credentials, parameters)
        except Exception as e:
            return {"error": str(e)}

        return {"result": result or "empty response"}

    @staticmethod
    def list_api_tools(tenant_id: str) -> list[ToolProviderApiEntity]:
        """
        list api tools
        """
        # get all api providers
        db_providers: list[ApiToolProvider] = (
            db.session.query(ApiToolProvider).where(ApiToolProvider.tenant_id == tenant_id).all() or []
        )

        result: list[ToolProviderApiEntity] = []

        for provider in db_providers:
            # convert provider controller to user provider
            provider_controller = ToolTransformService.api_provider_to_controller(db_provider=provider)
            labels = ToolLabelManager.get_tool_labels(provider_controller)
            user_provider = ToolTransformService.api_provider_to_user_provider(
                provider_controller, db_provider=provider, decrypt_credentials=True
            )
            user_provider.labels = labels

            # add icon
            ToolTransformService.repack_provider(tenant_id=tenant_id, provider=user_provider)

            tools = provider_controller.get_tools(tenant_id=tenant_id)

            for tool in tools or []:
                user_provider.tools.append(
                    ToolTransformService.convert_tool_entity_to_api_entity(
                        tenant_id=tenant_id, tool=tool, labels=labels
                    )
                )

            result.append(user_provider)

        return result

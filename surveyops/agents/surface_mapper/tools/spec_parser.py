import json
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, Field


class ParsedEndpoint(BaseModel):
    service: str
    method: str
    path: str
    full_endpoint: str  # e.g. "POST /payment/charge"
    summary: Optional[str] = None
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    request_body: Optional[dict[str, Any]] = None
    status_codes: list[str] = Field(default_factory=list)
    responses: dict[str, str] = Field(default_factory=dict)


class ParsedServiceSpec(BaseModel):
    service: str
    title: str
    version: str
    endpoints: list[ParsedEndpoint] = Field(default_factory=list)


class SystemSpec(BaseModel):
    services: dict[str, ParsedServiceSpec] = Field(default_factory=dict)
    all_endpoints: list[str] = Field(default_factory=list)

    def is_valid_endpoint(self, endpoint_str: str) -> bool:
        """Checks if endpoint string (e.g. 'POST /payment/charge') exists in spec."""
        normalized = endpoint_str.strip()
        # Direct match
        if normalized in self.all_endpoints:
            return True
        # Try case-insensitive method or trailing slash normalization
        for ep in self.all_endpoints:
            if ep.lower() == normalized.lower() or ep.rstrip("/") == normalized.rstrip("/"):
                return True
        return False


def parse_openapi_dict(service_name: str, openapi_dict: dict[str, Any]) -> ParsedServiceSpec:
    """Parses an in-memory OpenAPI dictionary into ParsedServiceSpec."""
    title = openapi_dict.get("info", {}).get("title", service_name)
    version = openapi_dict.get("info", {}).get("version", "1.0.0")
    endpoints: list[ParsedEndpoint] = []

    paths = openapi_dict.get("paths", {})
    for path, methods in paths.items():
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch", "options", "head"):
                continue

            method_upper = method.upper()
            full_endpoint = f"{method_upper} {path}"

            # Parameters
            params = []
            for p in op.get("parameters", []):
                params.append({
                    "name": p.get("name"),
                    "in": p.get("in"),
                    "required": p.get("required", False),
                    "schema": p.get("schema", {}),
                })

            # Request Body
            request_body = op.get("requestBody")

            # Responses and status codes
            responses = {}
            status_codes = []
            for status_code, resp_desc in op.get("responses", {}).items():
                status_codes.append(str(status_code))
                desc = resp_desc.get("description", "") if isinstance(resp_desc, dict) else str(resp_desc)
                responses[str(status_code)] = desc

            endpoints.append(
                ParsedEndpoint(
                    service=service_name,
                    method=method_upper,
                    path=path,
                    full_endpoint=full_endpoint,
                    summary=op.get("summary") or op.get("description"),
                    parameters=params,
                    request_body=request_body,
                    status_codes=status_codes,
                    responses=responses,
                )
            )

    return ParsedServiceSpec(
        service=service_name,
        title=title,
        version=version,
        endpoints=endpoints,
    )


def parse_system_specs(target_app_dir: str | Path) -> SystemSpec:
    """
    Parses OpenAPI specifications across all target app services.
    Looks for openapi.json in each service's subdirectory.
    """
    base_dir = Path(target_app_dir)
    services = ["orders", "payment", "inventory", "auth"]
    service_specs: dict[str, ParsedServiceSpec] = {}
    all_endpoints: list[str] = []

    for svc in services:
        spec_path = base_dir / svc / "openapi.json"
        if not spec_path.exists():
            continue

        with open(spec_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        parsed_svc = parse_openapi_dict(svc, data)
        service_specs[svc] = parsed_svc
        for ep in parsed_svc.endpoints:
            all_endpoints.append(ep.full_endpoint)

    return SystemSpec(
        services=service_specs,
        all_endpoints=all_endpoints,
    )

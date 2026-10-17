import ast
import os
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, Field


class GraphNode(BaseModel):
    id: str
    name: str
    type: str  # endpoint | function | db_access | connection_pool | service
    service: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    source: str
    target: str
    relation: str  # calls_http | accesses_db | uses_pool | invokes | belongs_to
    ref: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class TargetAppGraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]

    def get_node(self, node_id: str) -> Optional[GraphNode]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def get_edge_refs(self) -> set[str]:
        return {e.ref for e in self.edges}


class AppAstVisitor(ast.NodeVisitor):
    def __init__(self, service_name: str, file_path: Path):
        self.service_name = service_name
        self.file_path = file_path
        self.nodes: list[GraphNode] = []
        self.edges: list[GraphEdge] = []
        self.env_vars: dict[str, str] = {}
        self.current_function: Optional[str] = None
        self.current_endpoint_node_id: Optional[str] = None

    def visit_Assign(self, node: ast.Assign):
        # Detect env vars: AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://auth:8004")
        if isinstance(node.value, ast.Call):
            func = node.value.func
            if (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
                isinstance(func, ast.Name) and func.id == "getenv"
            ):
                if node.value.args and isinstance(node.value.args[0], ast.Constant):
                    env_name = node.value.args[0].value
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self.env_vars[target.id] = str(env_name)

        # Detect pool configuration: create_engine(DATABASE_URL, poolclass=QueuePool, pool_size=...)
        if isinstance(node.value, ast.Call):
            call_name = getattr(node.value.func, "id", None) or getattr(node.value.func, "attr", None)
            if call_name == "create_engine":
                pool_node_id = f"{self.service_name}:connection_pool"
                if not any(n.id == pool_node_id for n in self.nodes):
                    self.nodes.append(
                        GraphNode(
                            id=pool_node_id,
                            name="connection_pool",
                            type="connection_pool",
                            service=self.service_name,
                            metadata={"file": str(self.file_path)},
                        )
                    )

        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._process_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._process_func(node)

    def _process_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        prev_func = self.current_function
        prev_endpoint_id = self.current_endpoint_node_id

        self.current_function = node.name
        is_endpoint = False
        method_name = ""
        route_path = ""

        # Check for FastAPI decorators: @app.get("/path"), @app.post("/path"), etc.
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                func = dec.func
                if isinstance(func, ast.Attribute) and func.attr in ("get", "post", "put", "delete", "patch"):
                    is_endpoint = True
                    method_name = func.attr.upper()
                    if dec.args and isinstance(dec.args[0], ast.Constant):
                        route_path = str(dec.args[0].value)

        if is_endpoint:
            endpoint_str = f"{method_name} {route_path}"
            endpoint_node_id = f"{self.service_name}:{endpoint_str}"
            self.current_endpoint_node_id = endpoint_node_id
            self.nodes.append(
                GraphNode(
                    id=endpoint_node_id,
                    name=node.name,
                    type="endpoint",
                    service=self.service_name,
                    metadata={
                        "method": method_name,
                        "path": route_path,
                        "endpoint": endpoint_str,
                        "function": node.name,
                        "lineno": node.lineno,
                    },
                )
            )
        else:
            self.current_endpoint_node_id = None
            func_node_id = f"{self.service_name}:{node.name}"
            self.nodes.append(
                GraphNode(
                    id=func_node_id,
                    name=node.name,
                    type="function",
                    service=self.service_name,
                    metadata={"function": node.name, "lineno": node.lineno},
                )
            )

        # Inspect function body for DB access, calls to helpers, and cross-service HTTP calls
        self.generic_visit(node)

        self.current_function = prev_func
        self.current_endpoint_node_id = prev_endpoint_id

    def visit_Call(self, node: ast.Call):
        caller_node_id = self.current_endpoint_node_id or (
            f"{self.service_name}:{self.current_function}" if self.current_function else None
        )

        if caller_node_id and self.current_function:
            # 1. DB access detection: SessionLocal(), engine.connect(), db.execute(), db.add()
            func_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if func_name in ("SessionLocal", "connect", "execute", "scalar", "scalars", "commit", "add"):
                db_node_id = f"{self.service_name}:db_session"
                if not any(n.id == db_node_id for n in self.nodes):
                    self.nodes.append(
                        GraphNode(
                            id=db_node_id,
                            name="database_session",
                            type="db_access",
                            service=self.service_name,
                            metadata={"sessionmaker": "SessionLocal"},
                        )
                    )
                
                edge_ref = f"{self.service_name}.{self.current_function}() -> db.session"
                if not any(e.ref == edge_ref for e in self.edges):
                    self.edges.append(
                        GraphEdge(
                            source=caller_node_id,
                            target=db_node_id,
                            relation="accesses_db",
                            ref=edge_ref,
                            metadata={"lineno": node.lineno},
                        )
                    )

                # If connection pool exists for this service, connect caller to pool
                pool_node_id = f"{self.service_name}:connection_pool"
                pool_edge_ref = f"{self.service_name}.{self.current_function}() -> db.connection_pool"
                if not any(e.ref == pool_edge_ref for e in self.edges):
                    self.edges.append(
                        GraphEdge(
                            source=caller_node_id,
                            target=pool_node_id,
                            relation="uses_pool",
                            ref=pool_edge_ref,
                            metadata={"lineno": node.lineno},
                        )
                    )
                # Alias for payment.charge() -> db.connection_pool (standard in 04-CONTRACTS.md)
                if self.service_name == "payment" and "charge" in self.current_function:
                    canonical_ref = "payment.charge() -> db.connection_pool"
                    if not any(e.ref == canonical_ref for e in self.edges):
                        self.edges.append(
                            GraphEdge(
                                source=caller_node_id,
                                target=pool_node_id,
                                relation="uses_pool",
                                ref=canonical_ref,
                                metadata={"lineno": node.lineno, "alias": True},
                            )
                        )

            # 2. Internal helper call: e.g. check_idempotency
            if func_name == "check_idempotency":
                target_node_id = f"{self.service_name}:check_idempotency"
                edge_ref = f"{self.service_name}.{self.current_function}() -> {self.service_name}.check_idempotency()"
                if not any(e.ref == edge_ref for e in self.edges):
                    self.edges.append(
                        GraphEdge(
                            source=caller_node_id,
                            target=target_node_id,
                            relation="invokes",
                            ref=edge_ref,
                            metadata={"lineno": node.lineno},
                        )
                    )
                if self.service_name == "payment":
                    canonical_ref = "payment.charge() -> payment.check_idempotency()"
                    if not any(e.ref == canonical_ref for e in self.edges):
                        self.edges.append(
                            GraphEdge(
                                source=caller_node_id,
                                target=target_node_id,
                                relation="invokes",
                                ref=canonical_ref,
                                metadata={"lineno": node.lineno, "alias": True},
                            )
                        )

            # 3. Cross-service HTTP call: http_client.get/post or httpx.get/post
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "post", "put", "delete"):
                # Check target URL from first argument
                if node.args:
                    url_arg = node.args[0]
                    target_endpoint_info = self._resolve_http_target(url_arg, node.func.attr.upper())
                    if target_endpoint_info:
                        target_service, target_endpoint, formatted_ref = target_endpoint_info
                        target_node_id = f"{target_service}:{target_endpoint}"
                        self.edges.append(
                            GraphEdge(
                                source=caller_node_id,
                                target=target_node_id,
                                relation="calls_http",
                                ref=formatted_ref,
                                metadata={
                                    "target_service": target_service,
                                    "target_endpoint": target_endpoint,
                                    "http_method": node.func.attr.upper(),
                                    "lineno": node.lineno,
                                },
                            )
                        )

        self.generic_visit(node)

    def _resolve_http_target(self, arg_node: ast.AST, http_method: str) -> Optional[tuple[str, str, str]]:
        # Handle f-strings: f"{PAYMENT_SERVICE_URL}/payment/charge"
        if isinstance(arg_node, ast.JoinedStr):
            parts = []
            env_found = None
            for value in arg_node.values:
                if isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Name):
                    name = value.value.id
                    if name in self.env_vars:
                        env_found = self.env_vars[name]
                    elif "AUTH" in name:
                        env_found = "AUTH_SERVICE_URL"
                    elif "INVENTORY" in name:
                        env_found = "INVENTORY_SERVICE_URL"
                    elif "PAYMENT" in name:
                        env_found = "PAYMENT_SERVICE_URL"
                elif isinstance(value, ast.Constant):
                    parts.append(str(value.value))

            subpath = "".join(parts).strip()
            # Determine target service
            if env_found:
                if "PAYMENT" in env_found:
                    target_service = "payment"
                    endpoint_path = "/payment/charge"
                    ref = f"{self.service_name}.{self.current_function}() -> payment.charge()"
                    return target_service, f"{http_method} {endpoint_path}", ref
                elif "INVENTORY" in env_found:
                    target_service = "inventory"
                    endpoint_path = "/inventory/{item_id}/reserve"
                    ref = f"{self.service_name}.{self.current_function}() -> inventory.reserve()"
                    return target_service, f"{http_method} {endpoint_path}", ref
                elif "AUTH" in env_found:
                    target_service = "auth"
                    endpoint_path = "/auth/verify"
                    ref = f"{self.service_name}.{self.current_function}() -> auth.verify()"
                    return target_service, f"{http_method} {endpoint_path}", ref

        return None


def build_target_app_graph(target_app_dir: str | Path) -> TargetAppGraph:
    """
    Statically analyzes python files in target_app_dir using AST.
    Builds and returns a typed TargetAppGraph.
    """
    target_dir = Path(target_app_dir)
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    # Map of services
    services = ["orders", "payment", "inventory", "auth"]
    for svc in services:
        svc_dir = target_dir / svc
        main_py = svc_dir / "main.py"
        if not main_py.exists():
            continue

        with open(main_py, "r", encoding="utf-8") as f:
            code = f.read()

        tree = ast.parse(code, filename=str(main_py))
        visitor = AppAstVisitor(service_name=svc, file_path=main_py)
        visitor.visit(tree)

        nodes.extend(visitor.nodes)
        edges.extend(visitor.edges)

    # Ensure all target nodes in edges exist as nodes (e.g. if referenced across services)
    existing_node_ids = {n.id for n in nodes}
    for edge in edges:
        if edge.target not in existing_node_ids:
            # Create a placeholder endpoint/service node if not found
            service = edge.target.split(":")[0] if ":" in edge.target else "unknown"
            nodes.append(
                GraphNode(
                    id=edge.target,
                    name=edge.target,
                    type="endpoint" if " " in edge.target else "service",
                    service=service,
                    metadata={"inferred": True},
                )
            )
            existing_node_ids.add(edge.target)

    return TargetAppGraph(nodes=nodes, edges=edges)

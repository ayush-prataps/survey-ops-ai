from enum import Enum


class FailureMode(str, Enum):
    connection_pool_exhaustion = "connection_pool_exhaustion"
    missing_index_n_plus_1 = "missing_index_n_plus_1"
    race_condition_idempotency = "race_condition_idempotency"
    pagination_off_by_one = "pagination_off_by_one"
    retry_storm_no_backoff = "retry_storm_no_backoff"
    upstream_dependency_failure = "upstream_dependency_failure"

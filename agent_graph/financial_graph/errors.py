class FinancialGraphError(RuntimeError):
    pass


class GraphConfigurationError(FinancialGraphError):
    pass


class GraphUnavailableError(FinancialGraphError):
    pass


class GraphResolutionError(FinancialGraphError):
    pass


class AmbiguousGraphReferenceError(GraphResolutionError):
    pass


class GraphPatchValidationError(FinancialGraphError):
    pass

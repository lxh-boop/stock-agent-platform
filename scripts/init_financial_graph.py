from __future__ import annotations

import json

from agent_graph.financial_graph.bootstrap import FinancialGraphBootstrapper


def main() -> None:
    bootstrapper = FinancialGraphBootstrapper.from_env()
    try:
        bootstrapper.initialize()
        print(json.dumps({"success": True, "message": "financial_graph_initialized"}, ensure_ascii=False))
    finally:
        bootstrapper.store.close()


if __name__ == "__main__":
    main()

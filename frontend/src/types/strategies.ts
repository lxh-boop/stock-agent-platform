import type { BacktestDetail, BacktestSpec } from './backtests'
import type { TaskRecord, TaskStatus } from './task'

export interface StrategyEquityPoint {
  date: string
  nav: number | null
  benchmark_nav?: number | null
}

export interface StrategyWorkspaceItem {
  strategy_id: string
  spec: BacktestSpec
  version: number
  status: 'draft' | TaskStatus
  created_at: string
  updated_at: string
  latest_run_id: string | null
  latest_run_version: number | null
  latest_report: (BacktestDetail & { equity?: StrategyEquityPoint[] }) | null
  active_task_id: string | null
  last_task_id: string | null
  task: TaskRecord | null
  error: string | null
}

export interface StrategyWorkspaceList {
  records: StrategyWorkspaceItem[]
  total: number
  stats: {
    total: number
    draft: number
    running: number
    succeeded: number
    failed: number
    avg_annual_return: number | null
    avg_sharpe: number | null
  }
}

export interface NewStrategyDraft {
  name: string
  start_date: string
  end_date: string
  initial_cash: number
  rebalance_days: number
  max_positions: number
  benchmark: 'pool' | 'hs300' | 'csi500' | 'sse'
  factors: { field: string; direction: 1 | -1 }[]
}
